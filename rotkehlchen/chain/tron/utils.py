"""TRON address and TRC20 asset identity, see docs/designs/tron-integration.md sections 4-5"""
import hashlib
from typing import TYPE_CHECKING, Any

from rotkehlchen.assets.asset import CryptoAsset
from rotkehlchen.assets.types import AssetType
from rotkehlchen.assets.utils import check_if_spam_token
from rotkehlchen.errors.serialization import DeserializationError
from rotkehlchen.globaldb.handler import GlobalDBHandler
from rotkehlchen.types import SPAM_PROTOCOL, TronAddress
from rotkehlchen.utils.base58 import b58decode, b58encode

if TYPE_CHECKING:
    from rotkehlchen.db.dbhandler import DBHandler


def _checksum(payload: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]


def deserialize_tron_address(value: Any) -> TronAddress:
    """Return the canonical Base58Check form of a TRON address.

    Accepts case-sensitive Base58Check and the provider hex forms 41 + 20 bytes and
    0x + 20 bytes. Testnets share the 0x41 version byte, so mainnet is a property of the
    client, not of the address.

    May raise DeserializationError if the value is not a valid TRON address.
    """
    if not isinstance(value, str):
        raise DeserializationError(f'Invalid TRON address {value!r}')

    try:
        if len(value) == 42 and value[:2] in ('0x', '41'):
            payload = b'\x41' + bytes.fromhex(value[2:])
        elif len(raw := b58decode(value.encode())) == 25 and _checksum(raw[:21]) == raw[21:]:
            payload = raw[:21]
        else:
            payload = b''
    except ValueError:  # non-hex or non-base58 characters
        payload = b''

    if len(payload) != 21 or payload[0] != 0x41:
        raise DeserializationError(f'Invalid TRON address {value}')

    return TronAddress(b58encode(payload + _checksum(payload)).decode())


def tron_address_to_identifier(address: TronAddress) -> str:
    """Asset identifier of a TRC20 contract: lowercase hex of the 21 byte address payload.

    Lowercase hex cannot collide in the case-insensitive identifier columns, while the
    case-sensitive Base58 contract is stored in tron_tokens.address.
    """
    return f'tron/trc20:{b58decode(address.encode())[:21].hex()}'


def get_or_create_tron_token(
        userdb: DBHandler,
        contract: TronAddress,
        name: str,
        symbol: str,
        decimals: int,
) -> CryptoAsset:
    """Return the asset of a TRC20 contract, creating it from validated metadata.

    An existing contract mapping, such as the seeded USDT or a verified legacy asset, always
    wins over the derived identifier, so a contract never gets a second asset. A new token
    gets the shared spam check, and spam is marked and ignored as for EVM and Solana tokens.

    May raise DeserializationError if the contract is not a valid TRON address or the
    decimals are not an integer in [0, 255].
    """
    contract = deserialize_tron_address(contract)  # TronAddress is a NewType, so recheck it
    with userdb.get_or_create_token_lock:
        with GlobalDBHandler().conn.read_ctx() as cursor:
            if (row := cursor.execute(
                'SELECT identifier FROM tron_tokens WHERE address=?', (contract,),
            ).fetchone()) is not None:
                return CryptoAsset(row[0])

        if type(decimals) is not int or not 0 <= decimals <= 255:  # bool is also an int
            raise DeserializationError(f'Invalid decimals {decimals!r} for TRC20 {contract}')

        identifier = tron_address_to_identifier(contract)
        is_spam = check_if_spam_token(symbol=symbol, name=name)
        with GlobalDBHandler().conn.write_ctx() as write_cursor:
            write_cursor.execute(
                'INSERT INTO assets(identifier, name, type) VALUES(?, ?, ?)',
                (identifier, name, AssetType.TRON_TOKEN.serialize_for_db()),
            )
            write_cursor.execute(
                'INSERT INTO common_asset_details(identifier, symbol) VALUES(?, ?)',
                (identifier, symbol),
            )
            write_cursor.execute(
                'INSERT INTO tron_tokens(identifier, address, decimals, protocol) VALUES(?, ?, ?, ?)',  # noqa: E501
                (identifier, contract, decimals, SPAM_PROTOCOL if is_spam else None),
            )
        token = CryptoAsset(identifier)
        with userdb.user_write() as write_cursor:
            userdb.add_asset_identifiers(write_cursor, [identifier])
            if is_spam:
                userdb.add_to_ignored_assets(write_cursor=write_cursor, asset=token)

    return token
