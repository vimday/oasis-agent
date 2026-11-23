"""Mixed Base shadow requires both origins and reproduces their retained bytes."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from eth_abi.abi import decode, encode
from eth_hash.auto import keccak

from oasis_agent.cross_venue.live_cex_dex_shadow import build_synthetic_coinbase_transport
from oasis_agent.fastpath import base_mixed_snapshot as mixed
from oasis_agent.fastpath.bridge import FastpathBridge
from oasis_agent.fastpath.live_shadow_runner import (
    GET_L1_FEE_CALLDATA,
    GET_RESERVES_SELECTOR,
    LIQUIDITY_SELECTOR,
    SLOT0_SELECTOR,
    canonical_base_weth_usdc_pools,
)
from oasis_agent.fastpath.multicall_universe import (
    BALANCE_OF_SELECTOR,
    FEE_SELECTOR,
    MULTICALL3,
    UNIV3_FACTORY,
    DiscoveredPool,
    fetch_pinned_universe_snapshot,
)
from oasis_agent.fastpath.multihop_replay import MultihopReplayCapture, ReplayAnchorInput
from oasis_agent.models.common import canonical_json
from oasis_agent.uniswap_v4.liquidity_math import sqrt_price_at_tick
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key
from scripts import run_multicall_100tick_campaign as campaign

BLOCK = 61_268_083
HASH = "0x" + "ab" * 32
PARENT = "0x" + "37" * 32
ROOT = "0x" + "2c" * 32
TS = 1_795_443_992_200
RUNTIME = b"test-runtime"


def _specs() -> tuple[DiscoveredPool, ...]:
    controls = tuple(
        DiscoveredPool(
            pool_id=spec.address,
            label=spec.name,
            venue="test",
            kind=spec.kind,
            token0=spec.token0,
            token1=spec.token1,
            decimals0=spec.decimals0,
            decimals1=spec.decimals1,
            fixed_fee_pips=spec.fixed_fee_pips,
            tick_spacing=1 if spec.kind == mixed.PoolKind.SLIPSTREAM else None,
            factory=spec.factory or UNIV3_FACTORY,
            reserve0=50 * 10**18,
            reserve1=150_000 * 10**6,
        )
        for spec in canonical_base_weth_usdc_pools()
    )
    return (
        *controls,
        *(replace(controls[0], pool_id=f"0x{i:040x}", label=f"pool-{i}") for i in range(1, 55)),
    )


def _result(req: dict[str, Any], value: object) -> bytes:
    return canonical_json({"jsonrpc": "2.0", "id": req["id"], "result": value})


def _uint(value: int) -> bytes:
    return value.to_bytes(32, "big")


def _state_answer(req: dict[str, Any], calls: list[tuple[bool, bytes]]) -> bytes:
    return _result(req, "0x" + encode(["(bool,bytes)[]"], [calls]).hex())


def _address_mock(specs: tuple[DiscoveredPool, ...], req: dict[str, Any]) -> bytes:
    if req["method"] == "eth_getBlockByNumber":
        return _result(
            req,
            {
                "number": hex(BLOCK),
                "hash": HASH,
                "parentHash": PARENT,
                "stateRoot": ROOT,
                "timestamp": hex(TS // 1000),
                "timestampMs": hex(TS),
                "baseFeePerGas": "0x11",
            },
        )
    call, pin = req["params"]
    assert pin == {"blockHash": HASH, "requireCanonical": True} and call["to"] == MULTICALL3
    (args,) = decode(["(address,bool,bytes)[]"], bytes.fromhex(call["data"][10:]))
    by_id = {spec.pool_id: spec for spec in specs}
    output = []
    for target, _, data in args:
        cd, selector = f"0x{data.hex()}", f"0x{data[:4].hex()}"
        if target == mixed.BASE_TIME:
            value = _uint(TS if data == mixed._calldata("timestampMs()") else TS % 1000)
        elif cd == GET_L1_FEE_CALLDATA:
            value = _uint(100_000_000_000)
        elif target == MULTICALL3:
            assert data == mixed._calldata("getChainId()")
            value = _uint(8453)
        elif cd == SLOT0_SELECTOR:
            tick = -196_258
            value = encode(
                ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"],
                [sqrt_price_at_tick(tick), tick, 1, 1, 1, 0, True],
            )
        elif cd == LIQUIDITY_SELECTOR:
            value = _uint(200_000_000_000_000_000)
        elif cd == FEE_SELECTOR:
            value = _uint(by_id[target].fixed_fee_pips or 500)
        elif cd == GET_RESERVES_SELECTOR:
            value = encode(["uint256"] * 3, [50 * 10**18, 150_000 * 10**6, TS // 1000])
        elif selector == BALANCE_OF_SELECTOR:
            value = _uint(50 * 10**18 if target == mixed.WETH else 150_000 * 10**6)
        else:
            value = _uint(30)
        output.append((True, value))
    return _state_answer(req, output)


def _v4_answer(req: dict[str, Any], *, short: bool = False) -> bytes:
    call, pin = req["params"]
    assert pin == {"blockHash": HASH, "requireCanonical": True} and call["to"] == MULTICALL3
    (args,) = decode(["(address,bool,bytes)[]"], bytes.fromhex(call["data"][10:]))
    assert list(args) == mixed._v4_calls()
    output = [
        (True, _uint(8453)),
        (True, _uint(TS)),
        (True, encode(["address"], [mixed.MANAGER])),
        (True, encode(["address"], [mixed.MANAGER])),
        (True, _uint(0)),
        (True, _uint(0)),
        *((True, _uint(decimals)) for decimals in mixed.DECIMALS.values()),
    ]
    for key, page in mixed.V4_KEYS:
        tick = (page + 2) * 256 * key.tick_spacing
        output += [
            (True, encode(mixed.KEY_ABI, tuple(asdict(key).values()))),
            (
                True,
                encode(
                    ["uint160", "int24", "uint24", "uint24"],
                    [sqrt_price_at_tick(tick), tick, 0, key.fee],
                ),
            ),
            (True, _uint(10**20)),
            *([(True, _uint(0))] * 5),
        ]
    return _state_answer(req, output[:-1] if short else output)


def _prior(path: Path, specs: tuple[DiscoveredPool, ...]) -> Path:
    recorder = MultihopReplayCapture(
        path,
        source=mixed.SOURCES[mixed.PUBLICNODE],
        mode="public-shadow",
        ticks_requested=1,
    )

    def transport(url: str, request: bytes, timeout: float) -> bytes:
        return _address_mock(specs, json.loads(request))

    def read(send: mixed.Send) -> Any:
        return fetch_pinned_universe_snapshot(
            mixed.SOURCES[mixed.PUBLICNODE],
            specs,
            transport=send,
            pin_call_to_hash=True,
        )

    state = recorder.record_attempt(transport, read)
    price = campaign._compute_eth_price_micro_usdc(state.pool_nodes)
    fee = state.l1_fee_wei
    gas = state.base_fee_per_gas_wei
    recorder.save_snapshot(
        block_number=BLOCK,
        block_hash=HASH,
        pools=state.pool_nodes,
        anchors=(
            ReplayAnchorInput(mixed.WETH, 1, 10, 1, gas, fee),
            ReplayAnchorInput(
                mixed.USDC,
                1,
                10,
                1,
                (gas * price + 10**18 - 1) // 10**18,
                (fee * price + 10**18 - 1) // 10**18,
            ),
        ),
        gas_limit_units=1,
        priority_fee_wei=0,
        eth_price_micro_usdc=price,
        headroom_blocked=False,
    )
    recorder.finish(
        pool_specs=[asdict(spec) for spec in specs],
        snapshot_format="discovered_multicall",
        stopped_early=False,
        canonical_conflict=False,
        campaign_summary_sha256="0" * 64,
    )
    return path / "manifest.json"


def test_normal_campaign_mixed_parallel_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(mixed, "MANAGER_RUNTIME", "0x" + keccak(RUNTIME).hex())
    specs = _specs()
    shared = Barrier(2, timeout=2)

    def transport(url: str, request: bytes, timeout: float) -> bytes:
        req = json.loads(request)
        if req["method"] == "eth_call":
            shared.wait()
        if url == mixed.SOURCES[mixed.PUBLICNODE]:
            return _address_mock(specs, req)
        assert url == mixed.OFFICIAL_URL
        if req["method"] == "eth_call":
            return _v4_answer(req)
        if req["method"] == "eth_getCode":
            return _result(req, "0x" + RUNTIME.hex())
        assert req["params"] == [hex(BLOCK), False]
        return _address_mock(specs, req)

    path = tmp_path / "campaign"
    prior = _prior(tmp_path / "prior", specs)
    run = campaign.run_multicall_campaign(
        out_dir=path,
        mode="public-shadow",
        ticks=1,
        interval_ms=0,
        max_rpc_calls=5,
        rpc_transport=transport,
        coinbase_transport=build_synthetic_coinbase_transport(),
        bridge=FastpathBridge(),
        compare_resident_graph=True,
        mixed_base_address_manifest=prior,
        mixed_base_secondary_max_rpc_calls=3,
    )
    assert run.summary["rpc_calls_used"] == 5
    assert run.summary["mixed_base_state"]["rpc_attempts_by_source"] == {
        mixed.PUBLICNODE: 2,
        mixed.OFFICIAL: 3,
    }
    details = run.records[0]["shadow_cycle_lifecycle"]["mixed_base_state"]
    assert details["resident_graph_comparison"]["pool_count"] == 66
    assert details["execution_eligible"] is False
    assert run.summary["resident_graph_comparison"]["matched_ticks"] == 1

    manifest = path / details["manifest"]
    rebuilt = mixed.replay_mixed_snapshot(manifest)
    assert {node.pool_id for node in rebuilt.v4_pools} == {
        pool_id_from_key(key) for key, _ in mixed.V4_KEYS
    }
    assert campaign.main(["--replay-mixed-base", str(manifest)]) == 0
    assert json.loads(capsys.readouterr().out)["address_pools"] == 58

    response = manifest.parent / "v4_state.response.json"
    response.write_bytes(response.read_bytes() + b" ")
    assert campaign.main(["--replay-mixed-base", str(manifest)]) == 2
    assert "digest mismatch" in capsys.readouterr().err


@pytest.mark.parametrize("mismatch", [True, False])
def test_mixed_never_publishes_failed_join(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: bool,
) -> None:
    monkeypatch.setattr(mixed, "MANAGER_RUNTIME", "0x" + keccak(RUNTIME).hex())
    specs = _specs()
    capture = mixed.MixedBaseCapture(
        tmp_path / "mixed",
        specs,
        "0" * 64,
        primary_limit=2,
        secondary_limit=3,
        injected=True,
    )

    def transport(url: str, request: bytes, timeout: float) -> bytes:
        req = json.loads(request)
        if url == mixed.OFFICIAL_URL and req["method"] == "eth_getBlockByNumber" and mismatch:
            reply = json.loads(_address_mock(specs, req))
            reply["result"]["stateRoot"] = HASH
            return canonical_json(reply)
        if req["method"] == "eth_getCode":
            return _result(req, "0x" + RUNTIME.hex())
        if url == mixed.OFFICIAL_URL and req["method"] == "eth_call":
            return _v4_answer(req, short=True)
        return _address_mock(specs, req)

    primary = capture.budgeted(mixed.PUBLICNODE, transport)
    secondary = capture.budgeted(mixed.OFFICIAL, transport)
    with ThreadPoolExecutor(max_workers=2) as workers, pytest.raises(ValueError):
        capture.capture("latest", primary, secondary, 5.0, workers)
    assert capture.counts() == (
        {mixed.PUBLICNODE: 1, mixed.OFFICIAL: 1}
        if mismatch
        else {mixed.PUBLICNODE: 2, mixed.OFFICIAL: 3}
    )
    directory = capture.directory / "attempt-00000001"
    manifest = directory / "manifest.json"
    assert json.loads(manifest.read_bytes())["status"] == "failed"
    assert not (directory / "state.json").exists()
    with pytest.raises(ValueError, match="incomplete"):
        mixed.replay_mixed_snapshot(manifest)


@pytest.mark.parametrize("reply_kind", ["http_429", "json_rpc_error_-32005"])
def test_quoter_transport_preserves_first_refusal_and_stops_next_request(
    tmp_path: Path,
    reply_kind: str,
) -> None:
    capture = mixed.MixedBaseCapture(
        tmp_path / "mixed",
        _specs(),
        "0" * 64,
        primary_limit=5,
        secondary_limit=3,
        injected=True,
    )
    sent = []
    raw = (
        b"too many requests"
        if reply_kind == "http_429"
        else b'{"jsonrpc":"2.0","id":1,"error":{"code":-32005,"message":"limit"}}'
    )

    def transport(url: str, request: bytes, timeout: float) -> bytes:
        sent.append(request)
        if len(sent) == 1:
            return b'{"jsonrpc":"2.0","id":1,"error":{"code":3,"message":"execution reverted"}}'
        if reply_kind == "http_429":
            raise mixed.MixedRpcHttpError("limited", 429, raw)
        return raw

    url = mixed.SOURCES[mixed.PUBLICNODE]
    send = capture.budgeted(mixed.PUBLICNODE, transport)
    request = mixed._request("eth_call", [{"to": mixed.MANAGER, "data": "0x"}, "latest"])
    send(url, request, 1.0)
    assert not capture.stopped.is_set()
    if reply_kind == "http_429":
        with pytest.raises(mixed.MixedRpcHttpError, match="limited"):
            send(url, request, 1.0)
    else:
        assert send(url, request, 1.0) == raw
    assert capture.stopped.is_set()
    with pytest.raises(mixed.RpcError, match="scope stopped"):
        send(url, request, 1.0)
    assert sent == [request, request]
    assert capture.counts()[mixed.PUBLICNODE] == 2
    assert (capture.directory / "stop.request.json").read_bytes() == request
    assert (capture.directory / "stop.response.json").read_bytes() == raw
    terminal = json.loads((capture.directory / "STOP.json").read_bytes())
    assert terminal["reason"] == reply_kind
