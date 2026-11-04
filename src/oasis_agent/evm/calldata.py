"""Pure ABI calldata encoding, with no RPC, signing or broadcast dependency."""

from typing import Any

from eth_abi.abi import encode
from eth_hash.auto import keccak


def selector(signature: str) -> bytes:
    return keccak(signature.encode())[:4]


def calldata(signature: str, types: list[str], values: list[Any]) -> bytes:
    return selector(signature) + encode(types, values)
