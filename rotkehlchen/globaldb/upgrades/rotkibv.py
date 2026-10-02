"""Fork-only global DB additions, applied without bumping the upstream schema version.

Upstream asset updates only apply to the upstream schema version, and upstream already uses
the next version numbers, so these steps are tracked by their own setting instead.
See docs/designs/tron-integration.md section 6.
"""
from typing import TYPE_CHECKING, Final

from rotkehlchen.assets.types import AssetType
from rotkehlchen.globaldb.schema import DB_CREATE_TRON_TOKENS
from rotkehlchen.globaldb.utils import globaldb_get_setting_value

if TYPE_CHECKING:
    from rotkehlchen.db.drivers.sqlite import DBCursor

ROTKIBV_SCHEMA_EXTENSION_KEY: Final = 'rotkibv_schema_extension'
ROTKIBV_SCHEMA_EXTENSION_VERSION: Final = 1
ROTKIBV_MIN_GLOBALDB_VERSION: Final = 17  # the upstream schema the extension SQL targets
TRON_USDT_CONTRACT: Final = 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t'
TRON_USDT_IDENTIFIER: Final = 'tron/trc20:41a614f803b6fd780986a42c78ec9c7f77e6ded13c'
TRX_COLLECTION_MAIN_ASSET: Final = 'eip155:1/erc20:0x50327c6c5a14DCaDE707ABad2E27eB517df87AB5'
USDT_COLLECTION_MAIN_ASSET: Final = 'eip155:1/erc20:0xdAC17F958D2ee523a2206206994597C13D831ec7'


def apply_rotkibv_schema_extension(write_cursor: DBCursor) -> None:
    """Create the TRON token table and seed native TRX and the official USDT TRC20 once.

    Runs after upstream upgrades and before the schema sanity check. Seeds are applied only
    once so that assets a user later deletes or edits are not restored on the next start.
    """
    if (
            globaldb_get_setting_value(write_cursor, 'version', 0) < ROTKIBV_MIN_GLOBALDB_VERSION or  # noqa: E501
            globaldb_get_setting_value(write_cursor, ROTKIBV_SCHEMA_EXTENSION_KEY, 0) >= ROTKIBV_SCHEMA_EXTENSION_VERSION  # noqa: E501
    ):
        return

    write_cursor.execute(DB_CREATE_TRON_TOKENS)
    # OR IGNORE keeps a native TRX asset in case a future upstream assets update adds one
    write_cursor.execute(
        'INSERT OR IGNORE INTO assets(identifier, name, type) VALUES(?, ?, ?)',
        ('TRX', 'TRON', AssetType.OWN_CHAIN.serialize_for_db()),
    )
    write_cursor.execute(
        'INSERT OR IGNORE INTO common_asset_details(identifier, symbol, coingecko, '
        'cryptocompare, forked, started, swapped_for) VALUES(?, ?, ?, ?, ?, ?, ?)',
        ('TRX', 'TRX', 'tron', 'TRX', None, 1529891460, None),  # started: TRON block 0
    )
    write_cursor.execute(
        'INSERT INTO assets(identifier, name, type) VALUES(?, ?, ?)',
        (TRON_USDT_IDENTIFIER, 'Tether USD', AssetType.TRON_TOKEN.serialize_for_db()),
    )
    write_cursor.execute(
        'INSERT INTO common_asset_details(identifier, symbol, coingecko, cryptocompare, '
        'forked, started, swapped_for) VALUES(?, ?, ?, ?, ?, ?, ?)',
        (TRON_USDT_IDENTIFIER, 'USDT', 'tether', 'USDT', None, 1555400628, None),
    )
    write_cursor.execute(
        'INSERT INTO tron_tokens(identifier, address, decimals, protocol) VALUES(?, ?, ?, ?)',
        (TRON_USDT_IDENTIFIER, TRON_USDT_CONTRACT, 6, None),
    )
    for asset, main_asset in (
            ('TRX', TRX_COLLECTION_MAIN_ASSET),
            (TRON_USDT_IDENTIFIER, USDT_COLLECTION_MAIN_ASSET),
    ):  # collection membership prices them through the collection's main asset
        write_cursor.execute(
            'INSERT OR IGNORE INTO multiasset_mappings(collection_id, asset) '
            'SELECT id, ? FROM asset_collections WHERE main_asset=?',
            (asset, main_asset),
        )
    write_cursor.execute(
        'INSERT OR REPLACE INTO settings(name, value) VALUES(?, ?)',
        (ROTKIBV_SCHEMA_EXTENSION_KEY, str(ROTKIBV_SCHEMA_EXTENSION_VERSION)),
    )
