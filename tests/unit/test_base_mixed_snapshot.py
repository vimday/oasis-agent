"""Mixed Base shadow requires both origins and reproduces their retained bytes."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import asdict, replace
from pathlib import Path
from threading import Barrier, Event
from typing import Any

import pytest
from eth_abi.abi import decode, encode
from eth_hash.auto import keccak

from oasis_agent.cross_venue.live_cex_dex_shadow import build_synthetic_coinbase_transport
from oasis_agent.evidence import base_newheads as newheads
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
from oasis_agent.models.common import canonical_json, sha256_hex
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
        if (url == mixed.SOURCES[mixed.PUBLICNODE] and req["method"] == "eth_call") or (
            url == mixed.OFFICIAL_URL and req["method"] == "eth_getBlockByNumber"
        ):
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
    assert {node.pool_id: node.v4_tick_spacing for node in rebuilt.v4_pools} == {
        pool_id_from_key(key): key.tick_spacing for key, _ in mixed.V4_KEYS
    }
    assert campaign.main(["--replay-mixed-base", str(manifest)]) == 0
    assert json.loads(capsys.readouterr().out)["address_pools"] == 58

    captured = manifest.read_bytes()
    doc = json.loads(captured)
    assert doc["schema"] == mixed.SCHEMA
    overlap = doc["primary_address_official_transport_overlap_ns"]
    assert overlap["secondary_header"] > 0
    assert sum(overlap.values()) > 0
    doc["primary_address_official_transport_overlap_ns"]["secondary_header"] += 1
    manifest.write_bytes(canonical_json(doc))
    with pytest.raises(ValueError, match="transport overlap differs"):
        mixed.replay_mixed_snapshot(manifest)

    # The original schemas have a different timing gate and omit per-role transport clocks.
    state_path = manifest.parent / "state.json"
    captured_state = state_path.read_bytes()
    old_state = json.loads(captured_state)
    for node in old_state["pools"]:
        node.pop("v4_tick_spacing", None)
    prior_state_bytes = canonical_json(old_state)
    state_path.write_bytes(prior_state_bytes)
    legacy = json.loads(captured)
    legacy["state_sha256"] = sha256_hex(prior_state_bytes)
    legacy.pop("primary_address_official_transport_overlap_ns")
    legacy["start_monotonic_ns"] = 1
    legacy["state_ready_monotonic_ns"] = 20
    for index, role in enumerate(
        ("primary_header", "secondary_header", "manager_code", "address_state", "v4_state"),
        1,
    ):
        row = legacy["roles"][role]
        row["release_monotonic_ns"] = index * 2
        row["received_monotonic_ns"] = index * 2 + 1
        row.pop("transport_started_monotonic_ns")
        row.pop("transport_finished_monotonic_ns")
    for schema in (mixed.SCHEMA_V2, mixed.SCHEMA_V1):
        legacy["schema"] = schema
        manifest.write_bytes(canonical_json(legacy))
        replayed = mixed.replay_mixed_snapshot(manifest)
        assert replayed.block == rebuilt.block
        assert all(node.v4_tick_spacing is None for node in replayed.v4_pools)
    legacy["roles"]["address_state"]["release_monotonic_ns"] = 5
    manifest.write_bytes(canonical_json(legacy))
    with pytest.raises(ValueError, match="precedes its required pin"):
        mixed.replay_mixed_snapshot(manifest)
    state_path.write_bytes(captured_state)
    manifest.write_bytes(captured)

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

    shared = Barrier(2, timeout=2)

    def transport(url: str, request: bytes, timeout: float) -> bytes:
        req = json.loads(request)
        if mismatch and (
            (url == mixed.SOURCES[mixed.PUBLICNODE] and req["method"] == "eth_call")
            or (url == mixed.OFFICIAL_URL and req["method"] == "eth_getBlockByNumber")
        ):
            shared.wait()
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
        {mixed.PUBLICNODE: 2, mixed.OFFICIAL: 1}
        if mismatch
        else {mixed.PUBLICNODE: 2, mixed.OFFICIAL: 3}
    )
    directory = capture.directory / "attempt-00000001"
    manifest = directory / "manifest.json"
    outcome = json.loads(manifest.read_bytes())
    assert outcome["status"] == "failed"
    if mismatch:
        assert set(outcome["roles"]) == {"primary_header", "address_state", "secondary_header"}
        assert (directory / "address_state.response.json").is_file()
        assert not (directory / "manager_code.request.json").exists()
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


def test_speculative_address_refusal_stops_remaining_official_requests(tmp_path: Path) -> None:
    specs = _specs()
    capture = mixed.MixedBaseCapture(
        tmp_path / "mixed",
        specs,
        "0" * 64,
        primary_limit=2,
        secondary_limit=3,
        injected=True,
    )
    in_flight = Barrier(2, timeout=2)
    requests = []

    def transport(url: str, request: bytes, timeout: float) -> bytes:
        req = json.loads(request)
        method = req["method"]
        requests.append((url, method))
        if url == mixed.OFFICIAL_URL:
            assert method == "eth_getBlockByNumber"
            in_flight.wait()
            assert capture.stopped.wait(2)
        elif method == "eth_call":
            in_flight.wait()
            raise mixed.MixedRpcHttpError("limited", 429, b"address request rate limit")
        return _address_mock(specs, req)

    primary = capture.budgeted(mixed.PUBLICNODE, transport)
    official = capture.budgeted(mixed.OFFICIAL, transport)
    with (
        ThreadPoolExecutor(max_workers=2) as workers,
        pytest.raises(mixed.MixedRpcHttpError, match="limited"),
    ):
        capture.capture("latest", primary, official, 5.0, workers)
    assert capture.counts() == {mixed.PUBLICNODE: 2, mixed.OFFICIAL: 1}
    assert requests.count((mixed.OFFICIAL_URL, "eth_getBlockByNumber")) == 1
    assert len(requests) == 3
    terminal = json.loads((capture.directory / "STOP.json").read_bytes())
    assert terminal["source"] == mixed.PUBLICNODE and terminal["http_status"] == 429
    scope = capture.directory / "attempt-00000001"
    assert (scope / "secondary_header.response.json").is_file()
    assert not (scope / "state.json").exists()


class _HeadSocket:
    def __init__(self, headers: tuple[dict[str, object], ...]):
        self.headers = iter(headers)
        self.responses: list[bytes] = []

    def send(self, message: bytes, *, text: bool) -> None:
        assert text
        data = json.loads(message)
        self.responses.append(_result(data, "0x2105" if data["id"] == 1 else "test-id"))

    def recv(self, *, timeout: float, decode: bool) -> bytes:
        assert not decode and timeout > 0
        if self.responses:
            return self.responses.pop()
        return canonical_json(
            {
                "jsonrpc": "2.0",
                "method": "eth_subscription",
                "params": {"subscription": "test-id", "result": next(self.headers)},
            }
        )


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("mismatch", [False, True])
def test_newheads_campaign_binds_before_state_and_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: bool,
    parallel: bool,
) -> None:
    monkeypatch.setattr(mixed, "MANAGER_RUNTIME", "0x" + keccak(RUNTIME).hex())
    specs = _specs()
    header = json.loads(_address_mock(specs, {"method": "eth_getBlockByNumber", "id": 1}))["result"]
    hint = header | {"stateRoot": HASH} if mismatch else header
    prior = _prior(tmp_path / "prior", specs)
    out = tmp_path / "campaign"
    calls = []
    header_barrier = Barrier(2, timeout=3)

    def transport(url: str, request: bytes, timeout: float) -> bytes:
        req = json.loads(request)
        calls.append((url, req["method"]))
        if req["method"] == "eth_getBlockByNumber":
            assert req["params"] == [hex(BLOCK), False]
            if parallel:
                header_barrier.wait()
            return _address_mock(specs, req)
        if req["method"] == "eth_getCode":
            assert url == mixed.OFFICIAL_URL
            return _result(req, "0x" + RUNTIME.hex())
        return _address_mock(specs, req) if url != mixed.OFFICIAL_URL else _v4_answer(req)

    def run():
        return campaign.run_multicall_campaign(
            out_dir=out,
            mode="public-shadow",
            ticks=1,
            interval_ms=0,
            max_rpc_calls=8,
            rpc_transport=transport,
            coinbase_transport=build_synthetic_coinbase_transport(),
            bridge=FastpathBridge(),
            compare_resident_graph=True,
            mixed_base_address_manifest=prior,
            mixed_base_secondary_max_rpc_calls=3,
            base_newheads=True,
            base_newheads_overlap_headers=parallel,
            base_newheads_socket_factory=lambda: nullcontext(_HeadSocket((hint,))),
        )

    if mismatch:
        with pytest.raises(newheads.NewHeadsError, match="conflicts with"):
            run()
        expected = [(mixed.SOURCES[mixed.PUBLICNODE], "eth_getBlockByNumber")]
        if parallel:
            expected.append((mixed.OFFICIAL_URL, "eth_getBlockByNumber"))
        assert sorted(calls) == sorted(expected)
        assert (out / newheads.DIRECTORY / "STOP.json").is_file()
        assert not (out / mixed.DIRECTORY / "attempt-00000001" / "state.json").exists()
        return

    result = run()
    assert result.summary["rpc_calls_used"] == 8
    assert result.summary["base_newheads"]["rpc_attempts_by_source"] == {
        newheads.POLICIES[0].source: 3,
    }
    record = result.records[0]["shadow_cycle_lifecycle"]["mixed_base_state"]
    assert record["head_to_cohorts_ready_ns"] >= record["head_to_state_ready_ns"] > 0
    assert {k: v["pools"] for k, v in record["cohorts"].items()} == {
        "four_pool": 4,
        "address_58": 58,
        "mixed_66": 66,
    }
    manifest = out / record["manifest"]
    retained = json.loads(manifest.read_bytes())
    assert retained["schema"] == (mixed.PARALLEL_HEAD_SCHEMA if parallel else mixed.HEAD_SCHEMA)
    assert len(mixed.replay_mixed_snapshot(manifest).nodes) == 66
    if parallel:
        assert record["parallel_header_transport_overlap_ns"] > 0
        assert record["head_to_official_header_ns"] < record["head_to_state_ready_ns"]
        retained["parallel_header_transport_overlap_ns"] += 1
        original = manifest.read_bytes()
        manifest.write_bytes(canonical_json(retained))
        with pytest.raises(ValueError, match="header transport overlap differs"):
            mixed.replay_mixed_snapshot(manifest)
        manifest.write_bytes(original)
    raw = out / newheads.DIRECTORY / "frame-00000001.raw"
    raw.write_bytes(raw.read_bytes() + b" ")
    with pytest.raises(ValueError, match="frame digest"):
        mixed.replay_mixed_snapshot(manifest)


def test_newheads_gap_stops_without_retry(tmp_path: Path) -> None:
    specs = _specs()
    header = json.loads(_address_mock(specs, {"method": "eth_getBlockByNumber", "id": 1}))["result"]
    future = header | {"number": hex(BLOCK + 2)}
    stopped = Event()
    folder = tmp_path / "stream"
    session = newheads.NewHeadsSession(
        folder,
        2,
        stopped,
        socket_factory=lambda: nullcontext(_HeadSocket((header, future))),
    )
    session.start()
    try:
        assert stopped.wait(2)
        with pytest.raises(newheads.NewHeadsError, match="newHeads gap"):
            session.next_head()
        assert session.counts() == {newheads.POLICIES[0].source: 3}
        assert (folder / "frame-00000002.raw").is_file()
        assert json.loads((folder / "STOP.json").read_bytes())["canonical_conflict"] is False
    finally:
        session.close()


def test_latest_newheads_retains_superseded_frames_and_replays(tmp_path: Path) -> None:
    specs = _specs()
    header = json.loads(_address_mock(specs, {"method": "eth_getBlockByNumber", "id": 1}))["result"]
    next_hash = "0x" + "ba" * 32
    last_hash = "0x" + "da" * 32
    child = header | {"number": hex(BLOCK + 1), "hash": next_hash, "parentHash": header["hash"]}
    grandchild = child | {"number": hex(BLOCK + 2), "hash": last_hash, "parentHash": next_hash}
    folder = tmp_path / "latest"
    session = newheads.NewHeadsSession(
        folder,
        1,
        Event(),
        socket_factory=lambda: nullcontext(_HeadSocket((header, child, grandchild))),
        max_observed_heads=3,
    )
    session.start()
    session._thread.join(timeout=2)
    try:
        selected = session.next_head()
        assert selected.head.block_hash == last_hash
        with pytest.raises(newheads.NewHeadsError, match="window exhausted"):
            session.next_head()
    finally:
        session.close()
    assert session.schedule_sha256 is not None
    schedule = newheads.replay_head_schedule(folder, session.schedule_sha256)
    assert [row["status"] for row in schedule["frames"]] == [
        "superseded_in_queue",
        "superseded_in_queue",
        "dispatched",
    ]
    assert newheads.replay_head_observation(folder, selected.to_wire()) == selected
    raw = folder / "frame-00000001.raw"
    raw.write_bytes(raw.read_bytes() + b" ")
    with pytest.raises(ValueError, match="raw frame"):
        newheads.replay_head_schedule(folder, session.schedule_sha256)
