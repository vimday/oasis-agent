"""Narrow helpers for exact, bounded RPC capture and offline replay."""

import re
from collections.abc import Sequence

from eth_hash.auto import keccak

from ..models.chain import BlockSnapshot
from ..models.common import BlockRef, normalize_address, normalize_hash32, normalize_hex
from ..rpc.transport import JsonRpcTransport, RpcExchange
from .rpc_capture import BoundedExchangeRecorder
from .rpc_validation import decode_exchange, rpc_quantity

type RecordedSession = tuple[JsonRpcTransport, BoundedExchangeRecorder, str]


def validate_session_pair(primary: RecordedSession, secondary: RecordedSession) -> None:
    if primary[0] is secondary[0] or primary[1] is secondary[1] or primary[2] == secondary[2]:
        raise ValueError("distinct fresh provider sessions required")
    for _, recorder, source in (primary, secondary):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", source) or recorder.exchanges:
            raise ValueError("distinct fresh provider sessions required")


def wire_envelope(exchanges: Sequence[RpcExchange], *, response: bool) -> dict[str, object]:
    return {
        "encoding": "hex_exact_bytes",
        "exchanges": [
            {
                "method": item.method,
                "request_id": item.request_id,
                "attempt": item.attempt,
                "payload": (item.response_payload if response else item.request_payload).hex(),
            }
            for item in exchanges
        ],
    }


def successful_results(
    exchanges: Sequence[RpcExchange], expected: Sequence[tuple[str, list[object]]]
) -> tuple[object, ...]:
    if len(exchanges) != len(expected):
        raise ValueError("exact capture is incomplete or contains extra calls")
    seen: set[int] = set()
    results: list[object] = []
    for exchange, (method, params) in zip(exchanges, expected, strict=True):
        actual_params, response = decode_exchange(exchange)
        if (
            exchange.request_id in seen
            or exchange.method != method
            or actual_params != params
            or "error" in response
        ):
            raise ValueError("exact capture request/response mismatch")
        seen.add(exchange.request_id)
        results.append(response["result"])
    return tuple(results)


def _required_hash(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a hash string")
    return normalize_hash32(value, name)


def decode_header(value: object, *, chain_id: int) -> BlockSnapshot:
    if not isinstance(value, dict):
        raise ValueError("exact capture header must be an object")
    return BlockSnapshot(
        BlockRef(
            chain_id,
            rpc_quantity(value.get("number")),
            _required_hash(value.get("hash"), "block hash"),
            rpc_quantity(value.get("timestamp")),
        ),
        _required_hash(value.get("parentHash"), "parent hash"),
    )


def require_code_hash(value: object, expected_hash: str) -> str:
    if not isinstance(value, str):
        raise ValueError("runtime result must be hex bytes")
    normalized = normalize_hex(value)
    raw = bytes.fromhex(normalized[2:])
    if not raw or "0x" + keccak(raw).hex() != normalize_hash32(expected_hash):
        raise ValueError("runtime hash mismatch")
    return normalized


def decode_address_word(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("address getter returned non-hex data")
    normalized = normalize_hex(value)
    if len(normalized) != 66 or normalized[2:26] != "0" * 24:
        raise ValueError("address getter returned noncanonical data")
    return normalize_address("0x" + normalized[-40:])


__all__ = [
    "RecordedSession",
    "decode_address_word",
    "decode_header",
    "require_code_hash",
    "successful_results",
    "validate_session_pair",
    "wire_envelope",
]
