"""TRON history sync, see docs/designs/tron-integration.md sections 3.6, 3.8 and 3.9"""
import logging
from collections import defaultdict
from threading import Lock
from typing import TYPE_CHECKING, Any, Final, Literal, NamedTuple

from rotkehlchen.api.websockets.typedefs import (
    TransactionStatusStep,
    TransactionStatusSubType,
    WSMessageType,
)
from rotkehlchen.chain.tron.utils import deserialize_raw_amount, deserialize_tron_address
from rotkehlchen.db.ranges import DBQueryRanges
from rotkehlchen.db.trontx import (
    TronInternalTransfer,
    TronTransaction,
    TronTRC20Transfer,
    add_tron_history,
)
from rotkehlchen.errors.misc import RemoteError
from rotkehlchen.errors.serialization import DeserializationError
from rotkehlchen.externalapis.tronscan import TRONSCAN_FEED_LIMIT, TRONSCAN_MAX_START
from rotkehlchen.logging import RotkehlchenLogsAdapter
from rotkehlchen.types import SupportedBlockchain, Timestamp, TimestampMS, TronAddress
from rotkehlchen.utils.misc import ts_now

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rotkehlchen.db.dbhandler import DBHandler
    from rotkehlchen.externalapis.tronscan import Tronscan

logger = logging.getLogger(__name__)
log = RotkehlchenLogsAdapter(logger)

TronFeed = Literal['transaction', 'token_trc20/transfers', 'internal-transaction']
# Every feed has its own query range, named by the shared range prefixes
TRON_HISTORY_FEEDS: Final[tuple[tuple[TronFeed, Literal['txs', 'tokentxs', 'internaltxs']], ...]] = (  # noqa: E501
    ('transaction', 'txs'),
    ('token_trc20/transfers', 'tokentxs'),
    ('internal-transaction', 'internaltxs'),
)
# TronScan may still index rows of the latest seconds, so these are never marked complete
RECENT_HISTORY_MARGIN: Final = 60


class _Window(NamedTuple):
    """The resolved rows of one complete window. `complete_until` is the last second before
    the oldest pending or unresolved row, so coverage never passes it (section 3.9)."""
    transactions: list[TronTransaction]
    trc20_transfers: list[TronTRC20Transfer]
    internal_transfers: list[TronInternalTransfer]
    complete_until: Timestamp


def _hash(value: Any) -> bytes:
    """May raise DeserializationError if the value is not a 32 byte hex hash"""
    try:
        raw = bytes.fromhex(value)
    except (TypeError, ValueError) as e:
        raise DeserializationError(f'Invalid TRON hash {value!r}') from e
    if len(raw) != 32:
        raise DeserializationError(f'Invalid TRON hash {value!r}')
    return raw


def _parent(row: dict[str, Any], hash_key: str, timestamp_key: str, contract_ret: str | None) -> TronTransaction:  # noqa: E501
    """The parent transaction fields that every feed row has"""
    if type(row['block']) is not int or type(row[timestamp_key]) is not int or not all(type(row[x]) is bool for x in ('confirmed', 'revert')):  # noqa: E501
        raise DeserializationError(f'Invalid TRON row status or position in {row}')
    return TronTransaction(
        tx_hash=_hash(row[hash_key]),
        block_number=row['block'],
        timestamp=TimestampMS(row[timestamp_key]),
        confirmed=row['confirmed'],
        reverted=row['revert'],
        contract_ret=contract_ret,
    )


class TronTransactions:

    def __init__(self, tronscan: Tronscan, database: DBHandler) -> None:
        self.tronscan = tronscan
        self.database = database
        self.address_locks: defaultdict[TronAddress, Lock] = defaultdict(Lock)

    def query_transactions(
            self,
            addresses: Sequence[TronAddress],
            from_ts: Timestamp,
            to_ts: Timestamp,
    ) -> None:
        """Sync every history feed of the given accounts up to to_ts.

        Completed windows are stored with their query range in one write, so a failure or
        cancellation keeps them and the next sync continues after the last completed second.

        May raise RemoteError if TronScan fails, the key is missing or a row is malformed or
        does not belong to the queried account and window.
        """
        end_ts = Timestamp(min(to_ts, ts_now() - RECENT_HISTORY_MARGIN))
        for address in addresses:
            with self.address_locks[address]:  # one sync of an account at a time, as for EVM
                self._send_status(address, (from_ts, end_ts), TransactionStatusStep.QUERYING_TRANSACTIONS_STARTED)  # noqa: E501
                try:
                    for feed, range_type in TRON_HISTORY_FEEDS:
                        self._sync_feed(
                            address=address,
                            feed=feed,
                            range_name=f'{SupportedBlockchain.TRON.to_range_prefix(range_type)}_{address}',
                            from_ts=from_ts,
                            end_ts=end_ts,
                        )
                finally:  # always finish, so the frontend never shows a stuck query
                    self._send_status(address, (from_ts, end_ts), TransactionStatusStep.QUERYING_TRANSACTIONS_FINISHED)  # noqa: E501

    def _send_status(
            self,
            address: TronAddress,
            period: tuple[Timestamp, Timestamp],
            status: TransactionStatusStep,
    ) -> None:
        self.database.msg_aggregator.add_message(
            message_type=WSMessageType.TRANSACTION_STATUS,
            data={
                'address': address,
                'chain': SupportedBlockchain.TRON.value,
                'subtype': str(TransactionStatusSubType.TRON),
                'period': period,
                'status': str(status),
            },
        )

    def _sync_feed(
            self,
            address: TronAddress,
            feed: TronFeed,
            range_name: str,
            from_ts: Timestamp,
            end_ts: Timestamp,
    ) -> None:
        """Traverse the missing part of a feed's range oldest window first.

        A saturated window is split into its older and newer half, both inclusive whole
        seconds. Rows of later windows are still stored after coverage stopped, but only the
        contiguous complete part is recorded. A saturated single second keeps the range
        incomplete across it, as no other filter is verified to subdivide it.
        """
        dbranges = DBQueryRanges(self.database)
        with self.database.conn.read_ctx() as cursor:
            saved = self.database.get_used_query_range(cursor, range_name)
            missing = dbranges.get_location_query_ranges(cursor, range_name, from_ts, end_ts)

        for start_ts, stop_ts in missing:
            # A range after the saved one starts with the saved boundary second, deduplicated
            # by identity (section 3.6). A range before it is recorded only once complete, as
            # the saved range can not have a gap.
            after_saved = saved is not None and start_ts == saved[1] + 1
            joins_at = stop_ts if saved is not None and stop_ts < saved[0] else start_ts
            windows = [(Timestamp(start_ts - 1) if after_saved else start_ts, stop_ts)]
            covering = True
            while len(windows) != 0:
                low, high = windows.pop()
                if (rows := self._read_window(feed, address, low, high)) is None:
                    if low == high:
                        covering = False
                        self.database.msg_aggregator.add_warning(
                            f'TronScan lists at least {TRONSCAN_MAX_START + TRONSCAN_FEED_LIMIT} '
                            f'{feed} rows of TRON account {address} in the second {low}. Its '
                            f'history stays incomplete from that second.',
                        )
                        continue

                    windows.extend(((Timestamp((low + high) // 2 + 1), high), (low, Timestamp((low + high) // 2))))  # noqa: E501
                    continue

                window = self._resolve(feed, address, rows, low, high)
                with self.database.user_write() as write_cursor:
                    add_tron_history(
                        write_cursor=write_cursor,
                        address=address,
                        transactions=window.transactions,
                        trc20_transfers=window.trc20_transfers,
                        internal_transfers=window.internal_transfers,
                    )
                    if covering and window.complete_until >= joins_at:
                        dbranges.update_used_query_range(
                            write_cursor=write_cursor,
                            location_string=range_name,
                            queried_ranges=[(start_ts, window.complete_until)],
                        )
                    covering = covering and window.complete_until == high

    def _read_window(
            self,
            feed: TronFeed,
            address: TronAddress,
            low: Timestamp,
            high: Timestamp,
    ) -> list[dict[str, Any]] | None:
        """All rows of a window, or None if it is saturated. Only a page shorter than the
        page size ends a window: an empty page at the last start means more rows exist.

        May raise MissingAPIKey, RemoteError, DeserializationError.
        """
        rows: list[dict[str, Any]] = []
        for start in range(0, TRONSCAN_MAX_START + 1, TRONSCAN_FEED_LIMIT):
            rows.extend(page := self.tronscan.query_feed_page(feed, address, low, high, start))
            if len(page) < TRONSCAN_FEED_LIMIT:
                return rows

        return None

    def _resolve(
            self,
            feed: TronFeed,
            address: TronAddress,
            rows: list[dict[str, Any]],
            low: Timestamp,
            high: Timestamp,
    ) -> _Window:
        """Turn the rows of a complete window into storage records.

        The feeds only discover parents. TRC20 movements come from the event logs of those
        parents, and every TRC20 row must match one Transfer log by contract, parties and
        value, else the parent is unresolved (section 3.8). Pending, reverted and unresolved
        parents limit how far the window counts as complete.

        May raise RemoteError for malformed rows or rows outside the account and window.
        """
        parents: dict[bytes, TronTransaction] = {}
        internal: list[TronInternalTransfer] = []
        trc20_rows: dict[bytes, list[tuple[TronAddress, TronAddress, TronAddress, int]]] = {}
        try:
            for row in rows:
                if feed == 'transaction':
                    parent = _parent(row, 'hash', 'timestamp', row['contractRet'])._replace(**self._owner_fields(row))  # noqa: E501
                    parties: tuple[Any, ...] = (row['ownerAddress'], row['toAddress'], *(row.get('toAddressList') or ()))  # noqa: E501
                elif feed == 'token_trc20/transfers':
                    parent = _parent(row, 'transaction_id', 'block_ts', row['contractRet'])
                    parties = (row['from_address'], row['to_address'])
                    if row['contract_type'] == 'trc20' and row['event_type'] == 'Transfer':
                        trc20_rows.setdefault(parent.tx_hash, []).append((
                            deserialize_tron_address(row['contract_address']),
                            deserialize_tron_address(row['from_address']),
                            deserialize_tron_address(row['to_address']),
                            deserialize_raw_amount(row['quant']),
                        ))
                else:  # internal-transaction
                    parent = _parent(row, 'hash', 'timestamp', None)
                    parties = (row['from'], row['to'])
                    if row['token_list']['token_id'] == '_' and (amount := deserialize_raw_amount(row['call_value'])) != 0:  # TRX only  # noqa: E501
                        internal.append(TronInternalTransfer(
                            tx_hash=parent.tx_hash,
                            internal_hash=_hash(row['internal_hash']),
                            from_address=deserialize_tron_address(row['from']),
                            to_address=deserialize_tron_address(row['to']),
                            amount=amount,
                            success=row['result'] == 'SUCCESS' and row['rejected'] is False and row['revert'] is False,  # noqa: E501
                        ))

                if address not in parties or not low <= parent.timestamp // 1000 <= high:
                    raise DeserializationError(f'Row of another account or window in {row}')
                parents[parent.tx_hash] = parent
        except (DeserializationError, KeyError, TypeError, ValueError, AttributeError) as e:
            raise RemoteError(f'Unexpected TronScan {feed} data for {address}: {e!s}') from e

        trc20, unresolved = self._trc20_transfers(trc20_rows)
        if len(unresolved) != 0:
            log.warning(
                'TronScan event logs show no Transfer for TRC20 rows of the TRON transactions '
                '%s of %s, so their window stays incomplete before them',
                [x.hex() for x in unresolved], address,
            )
        limits = [
            parent.timestamp // 1000 for parent in parents.values()
            if not parent.confirmed or parent.reverted or parent.tx_hash in unresolved
        ]
        return _Window(
            transactions=list(parents.values()),
            trc20_transfers=trc20,
            internal_transfers=internal,
            complete_until=Timestamp(min(limits) - 1) if len(limits) != 0 else high,
        )

    @staticmethod
    def _owner_fields(row: dict[str, Any]) -> dict[str, Any]:
        """Fields only the owner's or recipient's transaction feed has. The native amount is
        the TransferContract amount or the call_value of a contract call."""
        if type(contract_type := row['contractType']) is not int:
            raise DeserializationError(f'Invalid TRON contract type in {row}')
        native = {1: 'amount', 31: 'call_value'}.get(contract_type)
        return {
            'owner_address': deserialize_tron_address(row['ownerAddress']),
            'to_address': deserialize_tron_address(row['toAddress']) if row['toAddress'] else None,
            'contract_type': contract_type,
            'native_amount': deserialize_raw_amount(row['contractData'].get(native, 0)) if native else None,  # noqa: E501
            'fee': deserialize_raw_amount(row['cost']['fee']),
        }

    def _trc20_transfers(
            self,
            trc20_rows: dict[bytes, list[tuple[TronAddress, TronAddress, TronAddress, int]]],
    ) -> tuple[list[TronTRC20Transfer], set[bytes]]:
        """Transfer logs of the TRC20 contracts in the feed rows, and the parents whose rows
        found no matching log.

        May raise MissingAPIKey, RemoteError.
        """
        transfers: list[TronTRC20Transfer] = []
        try:
            for event in self.tronscan.query_event_logs([x.hex() for x in trc20_rows]):
                if event['event_name'] != 'Transfer' or (tx_hash := _hash(event['transaction_id'])) not in trc20_rows:  # noqa: E501
                    continue
                transfer = TronTRC20Transfer(
                    tx_hash=tx_hash,
                    event_index=event['event_index'],
                    contract_address=deserialize_tron_address(event['contract_address']),
                    from_address=deserialize_tron_address(event['result']['from']),
                    to_address=deserialize_tron_address(event['result']['to']),
                    amount=deserialize_raw_amount(event['result']['value']),
                )
                if type(transfer.event_index) is not int:
                    raise DeserializationError(f'Invalid event index in {event}')
                if transfer.contract_address in {x[0] for x in trc20_rows[tx_hash]}:
                    transfers.append(transfer)
        except (DeserializationError, KeyError, TypeError, ValueError, AttributeError) as e:
            raise RemoteError(f'Unexpected TronScan event logs: {e!s}') from e

        found = {(x.tx_hash, x.contract_address, x.from_address, x.to_address, x.amount) for x in transfers}  # noqa: E501
        unresolved = {tx_hash for tx_hash, rows in trc20_rows.items() if any((tx_hash, *row) not in found for row in rows)}  # noqa: E501
        return transfers, unresolved
