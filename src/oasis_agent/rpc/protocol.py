"""Pure wire validation shared by live transport and retained evidence replay."""

import json
import math
from typing import cast


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("RPC JSON contains duplicate fields")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError("RPC JSON contains non-finite numbers")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("RPC JSON number overflows to non-finite value")
    return number


def strict_json_object(payload: bytes) -> dict[str, object]:
    """Decode JSON bytes, rejecting ambiguity and unparseable nesting."""
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_unique_fields,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("RPC contains invalid or ambiguous JSON") from exc
    if not isinstance(value, dict):
        raise TypeError("RPC envelope must be an object")
    return cast(dict[str, object], value)


def response_envelope(payload: bytes, request_id: int) -> dict[str, object]:
    """Require one unambiguous response for this exact integer request ID.

    Valid error envelopes are returned for the caller's explicit retry/error
    policy. False, null and zero results are valid protocol values.
    """
    response = strict_json_object(payload)
    if (
        type(request_id) is not int
        or request_id <= 0
        or response.get("jsonrpc") != "2.0"
        or type(response.get("id")) is not int
        or response["id"] != request_id
    ):
        raise ValueError("RPC version or request/response ID mismatch")
    if ("result" in response) == ("error" in response):
        raise ValueError("RPC response must contain exactly one result or error")
    if "error" in response:
        error = response["error"]
        if (
            not isinstance(error, dict)
            or type(error.get("code")) is not int
            or not isinstance(error.get("message"), str)
        ):
            raise ValueError("RPC error object is malformed")
    return response
