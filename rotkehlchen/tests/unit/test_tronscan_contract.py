"""Checks for the TronScan contract fixtures of the planned TRON integration.

The corpus in rotkehlchen/tests/data/tronscan/ records sanitized TronScan responses and the
expected normalized results described in docs/designs/tron-integration.md. These checks need
no network access and no TRON production code. They verify that the fixtures are consistent
and that the contract invariants the integration relies on hold on the recorded evidence.
"""
import hashlib
import json
import re
from collections import Counter
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any, Final

import pytest

from rotkehlchen.utils.base58 import b58decode, b58encode

DATA_DIR: Final = Path(__file__).resolve().parent.parent / 'data' / 'tronscan'
OFFICIAL_USDT: Final = 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t'
OFFICIAL_USDT_HEX: Final = '41a614f803b6fd780986a42c78ec9c7f77e6ded13c'
BASE58_ADDRESS_RE: Final = re.compile(r'(?<![1-9A-HJ-NP-Za-km-z])T[1-9A-HJ-NP-Za-km-z]{33}(?![1-9A-HJ-NP-Za-km-z])')  # noqa: E501
UUID_RE: Final = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')  # noqa: E501
HISTORY_CASES: Final = (
    'history-energy-burn-and-failed-call',
    'history-account-creation-fee',
    'history-multi-page',
    'history-internal-only-receipt',
    'history-zero-holding-token',
)
EVENT_LOGS_PATH: Final = '/api/contracts/smart-contract-triggers-batch'


@cache
def _manifest() -> dict[str, Any]:
    return json.loads((DATA_DIR / 'manifest.json').read_text())


def _case(case_id: str) -> dict[str, Any]:
    return next(case for case in _manifest()['cases'] if case['id'] == case_id)


@cache
def _response(path: str) -> Any:
    file = DATA_DIR / path
    return json.loads(file.read_text()) if file.suffix == '.json' else file.read_text()


def _exchange(case: dict[str, Any], role: str) -> dict[str, Any]:
    return next(exchange for exchange in case['exchanges'] if exchange['role'] == role)


def _body(case: dict[str, Any], role: str) -> Any:
    return _response(_exchange(case, role)['response'])


def _rows(case: dict[str, Any], role_prefix: str, key: str) -> list[dict[str, Any]]:
    rows = []
    for exchange in case['exchanges']:
        if exchange['role'].startswith(role_prefix):
            rows.extend(_response(exchange['response'])[key])
    return rows


def _address_payload(address: str) -> bytes | None:
    """Return the 21-byte payload of a valid TRON Base58Check address, else None."""
    try:
        raw = b58decode(address.encode())
    except ValueError:
        return None
    if len(raw) != 25 or raw[0] != 0x41:
        return None
    if hashlib.sha256(hashlib.sha256(raw[:21]).digest()).digest()[:4] != raw[21:]:
        return None
    return raw[:21]


def _address_from_hex(hex_address: str) -> str:
    """Base58Check form of a 0x-prefixed 20-byte or 41-prefixed 21-byte hex address."""
    payload = bytes.fromhex('41' + hex_address[2:] if hex_address.startswith('0x') else hex_address)  # noqa: E501
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return b58encode(payload + checksum).decode()


def _units(raw: str | int, decimals: int) -> Decimal:
    return Decimal(int(raw)).scaleb(-decimals)


def _trx_movements_from_transfer_feed(case: dict[str, Any]) -> set[tuple[str, str, str, int]]:
    return {
        (row['transactionHash'], row['transferFromAddress'], row['transferToAddress'], int(row['amount']))  # noqa: E501
        for row in _rows(case, 'trx_transfers', 'data')
        if row['tokenName'] == '_' and row['contractRet'] == 'SUCCESS'
    }


def _transfer_events_by_tx(case: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    events: dict[str, list[dict[str, Any]]] = {}
    for exchange in case['exchanges']:
        if exchange['endpoint'] == EVENT_LOGS_PATH:
            for event in _response(exchange['response'])['event_list']:
                if event['event_name'] == 'Transfer':
                    events.setdefault(event['transaction_id'], []).append(event)
    return events


def _event_index_for_feed_row(row: dict[str, Any], events: list[dict[str, Any]]) -> int:
    matches = [
        event['event_index'] for event in events
        if event['contract_address'] == row['contract_address'] and
        _address_from_hex(event['result']['from']) == row['from_address'] and
        _address_from_hex(event['result']['to']) == row['to_address'] and
        event['result']['value'] == row['quant']
    ]
    assert len(matches) == 1, f'feed row of {row["transaction_id"]} matches {matches}'
    return matches[0]


def test_manifest_cases_are_complete() -> None:
    manifest = _manifest()
    ids = [case['id'] for case in manifest['cases']]
    assert len(ids) == len(set(ids))
    referenced = set()
    for case in manifest['cases']:
        assert case['origin'] in manifest['origins']
        for exchange in case['exchanges']:
            assert (DATA_DIR / exchange['response']).is_file()
            referenced.add(exchange['response'])
            assert exchange['endpoint'].startswith('/api/')
            if case['origin'] == 'synthetic':
                assert exchange['auth'] == 'n/a'
                assert exchange['captured_at'] is None
            else:
                assert exchange['auth'] in {'none', 'api_key', 'invalid_dummy_key'}
                assert exchange['captured_at'].startswith('2026-10-02T')
                assert isinstance(exchange['http_status'], int)
    files = {str(path.relative_to(DATA_DIR)) for path in (DATA_DIR / 'responses').rglob('*') if path.is_file()}  # noqa: E501
    assert files == referenced

    origins = {case['id']: case['origin'] for case in manifest['cases']}
    for gate, entry in manifest['gate_status'].items():
        assert set(entry['cases']) == {case['id'] for case in manifest['cases'] if gate in case['gates']}  # noqa: E501
        if entry['status'] == 'verified_live':  # synthetic cases are never live proof
            assert any(origins[case_id] == 'live_capture' for case_id in entry['cases']), gate


def test_fixtures_contain_no_credentials_or_labels() -> None:
    dropped_keys = ('"addressTag"', '"publicTag"', '"contractInfo"', '"normalAddressInfo"', '"contract_map"', '"contractMap"', '"usdValue"', '"email"')  # noqa: E501
    for path in (DATA_DIR / 'responses').rglob('*'):
        if not path.is_file():
            continue
        text = path.read_text()
        assert UUID_RE.search(text) is None, path
        assert 'TRON-PRO-API-KEY' not in text, path
        for key in dropped_keys:
            assert key not in text, (path, key)
    assert UUID_RE.search((DATA_DIR / 'manifest.json').read_text()) is None


def test_all_addresses_are_valid_and_registered() -> None:
    sanitization = _manifest()['sanitization']
    known = (
        set(sanitization['official_allowlist']) |
        set(sanitization['generated_addresses_kept']) |
        set(sanitization['synthetic_address_registry']) |
        set(sanitization['constructed_fixture_addresses'])
    )
    seen = set()
    for path in (DATA_DIR / 'responses').rglob('*.json'):
        for address in BASE58_ADDRESS_RE.findall(path.read_text()):
            assert _address_payload(address) is not None, (path, address)
            seen.add(address)
    assert seen <= known, sorted(seen - known)[:5]
    for case in _manifest()['cases']:  # 0x-hex log addresses convert to registered addresses
        for events in _transfer_events_by_tx(case).values():
            for event in events:
                for side in ('from', 'to'):
                    if 'result' in event:  # the batch case keeps identity fields only
                        assert _address_from_hex(event['result'][side]) in known


def test_address_encoding_round_trips() -> None:
    payload = _address_payload(OFFICIAL_USDT)
    assert payload is not None
    assert payload.hex() == OFFICIAL_USDT_HEX
    assert _address_from_hex(OFFICIAL_USDT_HEX) == OFFICIAL_USDT
    assert _address_from_hex('0x' + OFFICIAL_USDT_HEX[2:]) == OFFICIAL_USDT
    # Base58 is case sensitive: changing the case of one character breaks the checksum
    for position, char in enumerate(OFFICIAL_USDT):
        if char.isalpha() and char.swapcase() in '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz':  # noqa: E501
            assert _address_payload(OFFICIAL_USDT[:position] + char.swapcase() + OFFICIAL_USDT[position + 1:]) is None  # noqa: E501
    assert _address_payload(OFFICIAL_USDT[:-1]) is None
    assert _address_payload(OFFICIAL_USDT[:-1] + 'u') is None  # the bad address of the 400 case
    bad = _exchange(_case('errors-http-200-and-400-bodies'), 'bad-checksum-accountv2')
    assert _address_payload(bad['params']['address']) is None
    # every expected TRC20 asset identifier is the lowercase hex of the contract payload
    identifier_re = re.compile(r'tron/trc20:41[0-9a-f]{40}')
    checked = 0
    for case in _manifest()['cases']:
        for match in re.finditer(r'"asset": "(tron/trc20:[^"]+)", "contract": "([^"]+)"', json.dumps(case['expected'])):  # noqa: E501
            identifier, contract = match.groups()
            contract_payload = _address_payload(contract)
            assert contract_payload is not None
            assert identifier_re.fullmatch(identifier)
            assert identifier == f'tron/trc20:{contract_payload.hex()}'
            checked += 1
    assert checked > 0


def test_paid_fee_is_the_transaction_list_cost_fee() -> None:
    case = _case('fees-detail-versus-list')
    details = {}
    for exchange in case['exchanges']:
        if exchange['role'].startswith('transaction-info'):
            body = _response(exchange['response'])
            details[body['hash']] = body
    for expected in case['expected']['fees']:
        cost = details[expected['tx_hash']]['cost']
        components = sum(int(cost.get(key, 0)) for key in ('energy_fee', 'net_fee', 'multi_sign_fee', 'memoFee', 'account_create_fee'))  # noqa: E501
        extras = sum(int(cost.get(key, 0)) for key in ('multi_sign_fee', 'memoFee', 'account_create_fee'))  # noqa: E501
        assert int(expected['paid_fee_sun']) == expected['list_cost_fee'] == components
        assert expected['detail_cost_fee'] == int(cost['fee']) == extras
        assert _units(expected['paid_fee_sun'], 6) == Decimal(expected['paid_fee_trx'])

    documented = _body(case, 'documented-sample')
    assert _exchange(case, 'documented-sample')['origin'] == 'documented_sample'
    live = details[documented['hash']]
    assert documented['cost']['fee'] == live['cost']['fee'] == 0
    assert documented['cost']['energy_fee'] == live['cost']['energy_fee'] == 13028500
    listed = next(fee for fee in case['expected']['fees'] if fee['tx_hash'] == documented['hash'])
    assert listed['paid_fee_sun'] == '13028500'


@pytest.mark.parametrize('case_id', HISTORY_CASES)
def test_history_balances_reconcile_only_with_the_paid_fee(case_id: str) -> None:
    case = _case(case_id)
    account = next(address for address in case['tracked_accounts'])
    transactions = _rows(case, 'transactions', 'data')
    internal = _rows(case, 'internal', 'data')
    native = _trx_movements_from_transfer_feed(case)
    native_net = sum(amount for _, _, to, amount in native if to == account) - sum(amount for _, sender, _, amount in native if sender == account)  # noqa: E501
    internal_net = sum(
        (1 if row['to'] == account else -1) * int(row['call_value'])
        for row in internal
        if row['token_id'] == '_' and row['result'] == 'SUCCESS' and not row['rejected'] and account in (row['to'], row['from'])  # noqa: E501
    )
    owned = [row for row in transactions if row['ownerAddress'] == account]
    paid_fees = sum(int(row['cost']['fee']) for row in owned)
    energy_and_bandwidth = sum(int(row['cost']['energy_fee']) + int(row['cost']['net_fee']) for row in owned)  # noqa: E501
    call_value = sum(int((row.get('trigger_info') or {}).get('call_value') or 0) for row in owned if row['contractRet'] == 'SUCCESS')  # noqa: E501
    balance = int(_body(case, 'accountv2')['balanceStr'])

    assert native_net + internal_net - call_value - paid_fees == balance
    # transaction-info cost.fee lacks energy and bandwidth fees, so it only reconciles without them
    reconciles_with_detail_fee = native_net + internal_net - call_value - (paid_fees - energy_and_bandwidth) == balance  # noqa: E501
    assert reconciles_with_detail_fee is (energy_and_bandwidth == 0)

    recon = case['expected']['trx_balance_reconciliation']
    assert recon['provider_balance_sun'] == str(balance)
    assert recon['fees_list_cost_fee_sun'] == str(paid_fees)
    assert recon['reconciles_with_detail_cost_fee'] is reconciles_with_detail_fee

    holdings = {row['tokenId']: row for row in _body(case, 'account-tokens')['data']}
    assert int(holdings['_']['balance']) == balance
    token_net: Counter[str] = Counter()
    for row in _rows(case, 'trc20', 'token_transfers'):
        if row['finalResult'] == 'SUCCESS' and row['event_type'] == 'Transfer':
            token_net[row['contract_address']] += int(row['quant']) * (1 if row['to_address'] == account else -1)  # noqa: E501
    for contract, net in token_net.items():
        assert net == int(holdings[contract]['balance'] if contract in holdings else 0)


@pytest.mark.parametrize('case_id', HISTORY_CASES)
def test_history_expected_movements_match_fixtures(case_id: str) -> None:
    case = _case(case_id)
    history = case['expected']['history']
    identities = [move['identity'] for move in history['movements']] + [fee['identity'] for fee in history['fees']]  # noqa: E501
    assert len(identities) == len(set(identities))
    expected_native = {
        (move['tx_hash'], move['from'], move['to'], int(move['amount_raw']))
        for move in history['movements'] if move['kind'] == 'trx'
    }
    # TRX TransferContract rows of the transaction list equal the TRX transfer feed
    listed = {
        (row['hash'], row['contractData']['owner_address'], row['contractData']['to_address'], int(row['contractData']['amount']))  # noqa: E501
        for row in _rows(case, 'transactions', 'data')
        if row['contractType'] == 1 and row['contractRet'] == 'SUCCESS'
    }
    assert expected_native == listed == _trx_movements_from_transfer_feed(case)
    for move in history['movements']:
        assert _units(move['amount_raw'], move['decimals']) == Decimal(move['amount'])
    events = _transfer_events_by_tx(case)
    trc20 = {move['identity'] for move in history['movements'] if move['kind'] == 'trc20'}
    feed = {
        f'trc20:{row["transaction_id"]}:{_event_index_for_feed_row(row, events[row["transaction_id"]])}'  # noqa: E501
        for row in _rows(case, 'trc20', 'token_transfers') if row['finalResult'] == 'SUCCESS'
    }
    assert trc20 == feed
    paying = {row['hash']: row for row in _rows(case, 'transactions', 'data') if row['ownerAddress'] in case['tracked_accounts'] and int(row['cost']['fee']) > 0}  # noqa: E501
    assert {fee['tx_hash'] for fee in history['fees']} == set(paying)
    for fee in history['fees']:
        row = paying[fee['tx_hash']]
        assert fee['amount_raw'] == str(row['cost']['fee'])
        assert fee['event'] == ('spend/fee' if row['contractRet'] == 'SUCCESS' else 'fail/fee')


def test_internal_receipts_are_only_in_the_internal_feed() -> None:
    case = _case('history-internal-only-receipt')
    account = case['tracked_accounts'][0]
    assert _rows(case, 'transactions', 'data') == []
    assert _rows(case, 'trx_transfers', 'data') == []
    (receipt,) = _rows(case, 'internal', 'data')
    assert receipt['to'] == account
    assert receipt['token_id'] == '_'
    assert int(receipt['call_value']) == int(_body(case, 'accountv2')['balanceStr']) == 1
    assert _body(case, 'internal-page0')['total'] == -1  # the internal feed does not count rows


def test_historical_token_without_holdings_is_found_by_the_trc20_feed() -> None:
    case = _case('history-zero-holding-token')
    holdings = {row['tokenId'] for row in _body(case, 'account-tokens')['data']}
    contracts = {row['contract_address'] for row in _rows(case, 'trc20', 'token_transfers')}
    assert OFFICIAL_USDT in contracts
    assert OFFICIAL_USDT not in holdings


def test_holdings_filter_semantics() -> None:
    case = _case('balances-holdings-filter-matrix')

    def kinds(role: str) -> set[str]:
        rows = _body(case, role)['data']
        return {'native' if row['tokenId'] == '_' else row['tokenType'] for row in rows}

    assert kinds('tokens-hidden0-show0') == {'native', 'trc20', 'trc10'}
    assert kinds('second-account-tokens-hidden0-show2') == {'trc20'}
    assert kinds('tokens-hidden0-show1') == {'trc10'}
    assert kinds('tokens-hidden0-show3') == set()
    assert kinds('second-account-tokens-hidden0-show4') == set()
    assert 'trc20' not in kinds('tokens-hidden1-show0')  # hidden=1 hides small balances
    native = next(row for row in _body(case, 'tokens-hidden0-show0')['data'] if row['tokenId'] == '_')  # noqa: E501
    assert native['tokenType'] == 'trc10'  # TRX must be recognized by tokenId, not tokenType

    for case_id in HISTORY_CASES:
        snapshot = _case(case_id)['expected']['balance_snapshot']
        rows = {row['tokenId']: row for row in _body(_case(case_id), 'account-tokens')['data']}
        assert Decimal(snapshot['native_trx']['amount']) == Decimal(rows['_']['quantity'])  # TronScan's own conversion  # noqa: E501
        for token in snapshot['trc20']:
            row = rows[token['contract']]
            assert _units(row['balance'], row['tokenDecimal']) == Decimal(token['amount'])
            assert float(token['amount']) == pytest.approx(row['quantity'])  # float, never exact
        assert snapshot['excluded_trc10_rows'] == sum(1 for row in rows.values() if row['tokenType'] == 'trc10' and row['tokenId'] != '_')  # noqa: E501


def test_inactive_and_many_token_accounts() -> None:
    inactive = _case('balances-inactive-account')
    account = _body(inactive, 'accountv2')
    assert account['activated'] is False
    assert account['balanceStr'] == '0'
    assert [(row['tokenId'], row['balance']) for row in _body(inactive, 'account-tokens')['data']] == [('_', '0')]  # noqa: E501
    assert _body(inactive, 'transactions')['data'] == []

    many = _case('balances-many-tokens')
    body = _body(many, 'account-tokens')
    assert body['total'] == len(body['data']) == many['expected']['total'] < 200
    largest = many['expected']['largest_raw_amount']
    assert len(largest['amount_raw']) > 20
    assert _units(largest['amount_raw'], largest['decimals']) == Decimal(largest['amount'])
    assert {row['level'] for row in body['data'] if row['tokenType'] == 'trc20'} >= {'0', '2', '3', '4'}  # noqa: E501


def test_failed_calls_move_no_tokens() -> None:
    case = _case('status-failed-calls-attempted-transfer')
    for index, expected in enumerate(case['expected']['transactions'], start=1):
        detail = _body(case, f'transaction-info-{index}')
        assert detail['hash'] == expected['tx_hash']
        assert detail['contractRet'] in {'OUT_OF_ENERGY', 'REVERT'}
        assert len(detail['trc20TransferInfo']) == 1  # decoded from the call input only
        assert detail['transfersAllList'] == []
        assert detail['event_count'] == 0
        assert _body(case, f'event-logs-{index}')['event_list'] == []
        assert all(row['transaction_id'] != detail['hash'] for row in _body(case, f'owner-trc20-window-{index}')['token_transfers'])  # noqa: E501
        (listed,) = _body(case, f'transactions-{index}')['data']
        assert expected['paid_fee_sun'] == str(listed['cost']['fee'])
        assert expected['fee_event'] == ('fail/fee' if int(listed['cost']['fee']) > 0 else None)
        assert expected['token_movements'] == []


def test_pending_transaction_confirms_without_changing_values() -> None:
    case = _case('status-pending-then-confirmed')
    before, after = _body(case, 'transaction-info-t0'), _body(case, 'transaction-info-t75s')
    assert before['hash'] == after['hash'] == case['expected']['tx_hash']
    assert before['confirmed'] is False
    assert after['confirmed'] is True
    assert after['confirmations'] > before['confirmations']
    for field in case['expected']['unchanged_fields']:
        assert before[field] == after[field], field


def test_transfer_identity_uses_log_indexes() -> None:
    relayer = _case('identity-two-transfers-relayer-paid')
    detail = _body(relayer, 'transaction-info')
    assert detail['ownerAddress'] not in relayer['tracked_accounts']
    events = _transfer_events_by_tx(relayer)[detail['hash']]
    assert len({event['event_index'] for event in events}) == len(events) == 2
    feed = _rows(relayer, 'block-trc20', 'token_transfers')
    indexes = sorted(_event_index_for_feed_row(row, events) for row in feed)
    assert indexes == [move['event_index'] for move in relayer['expected']['history']['movements']]
    assert relayer['expected']['trx_fee_events'] == []

    identical = _case('synthetic-identical-transfers-same-tx')
    assert identical['origin'] == 'synthetic'
    movements = identical['expected']['movements']
    naive = {(move['tx_hash'], move['from'], move['to'], move['amount_raw']) for move in movements}
    assert len(naive) == 1  # hash+from+to+amount cannot tell the two transfers apart
    feed_rows = _body(identical, 'trc20-feed-account-a')['token_transfers'] + _body(identical, 'trc20-feed-account-b')['token_transfers']  # noqa: E501
    logs = _body(identical, 'event-logs')['event_list']
    seen = {(row['transaction_id'], event['event_index']) for row in feed_rows for event in logs if event['transaction_id'] == row['transaction_id']}  # noqa: E501
    assert len(feed_rows) == 4
    assert len(seen) == len({move['identity'] for move in movements}) == 2

    calls = _case('identity-internal-identical-calls')
    rows = _body(calls, 'internal-block-page')['data']
    assert len({(row['hash'], row['from'], row['to'], row['call_value']) for row in rows}) < len(rows)  # noqa: E501
    assert len({row['internal_hash'] for row in rows}) == len(rows)

    batch = _case('identity-event-log-batch')
    requested = _exchange(batch, 'event-logs-100-hashes')['request_body']['hashList']
    batch_events = _body(batch, 'event-logs-100-hashes')['event_list']
    assert len(requested) == 100
    assert {event['transaction_id'] for event in batch_events} == set(requested)
    assert len({(event['transaction_id'], event['event_index']) for event in batch_events}) == len(batch_events)  # noqa: E501
    mixed = _exchange(batch, 'event-logs-hash-plus-contract')
    returned = {event['transaction_id'] for event in _response(mixed['response'])['event_list']}
    assert mixed['request_body']['hashList'][0] not in returned  # contractAddress overrides hashList  # noqa: E501


def test_offset_cap_and_errors() -> None:
    case = _case('pagination-offset-cap')
    statuses = {exchange['role']: exchange['http_status'] for exchange in case['exchanges']}
    rows = {role: len(_body(case, role)['data']) for role, status in statuses.items() if status == 200}  # noqa: E501
    assert rows == {'start0': 50, 'limit100': 50, 'start9950': 50, 'start9990': 10, 'start10000': 0}  # noqa: E501
    assert statuses['start10001'] == statuses['start12000'] == 400
    assert _body(case, 'start10001') == {'message': 'some parameters are invalid or out of range'}
    assert all(_body(case, role)['total'] == 10000 for role in rows)

    auth = _case('errors-authentication')
    missing = _exchange(auth, 'missing-key-key-required-endpoint')
    assert (missing['http_status'], missing['content_type']) == (401, 'text/html')
    assert '401 Authorization Required' in _response(missing['response'])
    for role in ('invalid-key-key-required-endpoint', 'invalid-key-optional-endpoint'):
        assert _exchange(auth, role)['http_status'] == 401
        assert _body(auth, role) == {'Error': 'ApiKey not exists'}

    bodies = _case('errors-http-200-and-400-bodies')
    assert _exchange(bodies, 'unknown-hash-transaction-info')['http_status'] == 200
    assert _body(bodies, 'unknown-hash-transaction-info') == {}
    assert _body(bodies, 'unknown-hash-event-logs')['event_list'] == []
    assert _exchange(bodies, 'bad-checksum-accountv2')['http_status'] == 400
    assert _exchange(bodies, 'bad-checksum-transactions')['http_status'] == 400


def test_time_window_bounds_and_counts() -> None:
    case = _case('pagination-time-window-semantics')
    reference = sorted(row['timestamp'] for row in _rows(_case(case['reference_case']), 'transactions', 'data'))  # noqa: E501
    day = 86_400_000
    for exchange in case['exchanges']:
        body = _response(exchange['response'])
        params = exchange['params']
        if 'start_timestamp' not in params:  # sort probes: always newest first
            stamps = [row['timestamp'] for row in body['data']]
            assert stamps == sorted(stamps, reverse=True)
            continue
        low = params['start_timestamp'] // 1000 * 1000
        high = params['end_timestamp'] // 1000 * 1000
        inside = [stamp for stamp in reference if low <= stamp <= high]
        assert sorted(row['timestamp'] for row in body['data']) == inside[-50:]
        whole_days = [stamp for stamp in reference if low // day * day <= stamp < (high // day + 1) * day]  # noqa: E501
        assert body['total'] == body['rangeTotal'] == len(whole_days)

    ties = _case('pagination-ties-and-block-filter')
    run_a = [row['hash'] for row in _body(ties, 'window-run-a')['data']]
    assert run_a == [row['hash'] for row in _body(ties, 'window-run-b')['data']]
    assert max(Counter(row['timestamp'] for row in _body(ties, 'window-run-a')['data']).values()) > 1  # noqa: E501
    account = ties['tracked_accounts'][0]
    block_query = _exchange(ties, 'address-plus-block')
    block_rows = _response(block_query['response'])['data']
    assert block_query['params']['address'] == account
    assert {row['block'] for row in block_rows} == {block_query['params']['block']}
    assert any(account not in (row['ownerAddress'], row['toAddress']) for row in block_rows)


def test_token_metadata_identity() -> None:
    case = _case('tokens-metadata-and-fake-usdt')
    official = _body(case, 'official-usdt')['trc20_tokens'][0]
    fake = _body(case, 'fake-usdt-showall0')['trc20_tokens'][0]
    assert official['contract_address'] == OFFICIAL_USDT
    assert (official['symbol'], official['decimals']) == ('USDT', 6)
    assert fake['symbol'] == official['symbol']
    assert fake['contract_address'] != OFFICIAL_USDT
    assert fake['decimals'] == 18
    expected = case['expected']
    assert expected['official_usdt']['asset'] != expected['fake_usdt']['asset']
    assert expected['fake_usdt']['collection'] is None
    unrelated = _exchange(case, 'token-trc20-without-contract')
    assert unrelated['http_status'] == 200
    assert unrelated['params'] == {}
    # HTTP 200 alone proves nothing: the body lists tokens that were never requested
    assert len(_response(unrelated['response'])['trc20_tokens']) > 0


def test_call_value_and_contract_owner_energy() -> None:
    case = _case('fees-call-value-and-origin-energy')
    (row,) = _body(case, 'transactions-router')['data']
    movement = case['expected']['call_value_movement']
    assert int(movement['amount_raw']) == row['trigger_info']['call_value'] == row['contractData']['call_value']  # noqa: E501
    assert _units(movement['amount_raw'], 6) == Decimal(movement['amount'])
    cost = _body(case, 'transaction-info-origin-energy')['cost']
    assert cost['origin_energy_usage'] > 0
    burned = cost['energy_usage_total'] - cost['energy_usage'] - cost['origin_energy_usage']
    assert cost['energy_fee'] == burned * cost['energy_fee_cost']  # the caller pays only the rest
