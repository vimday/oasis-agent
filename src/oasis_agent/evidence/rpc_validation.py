"""Offline validation of retained payloads, without trusting their labels.

Every helper here validates bytes or decoded fields a producer retained earlier.
Nothing fetches, and a malformed, ambiguous or unexpectedly typed field raises
instead of becoming a permissive default.
"""

import json
import re
from typing import cast

from ..rpc.transport import RpcExchange


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("RPC evidence contains duplicate JSON fields")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError("RPC evidence contains non-finite JSON numbers")


def _object(payload: bytes) -> dict[str, object]:
    try:
        value = json.loads(
            payload, object_pairs_hook=_unique_fields, parse_constant=_reject_constant
        )
    except (ValueError, UnicodeError) as exc:
        raise ValueError("RPC evidence contains invalid or ambiguous JSON") from exc
    if not isinstance(value, dict):
        raise TypeError("RPC evidence envelope must be an object")
    return cast(dict[str, object], value)


def strict_json_object(payload: bytes) -> dict[str, object]:
    """Decode one hostile JSON object, rejecting duplicate/non-finite fields."""
    return _object(payload)


def strict_object(value: object, name: str) -> dict[str, object]:
    """Require one JSON object; a retained payload field is never duck-typed."""
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return cast(dict[str, object], value)


def strict_list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array")
    return value


def strict_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a JSON string")
    return value


def strict_integer(value: object, name: str) -> int:
    """A JSON integer: never a bool, a float or a numeric string."""
    if type(value) is not int:
        raise ValueError(f"{name} must be a JSON integer")
    return value


def strict_boolean(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a JSON boolean")
    return value


def strict_digest(value: object, name: str) -> str:
    """One canonical, unprefixed, lowercase SHA-256 hex digest."""
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def rpc_quantity(value: object) -> int:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)", value) is None
    ):
        raise ValueError("RPC evidence requires a canonical hex quantity")
    return int(value, 16)


def decode_exchange(exchange: RpcExchange) -> tuple[list[object], dict[str, object]]:
    """Validate a request/response pair; callers decide how to handle valid errors."""
    request = _object(exchange.request_payload)
    response = _object(exchange.response_payload)
    for value in (exchange.request_id, exchange.attempt):
        if type(value) is not int or value <= 0:
            raise ValueError("RPC evidence request ID and attempt must be positive integers")
    for envelope in (request, response):
        if (
            envelope.get("jsonrpc") != "2.0"
            or type(envelope.get("id")) is not int
            or envelope["id"] != exchange.request_id
        ):
            raise ValueError("RPC evidence version or request/response ID mismatch")
    if request.get("method") != exchange.method:
        raise ValueError("RPC evidence method label does not match request")
    params = request.get("params")
    if not isinstance(params, list):
        raise TypeError("RPC evidence requires positional parameters")
    if ("result" in response) == ("error" in response):
        raise ValueError("RPC evidence must contain exactly one result or error")
    if "error" in response:
        error = response["error"]
        if (
            not isinstance(error, dict)
            or type(error.get("code")) is not int
            or not isinstance(error.get("message"), str)
        ):
            raise ValueError("RPC evidence error object is malformed")
    return params, response


def restore_exchanges(request_payload: bytes, response_payload: bytes) -> tuple[RpcExchange, ...]:
    """Restore exact captured bytes from the persisted paired envelope format."""
    request = _object(request_payload)
    response = _object(response_payload)
    for envelope in (request, response):
        if (
            set(envelope) != {"encoding", "exchanges"}
            or envelope["encoding"] != "hex_exact_bytes"
            or not isinstance(envelope["exchanges"], list)
        ):
            raise ValueError("invalid retained RPC envelope")
    requests, responses = request["exchanges"], response["exchanges"]
    assert isinstance(requests, list) and isinstance(responses, list)
    if not requests or len(requests) != len(responses):
        raise ValueError("retained RPC request/response coverage mismatch")
    result = []
    for req, res in zip(requests, responses, strict=True):
        for item in (req, res):
            if not isinstance(item, dict) or set(item) != {
                "method",
                "request_id",
                "attempt",
                "payload",
            }:
                raise ValueError("invalid retained RPC exchange")
            if (
                not isinstance(item["payload"], str)
                or re.fullmatch(r"(?:[0-9a-f]{2})*", item["payload"]) is None
            ):
                raise ValueError("retained RPC payload must be exact hex bytes")
            if (
                not isinstance(item["method"], str)
                or type(item["request_id"]) is not int
                or item["request_id"] <= 0
                or type(item["attempt"]) is not int
                or item["attempt"] <= 0
            ):
                raise ValueError("invalid retained RPC exchange metadata")
        if any(req[name] != res[name] for name in ("method", "request_id", "attempt")):
            raise ValueError("retained RPC exchange metadata mismatch")
        exchange = RpcExchange(
            req["method"],
            req["request_id"],
            bytes.fromhex(req["payload"]),
            bytes.fromhex(res["payload"]),
            req["attempt"],
        )
        decode_exchange(exchange)
        result.append(exchange)
    return tuple(result)


__all__ = [
    "decode_exchange",
    "restore_exchanges",
    "rpc_quantity",
    "strict_boolean",
    "strict_digest",
    "strict_integer",
    "strict_json_object",
    "strict_list",
    "strict_object",
    "strict_string",
]
