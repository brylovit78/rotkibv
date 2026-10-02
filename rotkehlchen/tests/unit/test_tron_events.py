"""TRON events (brylovit78/rotkibv#11) decoded from the stored history of the #2 TronScan
corpus. Every HTTP exchange is mocked; no VCR."""
from operator import attrgetter
from typing import TYPE_CHECKING, Any, Final
from unittest.mock import patch

import pytest

from rotkehlchen.accounting.mixins.event import AccountingEventType
from rotkehlchen.accounting.pnl import PNL, PnlTotals
from rotkehlchen.balances.historical import HistoricalBalancesManager
from rotkehlchen.chain.decoding.constants import CPT_GAS
from rotkehlchen.chain.tron.manager import TronManager
from rotkehlchen.chain.tron.utils import deserialize_tron_address, tron_address_to_identifier
from rotkehlchen.constants import ZERO
from rotkehlchen.db.constants import HISTORY_MAPPING_KEY_STATE, HistoryMappingState
from rotkehlchen.db.filtering import HistoricalBalancesFilterQuery, HistoryEventFilterQuery
from rotkehlchen.db.history_events import DBHistoryEvents
from rotkehlchen.db.trontx import TronTransaction, TronTRC20Transfer, add_tron_history
from rotkehlchen.errors.misc import RemoteError
from rotkehlchen.externalapis.tronscan import Tronscan
from rotkehlchen.fval import FVal
from rotkehlchen.history.events.structures.tron_event import TronEvent
from rotkehlchen.history.events.structures.types import HistoryEventSubType, HistoryEventType
from rotkehlchen.tasks.events import find_customized_event_duplicate_groups
from rotkehlchen.tasks.historical_balances import process_historical_balances
from rotkehlchen.tests.utils.accounting import accounting_history_process, check_pnls_and_csv
from rotkehlchen.tests.utils.tronscan import (
    set_tronscan_key,
    track_tron_accounts,
    tronscan_body,
    tronscan_case,
    tronscan_exchange,
    tronscan_history_request,
    tronscan_response,
)
from rotkehlchen.types import Location, SupportedBlockchain, Timestamp, TimestampMS, TronAddress

if TYPE_CHECKING:
    from rotkehlchen.accounting.accountant import Accountant
    from rotkehlchen.db.dbhandler import DBHandler
    from rotkehlchen.user_messages import MessagesAggregator

DIRECTIONS: Final = {
    'in': (HistoryEventType.RECEIVE, HistoryEventSubType.NONE),
    'out': (HistoryEventType.SPEND, HistoryEventSubType.NONE),
    'transfer': (HistoryEventType.TRANSFER, HistoryEventSubType.NONE),
}
FEE_TYPES: Final = {'spend/fee': HistoryEventType.SPEND, 'fail/fee': HistoryEventType.FAIL}
ALICE, BOB, CAROL, TOKEN = (deserialize_tron_address('41' + x * 20) for x in ('a1', 'b2', 'c3', 'd4'))  # noqa: E501


@pytest.fixture(name='manager')
def fixture_manager(database: DBHandler) -> TronManager:
    set_tronscan_key(database, 'test-tronscan-key')
    return TronManager(tronscan=Tronscan(database=database), database=database)


def _events(database: DBHandler) -> list[TronEvent]:
    with database.conn.read_ctx() as cursor:
        return DBHistoryEvents(database).get_history_events_internal(  # type: ignore[return-value]  # only TRON events
            cursor=cursor,
            filter_query=HistoryEventFilterQuery.make(location=Location.TRON),
        )


def _sync(manager: TronManager, feeds: dict[str, list[dict[str, Any]]], second: int, accounts: list[str]) -> None:  # noqa: E501
    """Sync and decode the accounts over the ten seconds around the rows"""
    track_tron_accounts(manager.database, accounts)
    with patch.object(manager.tronscan, 'query_feed_page', side_effect=lambda feed, *args: feeds.get(feed, [])):  # noqa: E501
        manager.query_transactions([TronAddress(x) for x in accounts], Timestamp(second - 10), Timestamp(second + 10))  # noqa: E501


@pytest.mark.parametrize('case', [
    'history-account-creation-fee',
    'history-energy-burn-and-failed-call',
    'history-internal-only-receipt',
    'history-multi-page',
    'history-zero-holding-token',
])
def test_history_cases_decode_to_the_expected_events(manager: TronManager, case: str) -> None:
    """Every movement and paid fee of the case is one event, the fee first with the gas
    counterparty, and there is no other event"""
    account = tronscan_exchange(case, 'accountv2')['params']['address']
    expected = tronscan_case(case)['expected']
    track_tron_accounts(manager.database, [account])
    with (
        patch.object(manager.tronscan._rate_limiter, 'acquire'),
        patch.object(manager.tronscan.session, 'request', side_effect=tronscan_history_request(case)),  # noqa: E501
    ):
        manager.query_transactions([account], Timestamp(0), Timestamp(expected['completed_range']['query_window_ms'][1] // 1000))  # noqa: E501

    events = _events(manager.database)
    assert {
        (x.tx_ref, x.event_type, x.event_subtype, x.asset.identifier, x.amount, x.location_label, x.address)  # noqa: E501
        for x in events if x.event_subtype != HistoryEventSubType.FEE
    } == {
        (x['tx_hash'], *DIRECTIONS[x['direction_for_tracked']], x['asset'], FVal(x['amount']), account, x['from'] if x['direction_for_tracked'] == 'in' else x['to'])  # noqa: E501
        for x in expected['history']['movements']
    }
    assert {
        (x.tx_ref, x.event_type, x.amount, x.location_label, x.counterparty, x.sequence_index)
        for x in events if x.event_subtype == HistoryEventSubType.FEE
    } == {
        (x['tx_hash'], FEE_TYPES[x['event']], FVal(x['amount']), x['payer'], CPT_GAS, 0)
        for x in expected['history']['fees']
    }
    assert len(events) == len(expected['history']['movements']) + len(expected['history']['fees'])


@pytest.mark.parametrize('case', ['fees-detail-versus-list', 'status-failed-calls-attempted-transfer'])  # noqa: E501
def test_fee_cases_pay_the_listed_fee(manager: TronManager, case: str) -> None:
    """The owner of each listed transaction pays the fee of its list row once: a failed call
    pays a failed fee, and a zero fee is no event"""
    for row in (rows := [row for x in tronscan_case(case)['exchanges'] if x['endpoint'] == '/api/transaction' for row in tronscan_body(case, x['role'])['data']]):  # noqa: E501
        _sync(manager, {'transaction': [row]}, row['timestamp'] // 1000, [row['ownerAddress']])

    expected = tronscan_case(case)['expected']
    assert len(rows) != 0
    assert {
        (x.tx_ref, x.event_type, x.amount)
        for x in _events(manager.database) if x.event_subtype == HistoryEventSubType.FEE
    } == {
        (x['tx_hash'], FEE_TYPES[x.get('fee_event', 'spend/fee')], FVal(x['paid_fee_sun']) / 10**6)
        for x in expected.get('fees') or expected['transactions']
        if x.get('fee_event', 'spend/fee') is not None
    }


def test_call_value_is_a_movement(manager: TronManager) -> None:
    """fees-call-value-and-origin-energy: the TRX sent with a successful contract call moves to
    the called contract"""
    row = tronscan_body(case := 'fees-call-value-and-origin-energy', 'transactions-router')['data'][0]  # noqa: E501
    expected = tronscan_case(case)['expected']['call_value_movement']
    _sync(manager, {'transaction': [row]}, row['timestamp'] // 1000, [expected['to']])

    assert [(x.event_type, x.asset.identifier, x.amount, x.address) for x in _events(manager.database)] == [  # noqa: E501
        (HistoryEventType.RECEIVE, expected['asset'], FVal(expected['amount']), expected['from']),
    ]


def test_identical_transfers_between_tracked_accounts(manager: TronManager) -> None:
    """synthetic-identical-transfers-same-tx: two equal transfers of one transaction stay two
    events, and between two tracked accounts each is one transfer, not a send and a receive"""
    rows = tronscan_body(case := 'synthetic-identical-transfers-same-tx', 'trc20-feed-account-a')['token_transfers']  # noqa: E501
    with patch.object(manager.tronscan.session, 'request', return_value=tronscan_response(case, 'event-logs')):  # noqa: E501
        _sync(manager, {'token_trc20/transfers': rows}, rows[0]['block_ts'] // 1000, [rows[0]['from_address'], rows[0]['to_address']])  # noqa: E501

    assert [(x.sequence_index, x.event_type, x.location_label, x.address, x.amount) for x in _events(manager.database)] == [  # noqa: E501
        (index, HistoryEventType.TRANSFER, x['from'], x['to'], FVal(x['amount']))
        for index, x in enumerate(tronscan_case(case)['expected']['movements'])
    ]


def test_events_wait_for_confirmation(manager: TronManager) -> None:
    """status-pending-then-confirmed: an unconfirmed transaction has no event. Once a sync
    sees it confirmed it is decoded again, and its paid fee becomes the fee event."""
    expected = tronscan_case(case := 'status-pending-then-confirmed')['expected']
    pending = next(x for x in tronscan_body(case, 'transactions-newest')['data'] if x['hash'] == expected['tx_hash'])  # noqa: E501
    _sync(manager, {'transaction': [pending]}, second := pending['timestamp'] // 1000, [account := pending['ownerAddress']])  # noqa: E501
    assert _events(manager.database) == []

    _sync(manager, {'transaction': [pending | {'confirmed': True}]}, second, [account])
    assert [(x.event_type, x.event_subtype, x.amount) for x in _events(manager.database)] == [
        (HistoryEventType.SPEND, HistoryEventSubType.FEE, FVal(expected['paid_fee_sun_from_list']) / 10**6),  # noqa: E501
    ]


def test_failed_call_moves_nothing_but_pays_its_fee(manager: TronManager) -> None:
    """A failed call sends none of its TRX or tokens, and its paid fee is a failed fee"""
    track_tron_accounts(manager.database, [ALICE])
    with manager.database.user_write() as write_cursor:
        add_tron_history(
            write_cursor=write_cursor,
            address=ALICE,
            transactions=[TronTransaction(tx_hash=(tx_hash := b'\x02' * 32), block_number=1, timestamp=TimestampMS(1700000000000), confirmed=True, reverted=False, contract_ret='REVERT', owner_address=ALICE, to_address=BOB, contract_type=31, native_amount=5 * 10**6, fee=300000)],  # noqa: E501
            trc20_transfers=[TronTRC20Transfer(tx_hash=tx_hash, event_index=0, contract_address=deserialize_tron_address('TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t'), from_address=ALICE, to_address=BOB, amount=10**6)],  # noqa: E501
        )

    assert manager.decoder.decode_transactions() == 1
    assert [(x.event_type, x.event_subtype, x.amount) for x in _events(manager.database)] == [
        (HistoryEventType.FAIL, HistoryEventSubType.FEE, FVal('0.3')),
    ]


@pytest.mark.parametrize(('case', 'feed', 'role', 'party'), [
    pytest.param('synthetic-reverted-transaction', 'transaction', 'transactions', 'ownerAddress', id='reverted'),  # noqa: E501
    pytest.param('synthetic-rejected-internal-transfer', 'internal-transaction', 'internal', 'to', id='rejected internal call'),  # noqa: E501
])
def test_no_movement_without_success(
        manager: TronManager,
        case: str,
        feed: str,
        role: str,
        party: str,
) -> None:
    """A reverted transaction has no event at all, and a rejected internal call moves no TRX"""
    row = tronscan_body(case, role)['data'][0]
    _sync(manager, {feed: [row]}, row['timestamp'] // 1000, [row[party]])
    assert _events(manager.database) == []


def test_redecode_keeps_customized_events_unless_deleting_them(manager: TronManager) -> None:
    """A redecode keeps a transaction with a customized event as it is, and replaces it only
    when told to delete customized events, as for the other chains"""
    row = tronscan_body('history-internal-only-receipt', 'internal-page0')['data'][0]
    _sync(manager, {'internal-transaction': [row]}, row['timestamp'] // 1000, [row['to']])
    (event,) = _events(manager.database)
    event.notes, event.sequence_index = 'my own note', 5  # moved away from the decoded index
    with manager.database.user_write() as write_cursor:
        DBHistoryEvents(manager.database).edit_history_event(write_cursor, event, HistoryMappingState.CUSTOMIZED)  # noqa: E501

    assert manager.decoder.decode_transactions([event.tx_ref]) == 1
    assert [x.notes for x in _events(manager.database)] == ['my own note']
    assert manager.decoder.decode_transactions([event.tx_ref], delete_custom=True) == 1
    assert [x.notes for x in _events(manager.database)] == [f'Receive 0.000001 TRX from {row["from"]} to {row["to"]}']  # noqa: E501


def test_removed_account_events_go_and_shared_ones_are_decoded_again(manager: TronManager) -> None:
    """Removing an account deletes the events of its own transactions. A transfer it shares
    with a tracked account is decoded again, from a transfer to a receive."""
    shared = tronscan_body('history-internal-only-receipt', 'internal-page0')['data'][0]
    removed, kept = shared['from'], shared['to']
    own = shared | {'hash': 'e' * 64, 'internal_hash': 'e' * 64, 'to': 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t'}  # noqa: E501
    track_tron_accounts(manager.database, [removed, kept])
    with patch.object(manager.tronscan, 'query_feed_page', side_effect=lambda feed, address, *args: [x for x in (shared, own) if address in (x['from'], x['to'])] if feed == 'internal-transaction' else []):  # noqa: E501
        manager.query_transactions([removed, kept], Timestamp((second := shared['timestamp'] // 1000) - 10), Timestamp(second + 10))  # noqa: E501
    assert {(x.tx_ref, x.event_type) for x in _events(manager.database)} == {
        (shared['hash'], HistoryEventType.TRANSFER),
        (own['hash'], HistoryEventType.SPEND),
    }

    with manager.database.user_write() as write_cursor:
        manager.database.remove_single_blockchain_accounts(write_cursor, SupportedBlockchain.TRON, [removed])  # noqa: E501
    assert {x.tx_ref for x in _events(manager.database)} == {shared['hash']}
    assert manager.decoder.decode_transactions() == 1
    assert [(x.event_type, x.location_label, x.address) for x in _events(manager.database)] == [
        (HistoryEventType.RECEIVE, kept, removed),
    ]


def test_transfer_waits_for_the_metadata_of_its_token(manager: TronManager) -> None:
    """A transaction moving a new token whose metadata can not be queried stays pending, so a
    later decode retries it, and an explicit redecode of it fails instead of keeping it"""
    track_tron_accounts(manager.database, [ALICE])
    with manager.database.user_write() as write_cursor:
        add_tron_history(
            write_cursor=write_cursor,
            address=ALICE,
            transactions=[TronTransaction(tx_hash=(tx_hash := b'\x01' * 32), block_number=1, timestamp=TimestampMS(1700000000000), confirmed=True, reverted=False, contract_ret='SUCCESS')],  # noqa: E501
            trc20_transfers=[TronTRC20Transfer(tx_hash=tx_hash, event_index=0, contract_address=TOKEN, from_address=BOB, to_address=ALICE, amount=1500000)],  # noqa: E501
        )

    with patch.object(manager.tronscan, 'query_token_metadata', side_effect=RemoteError('down')):
        assert manager.decoder.decode_transactions() == 0
        with pytest.raises(RemoteError, match='Could not decode 1 of the given TRON transactions'):
            manager.decoder.decode_transactions([tx_hash.hex()])  # type: ignore[list-item]  # a hex hash
    assert _events(manager.database) == []

    with patch.object(manager.tronscan, 'query_token_metadata', return_value={'contract_address': TOKEN, 'decimals': 6, 'name': 'Token', 'symbol': 'TKN'}):  # noqa: E501
        assert manager.decoder.decode_transactions() == 1
    assert [(x.event_type, x.asset.identifier, x.amount) for x in _events(manager.database)] == [
        (HistoryEventType.RECEIVE, tron_address_to_identifier(TOKEN), FVal('1.5')),
    ]


def test_customized_copy_of_an_event_is_its_duplicate(manager: TronManager) -> None:
    """A customized copy of a decoded TRON event is found as its duplicate, as for EVM"""
    row = tronscan_body('history-internal-only-receipt', 'internal-page0')['data'][0]
    _sync(manager, {'internal-transaction': [row]}, row['timestamp'] // 1000, [row['to']])
    (event,) = _events(manager.database)
    with manager.database.user_write() as write_cursor:
        DBHistoryEvents(manager.database).add_history_event(
            write_cursor=write_cursor,
            event=TronEvent(tx_ref=event.tx_ref, sequence_index=1, timestamp=event.timestamp, event_type=event.event_type, event_subtype=event.event_subtype, asset=event.asset, amount=event.amount, location_label=event.location_label, notes=event.notes, address=event.address),  # noqa: E501
            mapping_values={HISTORY_MAPPING_KEY_STATE: HistoryMappingState.CUSTOMIZED},
        )

    assert find_customized_event_duplicate_groups(manager.database) == ([event.group_identifier], [], [event.identifier])  # noqa: E501


@pytest.mark.parametrize('mocked_price_queries', [{'TRX': {'EUR': {1700000000: FVal('0.1'), 1700000100: FVal('0.1')}}}])  # noqa: E501
def test_own_transfer_moves_no_value_but_its_fee(
        manager: TronManager,
        accountant: Accountant,
        messages_aggregator: MessagesAggregator,
        google_service: Any,
) -> None:
    """A transfer between two tracked accounts debits the sender and credits the recipient,
    so the accounts together only lose its fee, and accounting counts no income or disposal
    for it. The fee of a payer that is not tracked is not charged to the recipient."""
    track_tron_accounts(manager.database, [ALICE, BOB])
    with manager.database.user_write() as write_cursor:
        for owner, to, second, amount, fee in ((CAROL, ALICE, 1700000000, 10, 1), (ALICE, BOB, 1700000100, 3, 0.5)):  # noqa: E501
            add_tron_history(
                write_cursor=write_cursor,
                address=ALICE,
                transactions=[TronTransaction(tx_hash=bytes([second % 256]) * 32, block_number=second, timestamp=TimestampMS(second * 1000), confirmed=True, reverted=False, contract_ret='SUCCESS', owner_address=owner, to_address=to, contract_type=1, native_amount=amount * 10**6, fee=int(fee * 10**6))],  # noqa: E501
            )
    assert manager.decoder.decode_transactions() == 2

    process_historical_balances(manager.database, messages_aggregator)
    _, balances = HistoricalBalancesManager(manager.database).get_balances(HistoricalBalancesFilterQuery.make(timestamp=Timestamp(1700000200)), group_by_account=True)  # noqa: E501
    assert {(x.location_label, x.amount) for x in balances} == {(ALICE, FVal('6.5')), (BOB, FVal('3'))}  # type: ignore[union-attr]  # grouped by account  # noqa: E501

    accounting_history_process(accountant, Timestamp(0), Timestamp(1700000200), sorted(_events(manager.database), key=attrgetter('timestamp', 'sequence_index')))  # noqa: E501
    check_pnls_and_csv(accountant, PnlTotals({  # the receipt as income, minus the fee
        AccountingEventType.TRANSACTION_EVENT: PNL(taxable=FVal('0.95'), free=ZERO),
    }), google_service)
