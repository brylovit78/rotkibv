"""TRON history sync (brylovit78/rotkibv#10) from the #2 TronScan corpus and its
`synthetic-pagination-coverage` outcomes. Every HTTP exchange is mocked; no VCR."""
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Final
from unittest.mock import patch

import pytest

from rotkehlchen.api.websockets.typedefs import TransactionStatusStep, WSMessageType
from rotkehlchen.chain.accounts import BlockchainAccountData
from rotkehlchen.chain.tron import transactions as tron_transactions
from rotkehlchen.chain.tron.transactions import TronTransactions
from rotkehlchen.concurrency.cancellation import TaskCancelledError
from rotkehlchen.db.trontx import TronTransaction, add_tron_history
from rotkehlchen.errors.misc import RemoteError
from rotkehlchen.externalapis.tronscan import TRONSCAN_API_URL, Tronscan
from rotkehlchen.tests.fixtures.messages import MockRotkiNotifier
from rotkehlchen.tests.utils.tronscan import (
    set_tronscan_key,
    tronscan_body,
    tronscan_case,
    tronscan_exchange,
    tronscan_response,
)
from rotkehlchen.types import SupportedBlockchain, Timestamp, TimestampMS, TronAddress

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from rotkehlchen.db.dbhandler import DBHandler
    from rotkehlchen.tests.utils.mock import MockResponse

INTERNAL_ROW: Final = tronscan_body('history-internal-only-receipt', 'internal-page0')['data'][0]
ACCOUNT: Final = TronAddress(INTERNAL_ROW['to'])
OTHER_ACCOUNT: Final = INTERNAL_ROW['from']
TIME_KEYS: Final = {'transaction': 'timestamp', 'token_trc20/transfers': 'block_ts', 'internal-transaction': 'timestamp'}  # noqa: E501
RANGE_TYPES: Final = {'transaction': 'txs', 'token_trc20/transfers': 'tokentxs', 'internal-transaction': 'internaltxs'}  # noqa: E501


@pytest.fixture(name='history')
def fixture_history(database: DBHandler) -> TronTransactions:
    set_tronscan_key(database, 'test-tronscan-key')
    return TronTransactions(tronscan=Tronscan(database=database), database=database)


@pytest.fixture(name='small_windows')
def fixture_small_windows() -> Iterator[None]:
    """One row per page up to start 4, so a window of five rows is saturated"""
    with (
        patch.object(tron_transactions, 'TRONSCAN_FEED_LIMIT', 1),
        patch.object(tron_transactions, 'TRONSCAN_MAX_START', 4),
    ):
        yield


class _Feeds:
    """TronScan history feeds over the given rows: each page holds the rows of the requested
    window, newest first, by the page size in use. A failure is raised for its window."""

    def __init__(
            self,
            rows: dict[str, list[dict[str, Any]]],
            fail: tuple[tuple[int, int], BaseException] | None = None,
    ) -> None:
        self.rows, self.fail = rows, fail
        self.windows: defaultdict[str, list[tuple[int, int]]] = defaultdict(list)

    def __call__(self, feed: str, address: str, from_ts: int, to_ts: int, start: int) -> list[dict[str, Any]]:  # noqa: E501
        if start == 0:
            self.windows[feed].append((from_ts, to_ts))
        if self.fail is not None and self.fail[0] == (from_ts, to_ts):
            raise self.fail[1]
        return sorted(
            (x for x in self.rows.get(feed, []) if from_ts <= x[TIME_KEYS[feed]] // 1000 <= to_ts),
            key=lambda x: -x[TIME_KEYS[feed]],
        )[start:start + tron_transactions.TRONSCAN_FEED_LIMIT]


def _sync(history: TronTransactions, feeds: _Feeds, from_ts: int, to_ts: int, accounts: Sequence[str] = (ACCOUNT,)) -> None:  # noqa: E501
    with patch.object(history.tronscan, 'query_feed_page', side_effect=feeds):
        history.query_transactions([TronAddress(x) for x in accounts], Timestamp(from_ts), Timestamp(to_ts))  # noqa: E501


def _internal(identity: str, second: int, confirmed: bool = True) -> dict[str, Any]:
    """The corpus internal receipt of ACCOUNT as another transaction and internal call"""
    return INTERNAL_ROW | {'hash': identity * 64, 'internal_hash': identity * 64, 'timestamp': second * 1000, 'confirmed': confirmed}  # noqa: E501


def _range(history: TronTransactions, feed: str, address: str = ACCOUNT) -> tuple[int, int] | None:
    with history.database.conn.read_ctx() as cursor:
        return history.database.get_used_query_range(cursor, f'TRON{RANGE_TYPES[feed]}_{address}')


def _query(history: TronTransactions, query: str, *bindings: Any) -> set[tuple[Any, ...]]:
    with history.database.conn.read_ctx() as cursor:
        return set(cursor.execute(query, bindings))


def _internal_identities(history: TronTransactions) -> list[str]:
    return sorted(x[0][0] for x in _query(history, 'SELECT lower(hex(internal_hash)) FROM tron_internal_transfers'))  # noqa: E501


@pytest.mark.parametrize('case', [
    'history-account-creation-fee',
    'history-energy-burn-and-failed-call',
    'history-internal-only-receipt',
    'history-multi-page',
    'history-zero-holding-token',
])
def test_history_cases_are_stored_by_identity(history: TronTransactions, case: str) -> None:
    """Every feed is paged to its short page. TRC20 transfers come from the event logs with
    their index, internal TRX and the paid fees are kept, and each feed records the whole
    window, so syncing it again sends no request."""
    roles = {(x['endpoint'], (x.get('params') or {}).get('start', 0)): x['role'] for x in tronscan_case(case)['exchanges']}  # noqa: E501
    account = TronAddress(tronscan_exchange(case, 'accountv2')['params']['address'])
    expected = tronscan_case(case)['expected']
    end = expected['completed_range']['query_window_ms'][1] // 1000

    def request(url: str, params: dict[str, Any] | None, **kwargs: Any) -> MockResponse:
        return tronscan_response(case, roles['/api' + url.removeprefix(TRONSCAN_API_URL), (params or {}).get('start', 0)])  # noqa: E501

    with (
        patch.object(history.tronscan._rate_limiter, 'acquire'),
        patch.object(history.tronscan.session, 'request', side_effect=request) as requests,
    ):
        history.query_transactions([account], Timestamp(0), Timestamp(end))
        requests_count = requests.call_count
        history.query_transactions([account], Timestamp(0), Timestamp(end))

    assert requests.call_count == requests_count
    assert {_range(history, x, account) for x in RANGE_TYPES} == {(0, end)}
    movements = expected['history']['movements']
    assert _query(
        history,
        'SELECT lower(hex(T.tx_hash)), R.event_index, R.contract_address, R.from_address, '
        'R.to_address, R.amount FROM tron_trc20_transfers R JOIN tron_transactions T ON '
        'T.identifier=R.tx_id WHERE ? IN (R.from_address, R.to_address)',
        account,
    ) == {(x['tx_hash'], x['event_index'], x['contract'], x['from'], x['to'], x['amount_raw']) for x in movements if x['kind'] == 'trc20'}  # noqa: E501
    assert _query(
        history,
        'SELECT lower(hex(T.tx_hash)), lower(hex(R.internal_hash)), R.from_address, '
        'R.to_address, R.amount FROM tron_internal_transfers R JOIN tron_transactions T ON '
        'T.identifier=R.tx_id WHERE R.success=1',
    ) == {(x['tx_hash'], x['internal_hash'], x['from'], x['to'], x['amount_raw']) for x in movements if x['kind'] == 'internal_trx'}  # noqa: E501
    assert _query(
        history,
        'SELECT lower(hex(tx_hash)), owner_address, to_address, native_amount FROM '
        "tron_transactions WHERE contract_type=1 AND contract_ret='SUCCESS'",
    ) == {(x['tx_hash'], x['from'], x['to'], x['amount_raw']) for x in movements if x['kind'] == 'trx'}  # noqa: E501
    assert _query(
        history,
        "SELECT lower(hex(tx_hash)), fee FROM tron_transactions WHERE owner_address=? AND fee!='0'",  # noqa: E501
        account,
    ) == {(x['tx_hash'], x['amount_raw']) for x in expected['history']['fees']}


@pytest.mark.usefixtures('small_windows')
def test_saturated_window_splits_into_its_older_and_newer_half(history: TronTransactions) -> None:
    """split-overlap: the saturated [100, 109] is read as [100, 104] and then [105, 109], and
    the twice returned c is stored once"""
    rows = [_internal('a', 100), _internal('b', 102), _internal('c', 104), _internal('c', 104), _internal('d', 107)]  # noqa: E501
    _sync(history, feeds := _Feeds({'internal-transaction': rows}), 100, 109)
    assert feeds.windows['internal-transaction'] == [(100, 109), (100, 104), (105, 109)]
    assert _internal_identities(history) == ['a', 'b', 'c', 'd']
    assert _range(history, 'internal-transaction') == (100, 109)


@pytest.mark.usefixtures('small_windows')
@pytest.mark.parametrize('error', [
    pytest.param(TaskCancelledError(), id='cancelled-gap'),
    pytest.param(RemoteError('TronScan failed'), id='failed-gap'),
])
def test_interrupted_window_keeps_the_gap_until_a_retry(
        history: TronTransactions,
        error: BaseException,
) -> None:
    """cancelled-gap and failed-gap keep [100, 104] with its rows and leave the interrupted
    [105, 109] uncovered, and the query still reports its end. retry-closes-gap re-reads the
    completed boundary second 104."""
    rows = [_internal('a', 100), _internal('b', 104), _internal('c', 104), _internal('c', 104), _internal('d', 107)]  # noqa: E501
    history.database.msg_aggregator.rotki_notifier = (notifier := MockRotkiNotifier())  # type: ignore[assignment]
    with pytest.raises(type(error)):
        _sync(history, _Feeds({'internal-transaction': rows}, fail=((105, 109), error)), 100, 109)
    assert [(x.message_type, x.data) for x in notifier.messages] == [
        (WSMessageType.TRANSACTION_STATUS, {'address': ACCOUNT, 'chain': 'TRON', 'subtype': 'tron', 'period': (100, 109), 'status': str(step)})  # noqa: E501
        for step in (TransactionStatusStep.QUERYING_TRANSACTIONS_STARTED, TransactionStatusStep.QUERYING_TRANSACTIONS_FINISHED)  # noqa: E501
    ]
    assert _internal_identities(history) == ['a', 'b', 'c']
    assert _range(history, 'internal-transaction') == (100, 104)

    _sync(history, retry := _Feeds({'internal-transaction': rows}), 100, 114)
    assert retry.windows['internal-transaction'] == [(104, 114)]
    assert _internal_identities(history) == ['a', 'b', 'c', 'd']
    assert _range(history, 'internal-transaction') == (100, 114)


@pytest.mark.usefixtures('small_windows')
def test_saturated_single_second_keeps_coverage_before_it(history: TronTransactions) -> None:
    """single-second-saturation: a second with more rows than a window holds can not be split.
    Coverage ends before it with a warning, and a later complete window can not jump it."""
    rows = [_internal('a', 100), _internal('b', 104), *(_internal(x, 105) for x in '01234'), _internal('e', 108)]  # noqa: E501
    _sync(history, feeds := _Feeds({'internal-transaction': rows}), 100, 109)
    assert (105, 105) in feeds.windows['internal-transaction']
    assert _internal_identities(history) == ['a', 'b', 'e']
    assert _range(history, 'internal-transaction') == (100, 104)
    assert history.database.msg_aggregator.consume_warnings() == [(
        f'TronScan lists at least 5 internal-transaction rows of TRON account {ACCOUNT} in the '
        'second 105. Its history stays incomplete from that second.'
    )]


def test_earlier_history_is_recorded_once_complete(history: TronTransactions) -> None:
    """A sync from before the recorded range records the earlier part only when all of it is
    complete, as the recorded range can not have a gap"""
    _sync(history, _Feeds({}), 105, 109)
    _sync(history, _Feeds({'internal-transaction': [_internal('a', 100), _internal('b', 102, confirmed=False)]}), 100, 109)  # noqa: E501
    assert _internal_identities(history) == ['a', 'b']
    assert _range(history, 'internal-transaction') == (105, 109)

    _sync(history, _Feeds({'internal-transaction': [_internal('a', 100), _internal('b', 102)]}), 100, 109)  # noqa: E501
    assert _range(history, 'internal-transaction') == (100, 109)


@pytest.mark.parametrize(('feed', 'row', 'party'), [
    pytest.param('transaction', tronscan_body('synthetic-reverted-transaction', 'transactions')['data'][0], 'ownerAddress', id='reverted'),  # noqa: E501
    pytest.param('token_trc20/transfers', tronscan_body('history-zero-holding-token', 'trc20-page0')['token_transfers'][0], 'from_address', id='unresolved'),  # noqa: E501
])
def test_unfinished_rows_end_coverage_before_their_second(
        history: TronTransactions,
        feed: str,
        row: dict[str, Any],
        party: str,
) -> None:
    """A reverted row, and a TRC20 row that no Transfer log of its transaction matches, are
    stored, but their window is complete only up to the second before them"""
    second, account = row[TIME_KEYS[feed]] // 1000, row[party]
    with patch.object(history.tronscan.session, 'request', return_value=tronscan_response('errors-http-200-and-400-bodies', 'unknown-hash-event-logs')):  # noqa: E501
        _sync(history, _Feeds({feed: [row]}), second - 10, second + 10, accounts=(account,))
    assert _range(history, feed, account) == (second - 10, second - 1)
    assert _query(history, 'SELECT COUNT(*) FROM tron_transactions') == {(1,)}
    assert _query(history, 'SELECT COUNT(*) FROM tron_trc20_transfers') == {(0,)}


def test_confirmation_completes_coverage_and_redecodes(history: TronTransactions) -> None:
    """status-pending-then-confirmed: a pending row holds coverage before its second. The same
    row confirmed later completes the window, keeps the paid fee and drops the decoded mark."""
    expected = tronscan_case(case := 'status-pending-then-confirmed')['expected']
    pending = next(x for x in tronscan_body(case, 'transactions-newest')['data'] if x['hash'] == expected['tx_hash'])  # noqa: E501
    assert (pending['confirmed'], tronscan_body(case, 'transaction-info-t75s')['confirmed']) == (False, True)  # noqa: E501
    account, second = pending['ownerAddress'], pending['timestamp'] // 1000
    _sync(history, _Feeds({'transaction': [pending]}), second - 10, second + 10, accounts=(account,))  # noqa: E501
    assert _range(history, 'transaction', account) == (second - 10, second - 1)
    with history.database.user_write() as write_cursor:  # what the decoder (#11) will write
        write_cursor.execute('INSERT INTO tron_tx_mappings(tx_id, value) SELECT identifier, 0 FROM tron_transactions')  # noqa: E501

    _sync(history, _Feeds({'transaction': [pending | {'confirmed': True}]}), second - 10, second + 10, accounts=(account,))  # noqa: E501
    assert _range(history, 'transaction', account) == (second - 10, second + 10)
    assert _query(history, 'SELECT confirmed, fee FROM tron_transactions') == {(1, expected['paid_fee_sun_from_list'])}  # noqa: E501
    assert _query(history, 'SELECT COUNT(*) FROM tron_tx_mappings') == {(0,)}


def test_later_rows_complete_a_transaction(database: DBHandler) -> None:
    """A later row never drops the confirmation or the owner fields an earlier row stored, and
    only a real change removes the decoded mark"""
    owned = TronTransaction(
        tx_hash=b'\x01' * 32, block_number=1, timestamp=TimestampMS(1000), confirmed=True,
        reverted=False, contract_ret='SUCCESS', owner_address=ACCOUNT, to_address=ACCOUNT,
        contract_type=1, native_amount=5, fee=7,
    )
    seen_pending = TronTransaction(tx_hash=owned.tx_hash, block_number=1, timestamp=owned.timestamp, confirmed=False, reverted=False)  # noqa: E501
    with database.user_write() as write_cursor:
        add_tron_history(write_cursor, ACCOUNT, [owned])
        write_cursor.execute('INSERT INTO tron_tx_mappings(tx_id, value) SELECT identifier, 0 FROM tron_transactions')  # noqa: E501
        add_tron_history(write_cursor, ACCOUNT, [seen_pending])
        assert write_cursor.execute('SELECT COUNT(*) FROM tron_tx_mappings').fetchone() == (1,)
        add_tron_history(write_cursor, TronAddress(OTHER_ACCOUNT), [seen_pending])  # a new party
        assert write_cursor.execute('SELECT COUNT(*) FROM tron_tx_mappings').fetchone() == (0,)
        assert write_cursor.execute(
            'SELECT confirmed, contract_ret, owner_address, contract_type, native_amount, fee '
            'FROM tron_transactions',
        ).fetchall() == [(1, 'SUCCESS', ACCOUNT, 1, '5', '7')]


def test_identical_transfers_of_one_transaction_stay_distinct(history: TronTransactions) -> None:
    """synthetic-identical-transfers-same-tx: two equal Transfer logs of one transaction are
    two transfers by event index, stored once although both parties' feeds list them"""
    rows = tronscan_body(case := 'synthetic-identical-transfers-same-tx', 'trc20-feed-account-a')['token_transfers']  # noqa: E501
    second = rows[0]['block_ts'] // 1000
    with patch.object(history.tronscan.session, 'request', return_value=tronscan_response(case, 'event-logs')):  # noqa: E501
        _sync(history, _Feeds({'token_trc20/transfers': rows}), second - 10, second + 10, accounts=(rows[0]['from_address'], rows[0]['to_address']))  # noqa: E501

    assert _query(
        history,
        'SELECT lower(hex(T.tx_hash)), R.event_index, R.from_address, R.to_address, R.amount '
        'FROM tron_trc20_transfers R JOIN tron_transactions T ON T.identifier=R.tx_id',
    ) == {(x['tx_hash'], x['event_index'], x['from'], x['to'], x['amount_raw']) for x in tronscan_case(case)['expected']['movements']}  # noqa: E501
    assert _range(history, 'token_trc20/transfers', rows[0]['to_address']) == (second - 10, second + 10)  # noqa: E501


def test_internal_rows_keep_only_trx_calls(history: TronTransactions) -> None:
    """synthetic-rejected-internal-transfer: the confirmed rejected call completes discovery but
    is no successful movement. Zero-value and TRC10 calls carry no TRX and are not kept."""
    row = tronscan_body('synthetic-rejected-internal-transfer', 'internal')['data'][0]
    second = row['timestamp'] // 1000
    _sync(history, _Feeds({'internal-transaction': [
        row,
        row | {'internal_hash': 'e' * 64, 'call_value': 0},
        row | {'internal_hash': 'f' * 64, 'token_list': row['token_list'] | {'token_id': '1002000'}},  # noqa: E501
    ]}), second - 10, second + 10)
    assert _query(history, 'SELECT lower(hex(internal_hash)), success FROM tron_internal_transfers') == {(row['internal_hash'], 0)}  # noqa: E501
    assert _range(history, 'internal-transaction') == (second - 10, second + 10)


@pytest.mark.parametrize(('change', 'error'), [
    pytest.param({'to': OTHER_ACCOUNT}, 'Row of another account or window', id='another account'),
    pytest.param({'timestamp': INTERNAL_ROW['timestamp'] + 11000}, 'Row of another account or window', id='outside the window'),  # noqa: E501
    pytest.param({'call_value': 1.5}, 'Invalid raw amount 1.5', id='fractional amount'),
    pytest.param({'call_value': -1}, 'Invalid raw amount -1', id='negative amount'),
])
def test_unexpected_rows_fail_their_window(
        history: TronTransactions,
        change: dict[str, Any],
        error: str,
) -> None:
    """Every row must be a well formed row of the queried account and window (section 3.6)"""
    second = INTERNAL_ROW['timestamp'] // 1000
    with (
        patch.object(history.tronscan, 'query_feed_page', side_effect=lambda feed, *args: [INTERNAL_ROW | change] if feed == 'internal-transaction' else []),  # noqa: E501
        pytest.raises(RemoteError, match=f'Unexpected TronScan internal-transaction data for {ACCOUNT}: {error}'),  # noqa: E501
    ):
        history.query_transactions([ACCOUNT], Timestamp(second - 10), Timestamp(second + 10))

    assert _range(history, 'internal-transaction') is None
    assert _query(history, 'SELECT COUNT(*) FROM tron_transactions') == {(0,)}


def test_removed_account_keeps_the_history_of_other_accounts(history: TronTransactions) -> None:
    """A transfer between two tracked accounts stays until both are removed, and only the
    removed account's query ranges are deleted"""
    second = INTERNAL_ROW['timestamp'] // 1000
    _sync(history, _Feeds({'internal-transaction': [INTERNAL_ROW]}), second - 10, second + 10, accounts=(ACCOUNT, OTHER_ACCOUNT))  # noqa: E501
    database = history.database
    with database.user_write() as write_cursor:
        database.add_blockchain_accounts(write_cursor, [BlockchainAccountData(chain=SupportedBlockchain.TRON, address=x) for x in (ACCOUNT, OTHER_ACCOUNT)])  # noqa: E501
        database.remove_single_blockchain_accounts(write_cursor, SupportedBlockchain.TRON, [ACCOUNT])  # noqa: E501

    assert _query(history, 'SELECT address FROM tron_tx_address_mappings') == {(OTHER_ACCOUNT,)}
    assert _query(history, 'SELECT COUNT(*) FROM tron_internal_transfers') == {(1,)}
    assert {_range(history, x, ACCOUNT) for x in RANGE_TYPES} == {None}
    assert {_range(history, x, OTHER_ACCOUNT) for x in RANGE_TYPES} == {(second - 10, second + 10)}

    with database.user_write() as write_cursor:
        database.remove_single_blockchain_accounts(write_cursor, SupportedBlockchain.TRON, [OTHER_ACCOUNT])  # noqa: E501
    assert _query(history, 'SELECT COUNT(*) FROM tron_transactions') == {(0,)}
    assert _query(history, "SELECT COUNT(*) FROM used_query_ranges WHERE name LIKE 'TRON%'") == {(0,)}  # noqa: E501
