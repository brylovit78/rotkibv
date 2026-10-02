"""TronScan client checks against the #2 contract corpus in rotkehlchen/tests/data/tronscan/.

Every HTTP exchange is mocked from the recorded responses. No VCR and no live requests.
"""
import json
import logging
import threading
from typing import TYPE_CHECKING, Any, Final
from unittest.mock import call, patch

import pytest
import requests
from requests.adapters import HTTPAdapter

from rotkehlchen.api.websockets.typedefs import WSMessageType
from rotkehlchen.concurrency.cancellation import CancellationToken, TaskCancelledError
from rotkehlchen.concurrency.tasks import Task
from rotkehlchen.db.settings import CachedSettings
from rotkehlchen.errors.misc import MissingAPIKey, RemoteError
from rotkehlchen.errors.serialization import DeserializationError
from rotkehlchen.externalapis.tronscan import (
    TRON_GENESIS_TS,
    TRONSCAN_API_URL,
    TRONSCAN_RATE_LIMIT_RPS,
    Tronscan,
)
from rotkehlchen.tests.fixtures.messages import MockRotkiNotifier
from rotkehlchen.tests.utils.mock import MockResponse
from rotkehlchen.tests.utils.tronscan import (
    set_tronscan_key,
    tronscan_body,
    tronscan_exchange,
    tronscan_response,
)
from rotkehlchen.types import (
    ApiKey,
    ExternalService,
    ExternalServiceApiCredentials,
    Timestamp,
    TronAddress,
)

if TYPE_CHECKING:
    from rotkehlchen.db.dbhandler import DBHandler

KEY: Final = ApiKey('test-tronscan-key-1')
ACCOUNT: Final = TronAddress('TXM3pBpWRemyMFtpRsXCiXSgPJqmuZG4c4')


@pytest.fixture(name='tronscan')
def fixture_tronscan(database: DBHandler) -> Tronscan:
    set_tronscan_key(database, KEY)
    return Tronscan(database=database)


def test_key_stays_in_the_header(tronscan: Tronscan, caplog: pytest.LogCaptureFixture) -> None:
    """The key is sent only as TRON-PRO-API-KEY. A rejected key is not retried, and neither
    the error nor a log line contains it."""
    caplog.set_level(logging.DEBUG)
    with patch.object(tronscan.session, 'request', side_effect=[
        tronscan_response(case := 'history-energy-burn-and-failed-call', 'accountv2'),
        tronscan_response('errors-authentication', 'invalid-key-key-required-endpoint'),
    ]) as request:
        assert tronscan.query_account(address := tronscan_exchange(case, 'accountv2')['params']['address']) == tronscan_body(case, 'accountv2')  # noqa: E501
        with pytest.raises(RemoteError, match='rejected the API key with HTTP 401') as error:
            tronscan.query_account(address)

    assert request.call_args_list == 2 * [call(
        method='GET',
        url=f'{TRONSCAN_API_URL}/accountv2',
        params={'address': address},
        json=None,
        headers={'TRON-PRO-API-KEY': KEY},
        timeout=CachedSettings().get_timeout_tuple(),
    )]
    assert KEY not in str(error.value)
    assert 'Querying TronScan accountv2' in caplog.text  # the requests are logged, the key not
    assert KEY not in caplog.text


@pytest.mark.parametrize('key', [
    pytest.param(' test-tronscan-key-malformed', id='invalid header'),
    pytest.param('test\u2013tronscan-key-malformed', id='not latin-1'),
])
def test_malformed_key_is_not_echoed(
        tronscan: Tronscan,
        database: DBHandler,
        caplog: pytest.LogCaptureFixture,
        key: str,
) -> None:
    """A key that is no valid header value fails before anything is sent, with a fixed error
    that does not repeat it, although the requests and encoding errors do"""
    caplog.set_level(logging.DEBUG)
    set_tronscan_key(database, key)
    tronscan.on_api_key_changed()
    with (
        patch('urllib3.connection.HTTPSConnection.connect'),  # no socket, so nothing is sent
        pytest.raises(RemoteError, match='not a valid HTTP header value') as error,
    ):
        tronscan.query_account(ACCOUNT)

    assert error.value.__suppress_context__ is True
    assert 'key-malformed' not in str(error.value)
    assert 'key-malformed' not in caplog.text


def test_missing_key_makes_no_request(database: DBHandler) -> None:
    """Without a key nothing is queried, so a sync can not turn into an empty result.
    The user is told once."""
    database.msg_aggregator.rotki_notifier = (notifier := MockRotkiNotifier())  # type: ignore[assignment]
    tronscan = Tronscan(database=database)
    with patch.object(tronscan.session, 'request') as request:
        for _ in range(2):
            with pytest.raises(MissingAPIKey):
                tronscan.query_account(ACCOUNT)

    assert request.call_count == 0
    assert (message := notifier.pop_message()) is not None
    assert (message.message_type, message.data) == (WSMessageType.MISSING_API_KEY, {'service': 'tronscan'})  # noqa: E501
    assert notifier.pop_message() is None


def test_response_classes(tronscan: Tronscan) -> None:
    """Success, empty, HTTP 200 provider errors and malformed bodies stay distinct (#2 3.4)"""
    holdings = tronscan_body(case := 'history-energy-burn-and-failed-call', 'account-tokens')
    usdt = tronscan_exchange('tokens-metadata-and-fake-usdt', 'official-usdt')['params']['contract']  # noqa: E501
    with patch.object(tronscan.session, 'request', side_effect=[
        tronscan_response(case, 'account-tokens'),
        MockResponse(200, json.dumps(holdings | {'code': 500, 'message': 'error'})),
        tronscan_response('errors-http-200-and-400-bodies', 'unknown-hash-transaction-info'),
        tronscan_response('fees-detail-versus-list', 'transaction-info-1'),
        tronscan_response('errors-http-200-and-400-bodies', 'unknown-hash-event-logs'),
        tronscan_response('tokens-metadata-and-fake-usdt', 'official-usdt'),
        tronscan_response('tokens-metadata-and-fake-usdt', 'token-trc20-without-contract'),
        tronscan_response('synthetic-malformed-json'),
        MockResponse(200, '{"total": 0}'),
    ]):
        assert tronscan.query_holdings_page(ACCOUNT, start=0) == holdings['data']
        with pytest.raises(RemoteError, match='provider error'):
            tronscan.query_holdings_page(ACCOUNT, start=0)
        assert tronscan.query_transaction_info('00' * 32) is None  # {} is not a transaction
        assert tronscan.query_transaction_info((info := tronscan_body('fees-detail-versus-list', 'transaction-info-1'))['hash']) == info  # noqa: E501
        assert tronscan.query_event_logs(['00' * 32]) == []
        assert tronscan.query_token_metadata(TronAddress(usdt)) == tronscan_body('tokens-metadata-and-fake-usdt', 'official-usdt')['trc20_tokens'][0]  # noqa: E501
        assert tronscan.query_token_metadata(TronAddress(usdt)) is None  # rows of other contracts
        with pytest.raises(RemoteError, match='invalid JSON'):
            tronscan.query_feed_page('transaction', ACCOUNT, Timestamp(0), Timestamp(1790000000), start=0)  # noqa: E501
        with pytest.raises(RemoteError, match='no data list'):
            tronscan.query_feed_page('transaction', ACCOUNT, Timestamp(0), Timestamp(1790000000), start=0)  # noqa: E501

    with pytest.raises(DeserializationError):  # the 400 of a bad address is never requested
        tronscan.query_account(TronAddress(tronscan_exchange('errors-http-200-and-400-bodies', 'bad-checksum-accountv2')['params']['address']))  # noqa: E501


def test_retries_are_bounded(tronscan: Tronscan) -> None:
    """The client is the only retry owner. 429 waits Retry-After and slows the shared limiter,
    timeouts, connection errors and 5xx back off up to the retry limit, for a POST as well,
    and 403 is final."""
    retries = CachedSettings().get_query_retry_limit()
    with (
        patch('rotkehlchen.externalapis.tronscan.cancellable_sleep') as sleep,
        patch.object(tronscan.session, 'request', side_effect=[
            tronscan_response('synthetic-http-429'),
            tronscan_response(case := 'balances-inactive-account', 'accountv2'),
            requests.exceptions.ReadTimeout('read timed out'),
            requests.exceptions.ConnectionError('connection reset'),
            tronscan_response('errors-http-200-and-400-bodies', 'unknown-hash-event-logs'),
            *[tronscan_response('synthetic-http-500')] * (retries + 1),  # a 503 for any 5xx
            tronscan_response('synthetic-http-403'),
        ]) as request,
    ):
        assert tronscan.query_account(ACCOUNT) == tronscan_body(case, 'accountv2')
        assert sleep.call_args_list == [call(2)]
        assert tronscan._rate_limiter.rps == TRONSCAN_RATE_LIMIT_RPS / 2

        sleep.reset_mock()
        assert tronscan.query_event_logs(['00' * 32]) == []
        assert sleep.call_args_list == [call(1), call(2)]

        sleep.reset_mock()
        with pytest.raises(RemoteError, match='HTTP 503 after retrying'):
            tronscan.query_account(ACCOUNT)
        assert sleep.call_args_list == [call(2 ** i) for i in range(retries)]

        with pytest.raises(RemoteError, match='HTTP 403'):
            tronscan.query_account(ACCOUNT)
        assert request.call_count == 2 + 3 + (retries + 1) + 1  # each 403 is one attempt

    # nothing below the client retries on its own, so these are all the attempts made
    assert isinstance(adapter := tronscan.session.get_adapter(TRONSCAN_API_URL), HTTPAdapter)
    assert adapter.max_retries.total == 0


def test_cancellation_interrupts_a_retry_wait(tronscan: Tronscan) -> None:
    """A task cancelled during a Retry-After wait stops at once instead of sleeping it out"""
    token = CancellationToken()

    def cancelled_response(**kwargs: Any) -> MockResponse:
        token.cancel('user cancelled the sync')
        return MockResponse(429, '', headers={'retry-after': '60'})

    with patch.object(tronscan.session, 'request', side_effect=cancelled_response) as request:
        task = Task(name='tronscan query', target=lambda: tronscan.query_account(ACCOUNT), token=token).start()  # noqa: E501
        task.join(timeout=5)  # far less than the 60 seconds the provider asked for

    assert task.dead is True
    assert isinstance(task.exception, TaskCancelledError)
    assert request.call_count == 1


def test_feed_request_rules(tronscan: Tronscan) -> None:
    """Pages of 50, inclusive millisecond bounds starting no earlier than block 0, no start
    above the provider window and at most 100 hashes per event log request (#2 3.2, 3.6)"""
    trc20 = tronscan_body(case := 'pagination-trc20-endpoint', 'reference')
    with patch.object(tronscan.session, 'request', side_effect=[
        tronscan_response(case, 'reference'),
        tronscan_response('pagination-internal-endpoint', 'reference'),
        tronscan_response('identity-event-log-batch', 'event-logs-100-hashes'),
        tronscan_response('errors-http-200-and-400-bodies', 'unknown-hash-event-logs'),
    ]) as request:
        assert tronscan.query_feed_page('token_trc20/transfers', ACCOUNT, Timestamp(0), Timestamp(1790947990), start=0) == trc20['token_transfers']  # noqa: E501
        assert request.call_args.kwargs['params'] == {
            'relatedAddress': ACCOUNT,
            'start_timestamp': TRON_GENESIS_TS * 1000,  # TronScan ignores a 0 bound
            'end_timestamp': 1790947990000,
            'start': 0,
            'limit': 50,
        }
        tronscan.query_feed_page('internal-transaction', ACCOUNT, Timestamp(1600000000), Timestamp(1600000001), start=9950)  # noqa: E501
        assert request.call_args.kwargs['params'] == {'address': ACCOUNT, 'start_timestamp': 1600000000000, 'end_timestamp': 1600000001000, 'start': 9950, 'limit': 50}  # noqa: E501
        assert tronscan.query_feed_page('transaction', ACCOUNT, Timestamp(0), Timestamp(TRON_GENESIS_TS - 1), start=0) == []  # noqa: E501
        with pytest.raises(RemoteError, match='outside the provider window'):
            tronscan.query_feed_page('transaction', ACCOUNT, Timestamp(0), Timestamp(1790000000), start=10000)  # noqa: E501
        assert request.call_count == 2

        hashes = [f'{i:064x}' for i in range(150)]
        assert len(tronscan.query_event_logs(hashes)) == len(tronscan_body('identity-event-log-batch', 'event-logs-100-hashes')['event_list'])  # noqa: E501
        assert [x.kwargs['json'] for x in request.call_args_list[2:]] == [{'hashList': hashes[:100]}, {'hashList': hashes[100:]}]  # noqa: E501


def test_key_change_applies_to_the_next_request(tronscan: Tronscan, database: DBHandler) -> None:
    """A replaced key is used at once, also by a query waiting to retry, instead of after the
    120 second cache, and the limiter starts again from its default. A removed key stops all
    requests, including the retry of a waiting query."""
    def change_key(key: str | None) -> None:
        if key is None:
            database.delete_external_service_credentials([ExternalService.TRONSCAN])
        else:
            set_tronscan_key(database, key)
        tronscan.on_api_key_changed()

    rate_limited = MockResponse(429, '', headers={'retry-after': '1'})
    with (
        patch('rotkehlchen.externalapis.tronscan.cancellable_sleep', side_effect=lambda _: change_key('test-tronscan-key-2')),  # noqa: E501
        patch.object(tronscan.session, 'request', side_effect=[rate_limited, tronscan_response('balances-inactive-account', 'accountv2')]) as request,  # noqa: E501
    ):
        tronscan.query_account(ACCOUNT)
    assert [x.kwargs['headers'] for x in request.call_args_list] == [{'TRON-PRO-API-KEY': KEY}, {'TRON-PRO-API-KEY': 'test-tronscan-key-2'}]  # noqa: E501
    assert tronscan._rate_limiter.rps == TRONSCAN_RATE_LIMIT_RPS  # the 429 halving was reset

    with (
        patch('rotkehlchen.externalapis.tronscan.cancellable_sleep', side_effect=lambda _: change_key(None)),  # noqa: E501
        patch.object(tronscan.session, 'request', return_value=rate_limited) as request,
        pytest.raises(MissingAPIKey),
    ):
        tronscan.query_account(ACCOUNT)
    assert request.call_count == 1


@pytest.mark.parametrize('new_key', ['test-tronscan-key-2', None])
def test_key_change_during_a_lookup_is_not_undone(
        tronscan: Tronscan,
        database: DBHandler,
        new_key: str | None,
) -> None:
    """A key lookup that read the old key before a replacement or deletion can not cache it
    after the hook invalidated the cache, because the hook waits for the lookup's lock"""
    read_old_key, release, hook_at_lock = threading.Event(), threading.Event(), threading.Event()
    released: list[bool] = []
    real_lookup, real_lock = database.get_external_service_credentials, tronscan._key_lock

    def paused_lookup(service: ExternalService) -> ExternalServiceApiCredentials | None:
        credentials = real_lookup(service)
        read_old_key.set()
        released.append(release.wait(5))
        return credentials

    class HandshakeLock:
        """Tells the test that the hook reached the lock the paused lookup holds"""
        def __enter__(self) -> None:
            hook_at_lock.set()
            real_lock.acquire()

        def __exit__(self, *args: object) -> None:
            real_lock.release()

    with patch.object(database, 'get_external_service_credentials', side_effect=paused_lookup):
        lookup = Task(name='key lookup', target=tronscan._get_api_key).start()
        assert read_old_key.wait(5)

    if new_key is None:
        database.delete_external_service_credentials([ExternalService.TRONSCAN])
    else:
        set_tronscan_key(database, new_key)
    assert real_lock.locked()  # held by the paused lookup, so the hook must wait for it
    with patch.object(tronscan, '_key_lock', HandshakeLock()):
        hook = Task(name='key change hook', target=tronscan.on_api_key_changed).start()
        assert hook_at_lock.wait(5)
        release.set()
        lookup.join(timeout=5)
        hook.join(timeout=5)

    assert (released, lookup.exception, hook.exception) == ([True], None, None)
    assert lookup.dead is hook.dead is True
    assert tronscan._get_api_key() == new_key
