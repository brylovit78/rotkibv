"""Events of the stored TRON history, see docs/designs/tron-integration.md sections 3.7, 3.8
and 7. They are always regenerated from the raw rows, so decoding needs no TronScan request
except for the metadata of a token seen for the first time."""
import logging
from typing import TYPE_CHECKING, Final

from rotkehlchen.assets.asset import Asset, CryptoAsset
from rotkehlchen.assets.utils import token_normalized_value_decimals
from rotkehlchen.chain.decoding.constants import CPT_GAS
from rotkehlchen.chain.decoding.utils import decode_transfer_direction
from rotkehlchen.chain.evm.decoding.constants import OUTGOING_EVENT_TYPES
from rotkehlchen.chain.tron.constants import TRX_DECIMALS
from rotkehlchen.chain.tron.utils import get_or_create_tron_token
from rotkehlchen.constants import ZERO
from rotkehlchen.db.constants import TX_DECODED
from rotkehlchen.db.history_events import DBHistoryEvents
from rotkehlchen.db.trontx import customized_tron_transactions
from rotkehlchen.errors.misc import InputError, MissingAPIKey, RemoteError
from rotkehlchen.errors.serialization import DeserializationError
from rotkehlchen.globaldb.handler import GlobalDBHandler
from rotkehlchen.history.events.structures.tron_event import TronEvent
from rotkehlchen.history.events.structures.types import HistoryEventSubType, HistoryEventType
from rotkehlchen.logging import RotkehlchenLogsAdapter
from rotkehlchen.types import Location, SupportedBlockchain, TimestampMS, TronAddress, TronTxHash
from rotkehlchen.utils.misc import get_chunks

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rotkehlchen.db.dbhandler import DBHandler
    from rotkehlchen.db.drivers.sqlite import DBCursor
    from rotkehlchen.externalapis.tronscan import Tronscan

logger = logging.getLogger(__name__)
log = RotkehlchenLogsAdapter(logger)

TRON_DECODING_CHUNK_SIZE: Final = 500
type Token = tuple[CryptoAsset, int]  # the asset and its decimals


class TronTransactionDecoder:

    def __init__(self, database: DBHandler, tronscan: Tronscan) -> None:
        self.database = database
        self.tronscan = tronscan
        self.trx = Asset(SupportedBlockchain.TRON.get_native_token_id())

    def decode_transactions(
            self,
            tx_hashes: Sequence[TronTxHash] | None = None,
            delete_custom: bool = False,
    ) -> int:
        """Decode the stored transactions that wait for decoding, or with tx_hashes those
        transactions again, replacing their events. Returns the number decoded.

        The raw rows are read, decoded and replaced by events in one write, so a sync, account
        removal or purge running meanwhile can not leave events of an older state behind. Only
        the metadata of new tokens is queried before, outside of it. A transaction with a token
        whose metadata is not available stays pending decoding, so a later decode retries it.

        A transaction with a customized event, in any group, is kept as it is, unless
        delete_custom is set (the shared redecode rule).

        May raise for given tx_hashes:
        - InputError if one of them is not stored
        - RemoteError if one could not be decoded, as the metadata of its tokens is not available
        """
        with self.database.conn.read_ctx() as cursor:  # hashes, as a deleted row id is reused
            if tx_hashes is None:
                hashes = [bytes(x[0]) for x in cursor.execute(
                    'SELECT tx_hash FROM tron_transactions WHERE identifier NOT IN '
                    '(SELECT tx_id FROM tron_tx_mappings WHERE value=?) ORDER BY timestamp',
                    (TX_DECODED,),
                )]
            else:
                hashes = [bytes.fromhex(x) for x in dict.fromkeys(tx_hashes)]
                for tx_hash in hashes:
                    if cursor.execute('SELECT 1 FROM tron_transactions WHERE tx_hash=?', (tx_hash,)).fetchone() is None:  # noqa: E501
                        raise InputError(f'TRON transaction {tx_hash.hex()} is not stored')

        dbevents, decoded = DBHistoryEvents(self.database), 0
        for chunk in get_chunks(hashes, TRON_DECODING_CHUNK_SIZE):
            tokens = self._tokens(chunk)
            with self.database.user_write() as write_cursor:
                tracked = set(self.database.get_blockchain_accounts(write_cursor).tron)
                decoded_txs: dict[bytes, list[TronEvent]] = {}
                for tx_hash in chunk:
                    if (decoded_tx := self._decode(write_cursor, tx_hash, tracked, tokens)) is not None:  # noqa: E501
                        decoded_txs[tx_hash] = decoded_tx[1]
                        write_cursor.execute(
                            'INSERT OR IGNORE INTO tron_tx_mappings(tx_id, value) VALUES(?, ?)',
                            (decoded_tx[0], TX_DECODED),
                        )

                kept = set() if delete_custom else customized_tron_transactions(write_cursor, [*decoded_txs])  # noqa: E501
                dbevents.delete_events_by_tx_ref(
                    write_cursor=write_cursor,
                    tx_refs=[x for x in decoded_txs if x not in kept],  # type: ignore[misc]  # TRON refs are bound as bytes
                    location=Location.TRON,
                    customized_handling='delete',
                )
                dbevents.add_history_events(
                    write_cursor=write_cursor,
                    history=[x for tx_hash, events in decoded_txs.items() if tx_hash not in kept for x in events],  # noqa: E501
                )
            decoded += len(decoded_txs)

        if tx_hashes is not None and decoded != len(hashes):
            raise RemoteError(f'Could not decode {len(hashes) - decoded} of the given TRON transactions, as they were removed meanwhile or the metadata of their TRC20 tokens is not available')  # noqa: E501

        return decoded

    def _tokens(self, tx_hashes: list[bytes]) -> dict[TronAddress, Token]:
        """The tokens of the TRC20 transfers of these transactions. A contract whose metadata
        is not available is left out, so its transactions stay pending decoding."""
        with self.database.conn.read_ctx() as cursor:
            contracts = {x[0] for chunk in get_chunks(tx_hashes, 500) for x in cursor.execute(
                'SELECT DISTINCT contract_address FROM tron_trc20_transfers WHERE tx_id IN '
                f'(SELECT identifier FROM tron_transactions WHERE tx_hash IN ({",".join("?" * len(chunk))}))',  # noqa: E501
                chunk,
            )}

        tokens: dict[TronAddress, Token] = {}
        for contract in contracts:
            try:
                if (token := self._token(contract)) is not None:
                    tokens[contract] = token
            except (RemoteError, MissingAPIKey) as e:
                log.warning('Could not query the metadata of TRC20 %s due to %s. Will retry', contract, e)  # noqa: E501
        return tokens

    def _decode(
            self,
            cursor: DBCursor,
            tx_hash: bytes,
            tracked: set[TronAddress],
            tokens: dict[TronAddress, Token],
    ) -> tuple[int, list[TronEvent]] | None:
        """The hash and events of one stored transaction in the order of section 7: the fee,
        the native transfer or call value, the TRC20 transfers by event index and the internal
        transfers by internal hash. The fee is always index 0 and a movement its position in
        that order, so tracking another account moves no event. Unconfirmed and reverted
        transactions have no events, and only a successful one moves value. Returns the row id
        and the events, or None if the transaction is gone or needs a token whose metadata is
        not available."""
        if (row := cursor.execute(
            'SELECT identifier, timestamp, confirmed, reverted, contract_ret, owner_address, '
            'to_address, contract_type, native_amount, fee FROM tron_transactions '
            'WHERE tx_hash=?',
            (tx_hash,),
        ).fetchone()) is None:
            return None

        tx_id, timestamp, confirmed, reverted, result, owner, to, contract_type, native, fee = row
        trc20 = cursor.execute(
            'SELECT contract_address, from_address, to_address, amount FROM '
            'tron_trc20_transfers WHERE tx_id=? ORDER BY event_index',
            (tx_id,),
        ).fetchall()
        if any(x[0] not in tokens for x in trc20):
            return None

        if not confirmed or reverted:
            return tx_id, []

        transfers: list[tuple[Asset, int, int, TronAddress | None, TronAddress | None]] = []  # asset, decimals, raw amount, from, to  # noqa: E501
        if result == 'SUCCESS':
            if contract_type in {1, 31} and native is not None:
                transfers.append((self.trx, TRX_DECIMALS, int(native), owner, to))
            transfers.extend((*tokens[contract], int(raw), from_address, to_address) for contract, from_address, to_address, raw in trc20)  # noqa: E501
        if result in {'SUCCESS', None}:  # None: the parent is known only from the internal feed
            transfers.extend((self.trx, TRX_DECIMALS, int(raw), from_address, to_address) for from_address, to_address, raw in cursor.execute(  # noqa: E501
                'SELECT from_address, to_address, amount FROM tron_internal_transfers '
                'WHERE tx_id=? AND success=1 ORDER BY internal_hash',
                (tx_id,),
            ))

        events: list[TronEvent] = []
        tx_ref = TronTxHash(tx_hash.hex())
        if owner in tracked and fee is not None and (amount := token_normalized_value_decimals(int(fee), TRX_DECIMALS)) != ZERO:  # noqa: E501
            events.append(TronEvent(
                tx_ref=tx_ref,
                sequence_index=0,
                timestamp=TimestampMS(timestamp),
                event_type=HistoryEventType.SPEND if result == 'SUCCESS' else HistoryEventType.FAIL,  # noqa: E501
                event_subtype=HistoryEventSubType.FEE,
                asset=self.trx,
                amount=amount,
                location_label=owner,
                notes=f'Burn {amount} TRX for gas' + ('' if result == 'SUCCESS' else ' of a failed transaction'),  # noqa: E501
                counterparty=CPT_GAS,
            ))

        for index, (asset, decimals, raw, from_address, to_address) in enumerate(transfers, 1):
            if (amount := token_normalized_value_decimals(raw, decimals)) == ZERO or (direction := decode_transfer_direction(  # noqa: E501
                from_address=from_address,
                to_address=to_address,
                tracked_accounts=tracked,
                maybe_get_exchange_fn=lambda _: None,  # no trusted TRON exchange addresses
            )) is None:
                continue

            event_type, event_subtype, location_label, address, counterparty, verb = direction
            symbol, other = asset.resolve_to_asset_with_symbol().symbol, counterparty or address
            if from_address == to_address and asset == self.trx:  # only the owner sends to itself
                event_type, notes = HistoryEventType.TRANSACTION_TO_SELF, f'Transaction to self of {amount} TRX'  # as for ETH  # noqa: E501
            elif event_type in OUTGOING_EVENT_TYPES:
                notes = f'{verb} {amount} {symbol} from {location_label} to {other}'
            else:
                notes = f'{verb} {amount} {symbol} from {other} to {location_label}'

            events.append(TronEvent(
                tx_ref=tx_ref,
                sequence_index=index,
                timestamp=TimestampMS(timestamp),
                event_type=event_type,
                event_subtype=event_subtype,
                asset=asset,
                amount=amount,
                location_label=location_label,
                notes=notes,
                address=address,
                counterparty=counterparty,
            ))

        return tx_id, events

    def _token(self, contract: TronAddress) -> Token | None:
        """The asset and decimals of a TRC20 contract. A new contract is created from its
        TronScan metadata; for one without valid metadata it is None.

        May raise RemoteError or MissingAPIKey.
        """
        with GlobalDBHandler().conn.read_ctx() as cursor:
            if (row := cursor.execute('SELECT identifier, decimals FROM tron_tokens WHERE address=?', (contract,)).fetchone()) is not None:  # noqa: E501
                return CryptoAsset(row[0]), row[1]

        if (
            (metadata := self.tronscan.query_token_metadata(contract)) is None or
            type(decimals := metadata.get('decimals')) is not int or
            not isinstance(name := metadata.get('name'), str) or
            not isinstance(symbol := metadata.get('symbol'), str)
        ):
            log.warning('TronScan has no valid TRC20 metadata for %s, so its transactions stay pending decoding', contract)  # noqa: E501
            return None

        try:
            get_or_create_tron_token(self.database, contract, name=name, symbol=symbol, decimals=decimals)  # noqa: E501
        except DeserializationError as e:
            log.warning('Could not create the TRC20 token %s due to %s', contract, e)
            return None

        return self._token(contract)  # with the decimals stored, also by a concurrent query
