from __future__ import annotations

import json
import random
import time
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from ..errors import RpcError


class HttpPoster(Protocol):
    def post(self, payload: bytes) -> bytes: ...


@dataclass(frozen=True, slots=True)
class RpcExchange:
    method: str
    request_id: int
    request_payload: bytes
    response_payload: bytes
    attempt: int


class RpcExchangeRecorder(Protocol):
    def record(self, exchange: RpcExchange) -> None: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise RpcError("RPC redirects are forbidden")


class _UrlLibPoster:
    def __init__(self, url: str, timeout: float) -> None:
        parsed = urllib.parse.urlsplit(url)
        # nosec B310
        # scheme is allowlisted to http/https and redirects are forbidden by
        # _NoRedirect, so file:/ and custom schemes cannot be opened.
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("RPC requires an HTTP(S) endpoint")
        self.url = url
        self.timeout = timeout

    def post(self, payload: bytes) -> bytes:
        request = urllib.request.Request(
            self.url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.build_opener(_NoRedirect()).open(
            request, timeout=self.timeout
        ) as response:
            return cast(bytes, response.read())


_RETRYABLE_METHODS = frozenset(
    {
        "eth_chainId",
        "eth_blockNumber",
        "eth_getBalance",
        "eth_getBlockByHash",
        "eth_getBlockByNumber",
        "eth_getCode",
        "eth_getLogs",
        "eth_getStorageAt",
        "eth_getTransactionByHash",
        "eth_getTransactionReceipt",
        "eth_call",
    }
)


class JsonRpcTransport:
    def __init__(
        self,
        url: str,
        http: HttpPoster | None = None,
        timeout: float = 20.0,
        retries: int = 2,
        backoff_seconds: float = 0.25,
        total_deadline_seconds: float = 60.0,
        jitter_seconds: float = 0.05,
        recorder: RpcExchangeRecorder | None = None,
    ) -> None:
        if timeout <= 0 or total_deadline_seconds <= 0:
            raise ValueError("RPC timeouts must be positive")
        self._http = http or _UrlLibPoster(url, timeout)
        self._request_id = 0
        self._retries = max(0, retries)
        self._backoff_seconds = max(0.0, backoff_seconds)
        self._total_deadline_seconds = total_deadline_seconds
        self._jitter_seconds = max(0.0, jitter_seconds)
        self._recorder = recorder

    def request(self, method: str, params: list[object]) -> object:
        retryable = method in _RETRYABLE_METHODS
        deadline = time.monotonic() + self._total_deadline_seconds
        last_error: RpcError | None = None
        attempts = self._retries + 1 if retryable else 1
        for attempt in range(attempts):
            if time.monotonic() >= deadline:
                raise RpcError(f"RPC deadline exceeded for {method}")
            self._request_id += 1
            request_id = self._request_id
            payload = json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            ).encode()
            try:
                raw_response = self._http.post(payload)
                if self._recorder is not None:
                    self._recorder.record(
                        RpcExchange(method, request_id, payload, raw_response, attempt + 1)
                    )
                decoded = json.loads(raw_response)
            except (ValueError, TypeError):
                last_error = RpcError("RPC transport failure (details redacted)")
                if attempt + 1 < attempts:
                    self._sleep(attempt, deadline)
                    continue
                raise last_error from None
            if not isinstance(decoded, Mapping):
                raise RpcError("invalid JSON-RPC response: top level must be an object")
            response = decoded
            if response.get("id") != request_id or response.get("jsonrpc") != "2.0":
                raise RpcError("invalid JSON-RPC response")
            if response.get("error") is not None:
                error = response["error"]
                code = error.get("code") if isinstance(error, Mapping) else None
                last_error = RpcError(
                    str(error.get("message", error)) if isinstance(error, Mapping) else str(error)
                )
                if code in {-32005, -32603} and attempt + 1 < attempts:
                    self._sleep(attempt, deadline)
                    continue
                raise last_error
            if "result" not in response:
                raise RpcError("JSON-RPC response has no result")
            return response["result"]
        raise last_error or RpcError("RPC request failed")

    def _sleep(self, attempt: int, deadline: float) -> None:
        delay = self._backoff_seconds * (2**attempt)
        if self._jitter_seconds:
            delay += random.uniform(0, self._jitter_seconds)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RpcError("RPC deadline exceeded during retry backoff")
        time.sleep(min(delay, remaining))


__all__ = ["HttpPoster", "JsonRpcTransport", "RpcExchange", "RpcExchangeRecorder"]
