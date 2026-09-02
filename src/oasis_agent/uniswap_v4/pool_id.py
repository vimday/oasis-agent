from __future__ import annotations

from eth_hash.auto import keccak

from ..models.v4 import PoolKey


def _word_address(address: str) -> bytes:
    value = bytes.fromhex(address[2:])
    return b"\x00" * 12 + value


def _word_uint(value: int) -> bytes:
    return value.to_bytes(32, "big", signed=False)


def _word_int(value: int) -> bytes:
    return value.to_bytes(32, "big", signed=True)


def pool_id_from_key(pool_key: PoolKey) -> str:
    encoded = b"".join(
        (
            _word_address(pool_key.currency0),
            _word_address(pool_key.currency1),
            _word_uint(pool_key.fee),
            _word_int(pool_key.tick_spacing),
            _word_address(pool_key.hooks),
        )
    )
    return "0x" + keccak(encoded).hex()
