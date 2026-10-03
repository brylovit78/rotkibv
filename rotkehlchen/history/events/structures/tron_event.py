from typing import TYPE_CHECKING, Any, Final, Literal

from rotkehlchen.chain.tron.utils import deserialize_tron_address
from rotkehlchen.chain.tron.validation import canonical_tron_tx_hash
from rotkehlchen.errors.serialization import DeserializationError
from rotkehlchen.history.events.structures.base import HistoryBaseEntryType
from rotkehlchen.history.events.structures.onchain_event import OnchainEvent
from rotkehlchen.types import FVal, Location, TimestampMS, TronAddress, TronTxHash

if TYPE_CHECKING:
    from rotkehlchen.assets.asset import Asset
    from rotkehlchen.history.events.structures.types import (
        HistoryEventSubType,
        HistoryEventType,
    )

TRON_GROUP_IDENTIFIER_PREFIX: Final = 'tron_'


def deserialize_tron_tx_hash(value: Any) -> TronTxHash:
    """May raise DeserializationError if the value is not a TRON transaction hash"""
    if not isinstance(value, str | bytes) or (tx_hash := canonical_tron_tx_hash(value)) is None:
        raise DeserializationError(f'Invalid TRON transaction hash {value!r}')

    return TronTxHash(tx_hash)


class TronEvent(OnchainEvent[TronTxHash, TronAddress]):  # hash in superclass
    """An event of a stored TRON transaction, see docs/designs/tron-integration.md section 7.
    The group identifier is unique across chains, as history_events requires."""

    def __init__(
            self,
            tx_ref: TronTxHash,
            sequence_index: int,
            timestamp: TimestampMS,
            event_type: HistoryEventType,
            event_subtype: HistoryEventSubType,
            asset: Asset,
            amount: FVal,
            # Keep location param to reuse parent's deserialize methods
            location: Literal[Location.TRON] = Location.TRON,
            location_label: str | None = None,
            notes: str | None = None,
            identifier: int | None = None,
            counterparty: str | None = None,
            address: TronAddress | None = None,
            extra_data: dict[str, Any] | None = None,
            group_identifier: str | None = None,
    ) -> None:
        super().__init__(
            tx_ref=tx_ref,
            sequence_index=sequence_index,
            timestamp=timestamp,
            location=location,
            event_type=event_type,
            event_subtype=event_subtype,
            asset=asset,
            amount=amount,
            location_label=location_label,
            notes=notes,
            identifier=identifier,
            counterparty=counterparty,
            address=address,
            extra_data=extra_data,
            group_identifier=group_identifier,
        )

    @staticmethod
    def _calculate_group_identifier(tx_ref: TronTxHash, location: Location) -> str:
        return TRON_GROUP_IDENTIFIER_PREFIX + tx_ref

    @staticmethod
    def _serialize_tx_ref_for_db(tx_ref: TronTxHash) -> bytes:
        return bytes.fromhex(deserialize_tron_tx_hash(tx_ref))

    @staticmethod
    def _deserialize_tx_ref(tx_ref_data: Any) -> TronTxHash:
        return deserialize_tron_tx_hash(tx_ref_data)

    @staticmethod
    def deserialize_address(address_data: Any) -> TronAddress:
        return deserialize_tron_address(address_data)

    @property
    def entry_type(self) -> HistoryBaseEntryType:
        return HistoryBaseEntryType.TRON_EVENT
