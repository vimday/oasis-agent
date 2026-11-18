from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .bridge import FastpathBridge, FastpathCallError, parse_int_str, require_int


class PoolKind(StrEnum):
    V2 = "V2"
    AERODROME_STABLE = "AerodromeStable"
    V3 = "V3"
    SLIPSTREAM = "Slipstream"
    V4 = "V4"


@dataclass(frozen=True, slots=True)
class TickLiquidity:
    tick: int
    liquidity_net: int

    def __post_init__(self) -> None:
        require_int(self.tick, "tick")
        require_int(self.liquidity_net, "liquidity_net")

    def to_wire(self) -> dict[str, object]:
        return {"tick": self.tick, "liquidity_net": str(self.liquidity_net)}


@dataclass(frozen=True, slots=True)
class PoolNode:
    chain_id: int
    pool_id: str
    kind: PoolKind
    token0: str
    token1: str
    decimals0: int
    decimals1: int
    fee_pips: int
    block_number: int = 0
    timestamp_ms: int = 0
    reserve0: int | None = None
    reserve1: int | None = None
    sqrt_price_x96: int | None = None
    tick: int | None = None
    liquidity: int | None = None
    ticks: tuple[TickLiquidity, ...] = ()

    def __post_init__(self) -> None:
        require_int(self.chain_id, "chain_id", non_negative=True)
        if self.chain_id <= 0:
            raise ValueError("chain_id must be positive")
        if not self.pool_id or not self.token0 or not self.token1:
            raise ValueError("pool_id, token0, and token1 must be non-empty")
        require_int(self.decimals0, "decimals0", non_negative=True)
        require_int(self.decimals1, "decimals1", non_negative=True)
        require_int(self.fee_pips, "fee_pips", non_negative=True)
        require_int(self.block_number, "block_number", non_negative=True)
        require_int(self.timestamp_ms, "timestamp_ms", non_negative=True)
        if self.reserve0 is not None:
            require_int(self.reserve0, "reserve0", non_negative=True)
        if self.reserve1 is not None:
            require_int(self.reserve1, "reserve1", non_negative=True)
        if self.sqrt_price_x96 is not None:
            require_int(self.sqrt_price_x96, "sqrt_price_x96", non_negative=True)
        if self.tick is not None:
            require_int(self.tick, "tick")
        if self.liquidity is not None:
            require_int(self.liquidity, "liquidity", non_negative=True)

    def to_wire(self) -> dict[str, object]:
        return {
            "chain_id": self.chain_id,
            "pool_id": self.pool_id,
            "kind": self.kind.value,
            "token0": self.token0,
            "token1": self.token1,
            "decimals0": self.decimals0,
            "decimals1": self.decimals1,
            "fee_pips": self.fee_pips,
            "reserve0": str(self.reserve0) if self.reserve0 is not None else None,
            "reserve1": str(self.reserve1) if self.reserve1 is not None else None,
            "sqrt_price_x96": str(self.sqrt_price_x96) if self.sqrt_price_x96 is not None else None,
            "tick": self.tick,
            "liquidity": str(self.liquidity) if self.liquidity is not None else None,
            "ticks": [t.to_wire() for t in self.ticks],
            "block_number": self.block_number,
            "timestamp_ms": self.timestamp_ms,
        }


@dataclass(frozen=True, slots=True)
class QuoteResult:
    pool_id: str
    kind: PoolKind
    token_in: str
    token_out: str
    amount_in: int
    amount_out: int
    fee_paid: int


@dataclass(frozen=True, slots=True)
class CycleHop:
    pool_id: str
    kind: PoolKind
    token_in: str
    token_out: str
    amount_in: int
    amount_out: int


@dataclass(frozen=True, slots=True)
class CycleCandidate:
    chain_id: int
    base_token: str
    hops: tuple[CycleHop, ...]
    optimal_amount_in: int
    amount_out: int
    gas_cost_in_base: int
    l1_fee_in_base: int
    net_profit: int


@dataclass(frozen=True, slots=True)
class CycleScanReport:
    candidates: tuple[CycleCandidate, ...]
    cycles_evaluated: int
    elapsed_ns: int


def _parse_hop(raw: object) -> CycleHop:
    if not isinstance(raw, Mapping):
        raise FastpathCallError("cycle hop payload must be an object")
    return CycleHop(
        pool_id=str(raw["pool_id"]),
        kind=PoolKind(str(raw["kind"])),
        token_in=str(raw["token_in"]),
        token_out=str(raw["token_out"]),
        amount_in=parse_int_str(raw.get("amount_in"), "amount_in"),
        amount_out=parse_int_str(raw.get("amount_out"), "amount_out"),
    )


def _parse_candidate(raw: object) -> CycleCandidate:
    if not isinstance(raw, Mapping):
        raise FastpathCallError("cycle candidate payload must be an object")
    raw_hops = raw.get("hops")
    if not isinstance(raw_hops, list):
        raise FastpathCallError("candidate hops must be a list")
    chain_id = raw.get("chain_id")
    if isinstance(chain_id, bool) or not isinstance(chain_id, int):
        raise FastpathCallError("candidate chain_id must be an integer")
    return CycleCandidate(
        chain_id=chain_id,
        base_token=str(raw["base_token"]),
        hops=tuple(_parse_hop(item) for item in raw_hops),
        optimal_amount_in=parse_int_str(raw.get("optimal_amount_in"), "optimal_amount_in"),
        amount_out=parse_int_str(raw.get("amount_out"), "amount_out"),
        gas_cost_in_base=parse_int_str(raw.get("gas_cost_in_base"), "gas_cost_in_base"),
        l1_fee_in_base=parse_int_str(raw.get("l1_fee_in_base"), "l1_fee_in_base"),
        net_profit=parse_int_str(raw.get("net_profit"), "net_profit"),
    )


def quote_pool(
    pool: PoolNode,
    token_in: str,
    amount_in: int,
    *,
    bridge: FastpathBridge | None = None,
) -> QuoteResult:
    """Quote an exact-input swap on a single pool via native Rust math."""
    checked_in = require_int(amount_in, "amount_in", non_negative=True)
    if checked_in <= 0:
        raise ValueError("amount_in must be positive")
    active_bridge = bridge if bridge is not None else FastpathBridge()
    result: dict[str, Any] = active_bridge.eval_json(
        {
            "cmd": "quote_pool",
            "pool": pool.to_wire(),
            "token_in": token_in,
            "amount_in": str(checked_in),
        }
    )
    return QuoteResult(
        pool_id=str(result["pool_id"]),
        kind=PoolKind(str(result["kind"])),
        token_in=str(result["token_in"]),
        token_out=str(result["token_out"]),
        amount_in=parse_int_str(result.get("amount_in"), "amount_in"),
        amount_out=parse_int_str(result.get("amount_out"), "amount_out"),
        fee_paid=parse_int_str(result.get("fee_paid"), "fee_paid"),
    )


def scan_cycles(
    pools: Sequence[PoolNode],
    base_tokens: Sequence[str],
    *,
    max_hops: int = 3,
    min_amount_in: int,
    max_amount_in: int,
    gas_cost_in_base: int = 0,
    l1_fee_in_base: int = 0,
    min_net_profit: int = 1,
    audit_ring_path: Path | None = None,
    block_number: int | None = None,
    timestamp_ns: int | None = None,
    bridge: FastpathBridge | None = None,
) -> CycleScanReport:
    """Scan 2-hop and 3-hop cycles across `pools` using native Rust ternary search."""
    checked_hops = require_int(max_hops, "max_hops", non_negative=True)
    checked_min = require_int(min_amount_in, "min_amount_in", non_negative=True)
    checked_max = require_int(max_amount_in, "max_amount_in", non_negative=True)
    checked_gas = require_int(gas_cost_in_base, "gas_cost_in_base", non_negative=True)
    checked_l1 = require_int(l1_fee_in_base, "l1_fee_in_base", non_negative=True)
    checked_profit = require_int(min_net_profit, "min_net_profit", non_negative=True)
    if block_number is not None:
        require_int(block_number, "block_number", non_negative=True)
    if timestamp_ns is not None:
        require_int(timestamp_ns, "timestamp_ns", non_negative=True)

    active_bridge = bridge if bridge is not None else FastpathBridge()
    result = active_bridge.eval_json(
        {
            "cmd": "scan_cycles",
            "pools": [pool.to_wire() for pool in pools],
            "base_tokens": list(base_tokens),
            "max_hops": checked_hops,
            "min_amount_in": str(checked_min),
            "max_amount_in": str(checked_max),
            "gas_cost_in_base": str(checked_gas),
            "l1_fee_in_base": str(checked_l1),
            "min_net_profit": str(checked_profit),
            "audit_ring_path": str(audit_ring_path) if audit_ring_path is not None else None,
            "block_number": block_number,
            "timestamp_ns": timestamp_ns,
        }
    )

    raw_candidates = result.get("candidates")
    if not isinstance(raw_candidates, list):
        raise FastpathCallError("scan_cycles candidates must be a list")
    cycles_evaluated = result.get("cycles_evaluated")
    elapsed_ns = result.get("elapsed_ns")
    if isinstance(cycles_evaluated, bool) or not isinstance(cycles_evaluated, int):
        raise FastpathCallError("cycles_evaluated must be an integer")
    if isinstance(elapsed_ns, bool) or not isinstance(elapsed_ns, int):
        raise FastpathCallError("elapsed_ns must be an integer")

    return CycleScanReport(
        candidates=tuple(_parse_candidate(item) for item in raw_candidates),
        cycles_evaluated=cycles_evaluated,
        elapsed_ns=elapsed_ns,
    )


class FastpathPoolGraph:
    """Mutable Python-side pool registry backed by the Rust fastpath scanner."""

    def __init__(self, bridge: FastpathBridge | None = None) -> None:
        self._bridge = bridge if bridge is not None else FastpathBridge()
        self._pools: dict[tuple[int, str], PoolNode] = {}

    def upsert_pool(self, pool: PoolNode) -> None:
        self._pools[(pool.chain_id, pool.pool_id.lower())] = pool

    def quote_pool(self, chain_id: int, pool_id: str, token_in: str, amount_in: int) -> QuoteResult:
        key = (chain_id, pool_id.lower())
        pool = self._pools.get(key)
        if pool is None:
            raise KeyError(f"pool not found for chain_id={chain_id} pool_id={pool_id}")
        return quote_pool(pool, token_in, amount_in, bridge=self._bridge)

    def scan_cycles(
        self,
        base_tokens: Sequence[str],
        *,
        max_hops: int = 3,
        min_amount_in: int,
        max_amount_in: int,
        gas_cost_in_base: int = 0,
        l1_fee_in_base: int = 0,
        min_net_profit: int = 1,
        audit_ring_path: Path | None = None,
        block_number: int | None = None,
        timestamp_ns: int | None = None,
    ) -> CycleScanReport:
        return scan_cycles(
            tuple(self._pools.values()),
            base_tokens,
            max_hops=max_hops,
            min_amount_in=min_amount_in,
            max_amount_in=max_amount_in,
            gas_cost_in_base=gas_cost_in_base,
            l1_fee_in_base=l1_fee_in_base,
            min_net_profit=min_net_profit,
            audit_ring_path=audit_ring_path,
            block_number=block_number,
            timestamp_ns=timestamp_ns,
            bridge=self._bridge,
        )
