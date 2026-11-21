"""Bounded keyless HTTPS & WebSocket frame client for Equation News (`eqn.news`).

All network access uses pinned ``https://`` origins (`eqn.news` and `api.eqn.news`),
``follow_redirects=False``, ``trust_env=False``, explicit request budgets, and bounded
payload sizes. Raw bytes are hashed with SHA-256 before any JSON parsing occurs.
"""

import json
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Final, Literal, Self

import httpx

from ..models.common import sha256_hex
from .eqn_catalog import (
    DEFAULT_MAX_FUTURE_SKEW_MS,
    EQN_CHANNEL_KEYS,
    MAX_EQN_PAYLOAD_BYTES,
    EqnBatchCapture,
    EqnWsPushFrame,
    parse_news_payload,
    strict_eqn_json,
)

EQN_REST_NEWS_URL: Final[str] = "https://eqn.news/api/v1/news"
EQN_MCP_URL: Final[str] = "https://api.eqn.news/public/mcp"
EQN_WS_URL: Final[str] = "wss://api.eqn.news/public/ws"
ALLOWED_REST_PREFIX: Final[str] = "https://eqn.news/"
ALLOWED_MCP_PREFIXES: Final[tuple[str, ...]] = (
    "https://api.eqn.news/",
    "https://eqn.news/",
)
ALLOWED_MCP_TOOLS: Final[frozenset[str]] = frozenset({"list_news", "get_news"})


class EqnFetchError(ValueError):
    """Fail-closed error raised on EQN transport, budget, status, or parse failure."""


@dataclass(frozen=True, slots=True)
class EqnMcpResponse:
    """Validated JSON-RPC 2.0 MCP response carrying raw bytes and SHA-256 digest."""

    request_id: int
    tool_name: str
    result: Mapping[str, object]
    raw_bytes: bytes
    raw_sha256: str

    def __post_init__(self) -> None:
        if type(self.request_id) is not int or self.request_id <= 0:
            raise ValueError("MCP request_id must be a positive integer")
        if not isinstance(self.tool_name, str) or not self.tool_name.strip():
            raise ValueError("MCP tool_name must be a non-empty string")
        if not isinstance(self.raw_bytes, bytes):
            raise TypeError("MCP raw_bytes must be bytes")
        if sha256_hex(self.raw_bytes) != self.raw_sha256:
            raise ValueError("MCP raw_sha256 does not match raw_bytes")


def _validate_eqn_url(
    url: str, *, allowed_prefixes: tuple[str, ...], allowed_hosts: set[str]
) -> str:
    if not isinstance(url, str) or not url.startswith(allowed_prefixes):
        raise EqnFetchError("EQN URL must start with a pinned https://eqn.news origin")
    parsed = httpx.URL(url)
    if (
        parsed.scheme != "https"
        or parsed.host not in allowed_hosts
        or parsed.port is not None
        or parsed.username
        or parsed.password
    ):
        raise EqnFetchError("EQN URL failed host/scheme SSRF validation")
    return url


def parse_ws_message(
    raw: str | bytes,
    received_ts_ms: int,
    *,
    max_future_skew_ms: int = DEFAULT_MAX_FUTURE_SKEW_MS,
) -> EqnWsPushFrame | Literal["pong"]:
    """Validate one message from ``wss://api.eqn.news/public/ws``.

    Returns ``"pong"`` for heartbeat responses or a validated ``EqnWsPushFrame``.
    Rejects empty payloads, malformed JSON, missing fields, non-positive timestamps,
    and excessive future clock skew.
    """
    if type(received_ts_ms) is not int or received_ts_ms <= 0:
        raise ValueError("received_ts_ms must be a positive integer")
    if isinstance(raw, bytes):
        raw_bytes = raw
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("WS frame is not valid UTF-8") from exc
    elif isinstance(raw, str):
        text = raw
        raw_bytes = raw.encode("utf-8")
    else:
        raise TypeError("WS frame must be str or bytes")

    if not text.strip():
        raise ValueError("WS frame must not be empty")
    if text.strip() == "pong":
        return "pong"

    raw_sha256 = sha256_hex(raw_bytes)
    frame = strict_eqn_json(raw_bytes)

    source_name = frame.get("source_name")
    news_title = frame.get("news_title")
    url = frame.get("url")
    published_ts_ms = frame.get("timestamp")
    raw_coins = frame.get("coins_included")

    for name, value in (
        ("source_name", source_name),
        ("news_title", news_title),
        ("url", url),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"WS frame {name} must be a non-empty string")

    if type(published_ts_ms) is not int or published_ts_ms <= 0:
        raise ValueError("WS frame timestamp must be a positive integer")

    if not isinstance(raw_coins, list):
        raise ValueError("WS frame coins_included must be a list")

    seen_coins: dict[str, None] = {}
    for entry in raw_coins:
        if isinstance(entry, str):
            sym = entry.strip().upper()
        elif isinstance(entry, Mapping) and isinstance(entry.get("symbol"), str):
            sym = str(entry["symbol"]).strip().upper()
        else:
            raise ValueError("WS frame coins_included entries must be strings")
        if not sym:
            raise ValueError("WS frame coins_included must not contain empty symbols")
        seen_coins.setdefault(sym, None)

    raw_id = frame.get("id")
    if isinstance(raw_id, str) and raw_id.strip():
        frame_id = raw_id.strip()
    elif type(raw_id) is int and raw_id > 0:
        frame_id = str(raw_id)
    else:
        frame_id = str(url).strip()

    return EqnWsPushFrame(
        frame_id=frame_id,
        source_name=str(source_name).strip(),
        news_title=str(news_title).strip(),
        coins_included=tuple(seen_coins),
        url=str(url).strip(),
        published_ts_ms=published_ts_ms,
        received_ts_ms=received_ts_ms,
        raw_sha256=raw_sha256,
        max_future_skew_ms=max_future_skew_ms,
    )


class EqnClient:
    """Budgeted read-only HTTPS client for Equation News REST and public MCP APIs."""

    def __init__(
        self,
        *,
        max_requests: int = 10,
        timeout_s: float = 10.0,
        max_response_bytes: int = MAX_EQN_PAYLOAD_BYTES,
        max_future_skew_ms: int = DEFAULT_MAX_FUTURE_SKEW_MS,
        rest_url: str = EQN_REST_NEWS_URL,
        mcp_url: str = EQN_MCP_URL,
        allowed_mcp_tools: frozenset[str] = ALLOWED_MCP_TOOLS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if type(max_requests) is not int or max_requests <= 0:
            raise ValueError("max_requests must be a positive integer")
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not math.isfinite(float(timeout_s))
            or float(timeout_s) <= 0
        ):
            raise ValueError("timeout_s must be a positive finite number")
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer")
        if type(max_future_skew_ms) is not int or max_future_skew_ms < 0:
            raise ValueError("max_future_skew_ms must be a non-negative integer")

        self.rest_url = _validate_eqn_url(
            rest_url,
            allowed_prefixes=(ALLOWED_REST_PREFIX,),
            allowed_hosts={"eqn.news"},
        )
        self.mcp_url = _validate_eqn_url(
            mcp_url,
            allowed_prefixes=ALLOWED_MCP_PREFIXES,
            allowed_hosts={"eqn.news", "api.eqn.news"},
        )
        self.max_requests = max_requests
        self.timeout_s = float(timeout_s)
        self.max_response_bytes = max_response_bytes
        self.max_future_skew_ms = max_future_skew_ms
        self.allowed_mcp_tools = frozenset(allowed_mcp_tools)
        self.requests_used = 0
        self._client = httpx.Client(
            timeout=self.timeout_s,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    @property
    def requests_remaining(self) -> int:
        return max(0, self.max_requests - self.requests_used)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _consume_budget(self) -> None:
        if self.requests_used >= self.max_requests:
            raise EqnFetchError("EQN request budget exhausted")
        self.requests_used += 1

    def _read_bounded(self, response: httpx.Response) -> bytes:
        if response.status_code != 200:
            raise EqnFetchError(f"EQN returned non-200 status: {response.status_code}")
        raw_bytes = response.content
        if len(raw_bytes) > self.max_response_bytes:
            raise EqnFetchError("EQN response exceeds max_response_bytes")
        return raw_bytes

    def fetch_news(
        self,
        channel: str = "all",
        limit: int = 50,
        cursor: str | None = None,
        received_ts_ms: int | None = None,
    ) -> EqnBatchCapture:
        """Fetch one bounded page from ``https://eqn.news/api/v1/news``."""
        if not isinstance(channel, str) or (channel != "all" and channel not in EQN_CHANNEL_KEYS):
            raise EqnFetchError(f"unsupported EQN channel: {channel!r}")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise EqnFetchError("EQN limit must be an integer between 1 and 100")
        if cursor is not None and (not isinstance(cursor, str) or not cursor.strip()):
            raise EqnFetchError("EQN cursor must be a non-empty string or None")

        obs_ms = int(time.time() * 1000) if received_ts_ms is None else received_ts_ms
        if type(obs_ms) is not int or obs_ms <= 0:
            raise EqnFetchError("received_ts_ms must be a positive integer")

        params: dict[str, str] = {"limit": str(limit), "ch": channel}
        if cursor is not None:
            params["cursor"] = cursor

        request = self._client.build_request("GET", self.rest_url, params=params)
        request_url = str(request.url)
        _validate_eqn_url(
            request_url,
            allowed_prefixes=(ALLOWED_REST_PREFIX,),
            allowed_hosts={"eqn.news"},
        )

        self._consume_budget()
        try:
            response = self._client.send(request)
        except httpx.HTTPError, OSError:
            raise EqnFetchError("EQN HTTP GET transport failure") from None

        raw_bytes = self._read_bounded(response)
        sha256_hex(raw_bytes)  # hash raw wire bytes before parsing
        try:
            return parse_news_payload(
                raw_bytes,
                channel=channel,
                request_url=request_url,
                received_ts_ms=obs_ms,
                max_future_skew_ms=self.max_future_skew_ms,
            )
        except (ValueError, TypeError) as exc:
            raise EqnFetchError(f"EQN payload validation failed: {exc}") from exc

    def call_mcp(
        self,
        tool_name: str,
        arguments: Mapping[str, object] | None = None,
        request_id: int = 1,
    ) -> EqnMcpResponse:
        """Invoke a read-only EQN MCP tool via JSON-RPC 2.0 HTTPS POST."""
        if not isinstance(tool_name, str) or tool_name not in self.allowed_mcp_tools:
            raise EqnFetchError(f"disallowed EQN MCP tool: {tool_name!r}")
        if type(request_id) is not int or request_id <= 0:
            raise EqnFetchError("MCP request_id must be a positive integer")
        if arguments is not None and not isinstance(arguments, Mapping):
            raise EqnFetchError("MCP arguments must be a mapping or None")

        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": dict(arguments) if arguments is not None else {},
            },
        }
        _validate_eqn_url(
            self.mcp_url,
            allowed_prefixes=ALLOWED_MCP_PREFIXES,
            allowed_hosts={"eqn.news", "api.eqn.news"},
        )
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        self._consume_budget()
        try:
            response = self._client.post(
                self.mcp_url,
                content=body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        except httpx.HTTPError, OSError:
            raise EqnFetchError("EQN MCP POST transport failure") from None

        raw_bytes = self._read_bounded(response)
        raw_sha256 = sha256_hex(raw_bytes)
        try:
            envelope = strict_eqn_json(raw_bytes)
        except (ValueError, TypeError) as exc:
            raise EqnFetchError(f"EQN MCP invalid JSON response: {exc}") from exc

        if (
            envelope.get("jsonrpc") != "2.0"
            or type(envelope.get("id")) is not int
            or envelope["id"] != request_id
        ):
            raise EqnFetchError("EQN MCP JSON-RPC version or request ID mismatch")
        if "error" in envelope:
            raise EqnFetchError("EQN MCP returned JSON-RPC error")
        if "result" not in envelope or not isinstance(envelope["result"], dict):
            raise EqnFetchError("EQN MCP response missing object result")
        result = envelope["result"]
        if result.get("isError") is True:
            raise EqnFetchError("EQN MCP tool invocation returned isError=true")

        return EqnMcpResponse(
            request_id=request_id,
            tool_name=tool_name,
            result=result,
            raw_bytes=raw_bytes,
            raw_sha256=raw_sha256,
        )

    @staticmethod
    def parse_ws_frame(
        raw: str | bytes,
        received_ts_ms: int,
        *,
        max_future_skew_ms: int = DEFAULT_MAX_FUTURE_SKEW_MS,
    ) -> EqnWsPushFrame | Literal["pong"]:
        return parse_ws_message(raw, received_ts_ms, max_future_skew_ms=max_future_skew_ms)
