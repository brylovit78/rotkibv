"""Storage of TRON history, see docs/designs/tron-integration.md sections 3.8 and 6.4"""
from typing import TYPE_CHECKING, Final, NamedTuple

from rotkehlchen.db.constants import HISTORY_MAPPING_KEY_STATE, HistoryMappingState
from rotkehlchen.types import Location, SupportedBlockchain, TimestampMS, TronAddress
from rotkehlchen.utils.misc import get_chunks

if TYPE_CHECKING:
    from collections.abc import Iterable

    from rotkehlchen.db.drivers.sqlite import DBCursor


class TronTransaction(NamedTuple):
    """A parent transaction as one feed row describes it. The fields after `contract_ret` are
    only in the owner's or recipient's transaction feed."""
    tx_hash: bytes
    block_number: int
    timestamp: TimestampMS
    confirmed: bool
    reverted: bool
    contract_ret: str | None = None
    owner_address: TronAddress | None = None
    to_address: TronAddress | None = None
    contract_type: int | None = None
    native_amount: int | None = None  # sun: TransferContract amount or call_value
    fee: int | None = None  # sun: /api/transaction cost.fee, the paid fee


class TronTRC20Transfer(NamedTuple):
    tx_hash: bytes
    event_index: int
    contract_address: TronAddress
    from_address: TronAddress
    to_address: TronAddress
    amount: int  # raw units


class TronInternalTransfer(NamedTuple):
    tx_hash: bytes
    internal_hash: bytes
    from_address: TronAddress
    to_address: TronAddress
    amount: int  # sun
    success: bool


# A later observation never loses confirmation and keeps fields that only an owner's row has
_MERGED: Final = {
    'confirmed': 'MAX(confirmed, excluded.confirmed)',
    'reverted': 'excluded.reverted',
    **{x: f'COALESCE(excluded.{x}, {x})' for x in ('contract_ret', 'owner_address', 'to_address', 'contract_type', 'native_amount', 'fee')},  # noqa: E501
}
_UPSERT_TRANSACTION: Final = (
    'INSERT INTO tron_transactions(tx_hash, block_number, timestamp, confirmed, reverted, '
    'contract_ret, owner_address, to_address, contract_type, native_amount, fee) '
    'VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(tx_hash) DO UPDATE SET '
    f'{", ".join(f"{column}={value}" for column, value in _MERGED.items())} '
    f'WHERE ({", ".join(_MERGED)}) IS NOT ({", ".join(_MERGED.values())})'  # only real changes
)


def add_tron_history(
        write_cursor: DBCursor,
        address: TronAddress,
        transactions: Iterable[TronTransaction],
        trc20_transfers: Iterable[TronTRC20Transfer] = (),
        internal_transfers: Iterable[TronInternalTransfer] = (),
) -> None:
    """Store what one window of one account's feed showed. Seeing the same identities again,
    through any feed, page or account, stores them once.

    A later observation completes a transaction: it never loses confirmation, and fields only
    an owner's row has are kept. The latest status of an internal transfer wins. Whenever
    anything changes for a transaction, its decoded marker is removed so that the decoder runs
    again.
    """
    changed: set[bytes] = set()
    for tx in transactions:
        write_cursor.execute(
            _UPSERT_TRANSACTION,
            (
                tx.tx_hash, tx.block_number, tx.timestamp, tx.confirmed, tx.reverted,
                tx.contract_ret, tx.owner_address, tx.to_address, tx.contract_type,
                None if tx.native_amount is None else str(tx.native_amount),
                None if tx.fee is None else str(tx.fee),
            ),
        )
        if write_cursor.rowcount != 0:
            changed.add(tx.tx_hash)
        write_cursor.execute(
            'INSERT OR IGNORE INTO tron_tx_address_mappings(tx_id, address) '
            'SELECT identifier, ? FROM tron_transactions WHERE tx_hash=?',
            (address, tx.tx_hash),
        )
        if write_cursor.rowcount != 0:
            changed.add(tx.tx_hash)

    for transfer in trc20_transfers:
        write_cursor.execute(
            'INSERT OR IGNORE INTO tron_trc20_transfers(tx_id, event_index, contract_address, '
            'from_address, to_address, amount) SELECT identifier, ?, ?, ?, ?, ? '
            'FROM tron_transactions WHERE tx_hash=?',
            (
                transfer.event_index, transfer.contract_address, transfer.from_address,
                transfer.to_address, str(transfer.amount), transfer.tx_hash,
            ),
        )
        if write_cursor.rowcount != 0:
            changed.add(transfer.tx_hash)

    for internal in internal_transfers:  # its status may change, as a parent's does
        write_cursor.execute(
            'INSERT INTO tron_internal_transfers(tx_id, internal_hash, from_address, '
            'to_address, amount, success) SELECT identifier, ?, ?, ?, ?, ? '
            'FROM tron_transactions WHERE tx_hash=? ON CONFLICT(tx_id, internal_hash) '
            'DO UPDATE SET success=excluded.success WHERE success IS NOT excluded.success',
            (
                internal.internal_hash, internal.from_address, internal.to_address,
                str(internal.amount), internal.success, internal.tx_hash,
            ),
        )
        if write_cursor.rowcount != 0:
            changed.add(internal.tx_hash)

    write_cursor.executemany(
        'DELETE FROM tron_tx_mappings WHERE tx_id=(SELECT identifier FROM tron_transactions WHERE tx_hash=?)',  # noqa: E501
        [(tx_hash,) for tx_hash in changed],
    )


def customized_tron_transactions(cursor: DBCursor, tx_hashes: list[bytes]) -> set[bytes]:
    """The ones of these transactions with a customized event, whatever its group. Redecoding
    and cleanup keep such a transaction whole, unless customized events are deleted."""
    return {bytes(x[0]) for chunk in get_chunks(tx_hashes, 500) for x in cursor.execute(
        'SELECT DISTINCT C.tx_ref FROM chain_events_info C '
        'JOIN history_events H ON H.identifier=C.identifier '
        'JOIN history_events_mappings M ON M.parent_identifier=H.identifier '
        f'WHERE H.location=? AND M.name=? AND M.value=? AND C.tx_ref IN ({",".join("?" * len(chunk))})',  # noqa: E501
        (Location.TRON.serialize_for_db(), HISTORY_MAPPING_KEY_STATE, HistoryMappingState.CUSTOMIZED.serialize_for_db(), *chunk),  # noqa: E501
    )}


def delete_tron_history(
        write_cursor: DBCursor,
        address: TronAddress | None = None,
) -> tuple[list[bytes], list[bytes]]:
    """Delete the TRON history of one account or, with no address, of all accounts. Return
    the hashes of the deleted transactions and of the kept ones, whose events the caller
    deletes.

    A transaction that another tracked account also maps to is kept and left pending decoding,
    since its events depend on the tracked accounts. The query ranges of the affected feeds
    are deleted, so a later sync reads the history again.
    """
    bindings: tuple[TronAddress, ...] = ()
    if address is None:
        where, name_pattern = '', f'{SupportedBlockchain.TRON.value}%'
    else:
        where = (
            'WHERE identifier IN (SELECT tx_id FROM tron_tx_address_mappings WHERE address=?) '
            'AND identifier NOT IN (SELECT tx_id FROM tron_tx_address_mappings WHERE address!=?)'
        )
        bindings, name_pattern = (address, address), f'{SupportedBlockchain.TRON.value}%\\_{address}'  # noqa: E501

    hashes = [x[0] for x in write_cursor.execute(f'SELECT tx_hash FROM tron_transactions {where}', bindings)]  # noqa: E501
    write_cursor.execute(f'DELETE FROM tron_transactions {where}', bindings)
    kept: list[bytes] = []
    if address is not None:
        kept = [x[0] for x in write_cursor.execute(  # only the shared ones are left
            'SELECT tx_hash FROM tron_transactions WHERE identifier IN '
            '(SELECT tx_id FROM tron_tx_address_mappings WHERE address=?)',
            (address,),
        )]
        write_cursor.execute(
            'DELETE FROM tron_tx_mappings WHERE tx_id IN '
            '(SELECT tx_id FROM tron_tx_address_mappings WHERE address=?)',
            (address,),
        )
        write_cursor.execute('DELETE FROM tron_tx_address_mappings WHERE address=?', (address,))

    write_cursor.execute(
        "DELETE FROM used_query_ranges WHERE name LIKE ? ESCAPE '\\'",
        (name_pattern,),
    )
    return hashes, kept
