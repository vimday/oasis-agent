"""Chain registry and read-only JSON-RPC client (P3).

Only non-mutating methods are exposed and every call is bounded by a timeout;
there is no transaction submission, no wallet and no signing path in this
module. Chains without a configured RPC endpoint are reported as disabled
rather than probed.
"""

import asyncio
import json
import re
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass
from typing import TypedDict

import httpx

from .config import ServerChainConfig

_WHITELISTED_METHODS = frozenset(
    {
        "eth_chainId",
        "eth_blockNumber",
        "eth_getBlockByNumber",
        "eth_getBlockByHash",
        "eth_getBalance",
        "eth_getCode",
        "eth_call",
        "eth_getLogs",
        "eth_getTransactionReceipt",
        "eth_gasPrice",
        "eth_feeHistory",
        "eth_getStorageAt",
    }
)
_RPC_TIMEOUT_SECONDS = 8.0


class RpcUnavailableError(Exception):
    """The chain has no usable read-only RPC endpoint."""


class RpcProtocolError(Exception):
    """The endpoint returned a malformed or error JSON-RPC response."""


def decode_rpc_quantity(value: object, name: str) -> int:
    """Decode one canonical JSON-RPC hex quantity without coercing hostile input."""
    if not isinstance(value, str) or not re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)", value):
        raise RpcProtocolError(f"{name} is not a canonical hex quantity")
    return int(value, 16)


def rpc_origin(url: str) -> str:
    """Mask an RPC endpoint down to ``scheme://host[:port]``.

    Paths and query strings may carry API keys or other material and are never
    surfaced to the dashboard; credentials in userinfo are already rejected by
    the strict configuration parser.
    """
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname
    if not host:
        return ""
    display_host = f"[{host}]" if ":" in host else host
    try:
        port = parsed.port
    except ValueError:
        port = None
    return f"{parsed.scheme}://{display_host}" + (f":{port}" if port is not None else "")


@dataclass(frozen=True, slots=True)
class RpcResult:
    result: object
    latency_ms: int
    cached: bool = False


class ChainSummary(TypedDict):
    chain_id: int
    name: str
    rpc_configured: bool
    rpc_origin: str
    pool_manager: str


class ReadOnlyRpcClient:
    def __init__(
        self, chain: ServerChainConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._chain = chain
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._cache: OrderedDict[tuple[str, str], tuple[float, object]] = OrderedDict()
        # Fixed-size lock striping coalesces identical polls without an unbounded
        # per-key lock registry or serializing every independent RPC method.
        self._locks = tuple(asyncio.Lock() for _ in range(64))

    @property
    def chain_id(self) -> int:
        return self._chain.chain_id

    @property
    def available(self) -> bool:
        return bool(self._chain.rpc_url.strip())

    async def call(self, method: str, params: list[object]) -> RpcResult:
        if method not in _WHITELISTED_METHODS:
            raise RpcUnavailableError(f"method not allowed: {method}")
        if not self.available:
            raise RpcUnavailableError(f"chain {self._chain.chain_id} has no RPC endpoint")
        cache_key = (method, json.dumps(params, sort_keys=True, separators=(",", ":")))
        async with self._locks[hash(cache_key) % len(self._locks)]:
            hit = self._cache.get(cache_key)
            if hit is not None and time.monotonic() - hit[0] < self._chain.cache_seconds:
                self._cache.move_to_end(cache_key)
                return RpcResult(hit[1], 0, cached=True)
            result = await self._fetch(method, params)
            self._cache[cache_key] = (time.monotonic(), result.result)
            self._cache.move_to_end(cache_key)
            while len(self._cache) > 256:
                self._cache.popitem(last=False)
            return result

    async def _fetch(self, method: str, params: list[object]) -> RpcResult:
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode()
        started = time.monotonic()
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=_RPC_TIMEOUT_SECONDS, transport=self._transport)
            self._client = client
        try:
            response = await client.post(
                self._chain.rpc_url,
                content=payload,
                headers={"accept": "application/json", "content-type": "application/json"},
            )
            response.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            raise RpcUnavailableError("RPC transport failure") from exc
        latency = int((time.monotonic() - started) * 1000)
        try:
            body = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise RpcProtocolError("RPC response is not JSON") from exc
        if (
            not isinstance(body, dict)
            or body.get("jsonrpc") != "2.0"
            or type(body.get("id")) is not int
            or body.get("id") != 1
        ):
            raise RpcProtocolError("RPC response envelope malformed")
        error = body.get("error")
        if error is not None:
            raise RpcProtocolError("RPC error response; provider details redacted")
        if "result" not in body:
            raise RpcProtocolError("RPC response missing result")
        result = body["result"]
        return RpcResult(result, latency)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class ChainRegistry:
    def __init__(self, chains: tuple[ServerChainConfig, ...]) -> None:
        self._chains = {chain.chain_id: chain for chain in chains}
        self._clients: dict[int, ReadOnlyRpcClient] = {}

    def client(
        self, chain_id: int, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> ReadOnlyRpcClient:
        if chain_id not in self._chains:
            raise RpcUnavailableError(f"chain {chain_id} is not configured")
        client = self._clients.get(chain_id)
        if client is None:
            client = ReadOnlyRpcClient(self._chains[chain_id], transport=transport)
            self._clients[chain_id] = client
        return client

    def summary(self) -> tuple[ChainSummary, ...]:
        return tuple(
            ChainSummary(
                chain_id=chain.chain_id,
                name=chain.name,
                rpc_configured=bool(chain.rpc_url.strip()),
                rpc_origin=rpc_origin(chain.rpc_url),
                pool_manager=chain.pool_manager,
            )
            for chain in self._chains.values()
        )

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
