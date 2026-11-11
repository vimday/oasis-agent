"""Evaluate mutually exclusive three-swap alternatives at frozen block ends."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from ..models.chain import BlockSnapshot
from ..models.common import ZERO_ADDRESS, canonical_json, sha256_hex
from .cycle_evidence import Export, Reads, Unavailable, header
from .cycle_plan import Plan, Pool, Window, utc
from .cycle_protocols import (
    UINT256,
    Reserves,
    quote,
    reserves,
    verify_asset,
    verify_pool,
    verify_quoter,
)

VERSION = "oasis-offline-cycle-v1"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def other(pool: Pool, token: str) -> str:
    if token == pool.token0:
        return pool.token1
    if token == pool.token1:
        return pool.token0
    raise Unavailable("route_asset_disconnected")


@dataclass(frozen=True, slots=True)
class Route:
    pools: tuple[Pool, Pool, Pool]
    currencies: tuple[str, str, str, str]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(pool.name for pool in self.pools)


@dataclass(frozen=True, slots=True)
class QuoteScope:
    block: BlockSnapshot
    paths: frozenset[tuple[str, ...]]


def routes(plan: Plan) -> tuple[Route, ...]:
    """Only enumerate native -> ERC20 -> ERC20 -> wrapped-native."""
    result: list[Route] = []
    ordered = sorted(plan.pools, key=lambda pool: pool.name)
    for first in ordered:
        if first.protocol != "v4" or first.token0 != ZERO_ADDRESS:
            continue
        token_a = first.token1
        if token_a == plan.wrapped_native:
            continue
        for second in ordered:
            if second.protocol != "v3" or token_a not in (second.token0, second.token1):
                continue
            token_b = other(second, token_a)
            if token_b == plan.wrapped_native:
                continue
            result.extend(
                Route((first, second, third), (ZERO_ADDRESS, token_a, token_b, plan.wrapped_native))
                for third in ordered
                if third.protocol == "solidly_volatile"
                and {third.token0, third.token1} == {token_b, plan.wrapped_native}
            )
    return tuple(result)


def _asset_view(plan: Plan, currency: str) -> dict[str, object]:
    asset = next(asset for asset in plan.assets if asset.currency == currency)
    return {"kind": "erc20" if asset.address else "native", **asset.model_dump()}


def _verify_window(
    reads: Reads,
    plan: Plan,
    window: Window,
    previous: BlockSnapshot | None,
) -> BlockSnapshot:
    reads.chain(plan.chain_id)
    raw, _ = reads.get("eth_getBlockByNumber", hex(window.block), False)
    current = header(raw, plan.chain_id, window.block)
    if window.expected_hash is not None and current.ref.block_hash != window.expected_hash:
        raise Unavailable("planned_block_hash_mismatch")
    if window.cutoff_utc is not None:
        elapsed = utc(window.cutoff_utc) - EPOCH
        assert current.ref.timestamp is not None
        if current.ref.timestamp > elapsed.days * 86_400 + elapsed.seconds:
            raise Unavailable("block_timestamp_after_cutoff")
    if previous is not None and (
        current.parent_hash != previous.ref.block_hash
        or (current.ref.timestamp or 0) < (previous.ref.timestamp or 0)
    ):
        raise Unavailable("block_parent_or_timestamp_mismatch")
    reads.bind(current.ref)
    return current


def _reserve_comparison(
    pool: Pool,
    state: Reserves,
    previous: Reserves | None,
    threshold: int,
) -> dict[str, Any]:
    change = None
    if previous is not None:
        a = state.reserve1 * previous.reserve0
        b = previous.reserve1 * state.reserve0
        change = max(a, b) * 10_000 // min(a, b)
    return {
        "kind": "single_pool_reserve_ratio",
        "pool": pool.name,
        "state": asdict(state),
        "previous_adjacent_state": asdict(previous) if previous else None,
        "change_factor_bps": change,
        "anomalous": change >= threshold if change is not None else None,
    }


def _evaluate(
    plan: Plan,
    config_id: str,
    reads: Reads,
    route: Route,
    size: int,
    assets: dict[str, tuple[str, ...]],
    pools: dict[str, tuple[str, ...]],
    states: dict[str, Reserves],
    issues: dict[str, str],
) -> dict[str, Any]:
    assert reads.block is not None
    identity = [VERSION, config_id, asdict(reads.block), route.names, size]
    point: dict[str, Any] = {
        "id": "cycle-" + sha256_hex(canonical_json(identity)),
        "path": route.names,
        "principal_raw": size,
        "legs": [],
        "output_raw": None,
        "net_raw": None,
        "quote_complete": False,
        "reasons": [],
        "atomic_simulation": "unavailable",
        "execution_eligible": False,
    }
    reasons: list[dict[str, str]] = point["reasons"]
    blocked = [
        {"subject": key, "reason": issues[key]}
        for key in (*route.names, *route.currencies)
        if key in issues
    ]
    addresses = {pool.address for pool in route.pools}
    if len(addresses) != len(route.pools) or plan.wrapped_native in addresses:
        blocked.append({"subject": "route", "reason": "repeated_or_shared_swap_target"})
    cost_values = [cost.raw for cost in plan.costs]
    if any(amount is None for amount in cost_values):
        reasons.append({"subject": "costs", "reason": "incremental_cost_unknown"})
    cost_total = sum(amount for amount in cost_values if amount is not None)
    gas = next(cost.raw for cost in plan.costs if cost.kind == "gas")
    if None in (plan.inventory_raw, plan.gas_reserve_raw, plan.commitments_raw):
        reasons.append({"subject": "capital", "reason": "funding_or_reservations_unknown"})
    else:
        assert plan.inventory_raw is not None and plan.gas_reserve_raw is not None
        assert plan.commitments_raw is not None
        if (
            size + plan.commitments_raw + cost_total + max(0, plan.gas_reserve_raw - (gas or 0))
            > plan.inventory_raw
        ):
            blocked.append({"subject": "capital", "reason": "insufficient_uncommitted_inventory"})
    reasons.extend(blocked)
    if blocked:
        return point
    amount = size
    for pool, token_in, token_out in zip(
        route.pools,
        route.currencies[:-1],
        route.currencies[1:],
        strict=True,
    ):
        try:
            output, refs = quote(reads, pool, token_in, amount, states.get(pool.name))
        except (ValueError, TypeError) as exc:
            reason = exc.reason if isinstance(exc, Unavailable) else "malformed_quote_result"
            reasons.append({"subject": pool.name, "reason": reason})
            return point
        point["legs"].append(
            {
                "protocol": pool.protocol,
                "pool": pool.name,
                "pool_address": pool.address,
                "pool_id": pool.pool_id,
                "quote_target": pool.quoter or pool.address,
                "asset_in": _asset_view(plan, token_in),
                "input_raw": amount,
                "asset_out": _asset_view(plan, token_out),
                "output_raw": output,
                "evidence": sorted(
                    {*pools[pool.name], *assets[token_in], *assets[token_out], *refs}
                ),
            }
        )
        amount = output
    point["legs"].append(
        {
            "protocol": "unwrap",
            "target": plan.wrapped_native,
            "asset_in": _asset_view(plan, plan.wrapped_native),
            "input_raw": amount,
            "asset_out": _asset_view(plan, ZERO_ADDRESS),
            "output_raw": amount,
            "evidence": assets[plan.wrapped_native],
            "assumption": "reviewed_1_to_1_wrapper",
        }
    )
    point.update(quote_complete=True, output_raw=amount)
    if cost_total >= UINT256:
        reasons.append({"subject": "costs", "reason": "incremental_cost_overflow"})
    if not reasons:
        point["net_raw"] = amount - size - cost_total
    return point


def _inspect(
    plan: Plan,
    reads: Reads,
    previous: dict[str, Reserves],
    selected: tuple[Route, ...] | None,
) -> tuple[
    dict[str, tuple[str, ...]],
    dict[str, tuple[str, ...]],
    dict[str, Reserves],
    dict[str, str],
    list[dict[str, Any]],
]:
    assets: dict[str, tuple[str, ...]] = {}
    pools: dict[str, tuple[str, ...]] = {}
    states: dict[str, Reserves] = {}
    issues: dict[str, str] = {}
    anomalies = []
    currencies = {token for route in selected or () for token in route.currencies}
    pool_names = {name for route in selected or () for name in route.names}
    for asset in sorted(plan.assets, key=lambda item: item.currency):
        if selected is not None and asset.currency not in currencies:
            continue
        try:
            assets[asset.currency] = verify_asset(reads, asset)
        except (ValueError, TypeError) as exc:
            issues[asset.currency] = (
                exc.reason if isinstance(exc, Unavailable) else "malformed_asset_identity"
            )
    for pool in sorted(plan.pools, key=lambda item: item.name):
        if selected is not None and pool.name not in pool_names:
            continue
        try:
            refs = verify_pool(reads, pool)
            if pool.protocol == "solidly_volatile":
                if pool.token0 in issues or pool.token1 in issues:
                    raise Unavailable("solidly_asset_identity_unavailable")
                state = reserves(reads, pool)
                states[pool.name] = state
                anomalies.append(
                    _reserve_comparison(
                        pool,
                        state,
                        previous.get(pool.name),
                        plan.anomaly_ratio_bps,
                    )
                )
                refs += state.evidence
            pools[pool.name] = refs + verify_quoter(reads, pool)
        except (ValueError, TypeError) as exc:
            issues[pool.name] = (
                exc.reason if isinstance(exc, Unavailable) else "malformed_pool_identity_or_state"
            )
    return assets, pools, states, issues, anomalies


def evaluate_windows(
    plan: Plan,
    records: tuple[Export, ...],
    config_id: str,
    *,
    scopes: Mapping[int, QuoteScope] | None = None,
) -> list[dict[str, Any]]:
    all_routes = routes(plan)
    windows: list[dict[str, Any]] = []
    previous: BlockSnapshot | None = None
    previous_states: dict[str, Reserves] = {}
    previous_number: int | None = None
    broken_lineage = False
    for window in sorted(plan.windows, key=lambda item: item.block):
        scope = scopes.get(window.block) if scopes is not None else None
        if scopes is not None and scope is None:
            continue
        available_routes = (
            all_routes
            if scope is None
            else tuple(route for route in all_routes if route.names in scope.paths)
        )
        point_count = len(available_routes) * len(plan.sizes_raw)
        cutoff = utc(window.cutoff_utc) if window.cutoff_utc else None
        reads = Reads(records, plan.sources, plan.max_queries_per_window, cutoff)
        adjacent = previous_number is not None and window.block == previous_number + 1
        entry: dict[str, Any] = {
            "name": window.name,
            "requested_block": window.block,
            "boundary": "end_of_block",
            "cutoff_utc": window.cutoff_utc,
            "time_basis": "export_retrieval_cutoff" if cutoff else "conditional_block_state",
            "block": None,
            "points": [],
            "anomalies": [],
            "issues": {},
            "queries": reads.queries,
            "lineage": None,
            "planned_points": point_count,
            "best_evaluated_id": None,
            "reason": None,
            "decision": "unavailable",
            "quote_points_are_mutually_exclusive": True,
        }
        states: dict[str, Reserves] = {}
        try:
            if adjacent and broken_lineage:
                raise Unavailable("unresolved_parent_lineage")
            current = _verify_window(reads, plan, window, previous if adjacent else None)
            if scope is not None and current != scope.block:
                raise Unavailable("shadow_quote_block_mismatch")
            entry["block"] = asdict(current)
            entry["lineage"] = (
                "adjacent_parent_checked" if adjacent and previous else "standalone_header"
            )
            assets, pools, states, issues, anomalies = _inspect(
                plan,
                reads,
                previous_states if adjacent else {},
                available_routes if scope is not None else None,
            )
            entry.update(anomalies=anomalies, issues=issues)
            if not available_routes:
                raise Unavailable("no_allowlisted_closed_route")
            if point_count > plan.max_points:
                raise Unavailable("configured_point_budget_exceeded")
            points = [
                _evaluate(plan, config_id, reads, route, size, assets, pools, states, issues)
                for route in available_routes
                for size in sorted(plan.sizes_raw)
            ]
            entry["points"] = points
            complete = [point for point in points if point["net_raw"] is not None]
            if complete:
                best = max(
                    complete,
                    key=lambda point: (
                        point["net_raw"],
                        -point["principal_raw"],
                        point["id"],
                    ),
                )
                entry["best_evaluated_id"] = best["id"]
                entry["decision"] = (
                    "positive_conditional_quote"
                    if best["net_raw"] > 0
                    else ("no_positive_evaluated_quote")
                )
            else:
                entry["decision"] = "no_cost_complete_quote"
        except (ValueError, TypeError) as exc:
            current = None
            entry["reason"] = (
                exc.reason if isinstance(exc, Unavailable) else "malformed_block_context"
            )
        broken_lineage = entry["reason"] in {
            "planned_block_hash_mismatch",
            "block_parent_or_timestamp_mismatch",
            "shadow_quote_block_mismatch",
            "unresolved_parent_lineage",
            "provider_results_conflict",
        }
        previous, previous_states = current, states
        previous_number = window.block
        windows.append(entry)
    return windows
