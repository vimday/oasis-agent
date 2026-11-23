"""Incremental changed-pools cycle scanner and pre-registered Arm A vs Arm B evaluator."""

import math
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, Final, Literal

from .bridge import FastpathBridge, require_int
from .pool_graph import (
    CycleCandidate,
    CycleSizingResidual,
    PoolKind,
    PoolNode,
    TickLiquidity,
    scan_cycles,
)

AB_EXPERIMENT_SCHEMA: Final[str] = "oasis.multihop-ab-experiment.v2"

FallbackReason = Literal[
    "initial_scan",
    "explicit_force_full_scan",
    "scan_parameters_changed",
    "pool_topology_changed",
    "periodic_checkpoint",
]


@dataclass(frozen=True, slots=True)
class IncrementalCycleScanResult:
    """Single-block scan result from `IncrementalCycleScanner` with provenance metadata."""

    candidates: tuple[CycleCandidate, ...]
    cycles_evaluated: int
    rust_elapsed_ns: int
    wall_elapsed_ns: int
    used_full_scan: bool
    fallback_reason: FallbackReason | None
    changed_pools: tuple[str, ...]
    retained_cached_candidates: int
    fresh_candidates: int
    sizing_residuals: tuple[CycleSizingResidual, ...] | None = None


@dataclass(frozen=True, slots=True)
class BlockPoolSnapshot:
    """Per-block pool universe and gas inputs for offline A/B comparison."""

    block_number: int
    timestamp_ms: int
    pools: tuple[PoolNode, ...]
    base_tokens: tuple[str, ...]
    gas_cost_in_base: int
    l1_fee_in_base: int
    per_hop_gas_cost_in_base: int = 0
    v2_hop_gas_cost_in_base: int | None = None
    cl_hop_gas_cost_in_base: int | None = None
    explicit_changed_pools: tuple[str, ...] | None = None
    min_amount_in: int | None = None
    max_amount_in: int | None = None
    min_net_profit: int | None = None
    force_full_scan: bool = False
    headroom_blocked: bool = False


@dataclass(frozen=True, slots=True)
class MultihopAbArmMetrics:
    """Aggregated latency and scan-volume metrics for one arm of the A/B experiment."""

    arm: str
    blocks_scanned: int
    total_cycles_evaluated: int
    total_candidates: int
    full_scan_blocks: int
    incremental_blocks: int
    mean_ns: int
    p50_ns: int
    p95_ns: int
    p99_ns: int
    max_ns: int
    first_block_ns: int
    subsequent_p99_ns: int | None

    def to_dict(self) -> dict[str, Any]:
        """Serialize arm metrics deterministically."""
        return {
            "arm": self.arm,
            "blocks_scanned": self.blocks_scanned,
            "total_cycles_evaluated": self.total_cycles_evaluated,
            "total_candidates": self.total_candidates,
            "full_scan_blocks": self.full_scan_blocks,
            "incremental_blocks": self.incremental_blocks,
            "mean_ns": self.mean_ns,
            "p50_ns": self.p50_ns,
            "p95_ns": self.p95_ns,
            "p99_ns": self.p99_ns,
            "max_ns": self.max_ns,
            "first_block_ns": self.first_block_ns,
            "subsequent_p99_ns": self.subsequent_p99_ns,
        }


@dataclass(frozen=True, slots=True)
class MultihopAbExperimentReport:
    """Pre-registered Arm A (Full CPU) vs Arm B (Incremental CPU) offline A/B report."""

    hypothesis_a_passed: bool
    min_p99_reduction_ratio: float
    p99_reduction_ratio: float
    cycles_evaluated_reduction_ratio: float
    recall: float | None
    missed_candidates_count: int
    sizing_unknown_blocks: int
    sizing_residual_mismatch_blocks: int
    full_scan_open_interval_count: int
    missed_net_profit_raw: int | None
    missed_net_profit_by_asset: dict[tuple[int, str, int], int]
    false_positive_count: int
    exact_bit_for_bit_blocks: int
    total_blocks: int
    changed_pools_per_block: int
    full_scan_every_n_blocks: int
    max_hops: int
    deduplicate_cycles: bool
    seed: int
    arm_a: MultihopAbArmMetrics
    arm_b: MultihopAbArmMetrics
    schema: str = AB_EXPERIMENT_SCHEMA
    execution_eligible: Literal[False] = False
    release_permitted: Literal[False] = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize A/B experiment report as JSON-safe dictionary."""
        return {
            "schema": self.schema,
            "execution_eligible": self.execution_eligible,
            "release_permitted": self.release_permitted,
            "hypothesis_a_passed": self.hypothesis_a_passed,
            "min_p99_reduction_ratio": self.min_p99_reduction_ratio,
            "p99_reduction_ratio": self.p99_reduction_ratio,
            "cycles_evaluated_reduction_ratio": self.cycles_evaluated_reduction_ratio,
            "recall": self.recall,
            "recall_scope": "same_model_candidate_agreement",
            "timing_scope": "in_memory_pools_to_candidate_scan_return",
            "arm_order": "alternating_ab_ba",
            "whole_transaction_recall": None,
            "sizing_unknown_blocks": self.sizing_unknown_blocks,
            "sizing_residual_mismatch_blocks": self.sizing_residual_mismatch_blocks,
            "full_scan_open_interval_count": self.full_scan_open_interval_count,
            "missed_candidates_count": self.missed_candidates_count,
            "missed_net_profit_raw": self.missed_net_profit_raw,
            "missed_net_profit_by_asset": [
                {"chain_id": chain, "token": token, "decimals": decimals, "net_raw": str(amount)}
                for (chain, token, decimals), amount in sorted(
                    self.missed_net_profit_by_asset.items()
                )
            ],
            "false_positive_count": self.false_positive_count,
            "exact_bit_for_bit_blocks": self.exact_bit_for_bit_blocks,
            "total_blocks": self.total_blocks,
            "changed_pools_per_block": self.changed_pools_per_block,
            "full_scan_every_n_blocks": self.full_scan_every_n_blocks,
            "max_hops": self.max_hops,
            "deduplicate_cycles": self.deduplicate_cycles,
            "seed": self.seed,
            "arm_a": self.arm_a.to_dict(),
            "arm_b": self.arm_b.to_dict(),
        }


def _pool_pricing_signature(pool: PoolNode) -> tuple[object, ...]:
    sorted_ticks = tuple(sorted((t.tick, t.liquidity_net) for t in pool.ticks))
    return (
        pool.chain_id,
        pool.pool_id.strip().lower(),
        pool.kind.value,
        pool.token0.strip().lower(),
        pool.token1.strip().lower(),
        pool.decimals0,
        pool.decimals1,
        pool.fee_pips,
        pool.reserve0,
        pool.reserve1,
        pool.sqrt_price_x96,
        pool.tick,
        pool.liquidity,
        pool.v4_tick_spacing,
        sorted_ticks,
    )


def _candidate_sort_key(cand: CycleCandidate) -> tuple[object, ...]:
    return (
        -cand.net_profit,
        cand.chain_id,
        cand.base_token,
        len(cand.hops),
        tuple((h.pool_id, h.token_in, h.token_out) for h in cand.hops),
    )


def _candidate_identity(cand: CycleCandidate) -> tuple[object, ...]:
    return (
        cand.chain_id,
        cand.base_token,
        tuple((h.pool_id, h.token_in, h.token_out) for h in cand.hops),
    )


class IncrementalCycleScanner:
    """Stateful incremental cycle scanner combining cache retention with Rust `changed_pools`."""

    def __init__(
        self,
        *,
        bridge: FastpathBridge | None = None,
        full_scan_every_n_blocks: int = 200,
    ) -> None:
        checked_every = require_int(
            full_scan_every_n_blocks, "full_scan_every_n_blocks", non_negative=True
        )
        self._bridge = bridge if bridge is not None else FastpathBridge()
        self._full_scan_every_n_blocks = checked_every
        self._initialized = False
        self._prev_pool_sigs: dict[tuple[int, str], tuple[object, ...]] = {}
        self._prev_params_key: tuple[object, ...] | None = None
        self._cached_candidates: tuple[CycleCandidate, ...] = ()
        self._cached_sizing_residuals: tuple[CycleSizingResidual, ...] | None = None
        self._steps_since_full_scan = 0

    def reset(self) -> None:
        """Clear cached pool signatures, scan parameters, and candidate cache."""
        self._initialized = False
        self._prev_pool_sigs.clear()
        self._prev_params_key = None
        self._cached_candidates = ()
        self._cached_sizing_residuals = None
        self._steps_since_full_scan = 0

    def _determine_fallback_reason(
        self,
        *,
        force_full_scan: bool,
        params_key: tuple[object, ...],
        current_sigs: dict[tuple[int, str], tuple[object, ...]],
    ) -> FallbackReason | None:
        if force_full_scan:
            return "explicit_force_full_scan"
        if not self._initialized:
            return "initial_scan"
        if params_key != self._prev_params_key:
            return "scan_parameters_changed"
        if set(current_sigs) != set(self._prev_pool_sigs):
            return "pool_topology_changed"
        if (
            self._full_scan_every_n_blocks > 0
            and (self._steps_since_full_scan + 1) >= self._full_scan_every_n_blocks
        ):
            return "periodic_checkpoint"
        return None

    def scan_block(
        self,
        pools: Sequence[PoolNode],
        base_tokens: Sequence[str],
        *,
        max_hops: int = 3,
        min_amount_in: int,
        max_amount_in: int,
        gas_cost_in_base: int = 0,
        l1_fee_in_base: int = 0,
        min_net_profit: int = 1,
        per_hop_gas_cost_in_base: int = 0,
        v2_hop_gas_cost_in_base: int | None = None,
        cl_hop_gas_cost_in_base: int | None = None,
        deduplicate_cycles: bool = False,
        explicit_changed_pools: Sequence[str] | None = None,
        force_full_scan: bool = False,
        audit_ring_path: Any = None,
        block_number: int | None = None,
        timestamp_ns: int | None = None,
    ) -> IncrementalCycleScanResult:
        """Scan one block snapshot, incrementally evaluating only changed-pool cycles when safe."""
        t0 = time.perf_counter_ns()
        if type(force_full_scan) is not bool:
            raise TypeError("force_full_scan must be a bool")
        if type(deduplicate_cycles) is not bool:
            raise TypeError("deduplicate_cycles must be a bool")

        current_sigs: dict[tuple[int, str], tuple[object, ...]] = {}
        for pool in pools:
            norm_key = (pool.chain_id, pool.pool_id.strip().lower())
            sig = _pool_pricing_signature(pool)
            existing = current_sigs.get(norm_key)
            if existing is not None and existing != sig:
                raise ValueError(f"conflicting pool snapshot in block for {norm_key}")
            current_sigs[norm_key] = sig

        params_key: tuple[object, ...] = (
            tuple(sorted({tok.strip().lower() for tok in base_tokens})),
            require_int(max_hops, "max_hops", non_negative=True),
            require_int(min_amount_in, "min_amount_in", non_negative=True),
            require_int(max_amount_in, "max_amount_in", non_negative=True),
            require_int(gas_cost_in_base, "gas_cost_in_base", non_negative=True),
            require_int(l1_fee_in_base, "l1_fee_in_base", non_negative=True),
            require_int(min_net_profit, "min_net_profit", non_negative=True),
            require_int(per_hop_gas_cost_in_base, "per_hop_gas_cost_in_base", non_negative=True),
            require_int(v2_hop_gas_cost_in_base, "v2_hop_gas_cost_in_base", non_negative=True)
            if v2_hop_gas_cost_in_base is not None
            else None,
            require_int(cl_hop_gas_cost_in_base, "cl_hop_gas_cost_in_base", non_negative=True)
            if cl_hop_gas_cost_in_base is not None
            else None,
            deduplicate_cycles,
        )

        fallback_reason = self._determine_fallback_reason(
            force_full_scan=force_full_scan,
            params_key=params_key,
            current_sigs=current_sigs,
        )

        if fallback_reason is not None:
            report = scan_cycles(
                pools,
                base_tokens,
                max_hops=max_hops,
                min_amount_in=min_amount_in,
                max_amount_in=max_amount_in,
                gas_cost_in_base=gas_cost_in_base,
                l1_fee_in_base=l1_fee_in_base,
                min_net_profit=min_net_profit,
                per_hop_gas_cost_in_base=per_hop_gas_cost_in_base,
                v2_hop_gas_cost_in_base=v2_hop_gas_cost_in_base,
                cl_hop_gas_cost_in_base=cl_hop_gas_cost_in_base,
                deduplicate_cycles=deduplicate_cycles,
                changed_pools=None,
                audit_ring_path=audit_ring_path,
                block_number=block_number,
                timestamp_ns=timestamp_ns,
                bridge=self._bridge,
            )
            wall_ns = time.perf_counter_ns() - t0
            self._initialized = True
            self._prev_pool_sigs = current_sigs
            self._prev_params_key = params_key
            self._cached_candidates = report.candidates
            self._cached_sizing_residuals = report.sizing_residuals
            self._steps_since_full_scan = 0
            return IncrementalCycleScanResult(
                candidates=report.candidates,
                cycles_evaluated=report.cycles_evaluated,
                rust_elapsed_ns=report.elapsed_ns,
                wall_elapsed_ns=wall_ns,
                used_full_scan=True,
                fallback_reason=fallback_reason,
                changed_pools=(),
                retained_cached_candidates=0,
                fresh_candidates=len(report.candidates),
                sizing_residuals=report.sizing_residuals,
            )

        changed_ids = {
            key[1] for key, sig in current_sigs.items() if self._prev_pool_sigs[key] != sig
        }
        if explicit_changed_pools is not None:
            if isinstance(explicit_changed_pools, str):
                raise TypeError("explicit_changed_pools must be a sequence of strings")
            for pid in explicit_changed_pools:
                if not isinstance(pid, str):
                    raise TypeError("explicit_changed_pools items must be strings")
                stripped = pid.strip().lower()
                if stripped:
                    changed_ids.add(stripped)

        sorted_changed = tuple(sorted(changed_ids))
        retained = tuple(
            c
            for c in self._cached_candidates
            if all(h.pool_id.lower() not in changed_ids for h in c.hops)
        )

        if not changed_ids:
            cycles_evaluated = 0
            rust_elapsed_ns = 0
            fresh: tuple[CycleCandidate, ...] = ()
            merged = self._cached_candidates
            merged_residuals = self._cached_sizing_residuals
        else:
            inc_report = scan_cycles(
                pools,
                base_tokens,
                max_hops=max_hops,
                min_amount_in=min_amount_in,
                max_amount_in=max_amount_in,
                gas_cost_in_base=gas_cost_in_base,
                l1_fee_in_base=l1_fee_in_base,
                min_net_profit=min_net_profit,
                per_hop_gas_cost_in_base=per_hop_gas_cost_in_base,
                v2_hop_gas_cost_in_base=v2_hop_gas_cost_in_base,
                cl_hop_gas_cost_in_base=cl_hop_gas_cost_in_base,
                deduplicate_cycles=deduplicate_cycles,
                changed_pools=sorted_changed,
                audit_ring_path=audit_ring_path,
                block_number=block_number,
                timestamp_ns=timestamp_ns,
                bridge=self._bridge,
            )
            cycles_evaluated = inc_report.cycles_evaluated
            rust_elapsed_ns = inc_report.elapsed_ns
            fresh = inc_report.candidates
            merged = tuple(sorted((*retained, *fresh), key=_candidate_sort_key))
            if self._cached_sizing_residuals is None or inc_report.sizing_residuals is None:
                merged_residuals = None
            else:
                retained_residuals = tuple(
                    residual
                    for residual in self._cached_sizing_residuals
                    if all(pool_id.lower() not in changed_ids for pool_id in residual.pool_ids)
                )
                merged_residuals = retained_residuals + inc_report.sizing_residuals

        wall_ns = time.perf_counter_ns() - t0
        self._prev_pool_sigs = current_sigs
        self._cached_candidates = merged
        self._cached_sizing_residuals = merged_residuals
        self._steps_since_full_scan += 1

        return IncrementalCycleScanResult(
            candidates=merged,
            cycles_evaluated=cycles_evaluated,
            rust_elapsed_ns=rust_elapsed_ns,
            wall_elapsed_ns=wall_ns,
            used_full_scan=False,
            fallback_reason=None,
            changed_pools=sorted_changed,
            retained_cached_candidates=len(retained),
            fresh_candidates=len(fresh),
            sizing_residuals=merged_residuals,
        )


def _percentile_int(sorted_vals: Sequence[int], pct: float) -> int:
    if not sorted_vals:
        return 0
    if len(sorted_vals) == 1:
        return int(sorted_vals[0])
    pos = (len(sorted_vals) - 1) * pct
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return int(sorted_vals[lo])
    frac = pos - lo
    return round(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac)


def build_benchmark_34pool_universe(
    bridge: FastpathBridge | None = None,
) -> tuple[tuple[PoolNode, ...], tuple[str, ...]]:
    """Build a synthetic 34-pool, 6-token Base universe matching Section 6 benchmark sizing."""
    active_bridge = bridge if bridge is not None else FastpathBridge()
    tokens = tuple(f"0x{i:02x}" * 20 for i in range(1, 7))
    # Pre-compute aligned and mild dispersion ticks so realistic 2-to-4-hop cycles fire.
    sqrt_at_tick = {t: active_bridge.sqrt_price_at_tick(t) for t in (-30, -18, 0, 18, 28, 36)}
    pools: list[PoolNode] = []
    pool_counter = 1

    def _next_id(*, is_v4: bool) -> str:
        nonlocal pool_counter
        pid = f"0x{pool_counter:064x}" if is_v4 else f"0x{pool_counter:040x}"
        pool_counter += 1
        return pid

    for i in range(len(tokens)):
        for j in range(i + 1, len(tokens)):
            # Mild alternating V2 reserve skew (+/- 0.25%) models realistic cross-pair spreads.
            skew = (
                1_002_500_000_000_000_000_000 if (i + j) % 2 == 0 else 997_500_000_000_000_000_000
            )
            pools.append(
                PoolNode(
                    chain_id=8453,
                    pool_id=_next_id(is_v4=False),
                    kind=PoolKind.V2,
                    token0=tokens[i],
                    token1=tokens[j],
                    decimals0=18,
                    decimals1=18,
                    fee_pips=3000,
                    reserve0=1_000_000_000_000_000_000_000,
                    reserve1=skew,
                    block_number=22_000_000,
                    timestamp_ms=1_794_000_000_000,
                )
            )
            cl_kind = (PoolKind.V3, PoolKind.SLIPSTREAM, PoolKind.V4)[(i + j) % 3]
            cl_tick = 18 if (i + j) % 3 == 0 else (-18 if (i + j) % 3 == 1 else 0)
            pools.append(
                PoolNode(
                    chain_id=8453,
                    pool_id=_next_id(is_v4=(cl_kind is PoolKind.V4)),
                    kind=cl_kind,
                    token0=tokens[i],
                    token1=tokens[j],
                    decimals0=18,
                    decimals1=18,
                    fee_pips=1500,
                    sqrt_price_x96=sqrt_at_tick[cl_tick],
                    tick=cl_tick,
                    liquidity=500_000_000_000_000_000_000,
                    ticks=(
                        TickLiquidity(tick=-120, liquidity_net=100_000_000_000_000_000_000),
                        TickLiquidity(tick=120, liquidity_net=-100_000_000_000_000_000_000),
                    ),
                    block_number=22_000_000,
                    timestamp_ms=1_794_000_000_000,
                )
            )

    for a, b, tick_val, fee in ((0, 1, 28, 100), (0, 1, 0, 500), (2, 3, 36, 500), (3, 4, -30, 500)):
        pools.append(
            PoolNode(
                chain_id=8453,
                pool_id=_next_id(is_v4=False),
                kind=PoolKind.SLIPSTREAM,
                token0=tokens[a],
                token1=tokens[b],
                decimals0=18,
                decimals1=18,
                fee_pips=fee,
                sqrt_price_x96=sqrt_at_tick[tick_val],
                tick=tick_val,
                liquidity=5_000_000_000_000_000_000_000,
                block_number=22_000_000,
                timestamp_ms=1_794_000_000_000,
            )
        )
    return tuple(pools), (tokens[0],)


def generate_34pool_block_snapshots(
    *,
    num_blocks: int = 120,
    changed_pools_per_block: int = 1,
    seed: int = 20261122,
    bridge: FastpathBridge | None = None,
) -> tuple[BlockPoolSnapshot, ...]:
    """Generate deterministic fixed-block 34-pool snapshots with bounded per-block mutations."""
    checked_blocks = require_int(num_blocks, "num_blocks", non_negative=True)
    if checked_blocks < 1:
        raise ValueError("num_blocks must be >= 1")
    checked_changed = require_int(
        changed_pools_per_block, "changed_pools_per_block", non_negative=True
    )
    active_bridge = bridge if bridge is not None else FastpathBridge()
    base_pools, base_tokens = build_benchmark_34pool_universe(active_bridge)
    if checked_changed > len(base_pools):
        raise ValueError("changed_pools_per_block cannot exceed pool count")

    rng = random.Random(seed)
    sqrt_cache: dict[int, int] = {}

    def _sqrt(t: int) -> int:
        val = sqrt_cache.get(t)
        if val is None:
            val = active_bridge.sqrt_price_at_tick(t)
            sqrt_cache[t] = val
        return val

    mutable = list(base_pools)
    snapshots: list[BlockPoolSnapshot] = []
    gas_l2 = 100_000_000_000_000
    gas_l1 = 50_000_000_000_000
    v2_hop_gas = 150_000_000_000_000
    cl_hop_gas = 250_000_000_000_000

    for offset in range(checked_blocks):
        block_num = 22_000_000 + offset
        ts_ms = 1_794_000_000_000 + offset * 200
        if offset > 0 and checked_changed > 0:
            chosen = rng.sample(range(len(mutable)), checked_changed)
            for idx in chosen:
                old = mutable[idx]
                if (
                    old.kind is PoolKind.V2
                    and old.reserve0 is not None
                    and old.reserve1 is not None
                ):
                    delta = rng.choice((-3, -2, 2, 3)) * 10**18
                    mutable[idx] = PoolNode(
                        chain_id=old.chain_id,
                        pool_id=old.pool_id,
                        kind=old.kind,
                        token0=old.token0,
                        token1=old.token1,
                        decimals0=old.decimals0,
                        decimals1=old.decimals1,
                        fee_pips=old.fee_pips,
                        reserve0=old.reserve0,
                        reserve1=max(10**18, old.reserve1 + delta),
                        block_number=block_num,
                        timestamp_ms=ts_ms,
                    )
                elif old.tick is not None:
                    new_tick = max(-60, min(60, old.tick + rng.choice((-4, -2, 2, 4))))
                    mutable[idx] = PoolNode(
                        chain_id=old.chain_id,
                        pool_id=old.pool_id,
                        kind=old.kind,
                        token0=old.token0,
                        token1=old.token1,
                        decimals0=old.decimals0,
                        decimals1=old.decimals1,
                        fee_pips=old.fee_pips,
                        sqrt_price_x96=_sqrt(new_tick),
                        tick=new_tick,
                        liquidity=old.liquidity,
                        ticks=old.ticks,
                        block_number=block_num,
                        timestamp_ms=ts_ms,
                    )
        snapshots.append(
            BlockPoolSnapshot(
                block_number=block_num,
                timestamp_ms=ts_ms,
                pools=tuple(mutable),
                base_tokens=base_tokens,
                gas_cost_in_base=gas_l2,
                l1_fee_in_base=gas_l1,
                per_hop_gas_cost_in_base=v2_hop_gas,
                v2_hop_gas_cost_in_base=v2_hop_gas,
                cl_hop_gas_cost_in_base=cl_hop_gas,
            )
        )
    return tuple(snapshots)


def _time_scan[T](scan: Callable[[], T]) -> tuple[T, int]:
    start = time.perf_counter_ns()
    result = scan()
    return result, time.perf_counter_ns() - start


def run_multihop_ab_experiment(
    snapshots: Sequence[BlockPoolSnapshot] | None = None,
    *,
    num_blocks: int = 120,
    changed_pools_per_block: int = 1,
    max_hops: int = 4,
    min_amount_in: int = 10**16,
    max_amount_in: int = 10**19,
    min_net_profit: int = 10**14,
    deduplicate_cycles: bool = True,
    full_scan_every_n_blocks: int = 200,
    min_p99_reduction_ratio: float = 0.50,
    seed: int = 20261122,
    bridge: FastpathBridge | None = None,
) -> MultihopAbExperimentReport:
    """Execute pre-registered Arm A (Full CPU) vs Arm B (Incremental CPU) A/B comparison."""
    active_bridge = bridge if bridge is not None else FastpathBridge()
    active_snapshots = (
        tuple(snapshots)
        if snapshots is not None
        else generate_34pool_block_snapshots(
            num_blocks=num_blocks,
            changed_pools_per_block=changed_pools_per_block,
            seed=seed,
            bridge=active_bridge,
        )
    )
    if not active_snapshots:
        raise ValueError("snapshots cannot be empty")

    scanner = IncrementalCycleScanner(
        bridge=active_bridge,
        full_scan_every_n_blocks=full_scan_every_n_blocks,
    )

    arm_a_durations: list[int] = []
    arm_b_durations: list[int] = []
    arm_a_evals = 0
    arm_b_evals = 0
    arm_a_cands_total = 0
    arm_b_cands_total = 0
    arm_b_full_blocks = 0
    arm_b_inc_blocks = 0

    exact_blocks = 0
    sizing_unknown_blocks = 0
    sizing_mismatches = 0
    open_intervals = 0
    missed_count = 0
    missed_by_asset: dict[tuple[int, str, int], int] = {}
    fp_count = 0

    for block_index, snap in enumerate(active_snapshots):
        block_min_in = min_amount_in if snap.min_amount_in is None else snap.min_amount_in
        block_max_in = max_amount_in if snap.max_amount_in is None else snap.max_amount_in
        block_min_profit = min_net_profit if snap.min_net_profit is None else snap.min_net_profit
        scan_a = partial(
            scan_cycles,
            snap.pools,
            snap.base_tokens,
            max_hops=max_hops,
            min_amount_in=block_min_in,
            max_amount_in=block_max_in,
            gas_cost_in_base=snap.gas_cost_in_base,
            l1_fee_in_base=snap.l1_fee_in_base,
            min_net_profit=block_min_profit,
            per_hop_gas_cost_in_base=snap.per_hop_gas_cost_in_base,
            v2_hop_gas_cost_in_base=snap.v2_hop_gas_cost_in_base,
            cl_hop_gas_cost_in_base=snap.cl_hop_gas_cost_in_base,
            deduplicate_cycles=deduplicate_cycles,
            block_number=snap.block_number,
            timestamp_ns=snap.timestamp_ms * 1_000_000,
            bridge=active_bridge,
        )
        scan_b = partial(
            scanner.scan_block,
            snap.pools,
            snap.base_tokens,
            max_hops=max_hops,
            min_amount_in=block_min_in,
            max_amount_in=block_max_in,
            gas_cost_in_base=snap.gas_cost_in_base,
            l1_fee_in_base=snap.l1_fee_in_base,
            min_net_profit=block_min_profit,
            per_hop_gas_cost_in_base=snap.per_hop_gas_cost_in_base,
            v2_hop_gas_cost_in_base=snap.v2_hop_gas_cost_in_base,
            cl_hop_gas_cost_in_base=snap.cl_hop_gas_cost_in_base,
            deduplicate_cycles=deduplicate_cycles,
            explicit_changed_pools=snap.explicit_changed_pools,
            force_full_scan=snap.force_full_scan,
            block_number=snap.block_number,
            timestamp_ns=snap.timestamp_ms * 1_000_000,
        )
        if block_index % 2 == 0:
            res_a, wall_a = _time_scan(scan_a)
            res_b, wall_b = _time_scan(scan_b)
        else:
            res_b, wall_b = _time_scan(scan_b)
            res_a, wall_a = _time_scan(scan_a)
        # The campaign still scans for diagnostics when headroom is blocked,
        # then excludes every candidate from ranking and quote admission.
        candidates_a = () if snap.headroom_blocked else res_a.candidates
        candidates_b = () if snap.headroom_blocked else res_b.candidates
        arm_a_durations.append(wall_a)
        arm_a_evals += res_a.cycles_evaluated
        arm_a_cands_total += len(candidates_a)
        arm_b_durations.append(wall_b)
        arm_b_evals += res_b.cycles_evaluated
        arm_b_cands_total += len(candidates_b)
        if res_b.used_full_scan:
            arm_b_full_blocks += 1
        else:
            arm_b_inc_blocks += 1

        if res_a.sizing_residuals is not None:
            open_intervals += len(res_a.sizing_residuals)
        if res_a.sizing_residuals is None or res_b.sizing_residuals is None:
            sizing_unknown_blocks += 1
        elif set(res_a.sizing_residuals) != set(res_b.sizing_residuals):
            sizing_mismatches += 1
        if candidates_b == candidates_a:
            exact_blocks += 1
        map_a = {_candidate_identity(c): c for c in candidates_a}
        map_b = {_candidate_identity(c): c for c in candidates_b}
        for ident, cand_a in map_a.items():
            cand_b = map_b.get(ident)
            if cand_b is None or cand_b != cand_a:
                missed_count += 1
                token = cand_a.base_token.lower()
                decimals = {
                    d
                    for pool in snap.pools
                    if pool.chain_id == cand_a.chain_id
                    for asset, d in ((pool.token0, pool.decimals0), (pool.token1, pool.decimals1))
                    if asset.lower() == token
                }
                if len(decimals) != 1:
                    raise ValueError("missing or conflicting anchor decimals in missed candidate")
                asset_key = (cand_a.chain_id, token, decimals.pop())
                missed_by_asset[asset_key] = missed_by_asset.get(asset_key, 0) + cand_a.net_profit
        for ident in map_b:
            if ident not in map_a:
                fp_count += 1

    sorted_a = sorted(arm_a_durations)
    sorted_b = sorted(arm_b_durations)
    p99_a = _percentile_int(sorted_a, 0.99)
    p99_b = _percentile_int(sorted_b, 0.99)

    p99_reduction = (p99_a - p99_b) / p99_a if p99_a > 0 else 0.0
    cycles_reduction = (arm_a_evals - arm_b_evals) / arm_a_evals if arm_a_evals > 0 else 0.0
    recall = (
        (arm_a_cands_total - missed_count) / arm_a_cands_total if arm_a_cands_total > 0 else None
    )

    arm_a_metrics = MultihopAbArmMetrics(
        arm="A_cpu_full_scan",
        blocks_scanned=len(active_snapshots),
        total_cycles_evaluated=arm_a_evals,
        total_candidates=arm_a_cands_total,
        full_scan_blocks=len(active_snapshots),
        incremental_blocks=0,
        mean_ns=sum(sorted_a) // len(sorted_a),
        p50_ns=_percentile_int(sorted_a, 0.50),
        p95_ns=_percentile_int(sorted_a, 0.95),
        p99_ns=p99_a,
        max_ns=sorted_a[-1],
        first_block_ns=arm_a_durations[0],
        subsequent_p99_ns=_percentile_int(sorted(arm_a_durations[1:]), 0.99)
        if len(arm_a_durations) > 1
        else None,
    )
    arm_b_metrics = MultihopAbArmMetrics(
        arm="B_cpu_incremental",
        blocks_scanned=len(active_snapshots),
        total_cycles_evaluated=arm_b_evals,
        total_candidates=arm_b_cands_total,
        full_scan_blocks=arm_b_full_blocks,
        incremental_blocks=arm_b_inc_blocks,
        mean_ns=sum(sorted_b) // len(sorted_b),
        p50_ns=_percentile_int(sorted_b, 0.50),
        p95_ns=_percentile_int(sorted_b, 0.95),
        p99_ns=p99_b,
        max_ns=sorted_b[-1],
        first_block_ns=arm_b_durations[0],
        subsequent_p99_ns=_percentile_int(sorted(arm_b_durations[1:]), 0.99)
        if len(arm_b_durations) > 1
        else None,
    )

    hypothesis_a_passed = bool(
        missed_count == 0
        and fp_count == 0
        and sizing_unknown_blocks == 0
        and sizing_mismatches == 0
        and exact_blocks == len(active_snapshots)
        and recall is not None
        and recall >= 1.0
        and p99_reduction >= min_p99_reduction_ratio
    )

    return MultihopAbExperimentReport(
        hypothesis_a_passed=hypothesis_a_passed,
        min_p99_reduction_ratio=min_p99_reduction_ratio,
        p99_reduction_ratio=p99_reduction,
        cycles_evaluated_reduction_ratio=cycles_reduction,
        recall=recall,
        missed_candidates_count=missed_count,
        sizing_unknown_blocks=sizing_unknown_blocks,
        sizing_residual_mismatch_blocks=sizing_mismatches,
        full_scan_open_interval_count=open_intervals,
        missed_net_profit_raw=sum(missed_by_asset.values()) if len(missed_by_asset) <= 1 else None,
        missed_net_profit_by_asset=missed_by_asset,
        false_positive_count=fp_count,
        exact_bit_for_bit_blocks=exact_blocks,
        total_blocks=len(active_snapshots),
        changed_pools_per_block=changed_pools_per_block,
        full_scan_every_n_blocks=full_scan_every_n_blocks,
        max_hops=max_hops,
        deduplicate_cycles=deduplicate_cycles,
        seed=seed,
        arm_a=arm_a_metrics,
        arm_b=arm_b_metrics,
    )


__all__ = [
    "AB_EXPERIMENT_SCHEMA",
    "BlockPoolSnapshot",
    "FallbackReason",
    "IncrementalCycleScanResult",
    "IncrementalCycleScanner",
    "MultihopAbArmMetrics",
    "MultihopAbExperimentReport",
    "build_benchmark_34pool_universe",
    "generate_34pool_block_snapshots",
    "run_multihop_ab_experiment",
]
