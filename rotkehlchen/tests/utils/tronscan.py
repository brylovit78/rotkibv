"""Mock TronScan responses from the #2 contract corpus in rotkehlchen/tests/data/tronscan/"""
import json
from functools import cache
from pathlib import Path
from threading import Event, Lock
from typing import TYPE_CHECKING, Any, Final

from rotkehlchen.tests.utils.mock import MockResponse
from rotkehlchen.types import ApiKey, ExternalService, ExternalServiceApiCredentials

if TYPE_CHECKING:
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
