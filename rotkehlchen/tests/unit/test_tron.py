"""TRON chain identity, addresses and TRC20 asset identity (brylovit78/rotkibv#7).

Values come from the TronScan contract corpus in rotkehlchen/tests/data/tronscan/.
"""
import json
import shutil
import sqlite3
from enum import UNIQUE, verify
from pathlib import Path
from typing import Any, Final
from unittest.mock import patch

import pytest

from rotkehlchen.assets.asset import CryptoAsset
from rotkehlchen.assets.types import AssetType
from rotkehlchen.assets.utils import token_normalized_value_decimals
from rotkehlchen.chain.tron.utils import (
    deserialize_tron_address,
    get_or_create_tron_token,
    tron_address_to_identifier,
)
from rotkehlchen.db.dbhandler import DBHandler
from rotkehlchen.errors.serialization import DeserializationError
from rotkehlchen.fval import FVal
from rotkehlchen.globaldb.asset_updates.manager import ASSETS_VERSION_KEY, AssetsUpdater
from rotkehlchen.globaldb.handler import GlobalDBHandler
from rotkehlchen.globaldb.upgrades.rotkibv import (
    ROTKIBV_SCHEMA_EXTENSION_KEY,
    TRON_USDT_CONTRACT,
    TRON_USDT_IDENTIFIER,
    USDT_COLLECTION_MAIN_ASSET,
    apply_rotkibv_schema_extension,
)
from rotkehlchen.history.events.structures.base import HistoryBaseEntryType
from rotkehlchen.tests.unit.globaldb.test_asset_updates import get_mock_github_assets_response
from rotkehlchen.types import Location, TokenKind, TronAddress
from rotkehlchen.user_messages import MessagesAggregator

DATA_DIR: Final = Path(__file__).resolve().parent.parent / 'data' / 'tronscan'
PACKAGED_GLOBALDB: Final = Path(__file__).resolve().parents[2] / 'data' / 'global.db'
USDT_HEX: Final = 'a614f803b6fd780986a42c78ec9c7f77e6ded13c'


def _case(case_id: str) -> dict[str, Any]:
    manifest = json.loads((DATA_DIR / 'manifest.json').read_text())
    return next(case for case in manifest['cases'] if case['id'] == case_id)


def _response(case: dict[str, Any], role: str) -> Any:
    exchange = next(x for x in case['exchanges'] if x['role'] == role)
    return json.loads((DATA_DIR / exchange['response']).read_text())


def test_tron_address_forms() -> None:
    for form in (TRON_USDT_CONTRACT, '41' + USDT_HEX, '41' + USDT_HEX.upper(), '0x' + USDT_HEX):
        assert deserialize_tron_address(form) == TRON_USDT_CONTRACT

    for invalid in (
            TRON_USDT_CONTRACT[:5] + TRON_USDT_CONTRACT[5].swapcase() + TRON_USDT_CONTRACT[6:],
            TRON_USDT_CONTRACT[:-1],
            TRON_USDT_CONTRACT + 'a',
            '0' + TRON_USDT_CONTRACT[1:],  # not in the base58 alphabet
            '1BoatSLRHtKNngkdXEeobR76b53LETtpyT',  # valid Base58Check, bitcoin version byte
            '41' + USDT_HEX[:-2],
            '0x' + USDT_HEX[:-1] + 'g',
            '',
            None,
            41,
    ):
        with pytest.raises(DeserializationError):
            deserialize_tron_address(invalid)

    # event logs carry 0x hex while the TRC20 feed carries Base58 for the same transfer
    case = _case('identity-two-transfers-relayer-paid')
    logs = _response(case, 'event-logs')['event_list']
    for row in _response(case, 'block-trc20-page1')['token_transfers']:
        event = next(x for x in logs if x['event_name'] == 'Transfer' and x['result']['value'] == row['quant'])  # noqa: E501
        assert deserialize_tron_address(event['result']['from']) == row['from_address']
        assert deserialize_tron_address(event['result']['to']) == row['to_address']


def test_tron_location_reserved_value() -> None:
    assert Location.TRON.value == 100
    assert list(Location)[-1] is Location.TRON  # deserialization bounds on the last member
    assert Location.deserialize_from_db(Location.TRON.serialize_for_db()) is Location.TRON
    with pytest.raises(DeserializationError):  # an unused value below TRON, not a ValueError
        Location.deserialize_from_db(chr(62 + 64))

    for enum_class in (Location, HistoryBaseEntryType, AssetType, TokenKind):
        verify(UNIQUE)(enum_class)  # an upstream value collision would alias silently


def test_tron_location_reaches_existing_user_dbs(user_data_dir, sql_vm_instructions_cb, globaldb):
    def open_db() -> DBHandler:
        return DBHandler(
            user_data_dir=user_data_dir,
            password='123',
            msg_aggregator=MessagesAggregator(),
            initial_settings=None,
            sql_vm_instructions_cb=sql_vm_instructions_cb,
            resume_from_backup=False,
        )

    tron_row = [(Location.TRON.serialize_for_db(), Location.TRON.value)]
    db = open_db()  # fresh database, created from the schema script
    with db.user_write() as write_cursor:
        assert write_cursor.execute('SELECT * FROM location WHERE seq=100').fetchall() == tron_row
        write_cursor.execute('DELETE FROM location WHERE seq=100')
    db.logout()

    db = open_db()  # an existing database never runs the schema script again
    with db.conn.read_ctx() as cursor:
        assert cursor.execute('SELECT * FROM location WHERE seq=100').fetchall() == tron_row
    db.logout()


def test_packaged_globaldb_is_the_extension_output(tmp_path: Path) -> None:
    """Undo the fork extension on a copy of the packaged DB, as in upstream's file, re-apply
    it and compare, so the packaged DB cannot drift from the extension code."""
    connection = sqlite3.connect(shutil.copy(PACKAGED_GLOBALDB, tmp_path / 'global.db'))
    cursor = connection.cursor()
    ids = ('TRX', TRON_USDT_IDENTIFIER)

    def snapshot() -> list[list[Any]]:
        return [cursor.execute(query, params).fetchall() for query, params in (
            ("SELECT sql FROM sqlite_master WHERE name='tron_tokens'", ()),
            ('SELECT * FROM tron_tokens', ()),
            ('SELECT * FROM assets WHERE identifier IN (?, ?)', ids),
            ('SELECT * FROM common_asset_details WHERE identifier IN (?, ?)', ids),
            ('SELECT * FROM multiasset_mappings WHERE asset IN (?, ?)', ids),
            ('SELECT value FROM settings WHERE name=?', (ROTKIBV_SCHEMA_EXTENSION_KEY,)),
        )]

    packaged = snapshot()
    assert packaged[1] == [(TRON_USDT_IDENTIFIER, TRON_USDT_CONTRACT, 6, None)]
    assert packaged[-1] == [('1',)]
    cursor.execute('DROP TABLE tron_tokens')
    cursor.execute('DELETE FROM multiasset_mappings WHERE asset IN (?, ?)', ids)
    for table in ('common_asset_details', 'assets'):
        cursor.execute(f'DELETE FROM {table} WHERE identifier IN (?, ?)', ids)
    cursor.execute('DELETE FROM settings WHERE name=?', (ROTKIBV_SCHEMA_EXTENSION_KEY,))
    apply_rotkibv_schema_extension(cursor)  # type: ignore[arg-type]  # same cursor API
    assert snapshot() == packaged

    cursor.execute('DELETE FROM assets WHERE identifier=?', (TRON_USDT_IDENTIFIER,))
    apply_rotkibv_schema_extension(cursor)  # type: ignore[arg-type]
    assert cursor.execute(  # applied once: a deleted seed is not restored
        'SELECT COUNT(*) FROM assets WHERE identifier=?', (TRON_USDT_IDENTIFIER,),
    ).fetchone() == (0,)
    connection.close()


def test_tron_token_identities(globaldb, database) -> None:
    usdt = get_or_create_tron_token(database, TronAddress(TRON_USDT_CONTRACT), name='Other', symbol='OTHER', decimals=18)  # noqa: E501
    assert (usdt.identifier, usdt.symbol, usdt.coingecko) == (TRON_USDT_IDENTIFIER, 'USDT', 'tether')  # noqa: E501

    fake = _case('tokens-metadata-and-fake-usdt')['expected']['fake_usdt']
    fake_usdt = get_or_create_tron_token(database, TronAddress(fake['contract']), name='USDT', symbol='USDT', decimals=18)  # noqa: E501
    assert fake_usdt.identifier == fake['asset'] == tron_address_to_identifier(TronAddress(fake['contract']))  # noqa: E501
    assert fake_usdt.asset_type == AssetType.TRON_TOKEN
    assert fake_usdt.coingecko is None
    assert get_or_create_tron_token(database, TronAddress(fake['contract']), name='x', symbol='x', decimals=1) == fake_usdt  # noqa: E501

    native = CryptoAsset('TRX')
    assert (native.asset_type, native.coingecko, native.cryptocompare) == (AssetType.OWN_CHAIN, 'tron', 'TRX')  # noqa: E501
    legacy_btt = CryptoAsset('BTT')  # legacy TRON_TOKEN entries keep their identity untouched
    assert legacy_btt.asset_type == AssetType.TRON_TOKEN
    with globaldb.conn.read_ctx() as cursor:
        assert cursor.execute(
            'SELECT asset FROM multiasset_mappings WHERE collection_id=(SELECT id FROM asset_collections WHERE main_asset=?) AND asset LIKE ?',  # noqa: E501
            (USDT_COLLECTION_MAIN_ASSET, 'tron/%'),
        ).fetchall() == [(TRON_USDT_IDENTIFIER,)]
        assert cursor.execute('SELECT COUNT(*) FROM multiasset_mappings WHERE asset=?', (fake_usdt.identifier,)).fetchone() == (0,)  # noqa: E501
        assert cursor.execute("SELECT COUNT(*) FROM tron_tokens WHERE identifier='BTT'").fetchone() == (0,)  # noqa: E501
    with database.conn.read_ctx() as cursor:
        assert cursor.execute('SELECT COUNT(*) FROM assets WHERE identifier=?', (fake_usdt.identifier,)).fetchone() == (1,)  # noqa: E501

    zero_case = _case('synthetic-zero-decimals-token')
    zero_contract = _response(zero_case, 'token-trc20')['trc20_tokens'][0]['contract_address']
    for invalid in (-1, 256, '6', None, True, 6.0):
        with pytest.raises(DeserializationError):
            get_or_create_tron_token(database, TronAddress(zero_contract), name='Z', symbol='Z', decimals=invalid)  # type: ignore[arg-type]  # noqa: E501
    zero = get_or_create_tron_token(database, TronAddress(zero_contract), name='Zero', symbol='ZDEC', decimals=0)  # noqa: E501
    assert zero.identifier == zero_case['expected']['asset']
    assert token_normalized_value_decimals(int(zero_case['expected']['transfer_raw']), 0) == FVal(zero_case['expected']['transfer_amount'])  # noqa: E501
    largest = _case('balances-many-tokens')['expected']['largest_raw_amount']
    assert token_normalized_value_decimals(int(largest['amount_raw']), largest['decimals']) == FVal(largest['amount'])  # noqa: E501


@pytest.mark.parametrize('use_in_memory_globaldb', [False])
def test_tron_tokens_survive_asset_updates_and_resets(globaldb, database, messages_aggregator):
    def tron_tokens() -> list[tuple]:
        with globaldb.conn.read_ctx() as cursor:
            return cursor.execute('SELECT identifier, address, decimals FROM tron_tokens ORDER BY identifier').fetchall()  # noqa: E501

    fake = _case('tokens-metadata-and-fake-usdt')['expected']['fake_usdt']
    get_or_create_tron_token(database, TronAddress(fake['contract']), name='USDT', symbol='USDT', decimals=18)  # noqa: E501
    expected = [(TRON_USDT_IDENTIFIER, TRON_USDT_CONTRACT, 6), (fake['asset'], fake['contract'], 18)]  # noqa: E501
    assert tron_tokens() == expected

    # an assets update only replaces the tables it lists, so tron_tokens is left as is
    GlobalDBHandler.add_setting_value(ASSETS_VERSION_KEY, 997)
    with patch('requests.get', wraps=get_mock_github_assets_response(True, True, True)):
        AssetsUpdater(globaldb=globaldb, msg_aggregator=messages_aggregator).perform_update(up_to_version=999, conflicts={})  # noqa: E501
    assert tron_tokens() == expected

    assert globaldb.soft_reset_assets_list()[0] is True  # keeps user-added tokens
    assert tron_tokens() == expected
    assert globaldb.hard_reset_assets_list(user_db=database, force=True)[0] is True
    assert tron_tokens() == expected[:1]  # back to the packaged USDT only
    assert CryptoAsset('TRX').asset_type == AssetType.OWN_CHAIN
