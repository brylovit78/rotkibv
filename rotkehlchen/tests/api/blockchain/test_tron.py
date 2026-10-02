"""TRON accounts (brylovit78/rotkibv#9) and history (#10) through the shared API. No request
reaches TronScan: saving an account needs no key and queries no balance."""
from http import HTTPStatus
from threading import Event, Thread
from typing import TYPE_CHECKING, Final
from unittest.mock import patch

import pytest
import requests

from rotkehlchen.tests.utils.api import (
    api_url_for,
    assert_error_response,
    assert_proper_sync_response_with_result,
)
from rotkehlchen.tests.utils.tronscan import set_tronscan_key, tronscan_body
from rotkehlchen.types import SupportedBlockchain

if TYPE_CHECKING:
    from rotkehlchen.api.server import APIServer

ACCOUNT: Final = 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t'
ACCOUNT_HEX: Final = 'a614f803b6fd780986a42c78ec9c7f77e6ded13c'  # the 20 byte body
OTHER_ACCOUNT: Final = 'TXM3pBpWRemyMFtpRsXCiXSgPJqmuZG4c4'


@pytest.mark.parametrize('number_of_eth_accounts', [0])
def test_tron_accounts_are_stored_canonical(rotkehlchen_api_server: APIServer) -> None:
    url = api_url_for(
        rotkehlchen_api_server,
        'blockchainsaccountsresource',
        blockchain=SupportedBlockchain.TRON.serialize(),
    )
    assert_error_response(
        response=requests.put(url, json={'accounts': [{'address': ACCOUNT[:-1] + 'u'}]}),
        contained_in_msg=f'Given value {ACCOUNT[:-1]}u is not a valid TRON address',
        status_code=HTTPStatus.BAD_REQUEST,
    )
    assert_error_response(  # the Base58 and hex forms of an address are one account
        response=requests.put(url, json={'accounts': [{'address': ACCOUNT}, {'address': '0x' + ACCOUNT_HEX}]}),  # noqa: E501
        contained_in_msg=f'Address {ACCOUNT} appears multiple times',
        status_code=HTTPStatus.BAD_REQUEST,
    )

    assert assert_proper_sync_response_with_result(requests.put(url, json={'accounts': [
        {'address': '41' + ACCOUNT_HEX, 'label': 'main'},
        {'address': OTHER_ACCOUNT},
    ]})) == [ACCOUNT, OTHER_ACCOUNT]
    rotki = rotkehlchen_api_server.rest_api.rotkehlchen
    with rotki.data.db.conn.read_ctx() as cursor:
        assert rotki.data.db.get_blockchain_accounts(cursor).tron == (ACCOUNT, OTHER_ACCOUNT)
    assert rotki.chains_aggregator.accounts.tron == (ACCOUNT, OTHER_ACCOUNT)
    assert {
        x['address']: x['label']
        for x in assert_proper_sync_response_with_result(requests.get(url))
    } == {ACCOUNT: 'main', OTHER_ACCOUNT: None}

    assert_proper_sync_response_with_result(requests.delete(url, json={'accounts': ['0x' + ACCOUNT_HEX]}))  # noqa: E501
    assert rotki.chains_aggregator.accounts.get(SupportedBlockchain.TRON) == (OTHER_ACCOUNT,)
    with rotki.data.db.user_write() as write_cursor:  # a stored hex form is not a valid account
        write_cursor.execute(
            'INSERT INTO blockchain_accounts(blockchain, account) VALUES(?, ?)',
            (SupportedBlockchain.TRON.value, '41' + ACCOUNT_HEX),
        )
    with rotki.data.db.conn.read_ctx() as cursor:
        assert rotki.data.db.get_blockchain_accounts(cursor).tron == (OTHER_ACCOUNT,)

    balances_url = api_url_for(rotkehlchen_api_server, 'named_blockchain_balances_resource', blockchain='TRON')  # noqa: E501
    assert_error_response(  # balance queries take the stored, canonical form only
        response=requests.get(balances_url, params={'addresses': '41' + ACCOUNT_HEX}),
        contained_in_msg='is not a TRON address',
        status_code=HTTPStatus.BAD_REQUEST,
    )
    with patch.object(rotki.tronscan.session, 'request') as request:
        assert_error_response(  # without a key the query fails and is not a zero balance
            response=requests.get(balances_url, params={'addresses': OTHER_ACCOUNT}),
            contained_in_msg='Querying TRON balances needs a TronScan API key',
            status_code=HTTPStatus.BAD_GATEWAY,
        )
    assert request.call_count == 0


@pytest.mark.parametrize('number_of_eth_accounts', [0])
def test_tron_transactions_refresh_and_deletion(rotkehlchen_api_server: APIServer) -> None:
    """TRON history refreshes through the shared transactions endpoint, reports its latest
    timestamps, and is deleted one transaction at a time or all at once"""
    row = tronscan_body('history-internal-only-receipt', 'internal-page0')['data'][0]
    account, second = row['to'], row['timestamp'] // 1000
    rotki = rotkehlchen_api_server.rest_api.rotkehlchen
    url = api_url_for(rotkehlchen_api_server, 'blockchaintransactionsresource')
    assert_proper_sync_response_with_result(requests.put(
        api_url_for(rotkehlchen_api_server, 'blockchainsaccountsresource', blockchain='TRON'),
        json={'accounts': [{'address': account}]},
    ))
    assert_error_response(
        response=requests.post(url, json={'accounts': [{'address': '0x' + ACCOUNT_HEX, 'blockchain': 'tron'}]}),  # noqa: E501
        contained_in_msg=f'The address 0x{ACCOUNT_HEX} is not a valid',
    )
    with patch.object(rotki.tronscan.session, 'request') as request:
        assert_error_response(  # without a key the history query fails as a whole
            response=requests.post(url, json={'accounts': [{'address': account, 'blockchain': 'tron'}]}),  # noqa: E501
            contained_in_msg='Querying TRON history needs a TronScan API key',
            status_code=HTTPStatus.BAD_GATEWAY,
        )
    assert request.call_count == 0

    set_tronscan_key(rotki.data.db, 'test-tronscan-key')
    with patch.object(rotki.tronscan, 'query_feed_page', side_effect=lambda feed, address, from_ts, to_ts, start: [row] if feed == 'internal-transaction' and from_ts <= second <= to_ts else []):  # noqa: E501
        assert_proper_sync_response_with_result(requests.post(url, json={'accounts': [{'address': account, 'blockchain': 'tron'}]}))  # noqa: E501
    assert assert_proper_sync_response_with_result(requests.get(
        api_url_for(rotkehlchen_api_server, 'latestblockchaintransactiontimestampsresource'),
    ))['tron'] == {'latest_timestamp': second, 'addresses': {account: second}}

    assert_error_response(
        response=requests.delete(url, json={'chain': 'tron', 'tx_ref': row['hash'][:-2]}),
        contained_in_msg=f'Invalid TRON transaction hash {row["hash"][:-2]}',
    )
    with rotki.data.db.conn.read_ctx() as cursor:
        assert cursor.execute('SELECT COUNT(*) FROM used_query_ranges WHERE name LIKE ?', (f'TRON%{account}',)).fetchone() == (3,)  # noqa: E501
    assert_proper_sync_response_with_result(requests.delete(url, json={'chain': 'tron', 'tx_ref': row['hash']}))  # noqa: E501
    with rotki.data.db.conn.read_ctx() as cursor:
        assert cursor.execute('SELECT COUNT(*) FROM tron_transactions').fetchone() == (0,)
        assert cursor.execute('SELECT COUNT(*) FROM used_query_ranges WHERE name LIKE ?', (f'TRON%{account}',)).fetchone() == (3,)  # noqa: E501

    assert_proper_sync_response_with_result(requests.delete(url, json={'chain': 'tron'}))
    with rotki.data.db.conn.read_ctx() as cursor:  # a purge also forgets what was queried
        assert cursor.execute('SELECT COUNT(*) FROM used_query_ranges WHERE name LIKE ?', (f'TRON%{account}',)).fetchone() == (0,)  # noqa: E501


@pytest.mark.parametrize('number_of_eth_accounts', [0])
def test_tron_account_removal_waits_for_its_sync(rotkehlchen_api_server: APIServer) -> None:
    """A removal during a paused sync of the account waits for it, so the sync can not write
    the history and ranges of a removed account back"""
    rotki = rotkehlchen_api_server.rest_api.rotkehlchen
    assert_proper_sync_response_with_result(requests.put(
        api_url_for(rotkehlchen_api_server, 'blockchainsaccountsresource', blockchain='TRON'),
        json={'accounts': [{'address': ACCOUNT}]},
    ))
    set_tronscan_key(rotki.data.db, 'test-tronscan-key')
    row = tronscan_body('history-internal-only-receipt', 'internal-page0')['data'][0] | {'to': ACCOUNT}  # noqa: E501
    paused, resume = Event(), Event()

    def page(feed: str, address: str, from_ts: int, to_ts: int, start: int) -> list[dict]:
        if feed != 'internal-transaction':
            return []
        paused.set()
        assert resume.wait(10)
        return [row]

    with patch.object(rotki.tronscan, 'query_feed_page', side_effect=page):
        (sync := Thread(target=rotki.chains_aggregator.tron.query_transactions, args=([ACCOUNT], 0, row['timestamp'] // 1000))).start()  # noqa: E501
        assert paused.wait(10)
        (removal := Thread(target=rotki.remove_single_blockchain_accounts, args=(SupportedBlockchain.TRON, [ACCOUNT]))).start()  # noqa: E501
        removal.join(timeout=0.5)
        assert removal.is_alive()  # waits for the sync, which holds the account

        resume.set()
        sync.join(timeout=10)
        removal.join(timeout=10)

    assert not removal.is_alive()
    assert rotki.chains_aggregator.accounts.tron == ()
    with rotki.data.db.conn.read_ctx() as cursor:
        assert cursor.execute('SELECT COUNT(*) FROM tron_transactions').fetchone() == (0,)
        assert cursor.execute("SELECT COUNT(*) FROM used_query_ranges WHERE name LIKE 'TRON%'").fetchone() == (0,)  # noqa: E501
