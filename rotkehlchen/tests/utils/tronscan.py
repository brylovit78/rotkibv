"""Mock TronScan responses from the #2 contract corpus in rotkehlchen/tests/data/tronscan/"""
import json
from functools import cache
from pathlib import Path
from threading import Event, Lock
from typing import TYPE_CHECKING, Any, Final

from rotkehlchen.externalapis.tronscan import TRONSCAN_API_URL
from rotkehlchen.tests.utils.mock import MockResponse
from rotkehlchen.types import (
    ApiKey,
    ExternalService,
    ExternalServiceApiCredentials,
    SupportedBlockchain,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from rotkehlchen.db.dbhandler import DBHandler

TRONSCAN_DATA_DIR: Final = Path(__file__).resolve().parent.parent / 'data' / 'tronscan'


@cache
def _manifest() -> dict[str, Any]:
    return json.loads((TRONSCAN_DATA_DIR / 'manifest.json').read_text())


def tronscan_case(case_id: str) -> dict[str, Any]:
    return next(x for x in _manifest()['cases'] if x['id'] == case_id)


def tronscan_exchange(case_id: str, role: str = 'response') -> dict[str, Any]:
    return next(x for x in tronscan_case(case_id)['exchanges'] if x['role'] == role)


def tronscan_response(case_id: str, role: str = 'response') -> MockResponse:
    exchange = tronscan_exchange(case_id, role)
    return MockResponse(
        status_code=exchange['http_status'],
        text=(TRONSCAN_DATA_DIR / exchange['response']).read_text(),
        headers=exchange.get('response_headers'),
    )


def tronscan_body(case_id: str, role: str = 'response') -> Any:
    return json.loads(tronscan_response(case_id, role).text)


def tronscan_history_request(case_id: str) -> Callable[..., MockResponse]:
    """A requests mock serving the captured pages of a history case by endpoint and offset.
    An unexpected request fails with a KeyError."""
    roles = {(x['endpoint'], (x.get('params') or {}).get('start', 0)): x['role'] for x in tronscan_case(case_id)['exchanges']}  # noqa: E501

    def request(url: str, params: dict[str, Any] | None, **kwargs: Any) -> MockResponse:
        return tronscan_response(case_id, roles['/api' + url.removeprefix(TRONSCAN_API_URL), (params or {}).get('start', 0)])  # noqa: E501

    return request


def track_tron_accounts(database: DBHandler, accounts: Iterable[str]) -> None:
    with database.user_write() as write_cursor:
        write_cursor.executemany(
            'INSERT OR IGNORE INTO blockchain_accounts(blockchain, account) VALUES(?, ?)',
            [(SupportedBlockchain.TRON.value, x) for x in accounts],
        )


def set_tronscan_key(database: DBHandler, key: str) -> None:
    with database.user_write() as write_cursor:
        database.add_external_service_credentials(
            write_cursor=write_cursor,
            credentials=[ExternalServiceApiCredentials(ExternalService.TRONSCAN, ApiKey(key))],
        )


class WaitedLock:
    """A lock that signals once another thread waits for it, so a test knows that a sync,
    removal or purge reached the lock instead of guessing from elapsed time"""

    def __init__(self) -> None:
        self.lock, self.waited = Lock(), Event()

    def acquire(self) -> bool:
        if not self.lock.acquire(blocking=False):
            self.waited.set()
            self.lock.acquire()
        return True

    def release(self) -> None:
        self.lock.release()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *args: object) -> None:
        self.release()
