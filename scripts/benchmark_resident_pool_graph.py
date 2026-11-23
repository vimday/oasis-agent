"""Offline A/B benchmark for per-anchor, resident snapshot, and resident delta scans."""

import argparse
import json
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from oasis_agent.evidence.rpc_validation import (
    strict_digest,
    strict_integer,
    strict_json_object,
    strict_list,
    strict_object,
    strict_string,
)
from oasis_agent.fastpath.bridge import FastpathBridge, FastpathCallError, parse_int_str
from oasis_agent.fastpath.incremental_scanner import BlockPoolSnapshot, _pool_pricing_signature
from oasis_agent.fastpath.multihop_replay import ReplayAnchorInput, load_multihop_replay
from oasis_agent.fastpath.pool_graph import (
    CycleScanReport,
    PoolKind,
    PoolNode,
    TickLiquidity,
    scan_cycles,
)
from oasis_agent.fastpath.resident_graph import AnchorScan, GraphBlock, ResidentPoolGraph
from oasis_agent.models.common import canonical_json, sha256_hex
from scripts.run_multicall_100tick_campaign import (
    USDC_ADDR,
    WETH_ADDR,
    decode_retained_multihop_snapshot,
)

ARMS = ("full_per_anchor", "resident_snapshot_batch", "resident_delta_batch")
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Frame:
    block: GraphBlock
    timestamp_ms: int
    pools: tuple[PoolNode, ...]
    anchors: tuple[AnchorScan, ...]


@dataclass(frozen=True, slots=True)
class Cohort:
    name: str
    frames: tuple[Frame, ...]
    metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class Observation:
    reports: tuple[CycleScanReport, ...]
    delta_ns: int
    apply_ns: int
    scan_ns: int
    update: str
    applied_pool_rows: int
    native_calls: dict[str, int]


@dataclass(frozen=True, slots=True)
class Trial:
    samples: tuple[Observation, ...]
    open_ns: int
    close_ns: int
    native_calls: dict[str, int]
    open_calls: dict[str, int]
    close_calls: dict[str, int]
    open_native_wall_ns: dict[str, int]


class CountingBridge(FastpathBridge):
    """Count the actual eval invocations without duplicating request serialization."""

    def __init__(self, library: Path | None) -> None:
        super().__init__(library)
        self.calls: Counter[str] = Counter()
        self.wall_ns: Counter[str] = Counter()

    def eval_json(self, command: Mapping[str, object]) -> dict[str, Any]:
        label = str(command.get("cmd"))
        self.calls[label] += 1
        started = time.perf_counter_ns()
        try:
            return super().eval_json(command)
        finally:
            self.wall_ns[label] += time.perf_counter_ns() - started


def _read(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected regular benchmark input {path}")
    with path.open("rb") as stream:
        raw = stream.read(MAX_ARTIFACT_BYTES + 1)
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError("benchmark artifact exceeds byte cap")
    return raw


def _anchor(snapshot: BlockPoolSnapshot, max_hops: int) -> AnchorScan:
    if (
        len(snapshot.base_tokens) != 1
        or snapshot.min_amount_in is None
        or snapshot.max_amount_in is None
        or snapshot.min_net_profit is None
        or snapshot.headroom_blocked
    ):
        raise ValueError("benchmark requires original complete, unblocked anchor inputs")
    return AnchorScan(
        base_token=snapshot.base_tokens[0],
        min_amount_in=snapshot.min_amount_in,
        max_amount_in=snapshot.max_amount_in,
        min_net_profit=snapshot.min_net_profit,
        gas_cost_in_base=snapshot.gas_cost_in_base,
        l1_fee_in_base=snapshot.l1_fee_in_base,
        per_hop_gas_cost_in_base=snapshot.per_hop_gas_cost_in_base,
        v2_hop_gas_cost_in_base=snapshot.v2_hop_gas_cost_in_base,
        cl_hop_gas_cost_in_base=snapshot.cl_hop_gas_cost_in_base,
        max_hops=max_hops,
    )


def load_manifest_cohort(manifest: Path) -> Cohort:
    """Replay both anchors through the existing raw-transport validation path."""
    manifest_raw = _read(manifest)
    wire = strict_json_object(manifest_raw)
    loaded = tuple(
        load_multihop_replay(
            manifest, base_token=token, decode_snapshot=decode_retained_multihop_snapshot
        )
        for token in (WETH_ADDR, USDC_ADDR)
    )
    if _read(manifest) != manifest_raw:
        raise ValueError("benchmark manifest changed during replay verification")
    weth, usdc = loaded
    if (
        weth.max_hops != usdc.max_hops
        or weth.metadata["stopped_early"] is not False
        or usdc.metadata["stopped_early"] is not False
        or weth.metadata["manifest_sha256"] != usdc.metadata["manifest_sha256"]
    ):
        raise ValueError("benchmark requires one complete manifest shared by both anchors")

    frames = []
    refs = strict_list(wire["snapshots"], "snapshot references")
    for index, (first, second, reference) in enumerate(
        zip(weth.snapshots, usdc.snapshots, refs, strict=True), start=1
    ):
        raw = _read(manifest.parent / f"snapshot-{index:08d}.json")
        expected = strict_object(reference, "snapshot reference")
        if sha256_hex(raw) != strict_digest(expected["sha256"], "snapshot SHA-256"):
            raise ValueError("retained snapshot no longer matches manifest")
        fields = strict_json_object(raw)
        if (
            first.block_number != second.block_number
            or first.timestamp_ms != second.timestamp_ms
            or first.pools != second.pools
            or fields["block_number"] != first.block_number
            or fields["timestamp_ms"] != first.timestamp_ms
            or fields["pools"] != [pool.to_wire() for pool in first.pools]
        ):
            raise ValueError("anchor datasets disagree on block or pool inputs")
        block = GraphBlock(
            strict_integer(wire["chain_id"], "chain ID"),
            first.block_number,
            strict_string(fields["block_hash"], "block hash"),
            strict_string(fields["parent_hash"], "parent hash"),
        )
        frames.append(
            Frame(
                block,
                first.timestamp_ms,
                first.pools,
                (_anchor(first, weth.max_hops), _anchor(second, weth.max_hops)),
            )
        )
    return Cohort("manifest", tuple(frames), {**weth.metadata, "raw_wire_replayed": True})


def _model_pool(value: object) -> PoolNode:
    raw = strict_object(value, "mixed model pool")
    reserves = {
        name: None if raw.get(name) is None else parse_int_str(raw[name], name)
        for name in ("reserve0", "reserve1", "sqrt_price_x96", "liquidity")
    }
    tick_rows = [strict_object(v, "tick") for v in strict_list(raw["ticks"], "ticks")]
    pool = PoolNode(
        chain_id=strict_integer(raw["chain_id"], "chain ID"),
        pool_id=strict_string(raw["pool_id"], "pool ID"),
        kind=PoolKind(strict_string(raw["kind"], "kind")),
        token0=strict_string(raw["token0"], "token0"),
        token1=strict_string(raw["token1"], "token1"),
        decimals0=strict_integer(raw["decimals0"], "decimals0"),
        decimals1=strict_integer(raw["decimals1"], "decimals1"),
        fee_pips=strict_integer(raw["fee_pips"], "fee"),
        block_number=strict_integer(raw["block_number"], "pool block"),
        timestamp_ms=strict_integer(raw["timestamp_ms"], "pool timestamp"),
        reserve0=reserves["reserve0"],
        reserve1=reserves["reserve1"],
        sqrt_price_x96=reserves["sqrt_price_x96"],
        liquidity=reserves["liquidity"],
        tick=None if raw["tick"] is None else strict_integer(raw["tick"], "active tick"),
        v4_tick_spacing=(
            strict_integer(raw["v4_tick_spacing"], "V4 tick spacing")
            if "v4_tick_spacing" in raw
            else None
        ),
        ticks=tuple(
            TickLiquidity(
                strict_integer(row["tick"], "initialized tick"),
                parse_int_str(row["liquidity_net"], "liquidity_net"),
            )
            for row in tick_rows
        ),
    )
    if pool.to_wire() != raw:
        raise ValueError("mixed pool fails canonical wire round trip")
    return pool


def load_mixed_cohort(path: Path, expected_sha: str) -> Cohort:
    """Check a decoded Base model artifact against a separately supplied SHA-256."""
    raw = _read(path)
    if sha256_hex(raw) != strict_digest(expected_sha, "mixed-state SHA-256"):
        raise ValueError("decoded mixed state hash mismatch")
    body = strict_json_object(raw)
    meta = strict_object(body["block"], "block")
    timestamp = strict_integer(meta["timestamp_ms"], "timestamp")
    block = GraphBlock(
        8453,
        strict_integer(meta["number"], "block height"),
        strict_string(meta["hash"], "block hash"),
        strict_string(meta["parent"], "parent hash"),
    )
    pools = tuple(_model_pool(p) for p in strict_list(body["pools"], "pool states"))
    if not pools or any(
        pool.chain_id != block.chain_id
        or pool.block_number != block.block_number
        or pool.timestamp_ms != timestamp
        for pool in pools
    ):
        raise ValueError("mixed graph pools do not share the decoded state pin")
    records = tuple(
        ReplayAnchorInput.from_wire(row)
        for row in strict_list(body["armed_inputs"], "anchor inputs")
    )
    if tuple(record.base_token for record in records) != (WETH_ADDR, USDC_ADDR):
        raise ValueError("decoded Base mixed state requires WETH and USDC scan inputs")
    args = tuple(AnchorScan(**asdict(record), max_hops=3) for record in records)
    return Cohort(
        "decoded_mixed_state",
        (Frame(block, timestamp, pools, args),),
        {
            "input_sha256": expected_sha,
            "raw_wire_replayed": False,
            "evidence": "separately hashed decoded model state; verify original wire separately",
        },
    )


def positive_control() -> Cohort:
    """Make deterministic positive two/three-hop paths with empty and nonempty updates."""
    token_a, token_b, token_c = (f"0x{value:040x}" for value in (10, 11, 12))
    unit = 10**18
    reserves = (
        (token_a, token_b, 1000 * unit, 1050 * unit),
        (token_a, token_b, 1000 * unit, 1000 * unit),
        (token_b, token_c, 1000 * unit, 1000 * unit),
        (token_a, token_c, 1000 * unit, 1000 * unit),
    )
    pools = tuple(
        PoolNode(
            chain_id=8453,
            pool_id=f"0x{index:040x}",
            kind=PoolKind.V2,
            token0=token0,
            token1=token1,
            decimals0=18,
            decimals1=18,
            fee_pips=1_000,
            reserve0=amount0,
            reserve1=amount1,
        )
        for index, (token0, token1, amount0, amount1) in enumerate(reserves, start=1)
    )
    anchors = (
        AnchorScan(token_a, unit // 1_000, unit, gas_cost_in_base=100, l1_fee_in_base=6),
        AnchorScan(token_b, unit // 100, unit // 2, gas_cost_in_base=250, l1_fee_in_base=42),
    )
    frames = []
    for ordinal, adjustment in enumerate((0, 0, unit, unit)):
        height = 100 + ordinal
        timestamp = 1_795_430_000_000 + ordinal * 200
        block = GraphBlock(8453, height, f"0x{height:064x}", f"0x{height - 1:064x}")
        first = replace(pools[0], reserve1=1050 * unit + adjustment)
        state = tuple(
            replace(pool, block_number=height, timestamp_ms=timestamp)
            for pool in (first, *pools[1:])
        )
        frames.append(Frame(block, timestamp, state, anchors))
    return Cohort("synthetic_positive_control", tuple(frames), {"fixture": True})


def _block_step(previous: Frame, current: Frame) -> str:
    if current.block == previous.block:
        if previous.pools != current.pools:
            raise ValueError("same block has a conflicting pool state")
        return "repeated_snapshot"
    if current.block.chain_id != previous.block.chain_id:
        raise ValueError("unexpected chain change within one benchmark cohort")
    if current.block.block_number <= previous.block.block_number or (
        current.block.block_number == previous.block.block_number + 1
        and current.block.parent_hash != previous.block.block_hash
    ):
        raise ValueError("benchmark cohort regressed or reorged")
    return "advance" if current.block.block_number == previous.block.block_number + 1 else "replace"


def delta_for(previous: Frame, current: Frame) -> tuple[str, tuple[PoolNode, ...]]:
    """Require fresh block identity even when every pool's economic fields are unchanged."""
    operation = _block_step(previous, current)
    if operation != "advance":
        return operation, current.pools if operation == "replace" else ()
    old = {(pool.chain_id, pool.pool_id.lower()): pool for pool in previous.pools}
    new = {(pool.chain_id, pool.pool_id.lower()): pool for pool in current.pools}
    if len(old) != len(previous.pools) or len(new) != len(current.pools):
        raise ValueError("benchmark frame repeats a pool identity")
    if old.keys() != new.keys():
        return "replace", current.pools
    changed = tuple(
        pool
        for key, pool in new.items()
        if _pool_pricing_signature(old[key]) != _pool_pricing_signature(pool)
    )
    return "advance", changed


def _legacy_report(frame: Frame, anchor: AnchorScan, bridge: CountingBridge) -> CycleScanReport:
    settings = asdict(anchor)
    base_token = settings.pop("base_token")
    return scan_cycles(
        frame.pools,
        (base_token,),
        block_number=frame.block.block_number,
        timestamp_ns=frame.timestamp_ms * 1_000_000,
        bridge=bridge,
        **settings,
    )


def run_trial(arm: str, cohort: Cohort, bridge: CountingBridge) -> Trial:
    if arm not in ARMS:
        raise ValueError("unknown benchmark arm")
    calls_at_start = bridge.calls.copy()
    samples = []
    if arm == "full_per_anchor":
        for frame in cohort.frames:
            before = bridge.calls.copy()
            started = time.perf_counter_ns()
            reports = tuple(_legacy_report(frame, anchor, bridge) for anchor in frame.anchors)
            elapsed = time.perf_counter_ns() - started
            samples.append(
                Observation(
                    reports,
                    0,
                    0,
                    elapsed,
                    "full_per_anchor",
                    len(frame.pools) * len(frame.anchors),
                    dict(bridge.calls - before),
                )
            )
        return Trial(tuple(samples), 0, 0, dict(bridge.calls - calls_at_start), {}, {}, {})

    first = cohort.frames[0]
    elapsed_before_open = bridge.wall_ns.copy()
    open_started = time.perf_counter_ns()
    with ResidentPoolGraph(first.block, first.pools, bridge=bridge) as graph:
        open_ns = time.perf_counter_ns() - open_started
        open_calls = dict(bridge.calls - calls_at_start)
        open_native_wall = dict(bridge.wall_ns - elapsed_before_open)
        previous = None
        for frame in cohort.frames:
            before = bridge.calls.copy()
            started = time.perf_counter_ns()
            delta_ns = 0
            apply_ns = 0
            operation, payload = "opened", first.pools
            if previous is not None:
                if arm == "resident_delta_batch":
                    operation, payload = delta_for(previous, frame)
                else:
                    operation = _block_step(previous, frame)
                    if operation == "repeated_snapshot":
                        payload = ()
                    else:
                        operation, payload = "replace", frame.pools
                delta_ns = time.perf_counter_ns() - started
                apply_started = time.perf_counter_ns()
                if operation == "replace":
                    graph.replace(frame.block, payload)
                elif operation == "advance":
                    graph.advance(frame.block, payload)
                apply_ns = time.perf_counter_ns() - apply_started
            scan_started = time.perf_counter_ns()
            reports = graph.scan(frame.block, frame.anchors)
            scan_ns = time.perf_counter_ns() - scan_started
            samples.append(
                Observation(
                    reports,
                    delta_ns,
                    apply_ns,
                    scan_ns,
                    operation,
                    len(payload),
                    dict(bridge.calls - before),
                )
            )
            previous = frame
        close_started = time.perf_counter_ns()
        close_before = bridge.calls.copy()
    return Trial(
        tuple(samples),
        open_ns,
        time.perf_counter_ns() - close_started,
        dict(bridge.calls - calls_at_start),
        open_calls,
        dict(bridge.calls - close_before),
        open_native_wall,
    )


def report_signature(report: CycleScanReport) -> dict[str, object]:
    """Keep all integer fields and exact scan order, including the legs of each candidate."""
    return {
        "cycles_evaluated": report.cycles_evaluated,
        "candidates": [asdict(candidate) for candidate in report.candidates],
        "sizing_residuals": None
        if report.sizing_residuals is None
        else [asdict(residual) for residual in report.sizing_residuals],
    }


def _stats(samples: Sequence[int]) -> dict[str, int] | None:
    if not samples:
        return None
    values = sorted(samples)
    return {
        "samples": len(values),
        "min_ns": values[0],
        "p50_ns": values[(50 * len(values) + 99) // 100 - 1],
        "p95_ns": values[(95 * len(values) + 99) // 100 - 1],
        "p99_ns": values[(99 * len(values) + 99) // 100 - 1],
        "max_ns": values[-1],
    }


def _summary(trials: Sequence[Trial]) -> dict[str, object]:
    samples = [sample for trial in trials for sample in trial.samples]
    followups = [sample for trial in trials for sample in trial.samples[1:]]
    total_calls: Counter[str] = Counter()
    open_calls: Counter[str] = Counter()
    close_calls: Counter[str] = Counter()
    for trial in trials:
        total_calls.update(trial.native_calls)
        open_calls.update(trial.open_calls)
        close_calls.update(trial.close_calls)
    return {
        "open_wall": _stats([trial.open_ns for trial in trials]),
        "close_wall": _stats([trial.close_ns for trial in trials]),
        "all_frame_scan_wall": _stats([sample.scan_ns for sample in samples]),
        "followup_delta_plan_wall": _stats([sample.delta_ns for sample in followups]),
        "followup_apply_wall": _stats([sample.apply_ns for sample in followups]),
        "followup_scan_wall": _stats([sample.scan_ns for sample in followups]),
        "followup_combined_wall": _stats(
            [sample.delta_ns + sample.apply_ns + sample.scan_ns for sample in followups]
        ),
        "all_frame_native_reported_scan_wall": _stats(
            [
                sum(report.elapsed_ns for report in sample.reports)
                if sample.update == "full_per_anchor"
                else sample.reports[0].elapsed_ns
                for sample in samples
            ]
        ),
        "initial_open_and_scan_wall": _stats(
            [trial.open_ns + trial.samples[0].scan_ns for trial in trials]
        ),
        "native_calls": dict(total_calls),
        "open_native_calls": dict(open_calls),
        "open_native_wall_by_command": {
            command: _stats([trial.open_native_wall_ns[command] for trial in trials])
            for command in open_calls
        },
        "close_native_calls": dict(close_calls),
        "first_trial_frames": [
            {
                "transition": row.update,
                "pool_rows_submitted": row.applied_pool_rows,
                "native_calls": row.native_calls,
            }
            for row in trials[0].samples
        ],
    }


def benchmark(
    cohort: Cohort, bridge: CountingBridge, *, repetitions: int, warmups: int
) -> dict[str, object]:
    if not cohort.frames:
        raise ValueError("benchmark dataset has no frames")
    trials = {arm: [] for arm in ARMS}
    for repetition in range(repetitions + warmups):
        # Rotate first-arm position to reduce one-sided allocator or cache warmup bias.
        offset = repetition % len(ARMS)
        for arm in ARMS[offset:] + ARMS[:offset]:
            trial = run_trial(arm, cohort, bridge)
            if repetition >= warmups:
                trials[arm].append(trial)

    baseline = trials["full_per_anchor"][0]
    mismatches = []
    unknown_sizing = 0
    frames = []
    for idx, frame in enumerate(cohort.frames):
        expected = tuple(report_signature(report) for report in baseline.samples[idx].reports)
        unknown_sizing += sum(item["sizing_residuals"] is None for item in expected)
        frames.append(
            {
                "block": asdict(frame.block),
                "pool_count": len(frame.pools),
                "anchors": [
                    {
                        "asset": anchor.base_token,
                        "cycles_evaluated": report.cycles_evaluated,
                        "candidate_count": len(report.candidates),
                        "candidate_legs": sum(
                            len(candidate.hops) for candidate in report.candidates
                        ),
                        "residual_count": (
                            len(report.sizing_residuals)
                            if report.sizing_residuals is not None
                            else None
                        ),
                        "output_sha256": sha256_hex(canonical_json(signature)),
                    }
                    for anchor, report, signature in zip(
                        frame.anchors, baseline.samples[idx].reports, expected, strict=True
                    )
                ],
            }
        )
        for arm, observations in trials.items():
            for repetition, trial in enumerate(observations):
                actual = tuple(report_signature(r) for r in trial.samples[idx].reports)
                if actual != expected:
                    mismatches.append(
                        {
                            "arm": arm,
                            "repetition": repetition,
                            "block": frame.block.block_number,
                            "expected_output_sha256": sha256_hex(canonical_json(expected)),
                            "actual_output_sha256": sha256_hex(canonical_json(actual)),
                        }
                    )
    return {
        "cohort": cohort.name,
        "provenance": cohort.metadata,
        "frames": frames,
        "positive_cases_available": any(
            sample.candidates for row in baseline.samples for sample in row.reports
        ),
        "unknown_sizing_coverage": unknown_sizing,
        "all_outputs_equal": not mismatches,
        "mismatches": mismatches,
        "arms": {arm: _summary(observations) for arm, observations in trials.items()},
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-manifest", type=Path, required=True)
    parser.add_argument("--mixed-state", type=Path)
    parser.add_argument("--mixed-state-sha256")
    parser.add_argument("--library", type=Path)
    parser.add_argument("--positive-control", action="store_true")
    parser.add_argument("--repetitions", type=int, default=60)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.repetitions <= 200 or not 0 <= args.warmups <= 20:
        parser.error("repetitions must be 1..200 and warmups 0..20")
    if (args.mixed_state is None) != (args.mixed_state_sha256 is None):
        parser.error("mixed state and its independently retained SHA-256 are required together")
    if args.out is not None and (
        args.out.is_symlink()
        or args.out.resolve().is_relative_to(args.snapshot_manifest.resolve().parent)
        or (
            args.mixed_state is not None
            and args.out.resolve().is_relative_to(args.mixed_state.resolve().parent)
        )
    ):
        parser.error("benchmark output must be outside the retained evidence directories")
    try:
        start = time.perf_counter_ns()
        cohorts = [load_manifest_cohort(args.snapshot_manifest)]
        if args.mixed_state is not None:
            cohorts.append(load_mixed_cohort(args.mixed_state, args.mixed_state_sha256))
        if args.positive_control:
            cohorts.append(positive_control())
        loaded_ns = time.perf_counter_ns() - start
        bridge = CountingBridge(args.library)
        started = time.perf_counter_ns()
        info = bridge.ping()
        capabilities = bridge.eval_json({"cmd": "graph_session_capabilities"})
        handshake_ns = time.perf_counter_ns() - started
        handshake_calls = dict(bridge.calls)
        results = [
            benchmark(cohort, bridge, repetitions=args.repetitions, warmups=args.warmups)
            for cohort in cohorts
        ]
        report = {
            "schema": "oasis.resident-pool-graph-benchmark.v1",
            "input_load_and_check_ns": loaded_ns,
            "initial_handshake_ns": handshake_ns,
            "initial_handshake_native_calls": handshake_calls,
            "native_version": info,
            "native_resident_capabilities": capabilities,
            "native_library_sha256": sha256_hex(bridge.lib_path.read_bytes()),
            "repetitions": args.repetitions,
            "warmups": args.warmups,
            "datasets": results,
            "all_outputs_equal": all(result["all_outputs_equal"] is True for result in results),
            "positive_control_met": results[-1]["positive_cases_available"]
            if args.positive_control
            else None,
            "rpc_calls": 0,
            "execution_eligible": False,
        }
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.out is not None:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            with args.out.open("x", encoding="utf-8") as stream:
                stream.write(rendered)
        sys.stdout.write(rendered)
    except (OSError, ValueError, TypeError, FastpathCallError) as exc:
        print(f"resident benchmark failed: {exc}", file=sys.stderr)
        return 2
    return 0 if report["all_outputs_equal"] and report["positive_control_met"] is not False else 3


if __name__ == "__main__":
    raise SystemExit(main())
