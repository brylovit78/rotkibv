"""Read-only TronScan client, see docs/designs/tron-integration.md section 3"""
import logging
from http import HTTPStatus
from json import JSONDecodeError
from threading import Lock
from typing import TYPE_CHECKING, Any, Final, Literal

import requests

from rotkehlchen.chain.tron.utils import deserialize_tron_address
from rotkehlchen.concurrency import cancellable_sleep
from rotkehlchen.db.settings import CachedSettings
from rotkehlchen.errors.misc import MissingAPIKey, RemoteError
from rotkehlchen.externalapis.interface import ExternalServiceWithRecommendedApiKey
from rotkehlchen.logging import RotkehlchenLogsAdapter
from rotkehlchen.types import ApiKey, ExternalService, Timestamp, TronAddress
from rotkehlchen.utils.misc import get_chunks, set_user_agent
from rotkehlchen.utils.rate_limiter import TokenBucket
from rotkehlchen.utils.serialization import jsonloads_dict

if TYPE_CHECKING:
    from rotkehlchen.db.dbhandler import DBHandler

logger = logging.getLogger(__name__)
log = RotkehlchenLogsAdapter(logger)

TRONSCAN_API_URL: Final = 'https://apilist.tronscanapi.com/api'
# TronScan publishes no rate numbers, so this start rate is an implementation default.
# A 429 halves it and a key change resets it.
TRONSCAN_RATE_LIMIT_RPS: Final = 3.0
TRONSCAN_RATE_LIMIT_BURST: Final = 3
TRONSCAN_MAX_WAIT_SECONDS: Final = 60
TRONSCAN_FEED_LIMIT: Final = 50  # documented page size of the three history feeds
TRONSCAN_HOLDINGS_LIMIT: Final = 200
TRONSCAN_MAX_START: Final = 9950  # last page of the 10000-row window: start=10000 is always empty
TRONSCAN_MAX_HASHES: Final = 100  # per smart-contract-triggers-batch request
TRON_GENESIS_TS: Final = Timestamp(1529891460)  # block 0, the earliest query bound


def _rows(endpoint: str, result: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """May raise RemoteError if the response has no list of row objects under the key"""
    if not isinstance(rows := result.get(key), list) or not all(isinstance(x, dict) for x in rows):
        raise RemoteError(f'TronScan {endpoint} returned no {key} list: {result}')
    return rows


class Tronscan(ExternalServiceWithRecommendedApiKey):
    """One client and one rate limiter for every endpoint and account of the user's key."""

    def __init__(self, database: DBHandler) -> None:
        super().__init__(database=database, service_name=ExternalService.TRONSCAN)
        self.session = requests.Session()  # no transport retries: _query() owns all of them
        set_user_agent(self.session)
        self._rate_limiter = TokenBucket(
            rps=TRONSCAN_RATE_LIMIT_RPS,
            capacity=TRONSCAN_RATE_LIMIT_BURST,
        )
        # a lookup in progress must not cache a key that the hook has just invalidated
        self._key_lock = Lock()

    def _get_api_key(self) -> ApiKey | None:
        with self._key_lock:
            return super()._get_api_key()

    def on_api_key_changed(self) -> None:
        """Called from the External Services save/delete hook. The next request reads the new
        key from the DB instead of the cached one and starts from the default rate."""
        with self._key_lock:
            self.api_key, self.last_ts = None, Timestamp(0)
        self._rate_limiter.reset(rps=TRONSCAN_RATE_LIMIT_RPS, capacity=TRONSCAN_RATE_LIMIT_BURST)

    def _query(
            self,
            endpoint: str,
            params: dict[str, Any] | None = None,
            body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Query an endpoint and return its JSON object. A body makes it a POST.

        The key is sent only in the TRON-PRO-API-KEY header, so it never appears in a URL, log
        or error, and it is read again for every attempt, so a changed key applies at once.
        Connection errors, timeouts, 429 and 5xx are retried up to the shared retry limit,
        waiting Retry-After or an exponential backoff in cancellable sleeps. 401 and 403 are
        never retried.

        May raise:
        - MissingAPIKey if there is no key. The user is notified once and no request is made.
        - RemoteError if the request fails, the key is rejected or the body is not a JSON object.
        """
        retries_left, backoff = CachedSettings().get_query_retry_limit(), 1
        while True:
            self._rate_limiter.acquire()
            if (api_key := self._get_api_key()) is None:
                raise MissingAPIKey('TronScan API key is missing')

            log.debug('Querying TronScan %s with params=%s body=%s', endpoint, params, body)
            retry_after = ''
            try:
                response = self.session.request(
                    method='GET' if body is None else 'POST',
                    url=f'{TRONSCAN_API_URL}/{endpoint}',
                    params=params,
                    json=body,
                    headers={'TRON-PRO-API-KEY': api_key},
                    timeout=CachedSettings().get_timeout_tuple(),
                )
            except requests.exceptions.InvalidHeader:  # its message repeats the key
                raise RemoteError(
                    'The TronScan API key is not a valid HTTP header value. '
                    'Enter it again in the external services settings',
                ) from None
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                failure = f'request failed due to {e!s}'
            except requests.exceptions.RequestException as e:
                raise RemoteError(f'TronScan {endpoint} request failed due to {e!s}') from e
            else:
                if (status := response.status_code) == HTTPStatus.TOO_MANY_REQUESTS:
                    self._rate_limiter.shrink_after_429()
                elif status < HTTPStatus.INTERNAL_SERVER_ERROR:
                    break
                failure, retry_after = f'failed with HTTP {status}', response.headers.get('retry-after', '')  # noqa: E501

            if retries_left == 0:
                raise RemoteError(f'TronScan {endpoint} {failure} after retrying')

            wait = int(retry_after) if retry_after.isdigit() else backoff
            log.debug('TronScan %s %s, retrying in %ss', endpoint, failure, wait)
            cancellable_sleep(min(wait, TRONSCAN_MAX_WAIT_SECONDS))
            retries_left, backoff = retries_left - 1, backoff * 2

        if status in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
            raise RemoteError(
                f'TronScan rejected the API key with HTTP {status}. '
                f'Check the TronScan key in the external services settings',
            )
        if status != HTTPStatus.OK:
            raise RemoteError(f'TronScan {endpoint} failed with HTTP {status}: {response.text}')

        try:
            return jsonloads_dict(response.text)
        except JSONDecodeError as e:
            raise RemoteError(f'TronScan {endpoint} returned invalid JSON: {response.text}') from e

    def query_account(self, address: TronAddress) -> dict[str, Any]:
        """TRX balance (`balanceStr`, sun) and activation of an account.

        May raise MissingAPIKey, RemoteError or DeserializationError for an invalid address.
        """
        return self._query('accountv2', params={'address': deserialize_tron_address(address)})

    def query_holdings_page(self, address: TronAddress, start: int) -> list[dict[str, Any]]:
        """One page of the account's token holdings, with the filter verified to list them all.

        May raise MissingAPIKey, RemoteError (also for a provider error in an HTTP 200 body)
        or DeserializationError for an invalid address.
        """
        params = {'address': deserialize_tron_address(address), 'start': start, 'limit': TRONSCAN_HOLDINGS_LIMIT, 'hidden': 0, 'show': 0}  # noqa: E501
        result = self._query('account/tokens', params=params)
        if result.get('code') != HTTPStatus.OK:
            raise RemoteError(f'TronScan account/tokens returned a provider error: {result}')
        return _rows('account/tokens', result, 'data')

    def query_feed_page(
            self,
            feed: Literal['transaction', 'token_trc20/transfers', 'internal-transaction'],
            address: TronAddress,
            from_ts: Timestamp,
            to_ts: Timestamp,
            start: int,
    ) -> list[dict[str, Any]]:
        """One newest-first page of an account's history feed in [from_ts, to_ts].

        Both bounds are inclusive whole seconds. The lower bound is at least block 0, so 0 is
        never sent and a window ending before block 0 is empty without a request. A full page
        at TRONSCAN_MAX_START means a saturated window, which the caller splits.

        May raise MissingAPIKey, RemoteError or DeserializationError for an invalid address.
        """
        if not 0 <= start <= TRONSCAN_MAX_START:
            raise RemoteError(f'TronScan {feed} start {start} is outside the provider window')
        if to_ts < TRON_GENESIS_TS:
            return []

        params: dict[str, Any] = {
            'relatedAddress' if feed == 'token_trc20/transfers' else 'address': deserialize_tron_address(address),  # noqa: E501
            'start_timestamp': max(from_ts, TRON_GENESIS_TS) * 1000,
            'end_timestamp': to_ts * 1000,
            'start': start,
            'limit': TRONSCAN_FEED_LIMIT,
        }
        if feed == 'transaction':
            params['sort'] = '-timestamp'
        return _rows(feed, self._query(feed, params=params), 'token_transfers' if feed == 'token_trc20/transfers' else 'data')  # noqa: E501

    def query_event_logs(self, tx_hashes: list[str]) -> list[dict[str, Any]]:
        """Event logs of the given transactions, with the `event_index` of each log.

        Unknown hashes have no logs. May raise MissingAPIKey or RemoteError.
        """
        endpoint = 'contracts/smart-contract-triggers-batch'
        return [
            row for chunk in get_chunks(tx_hashes, TRONSCAN_MAX_HASHES)
            for row in _rows(endpoint, self._query(endpoint, body={'hashList': chunk}), 'event_list')  # noqa: E501
        ]

    def query_token_metadata(self, contract: TronAddress) -> dict[str, Any] | None:
        """Metadata of a TRC20 contract, or None if the response is not about that contract.

        May raise MissingAPIKey, RemoteError or DeserializationError for an invalid address.
        """
        contract = deserialize_tron_address(contract)
        tokens = _rows('token_trc20', self._query('token_trc20', params={'contract': contract, 'showAll': 1}), 'trc20_tokens')  # noqa: E501
        return tokens[0] if len(tokens) != 0 and tokens[0].get('contract_address') == contract else None  # noqa: E501

    def query_transaction_info(self, tx_hash: str) -> dict[str, Any] | None:
        """Details of one transaction, or None for an unknown hash (an empty object).

        May raise MissingAPIKey or RemoteError.
        """
        return self._query('transaction-info', params={'hash': tx_hash}) or None
