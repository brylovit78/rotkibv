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


class _FeedSpec(NamedTuple):
    range_type: Literal['txs', 'tokentxs', 'internaltxs']
    time_key: str  # the parent's block time in milliseconds
    party_keys: tuple[str, ...]
    # TRC20 rows have no unique key, so equal transfers of one transaction share an identity
    identity_keys: tuple[str, ...]


# Every feed has its own query range, named by the shared range prefixes
TRON_HISTORY_FEEDS: Final[dict[TronFeed, _FeedSpec]] = {
    'transaction': _FeedSpec('txs', 'timestamp', ('ownerAddress', 'toAddress'), ('hash',)),
    'token_trc20/transfers': _FeedSpec('tokentxs', 'block_ts', ('from_address', 'to_address'), ('transaction_id', 'contract_address', 'from_address', 'to_address', 'quant')),  # noqa: E501
    'internal-transaction': _FeedSpec('internaltxs', 'timestamp', ('from', 'to'), ('internal_hash',)),  # noqa: E501
}
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


def _parent(row: dict[str, Any], hash_key: str, timestamp_key: str, result_key: str | None) -> TronTransaction:  # noqa: E501
    """The parent transaction fields that every feed row has, with its result if the feed
    has it"""
    if (
        type(row['block']) is not int or type(row[timestamp_key]) is not int or
        not all(type(row[x]) is bool for x in ('confirmed', 'revert')) or
        (result_key is not None and type(row[result_key]) is not str)
    ):
        raise DeserializationError(f'Invalid TRON row status or position in {row}')
    return TronTransaction(
        tx_hash=_hash(row[hash_key]),
        block_number=row['block'],
        timestamp=TimestampMS(row[timestamp_key]),
        confirmed=row['confirmed'],
        reverted=row['revert'],
        contract_ret=None if result_key is None else row[result_key],
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
            refetch: bool = False,
    ) -> None:
        """Sync every history feed of the given accounts up to to_ts.

        Completed windows are stored with their query range in one write, so a failure or
        cancellation keeps them and the next sync continues after the last completed second.
        A refetch reads the whole range again, also where it was synced, and changes no range.

        May raise RemoteError if TronScan fails, the key is missing or a row is malformed or
        does not belong to the queried account and window.
        """
        end_ts = Timestamp(min(to_ts, ts_now() - RECENT_HISTORY_MARGIN))
        for address in addresses:
            with self.address_locks[address]:  # one sync of an account at a time, as for EVM
                with self.database.conn.read_ctx() as cursor:  # removed while this one waited
                    if address not in self.database.get_blockchain_accounts(cursor).tron:
                        continue

                self._send_status(address, (from_ts, end_ts), TransactionStatusStep.QUERYING_TRANSACTIONS_STARTED)  # noqa: E501
                try:
                    for feed, spec in TRON_HISTORY_FEEDS.items():
                        self._sync_feed(
                            address=address,
                            feed=feed,
                            range_name=f'{SupportedBlockchain.TRON.to_range_prefix(spec.range_type)}_{address}',
                            from_ts=from_ts,
                            end_ts=end_ts,
                            refetch=refetch,
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
            refetch: bool,
    ) -> None:
        """Traverse the missing part of a feed's range oldest window first, or for a refetch
        all of it.

        A window that can not be listed completely is split into its older and newer half,
        both inclusive whole seconds. Such a single second can not be split, as no other filter
        is verified to subdivide it: its rows are stored, but it keeps the range incomplete
        across it. Rows of later windows are still stored after coverage stopped, but only the
        contiguous complete part is recorded.
        """
        dbranges = DBQueryRanges(self.database)
        with self.database.conn.read_ctx() as cursor:
            saved = self.database.get_used_query_range(cursor, range_name)
            if refetch:
                missing = [(from_ts, end_ts)] if from_ts <= end_ts else []
            else:
                missing = dbranges.get_location_query_ranges(cursor, range_name, from_ts, end_ts)

        expected = saved  # the recorded range as this sync last saw it
        for start_ts, stop_ts in missing:
            # A range after the saved one starts with the saved boundary second, deduplicated
            # by identity (section 3.6). A range before it is recorded only once complete, as
            # the saved range can not have a gap.
            after_saved = saved is not None and start_ts == saved[1] + 1
            joins_at = stop_ts if saved is not None and stop_ts < saved[0] else start_ts
            windows = [(Timestamp(start_ts - 1) if after_saved else start_ts, stop_ts)]
            covering = not refetch  # a refetch records no coverage
            while len(windows) != 0:
                low, high = windows.pop()
                rows, complete = self._read_window(feed, address, low, high)
                if not complete and low != high:  # the halves are read again
                    windows.extend(((Timestamp((low + high) // 2 + 1), high), (low, Timestamp((low + high) // 2))))  # noqa: E501
                    continue

                if not complete:
                    covering = False
                    self.database.msg_aggregator.add_warning(
                        f'TronScan can not list every {feed} row of TRON account {address} '
                        f'in the second {low}: they fill its '
                        f'{TRONSCAN_MAX_START + TRONSCAN_FEED_LIMIT} row window or change '
                        f'order between pages. Its history stays incomplete from that second.',
                    )

                window = self._resolve(feed, address, rows, high)
                with self.database.user_write() as write_cursor:
                    add_tron_history(
                        write_cursor=write_cursor,
                        address=address,
                        transactions=window.transactions,
                        trc20_transfers=window.trc20_transfers,
                        internal_transfers=window.internal_transfers,
                    )
                    if covering and window.complete_until >= joins_at:
                        # a purge meanwhile deleted rows this sync covered: record nothing
                        if self.database.get_used_query_range(write_cursor, range_name) != expected:  # noqa: E501
                            covering = False
                        else:
                            dbranges.update_used_query_range(
                                write_cursor=write_cursor,
                                location_string=range_name,
                                queried_ranges=[(start_ts, window.complete_until)],
                            )
                            expected = self.database.get_used_query_range(write_cursor, range_name)
                    covering = covering and window.complete_until == high

    def _read_window(
            self,
            feed: TronFeed,
            address: TronAddress,
            low: Timestamp,
            high: Timestamp,
    ) -> tuple[list[dict[str, Any]], bool]:
        """The rows of a window read so far, and whether they are proven complete. Only a
        page shorter than the page size ends a window: a full page at the last start means
        more rows may exist. A row repeating from an earlier page means the order changed
        between pages, so another row may be on none of them (section 3.6).

        May raise MissingAPIKey, RemoteError, DeserializationError.
        """
        spec = TRON_HISTORY_FEEDS[feed]
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, ...]] = set()
        for start in range(0, TRONSCAN_MAX_START + 1, TRONSCAN_FEED_LIMIT):
            page = self.tronscan.query_feed_page(feed, address, low, high, start)
            for row in page:  # only rows of the queried account and window prove pagination
                parties = [row.get(x) for x in spec.party_keys] + (row['toAddressList'] if isinstance(row.get('toAddressList'), list) else [])  # noqa: E501
                if address not in parties or type(timestamp := row.get(spec.time_key)) is not int or not low <= timestamp // 1000 <= high:  # noqa: E501
                    raise RemoteError(f'Unexpected TronScan {feed} data for {address}: Row of another account or window in {row}')  # noqa: E501

            repeated = not seen.isdisjoint(identities := {tuple(str(row.get(x)) for x in spec.identity_keys) for row in page})  # noqa: E501
            seen |= identities
            rows.extend(page)
            if repeated or len(page) < TRONSCAN_FEED_LIMIT:
                return rows, not repeated

        return rows, False

    def _resolve(
            self,
            feed: TronFeed,
            address: TronAddress,
            rows: list[dict[str, Any]],
            high: Timestamp,
    ) -> _Window:
        """Turn the rows of a complete window into storage records.

        The feeds only discover parents. TRC20 movements come from the event logs of those
        parents, and every TRC20 row must match one Transfer log by contract, parties and
        value, else the parent is unresolved (section 3.8). Pending, reverted and unresolved
        parents limit how far the window counts as complete. Classifications and statuses
        must be well formed, so a malformed row can not be skipped as unsupported.

        May raise RemoteError for malformed rows.
        """
        parents: dict[bytes, TronTransaction] = {}
        internal: list[TronInternalTransfer] = []
        trc20_rows: dict[bytes, list[tuple[TronAddress, TronAddress, TronAddress, int]]] = {}
        unknown: set[bytes] = set()  # parents of rows with an unknown classification
        try:
            for row in rows:
                if feed == 'transaction':
                    parent = _parent(row, 'hash', 'timestamp', 'contractRet')._replace(**self._owner_fields(row))  # noqa: E501
                elif feed == 'token_trc20/transfers':
                    parent = _parent(row, 'transaction_id', 'block_ts', 'contractRet')
                    if not all(type(row[x]) is str for x in ('contract_type', 'event_type')):
                        raise DeserializationError(f'Invalid TRC20 row classification in {row}')
                    # TRC721 transfers carry no TRC20 movement. Any other classification, also
                    # the empty one of tokens TronScan has not classified yet (section 3.8),
                    # leaves the parent unresolved
                    if row['event_type'] != 'Transfer' or row['contract_type'] not in ('trc20', 'trc721'):  # noqa: E501
                        unknown.add(parent.tx_hash)
                    elif row['contract_type'] == 'trc20':
                        trc20_rows.setdefault(parent.tx_hash, []).append((
                            deserialize_tron_address(row['contract_address']),
                            deserialize_tron_address(row['from_address']),
                            deserialize_tron_address(row['to_address']),
                            deserialize_raw_amount(row['quant']),
                        ))
                else:  # internal-transaction
                    parent = _parent(row, 'hash', 'timestamp', None)
                    if type(row['token_id']) is not str or type(row['result']) is not str or type(row['rejected']) is not bool:  # noqa: E501
                        raise DeserializationError(f'Invalid internal transfer status in {row}')
                    if row['token_id'] == '_' and (amount := deserialize_raw_amount(row['call_value'])) != 0:  # TRX only  # noqa: E501
                        internal.append(TronInternalTransfer(
                            tx_hash=parent.tx_hash,
                            internal_hash=_hash(row['internal_hash']),
                            from_address=deserialize_tron_address(row['from']),
                            to_address=deserialize_tron_address(row['to']),
                            amount=amount,
                            success=row['result'] == 'SUCCESS' and row['rejected'] is False and row['revert'] is False,  # noqa: E501
                        ))

                parents[parent.tx_hash] = parent
        except (DeserializationError, KeyError, TypeError, ValueError, AttributeError) as e:
            raise RemoteError(f'Unexpected TronScan {feed} data for {address}: {e!s}') from e

        trc20, unresolved = self._trc20_transfers(trc20_rows)
        if len(unresolved := unresolved | unknown) != 0:
            log.warning(
                'TronScan rows of the TRON transactions %s of %s have an unknown '
                'classification or no matching Transfer log, so their window stays incomplete '
                'before them',
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
        the TransferContract amount or the call_value of a contract call (section 3.3)."""
        if type(contract_type := row['contractType']) is not int:
            raise DeserializationError(f'Invalid TRON contract type in {row}')
        return {
            'owner_address': deserialize_tron_address(row['ownerAddress']),
            'to_address': deserialize_tron_address(row['toAddress']) if row['toAddress'] else None,
            'contract_type': contract_type,
            'native_amount': deserialize_raw_amount(
                row['contractData']['amount'] if contract_type == 1 else
                row['trigger_info']['call_value'],
            ) if contract_type in {1, 31} else None,
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
                if (  # only these contracts' logs are parsed: others may lack decoded results
                    event['event_name'] != 'Transfer' or
                    (tx_hash := _hash(event['transaction_id'])) not in trc20_rows or
                    (contract := deserialize_tron_address(event['contract_address'])) not in {x[0] for x in trc20_rows[tx_hash]}  # noqa: E501
                ):
                    continue
                if type(event_index := event['event_index']) is not int:
                    raise DeserializationError(f'Invalid event index in {event}')
                transfers.append(TronTRC20Transfer(  # by position, as tokens name the fields apart
                    tx_hash=tx_hash,
                    event_index=event_index,
                    contract_address=contract,
                    from_address=deserialize_tron_address(event['result']['0']),
                    to_address=deserialize_tron_address(event['result']['1']),
                    amount=deserialize_raw_amount(event['result']['2']),
                ))
        except (DeserializationError, KeyError, TypeError, ValueError, AttributeError) as e:
            raise RemoteError(f'Unexpected TronScan event logs: {e!s}') from e

        found = {(x.tx_hash, x.contract_address, x.from_address, x.to_address, x.amount) for x in transfers}  # noqa: E501
        unresolved = {tx_hash for tx_hash, rows in trc20_rows.items() if any((tx_hash, *row) not in found for row in rows)}  # noqa: E501
        return transfers, unresolved
