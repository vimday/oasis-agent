from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from oasis_agent.fastpath.bridge import FastpathBridge, FastpathCallError
from oasis_agent.fastpath.pool_graph import PoolKind, PoolNode, scan_cycles
from oasis_agent.fastpath.resident_graph import AnchorScan, GraphBlock, ResidentPoolGraph


def _hash(value: int) -> str:
    return f"0x{value:064x}"


def _pool(pool_id: str, reserve1: int) -> PoolNode:
    return PoolNode(
        8453,
        pool_id,
        PoolKind.V2,
        "token-a",
        "token-b",
        18,
        6,
        3_000,
        block_number=20,
        timestamp_ms=4000,
        reserve0=100_000,
        reserve1=reserve1,
    )


def test_native_batch_matches_single_anchor_scans_across_updates_and_recovery() -> None:
    bridge = FastpathBridge()
    first = GraphBlock(8453, 20, _hash(20), _hash(19))
    second = GraphBlock(8453, 21, _hash(21), first.block_hash)
    fork = GraphBlock(8453, 21, _hash(101), first.block_hash)
    pools = (_pool("pool-a", 200_000), _pool("pool-b", 100_000))
    anchors = (AnchorScan("token-a", 1, 32), AnchorScan("token-b", 1, 64, gas_cost_in_base=3))

    def assert_same(graph: ResidentPoolGraph, block: GraphBlock, state: tuple[PoolNode, ...]):
        reports = graph.scan(block, anchors)
        for anchor, report in zip(anchors, reports, strict=True):
            baseline = scan_cycles(
                state,
                [anchor.base_token],
                min_amount_in=anchor.min_amount_in,
                max_amount_in=anchor.max_amount_in,
                gas_cost_in_base=anchor.gas_cost_in_base,
                bridge=bridge,
            )
            assert replace(report, elapsed_ns=0) == replace(baseline, elapsed_ns=0)
        return reports

    with ResidentPoolGraph(first, pools, bridge=bridge) as graph:
        original = assert_same(graph, first, pools)
        assert all(report.candidates for report in original)
        updated = replace(pools[0], reserve1=90_000, block_number=21, timestamp_ms=4200)
        graph.advance(second, (updated,))
        new = assert_same(graph, second, (updated, pools[1]))
        assert new[0].candidates != original[0].candidates

        with pytest.raises(FastpathCallError):
            graph.scan(fork, anchors)
        with pytest.raises(FastpathCallError, match="full replacement"):
            graph.scan(second, anchors)

        fork_pools = tuple(replace(pool, block_number=21, timestamp_ms=4200) for pool in pools)
        graph.replace(fork, fork_pools)
        recovered = assert_same(graph, fork, fork_pools)
        assert recovered[0].candidates == original[0].candidates
        third = GraphBlock(8453, 22, _hash(102), fork.block_hash)
        graph.advance(third, ())
        assert_same(graph, third, fork_pools)
    graph.close()
    with pytest.raises(FastpathCallError, match="closed"):
        graph.scan(first, anchors)


def test_bad_abi_rejected_before_open_and_bad_hash_rejected_before_io() -> None:
    class OldBridge(FastpathBridge):
        def __init__(self) -> None:
            self.commands: list[object] = []

        def eval_json(self, command: Mapping[str, object]) -> dict[str, Any]:
            self.commands.append(command["cmd"])
            return {"service": "oasis-core", "abi_version": True}

    old = OldBridge()
    with pytest.raises(FastpathCallError, match="ABI"):
        ResidentPoolGraph(GraphBlock(8453, 20, _hash(20), _hash(19)), (), bridge=old)
    assert old.commands == ["graph_session_capabilities"]
    with pytest.raises(ValueError, match="32 hex bytes"):
        GraphBlock(8453, 20, _hash(20)[:30] + "_" + _hash(20)[31:], _hash(19))


def test_native_missing_residuals_invalidates_python_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = FastpathBridge()
    initial = GraphBlock(8453, 20, _hash(20), _hash(19))
    pools = (_pool("pool-a", 200_000),)
    anchor = (AnchorScan("token-a", 1, 32),)
    real = bridge.eval_json

    def malformed(command: Mapping[str, object]) -> dict[str, Any]:
        response = real(command)
        if command["cmd"] == "graph_session_scan":
            response["reports"][0].pop("sizing_residuals")
        return response

    with ResidentPoolGraph(initial, pools, bridge=bridge) as graph:
        assert graph.scan(initial, anchor)[0].sizing_residuals == ()
        with monkeypatch.context() as patch:
            patch.setattr(bridge, "eval_json", malformed)
            with pytest.raises(FastpathCallError, match="omitted sizing coverage"):
                graph.scan(initial, anchor)
        with pytest.raises(FastpathCallError, match="full replacement"):
            graph.scan(initial, anchor)
        graph.replace(initial, pools)
        assert graph.scan(initial, anchor)[0].sizing_residuals == ()
