"""TRON address and transaction hash codecs, free of rotkehlchen.types so that it can use
them"""
import hashlib

from rotkehlchen.utils.base58 import b58decode, b58encode


def _checksum(payload: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]


def canonical_tron_address(value: str) -> str | None:
    """Return the canonical Base58Check form of a TRON address given as Base58Check or as the
    provider hex forms 41 + 20 bytes and 0x + 20 bytes, or None if it is not a TRON address.

    Testnets share the 0x41 version byte, so mainnet is a property of the client, not of the
    address.
    """
    try:
        if len(value) == 42 and value[:2] in ('0x', '41'):
            payload = b'\x41' + bytes.fromhex(value[2:])
        elif len(raw := b58decode(value.encode())) == 25 and _checksum(raw[:21]) == raw[21:]:
            payload = raw[:21]
        else:
            return None
    except ValueError:  # non-hex or non-base58 characters
        return None

    if len(payload) != 21 or payload[0] != 0x41:
        return None

    return b58encode(payload + _checksum(payload)).decode()


def canonical_tron_tx_hash(value: str | bytes) -> str | None:
    """Return the lowercase hex of a TRON transaction hash given as its 32 bytes or as hex,
    optionally 0x prefixed, or None if it is not one"""
    try:
        raw = value if isinstance(value, bytes) else bytes.fromhex(value.removeprefix('0x'))
    except ValueError:
        return None

    return raw.hex() if len(raw) == 32 else None
