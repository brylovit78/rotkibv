"""TRON wallet balances (brylovit78/rotkibv#9) from the #2 TronScan corpus.

Every HTTP exchange is mocked from rotkehlchen/tests/data/tronscan/. No VCR and no live requests.
"""
import json
from typing import TYPE_CHECKING, Any, Final
from unittest.mock import patch

import pytest

from rotkehlchen.accounting.structures.balance import Balance
from rotkehlchen.assets.asset import Asset, CryptoAsset
from rotkehlchen.chain.tron.manager import TronManager
from rotkehlchen.chain.tron.utils import get_or_create_tron_token, tron_address_to_identifier
from rotkehlchen.constants import DEFAULT_BALANCE_LABEL
from rotkehlchen.errors.misc import RemoteError
from rotkehlchen.externalapis.tronscan import TRONSCAN_HOLDINGS_LIMIT, Tronscan
from rotkehlchen.fval import FVal
from rotkehlchen.globaldb.upgrades.rotkibv import TRON_USDT_IDENTIFIER
from rotkehlchen.inquirer import Inquirer
from rotkehlchen.tests.utils.mock import MockResponse
from rotkehlchen.tests.utils.tronscan import (
    set_tronscan_key,
    tronscan_body,
    tronscan_case,
    tronscan_exchange,
    tronscan_response,
)
from rotkehlchen.types import Price, SupportedBlockchain, TronAddress

if TYPE_CHECKING:
    from rotkehlchen.chain.aggregator import ChainsAggregator
    from rotkehlchen.db.dbhandler import DBHandler

HISTORY_CASE: Final = 'history-energy-burn-and-failed-call'
HISTORY_ACCOUNT: Final = TronAddress(tronscan_exchange(HISTORY_CASE, 'accountv2')['params']['address'])  # noqa: E501
PRICE: Final = Price(FVal(2))


def _prices(assets: list[Asset]) -> dict[Asset, Price]:
    return dict.fromkeys(assets, PRICE)


def _holdings(rows: list[dict[str, Any]]) -> MockResponse:
    return MockResponse(200, json.dumps({'total': len(rows), 'data': rows, 'code': 200, 'status': '1', 'message': 'request ok'}))  # noqa: E501


def _expected(case_id: str) -> dict[str, Any]:
    return tronscan_case(case_id)['expected']


@pytest.fixture(name='manager')
def fixture_manager(database: DBHandler) -> TronManager:
    set_tronscan_key(database, 'test-tronscan-key')
    return TronManager(tronscan=Tronscan(database=database), database=database)


def test_wallet_balances_from_the_corpus(manager: TronManager) -> None:
    """TRX comes from accountv2 and TRC20 from the holdings, exactly and without metadata
    requests for a known contract. The TRX holdings row and TRC10 rows are not wallet TRC20."""
    expected = _expected(HISTORY_CASE)['balance_snapshot']
    with (
        patch.object(Inquirer, 'find_main_currency_prices', side_effect=_prices),
        patch.object(manager.tronscan.session, 'request', side_effect=[
            tronscan_response(HISTORY_CASE, 'accountv2'),
            tronscan_response(HISTORY_CASE, 'account-tokens'),
        ]) as request,
    ):
        balances = manager.query_balances([HISTORY_ACCOUNT])

    assert request.call_count == 2
    assert {asset.identifier: balance[DEFAULT_BALANCE_LABEL] for asset, balance in balances[HISTORY_ACCOUNT].assets.items()} == {  # noqa: E501
        'TRX': Balance(amount=(trx := FVal(expected['native_trx']['amount'])), value=trx * PRICE),
        TRON_USDT_IDENTIFIER: Balance(amount=(usdt := FVal(expected['trc20'][0]['amount'])), value=usdt * PRICE),  # noqa: E501
    }


def test_inactive_account_is_a_successful_zero_snapshot(manager: TronManager) -> None:
    case = 'balances-inactive-account'
    with (
        patch.object(Inquirer, 'find_main_currency_prices', side_effect=_prices),
        patch.object(manager.tronscan.session, 'request', side_effect=[
            tronscan_response(case, 'accountv2'),
            tronscan_response(case, 'account-tokens'),
        ]),
    ):
        assert manager.query_balances([TronAddress(tronscan_exchange(case, 'accountv2')['params']['address'])]) == {}  # noqa: E501


def test_unknown_token_is_created_from_validated_metadata(manager: TronManager) -> None:
    """An unknown contract gets its own asset from the matching token_trc20 metadata. A fake
    USDT is not the official USDT, and a decimals disagreement with a stored contract fails."""
    fake = (case := 'tokens-metadata-and-fake-usdt', 'fake-usdt-showall1')
    contract = tronscan_body(*fake)['trc20_tokens'][0]['contract_address']
    row = {'tokenId': contract, 'balance': '1500000000000000000', 'tokenDecimal': 18, 'tokenType': 'trc20'}  # noqa: E501
    with (
        patch.object(Inquirer, 'find_main_currency_prices', side_effect=_prices),
        patch.object(manager.tronscan.session, 'request', side_effect=[
            tronscan_response(HISTORY_CASE, 'accountv2'),
            _holdings([row | {'tokenId': '_', 'tokenDecimal': 6}, row]),  # TRX, even as trc20
            tronscan_response(*fake),
        ]),
    ):
        balances = manager.query_balances([HISTORY_ACCOUNT])

    token = CryptoAsset(_expected(case)['fake_usdt']['asset'])
    assert token.identifier == tron_address_to_identifier(TronAddress(contract))
    assert (token.symbol, token.coingecko) == ('USDT', None)
    assert balances[HISTORY_ACCOUNT].assets[token][DEFAULT_BALANCE_LABEL].amount == FVal('1.5')

    usdt_row = row | {'tokenId': tronscan_exchange(case, 'official-usdt')['params']['contract']}
    with (
        patch.object(manager.tronscan.session, 'request', side_effect=[
            tronscan_response(HISTORY_CASE, 'accountv2'),
            _holdings([usdt_row]),  # USDT has 6 decimals, not 18
        ]),
        pytest.raises(RemoteError, match='18 decimals in the holdings instead of 6'),
    ):
        manager.query_balances([HISTORY_ACCOUNT])


@pytest.mark.parametrize(('decimals', 'wrong'), [(18, 6), (18, 18.0), (0, False)])
def test_rejected_metadata_is_not_stored(
        manager: TronManager,
        globaldb,
        decimals: int,
        wrong: Any,
) -> None:
    """Holdings and metadata decimals that disagree, also as a float or boolean equal to the
    holdings integer, fail before the token is stored, so a corrected provider response is
    accepted on the next query"""
    metadata = tronscan_body('tokens-metadata-and-fake-usdt', 'fake-usdt-showall1')
    contract = (token := metadata['trc20_tokens'][0])['contract_address']
    row = {'tokenId': contract, 'balance': '2000000000000000000', 'tokenDecimal': decimals, 'tokenType': 'trc20'}  # noqa: E501
    with (
        patch.object(Inquirer, 'find_main_currency_prices', side_effect=_prices),
        patch.object(manager.tronscan.session, 'request', side_effect=[
            tronscan_response(HISTORY_CASE, 'accountv2'), _holdings([row]), MockResponse(200, json.dumps(metadata | {'trc20_tokens': [token | {'decimals': wrong}]})),  # noqa: E501
            tronscan_response(HISTORY_CASE, 'accountv2'), _holdings([row]), MockResponse(200, json.dumps(metadata | {'trc20_tokens': [token | {'decimals': decimals}]})),  # noqa: E501
        ]),
    ):
        with pytest.raises(RemoteError, match=f'{decimals} decimals in the holdings but {wrong!r} in its metadata'):  # noqa: E501
            manager.query_balances([HISTORY_ACCOUNT])
        with globaldb.conn.read_ctx() as cursor:
            assert cursor.execute('SELECT COUNT(*) FROM tron_tokens WHERE address=?', (contract,)).fetchone() == (0,)  # noqa: E501

        balances = manager.query_balances([HISTORY_ACCOUNT])
    assert balances[HISTORY_ACCOUNT].assets[CryptoAsset(tron_address_to_identifier(contract))][DEFAULT_BALANCE_LABEL].amount == FVal(2 * 10 ** (18 - decimals))  # noqa: E501


def test_concurrently_stored_decimals_win(manager: TronManager) -> None:
    """A contract stored by another query during the metadata request is checked against what
    was stored, not against this query's holdings and metadata"""
    fake = ('tokens-metadata-and-fake-usdt', 'fake-usdt-showall1')
    metadata = tronscan_body(*fake)
    contract = TronAddress(metadata['trc20_tokens'][0]['contract_address'])
    six = metadata | {'trc20_tokens': [metadata['trc20_tokens'][0] | {'decimals': 6}]}

    responses = iter([
        tronscan_response(HISTORY_CASE, 'accountv2'),
        _holdings([{'tokenId': contract, 'balance': '1000000000000000000', 'tokenDecimal': 6, 'tokenType': 'trc20'}]),  # noqa: E501
    ])

    def request(url: str, **kwargs: Any) -> MockResponse:
        if url.endswith('/token_trc20'):  # the other query stores the contract with 18 meanwhile
            get_or_create_tron_token(manager.database, contract, name='USDT', symbol='USDT', decimals=18)  # noqa: E501
            return MockResponse(200, json.dumps(six))
        return next(responses)

    with (
        patch.object(manager.tronscan.session, 'request', side_effect=request),
        pytest.raises(RemoteError, match='6 decimals in the holdings instead of 18'),
    ):
        manager.query_balances([HISTORY_ACCOUNT])


def test_new_spam_is_not_a_balance(manager: TronManager, database: DBHandler) -> None:
    """A token that the shared spam check ignores on discovery is neither priced nor returned,
    as the frontend does not learn about the new ignored asset during the refresh"""
    fake = ('tokens-metadata-and-fake-usdt', 'fake-usdt-showall1')
    metadata = tronscan_body(*fake)
    contract = metadata['trc20_tokens'][0]['contract_address']
    spam = metadata | {'trc20_tokens': [metadata['trc20_tokens'][0] | {'name': 'Claim rewards at https://example.com'}]}
    with (
        patch.object(Inquirer, 'find_main_currency_prices', side_effect=_prices) as prices,
        patch.object(manager.tronscan.session, 'request', side_effect=[
            tronscan_response(HISTORY_CASE, 'accountv2'),
            _holdings([{'tokenId': contract, 'balance': '1', 'tokenDecimal': 18, 'tokenType': 'trc20'}]),  # noqa: E501
            MockResponse(200, json.dumps(spam)),
        ]),
    ):
        balances = manager.query_balances([HISTORY_ACCOUNT])

    token_id = tron_address_to_identifier(contract)
    with database.conn.read_ctx() as cursor:
        assert token_id in database.get_ignored_asset_ids(cursor)
    assert {x.identifier for x in balances[HISTORY_ACCOUNT].assets} == {'TRX'}
    assert [x.identifier for x in prices.call_args.args[0]] == ['TRX']


def test_holdings_end_on_a_short_page(manager: TronManager) -> None:
    """Holdings pages are read until one is short, never by the total, and a list that does
    not end within the verified 10000 rows fails"""
    case = 'pagination-holdings-endpoint'
    address = TronAddress(tronscan_exchange(case, 'page0')['params']['address'])
    with patch.object(manager.tronscan.session, 'request', side_effect=[
        tronscan_response(case, f'page{i}') for i in range(3)
    ]) as request:
        assert len(manager._query_holdings(address)) == _expected(case)['total']
    assert [x.kwargs['params'] for x in request.call_args_list] == [  # the filter listing all
        {'address': address, 'start': start, 'limit': 200, 'hidden': 0, 'show': 0}
        for start in (0, 200, 400)
    ]

    full_page = _holdings([{'tokenId': '_'}] * TRONSCAN_HOLDINGS_LIMIT)
    with (
        patch.object(manager.tronscan.session, 'request', return_value=full_page) as request,
        patch.object(manager.tronscan._rate_limiter, 'acquire'),
        pytest.raises(RemoteError, match='can not be proven complete'),
    ):
        manager._query_holdings(address)
    assert request.call_count == 50  # the verified 10000 rows in pages of 200


@pytest.mark.parametrize('responses', [
    pytest.param([MockResponse(200, json.dumps({'balanceStr': '1.5'}))], id='malformed TRX amount'),  # noqa: E501
    pytest.param([tronscan_response(HISTORY_CASE, 'accountv2'), _holdings([{'tokenId': 'not-an-address', 'balance': '1', 'tokenDecimal': 6, 'tokenType': 'trc20'}])], id='malformed contract'),  # noqa: E501
    pytest.param([tronscan_response(HISTORY_CASE, 'accountv2'), _holdings([{'tokenId': 'TYimXEh5J7PYebVzQQcuckcCr1QSKy1it1', 'balance': '1', 'tokenDecimal': 18, 'tokenType': 'trc20'}]), tronscan_response('tokens-metadata-and-fake-usdt', 'token-trc20-without-contract')], id='metadata of another contract'),  # noqa: E501
    pytest.param([tronscan_response(HISTORY_CASE, 'accountv2'), *[tronscan_response('synthetic-http-500')] * 6], id='holdings page failure'),  # noqa: E501
    pytest.param([tronscan_response('errors-authentication', 'invalid-key-key-required-endpoint')], id='invalid key'),  # noqa: E501
    pytest.param([tronscan_response(HISTORY_CASE, 'accountv2'), _holdings([{'tokenId': 'TYimXEh5J7PYebVzQQcuckcCr1QSKy1it1', 'balance': '1', 'tokenDecimal': 18, 'tokenType': []}])], id='unhashable token type'),  # noqa: E501
])
def test_failures_never_return_a_partial_snapshot(
        manager: TronManager,
        responses: list[MockResponse],
) -> None:
    with (
        patch('rotkehlchen.externalapis.tronscan.cancellable_sleep'),
        patch.object(manager.tronscan.session, 'request', side_effect=responses),
        pytest.raises(RemoteError),
    ):
        manager.query_balances([HISTORY_ACCOUNT])


def test_boolean_decimals_are_not_zero(manager: TronManager) -> None:
    """False equals the 0 decimals of a stored contract, but it is no decimals value"""
    contract = TronAddress(tronscan_body('synthetic-zero-decimals-token', 'token-trc20')['trc20_tokens'][0]['contract_address'])  # noqa: E501
    get_or_create_tron_token(manager.database, contract, name='Zero', symbol='ZDEC', decimals=0)
    with (
        patch.object(manager.tronscan.session, 'request', side_effect=[
            tronscan_response(HISTORY_CASE, 'accountv2'),
            _holdings([{'tokenId': contract, 'balance': '5', 'tokenDecimal': False, 'tokenType': 'trc20'}]),  # noqa: E501
        ]),
        pytest.raises(RemoteError, match='Invalid TRC20 decimals'),
    ):
        manager.query_balances([HISTORY_ACCOUNT])


def test_missing_key_is_a_failure_without_requests(database: DBHandler) -> None:
    manager = TronManager(tronscan=Tronscan(database=database), database=database)
    with (
        patch.object(manager.tronscan.session, 'request') as request,
        pytest.raises(RemoteError, match='needs a TronScan API key'),
    ):
        manager.query_balances([HISTORY_ACCOUNT])
    assert request.call_count == 0


@pytest.mark.parametrize('tron_accounts', [[HISTORY_ACCOUNT]])
@pytest.mark.parametrize('failure', [
    pytest.param([tronscan_response('errors-authentication', 'invalid-key-key-required-endpoint')], id='invalid key'),  # noqa: E501
    pytest.param([
        tronscan_response(HISTORY_CASE, 'accountv2'),
        _holdings([row | {'tokenType': None} if row['tokenType'] == 'trc20' else row for row in tronscan_body(HISTORY_CASE, 'account-tokens')['data']]),  # noqa: E501
    ], id='unknown token type'),
])
def test_failed_query_keeps_the_previous_balances(
        blockchain: ChainsAggregator,
        database: DBHandler,
        failure: list[MockResponse],
) -> None:
    """The aggregator swaps in TRON balances only after a complete snapshot"""
    set_tronscan_key(database, 'test-tronscan-key')
    tronscan = blockchain.tron.tronscan
    with (
        patch.object(Inquirer, 'find_main_currency_prices', side_effect=_prices),
        patch.object(tronscan.session, 'request', side_effect=[
            tronscan_response(HISTORY_CASE, 'accountv2'),
            tronscan_response(HISTORY_CASE, 'account-tokens'),
        ]),
    ):
        blockchain.query_balances(blockchain=SupportedBlockchain.TRON)
    assert (previous := dict(blockchain.balances.tron)) != {}

    with (
        patch.object(tronscan.session, 'request', side_effect=failure),
        pytest.raises(RemoteError),
    ):
        blockchain.query_balances(blockchain=SupportedBlockchain.TRON, ignore_cache=True)
    assert dict(blockchain.balances.tron) == previous
