"""Read-only JSON-RPC client and chain registry (P3)."""

import json

import httpx
import pytest

from oasis_agent.server.chains import (
    ChainRegistry,
    ReadOnlyRpcClient,
    RpcProtocolError,
    RpcUnavailableError,
    decode_rpc_quantity,
    rpc_origin,
)
from oasis_agent.server.config import ServerChainConfig


def chain(*, rpc_url="https://rpc.example/v1", cache_seconds=15):
    return ServerChainConfig(
        chain_id=46630, name="Testnet", rpc_url=rpc_url, cache_seconds=cache_seconds
    )


def ok_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept"] == "application/json"
    body = json.loads(request.content)
    method = body["method"]
    if method == "eth_chainId":
        result = "0x4663"
    elif method == "eth_blockNumber":
        result = "0x10"
    elif method == "eth_getBalance":
        result = "0x0"
    else:
        result = "0x6000"
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def transport(handler):
    return httpx.MockTransport(handler)


def test_rpc_quantity_decoder_rejects_coercion_and_noncanonical_hex():
    assert decode_rpc_quantity("0x0", "amount") == 0
    assert decode_rpc_quantity("0xA", "amount") == 10
    for value in (0, True, "10", "0x", "0x00", "0xg"):
        with pytest.raises(RpcProtocolError, match="canonical hex quantity"):
            decode_rpc_quantity(value, "amount")


@pytest.mark.anyio
async def test_read_only_call_returns_result():
    client = ReadOnlyRpcClient(chain(), transport=transport(ok_handler))
    try:
        result = await client.call("eth_chainId", [])
        assert result.result == "0x4663"
        assert result.cached is False
        assert result.latency_ms >= 0
    finally:
        await client.close()


@pytest.mark.anyio
async def test_cache_hit_within_window():
    client = ReadOnlyRpcClient(chain(cache_seconds=60), transport=transport(ok_handler))
    try:
        await client.call("eth_chainId", [])
        cached = await client.call("eth_chainId", [])
        assert cached.cached is True
    finally:
        await client.close()


@pytest.mark.anyio
async def test_method_not_whitelisted_fails_closed():
    client = ReadOnlyRpcClient(chain(), transport=transport(ok_handler))
    try:
        with pytest.raises(RpcUnavailableError, match="not allowed"):
            await client.call("eth_sendTransaction", [])
    finally:
        await client.close()


@pytest.mark.anyio
async def test_missing_endpoint_fails_closed():
    client = ReadOnlyRpcClient(chain(rpc_url=""), transport=transport(ok_handler))
    try:
        with pytest.raises(RpcUnavailableError, match="no RPC endpoint"):
            await client.call("eth_chainId", [])
    finally:
        await client.close()


@pytest.mark.anyio
async def test_rpc_error_response_raises_protocol_error():
    def handler(request):
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}}
        )

    client = ReadOnlyRpcClient(chain(), transport=transport(handler))
    try:
        with pytest.raises(RpcProtocolError, match="RPC error"):
            await client.call("eth_chainId", [])
    finally:
        await client.close()


@pytest.mark.anyio
async def test_malformed_response_raises_protocol_error():
    def handler(request):
        return httpx.Response(200, content=b"<html>not json</html>")

    client = ReadOnlyRpcClient(chain(), transport=transport(handler))
    try:
        with pytest.raises(RpcProtocolError, match="not JSON"):
            await client.call("eth_chainId", [])
    finally:
        await client.close()


def test_rpc_origin_masks_path_query_and_credentials():
    assert rpc_origin("https://rpc.example/v1?key=secret") == "https://rpc.example"
    assert rpc_origin("https://rpc.example:8545/path") == "https://rpc.example:8545"
    assert rpc_origin("http://127.0.0.1:8545") == "http://127.0.0.1:8545"
    assert rpc_origin("http://[::1]:8545/private") == "http://[::1]:8545"
    assert rpc_origin("") == ""


def test_registry_summary_and_missing_chain():
    registry = ChainRegistry((ServerChainConfig(46630, "Testnet", rpc_url="https://rpc.example"),))
    summary = registry.summary()
    assert summary[0]["chain_id"] == 46630
    assert summary[0]["rpc_configured"] is True
    assert summary[0]["rpc_origin"] == "https://rpc.example"
    assert summary[0]["pool_manager"] == ""
    with pytest.raises(RpcUnavailableError, match="not configured"):
        registry.client(1)


@pytest.mark.anyio
async def test_registry_caches_clients_per_chain():
    registry = ChainRegistry((ServerChainConfig(46630, "Testnet"),))
    first = registry.client(46630, transport=transport(ok_handler))
    second = registry.client(46630)
    assert first is second
    await registry.close()
