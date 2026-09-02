from __future__ import annotations

import json
import urllib.request
from typing import Protocol

from ..errors import RpcError


class HttpPoster(Protocol):
    def post(self, payload: bytes) -> bytes: ...


class _UrlLibPoster:
    def __init__(self, url: str, timeout: float) -> None:
        self.url = url
        self.timeout = timeout

    def post(self, payload: bytes) -> bytes:
        request = urllib.request.Request(self.url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read()


class JsonRpcTransport:
    def __init__(self, url: str, http: HttpPoster | None = None, timeout: float = 20.0) -> None:
        self._http = http or _UrlLibPoster(url, timeout)
        self._request_id = 0

    def request(self, method: str, params: list[object]) -> object:
        self._request_id += 1
        payload = json.dumps({"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}).encode()
        try:
            response = json.loads(self._http.post(payload))
        except Exception as exc:
            raise RpcError(f"RPC transport failure: {exc}") from exc
        if response.get("error") is not None:
            error = response["error"]
            raise RpcError(str(error.get("message", error)))
        if response.get("id") != self._request_id or response.get("jsonrpc") != "2.0":
            raise RpcError("invalid JSON-RPC response")
        if "result" not in response:
            raise RpcError("JSON-RPC response has no result")
        return response["result"]
