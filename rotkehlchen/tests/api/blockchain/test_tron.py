"""TRON accounts through the shared accounts API (brylovit78/rotkibv#9). No request reaches
TronScan: saving an account needs no key and queries no balance."""
from http import HTTPStatus
from typing import TYPE_CHECKING, Final
from unittest.mock import patch

import pytest
import requests

from rotkehlchen.tests.utils.api import (
    api_url_for,
    assert_error_response,
    assert_proper_sync_response_with_result,
)
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
