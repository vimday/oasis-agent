"""Metric API depth research. Provider estimates never confer execution authority.

Units and the conservative within-bin fill follow the public Metric API docs.
See docs/strategies/METRIC_EVENT_RESEARCH.md for source pins and limitations.
"""

import re
from dataclasses import asdict, dataclass
from itertools import permutations
from typing import Annotated, Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from ..evidence.rpc_validation import strict_integer, strict_list, strict_object
from ..models.common import ZERO_ADDRESS
from .cycle_plan import Address, Asset, Frozen, Raw, Size, address

MAX128 = (1 << 128) - 1
API = "https://api.metric.xyz"
COST_KINDS = {"gas", "builder", "financing", "failure_buffer", "opportunity"}


class IncrementalCost(Frozen):
    kind: Literal["gas", "builder", "financing", "failure_buffer", "opportunity"]
    raw: Raw | None


class MetricPlan(Frozen):
    schema_version: Literal["oasis-metric-plan-v1"] = Field(alias="schema")
    scenario: Literal["synthetic", "provider_observation"]
    chain_id: Annotated[int, Field(gt=0, lt=1 << 64)]
    tokens: tuple[Asset, Asset]
    pools: Annotated[tuple[Address, ...], Field(min_length=2, max_length=8)]
    start_token: Literal[0, 1]
    sizes_raw: Annotated[tuple[Size, ...], Field(min_length=1, max_length=16)]
    max_age_seconds: Annotated[int, Field(ge=1, le=60)]
    max_skew_seconds: Annotated[int, Field(ge=0, le=10)]
    max_capture_seconds: Annotated[int, Field(ge=1, le=120)]
    capital_raw: Raw | None
    commitments_raw: Raw | None
    reserve_raw: Raw | None
    costs: tuple[IncrementalCost, ...]

    @field_validator("start_token", mode="before")
    @classmethod
    def direction_type(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("direction must be an integer")
        return value

    @model_validator(mode="after")
    def scope(self) -> Self:
        if any(t.address in (None, ZERO_ADDRESS) for t in self.tokens):
            raise ValueError("Metric scope requires two ERC20 identities")
        if self.tokens[0].address == self.tokens[1].address:
            raise ValueError("distinct tokens required")
        if len(set(self.pools)) != len(self.pools) or ZERO_ADDRESS in self.pools:
            raise ValueError("distinct nonzero pools required")
        if len(set(self.sizes_raw)) != len(self.sizes_raw):
            raise ValueError("duplicate sizes")
        if len(self.costs) != len(COST_KINDS) or {c.kind for c in self.costs} != COST_KINDS:
            raise ValueError("each incremental cost is required exactly once")
        return self


def requests(plan: MetricPlan) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fixed public read endpoints; no user URLs, auth, paging or retry capability."""
    base = f"{API}/public/v1/evm/{plan.chain_id}"
    return (
        {
            "method": "GET",
            "url": f"{base}/metadata",
            "params": {"addresses": ",".join(plan.pools), "count": 8, "minTvlFiat": 0},
        },
        {
            "method": "POST",
            "url": f"{base}/bid_ask/batch",
            "json": {"pools": list(plan.pools), "depth": True},
        },
    )


def raw_uint(value: object) -> int:
    if (
        not isinstance(value, str)
        or len(value) > 78
        or re.fullmatch(r"0|[1-9][0-9]*", value) is None
        or int(value) >= 1 << 256
    ):
        raise ValueError("noncanonical_uint_string")
    return int(value)


@dataclass(frozen=True, slots=True)
class Level:
    index: int
    price: int
    input_raw: int
    output_raw: int


@dataclass(frozen=True, slots=True)
class Book:
    pool: str
    timestamp: int
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]


def _side(value: object, best: int, total: int, *, bids: bool) -> tuple[Level, ...]:
    levels = strict_list(value, "depth")
    if len(levels) > 2048:
        raise ValueError("depth_limit")
    if best == (0 if bids else MAX128):
        if levels:
            raise ValueError("sentinel_with_depth")
        return ()
    result = []
    prev_in = prev_out = 0
    prev_price = best
    prev_index = None
    for entry in levels:
        row = strict_object(entry, "level")
        index = strict_integer(row.get("binIdx"), "binIdx")
        price = raw_uint(row.get("price"))
        amount_in = raw_uint(row.get("cumulativeInputVolume"))
        amount_out = raw_uint(row.get("cumulativeVolume"))
        raw_uint(row.get("priceImpactE6"))
        if not -(1 << 255) <= index < 1 << 255 or not 0 < price < MAX128:
            raise ValueError("depth_price_or_index_bounds")
        if (bids and price > prev_price) or (not bids and price < prev_price):
            raise ValueError("depth_price_order")
        if prev_index is not None and (
            (bids and index >= prev_index) or (not bids and index <= prev_index)
        ):
            raise ValueError("depth_bin_order")
        if amount_in < prev_in or amount_out < prev_out or amount_out > total:
            raise ValueError("depth_amount_bounds")
        if (amount_in == prev_in) != (amount_out == prev_out):
            raise ValueError("depth_unfunded_increment")
        result.append(Level(index, price, amount_in, amount_out))
        prev_in, prev_out, prev_price, prev_index = amount_in, amount_out, price, index
    return tuple(result)


def parse_book(value: object, pool: str, received: int, plan: MetricPlan) -> Book:
    row = strict_object(value, "quote")
    if address(row.get("poolAddress")) != pool:
        raise ValueError("quote_pool_mismatch")
    if row.get("priceProviderStatus") != "healthy":
        raise ValueError("provider_not_healthy")
    timestamp = strict_integer(row.get("serverTs"), "serverTs")
    if not 0 <= received - timestamp <= plan.max_age_seconds:
        raise ValueError("snapshot_stale_or_future")
    bid, ask = raw_uint(row.get("bidAdj")), raw_uint(row.get("askAdj"))
    if not 0 <= bid < ask <= MAX128:
        raise ValueError("invalid_bid_ask")
    total0 = raw_uint(row.get("totalToken0Available"))
    total1 = raw_uint(row.get("totalToken1Available"))
    depth = strict_object(row.get("depth"), "depth")
    return Book(
        pool,
        timestamp,
        _side(depth.get("bids"), bid, total1, bids=True),
        _side(depth.get("asks"), ask, total0, bids=False),
    )


@dataclass(frozen=True, slots=True)
class Fill:
    requested_raw: int
    consumed_raw: int
    output_raw: int
    unfilled_raw: int


def depth_fill(book: Book, token_in: int, amount: int) -> Fill:
    """Floor proportional per-bin output; a lower estimate conditional on API depth.

    The endpoint already accounts for spread, deviation and pool fees. Multiplying
    Q64 prices by raw units would mix decimals and reapply those adjustments.
    """
    if type(amount) is not int or not 0 < amount < 1 << 127:
        raise ValueError("invalid_input_amount")
    if type(token_in) is not int or token_in not in (0, 1):
        raise ValueError("invalid_direction")
    remaining, output = amount, 0
    prev_in = prev_out = 0
    for level in book.bids if token_in == 0 else book.asks:
        bin_in = level.input_raw - prev_in
        bin_out = level.output_raw - prev_out
        prev_in, prev_out = level.input_raw, level.output_raw
        if not bin_in:
            continue
        used = min(remaining, bin_in)
        output += bin_out * used // bin_in
        remaining -= used
        if not remaining:
            break
    return Fill(amount, amount - remaining, output, remaining)


def _metadata(plan: MetricPlan, value: dict[str, object]) -> dict[str, str]:
    rows = strict_list(value.get("data"), "metadata data")
    if len(rows) > len(plan.pools):
        raise ValueError("unexpected_metadata_coverage")
    seen: set[str] = set()
    issues = dict.fromkeys(plan.pools, "metadata_missing")
    for entry in rows:
        row = strict_object(entry, "metadata")
        pool = address(row.get("poolAddress"))
        if pool not in issues or pool in seen:
            raise ValueError("unexpected_or_duplicate_metadata")
        seen.add(pool)
        issues[pool] = "metadata_identity_mismatch"
        if row.get("contractVersion") != "v1":
            continue
        if any(
            address(row.get(f"token{i}")) != token.address
            or strict_integer(row.get(f"token{i}Decimals"), "decimals") != token.decimals
            for i, token in enumerate(plan.tokens)
        ):
            continue
        if strict_integer(row.get("pauseLevel"), "pauseLevel") != 0:
            issues[pool] = "metadata_pool_paused"
            continue
        issues[pool] = ""
    return issues


def screen(
    plan: MetricPlan,
    metadata: dict[str, object],
    batch: dict[str, object],
    received: int,
) -> dict[str, Any]:
    """Keep every planned directed pair and size, including missing/failed data."""
    issues = _metadata(plan, metadata)
    books = {}
    seen: set[str] = set()
    rows = strict_list(batch.get("results"), "results")
    if len(rows) > len(plan.pools):
        raise ValueError("unexpected_quote_coverage")
    for entry in rows:
        row = strict_object(entry, "result")
        pool = address(row.get("poolAddress"))
        if pool not in issues or pool in seen:
            raise ValueError("unexpected_or_duplicate_quote")
        seen.add(pool)
        if ("quote" in row) == ("error" in row):
            raise ValueError("ambiguous_quote_result")
        if issues[pool]:
            continue
        if "error" in row:
            issues[pool] = "provider_quote_error"
            continue
        try:
            books[pool] = parse_book(row["quote"], pool, received, plan)
        except ValueError:
            # Provider strings and malformed values are not echoed to logs/reports.
            issues[pool] = "invalid_or_unavailable_depth"
    for pool in plan.pools:
        if pool not in seen and not issues[pool]:
            issues[pool] = "quote_missing"
    points = []
    costs = None if any(c.raw is None for c in plan.costs) else sum(c.raw or 0 for c in plan.costs)
    for first, second in permutations(sorted(plan.pools), 2):
        for size in sorted(plan.sizes_raw):
            point: dict[str, Any] = {
                "pools": [first, second],
                "input_raw": size,
                "fills": [],
                "output_raw": None,
                "gross_raw": None,
                "net_estimate_raw": None,
                "reasons": [],
                "execution_eligible": False,
            }
            reasons = point["reasons"]
            if costs is None:
                reasons.append("incremental_cost_unknown")
            elif costs >= 1 << 256:
                reasons.append("cost_overflow")
            if None in (plan.capital_raw, plan.commitments_raw, plan.reserve_raw):
                reasons.append("funding_unknown")
            elif costs is not None:
                assert plan.capital_raw is not None
                required = size + costs + (plan.commitments_raw or 0) + (plan.reserve_raw or 0)
                if required > plan.capital_raw:
                    reasons.append("insufficient_uncommitted_capital")
            pool_issues = [f"{pool}:{issues[pool]}" for pool in (first, second) if issues[pool]]
            reasons.extend(pool_issues)
            if not pool_issues:
                a, b = books[first], books[second]
                if abs(a.timestamp - b.timestamp) > plan.max_skew_seconds:
                    reasons.append("snapshot_skew")
                else:
                    amount = size
                    for book, direction in ((a, plan.start_token), (b, 1 - plan.start_token)):
                        if not 0 < amount < 1 << 127:
                            reasons.append("intermediate_amount_outside_bounds")
                            break
                        fill = depth_fill(book, direction, amount)
                        point["fills"].append(
                            {
                                **asdict(fill),
                                "pool": book.pool,
                                "asset_in": plan.tokens[direction].model_dump(),
                                "asset_out": plan.tokens[1 - direction].model_dump(),
                            }
                        )
                        if fill.unfilled_raw or not fill.output_raw:
                            reasons.append("partial_or_empty_depth")
                            break
                        amount = fill.output_raw
                    else:
                        if amount >= 1 << 127:
                            reasons.append("output_outside_signed_bounds")
                        else:
                            point.update(output_raw=amount, gross_raw=amount - size)
                            if not reasons:
                                assert costs is not None
                                point["net_estimate_raw"] = amount - size - costs
            points.append(point)
    depth_complete = sum(p["gross_raw"] is not None for p in points)
    cost_complete = sum(p["net_estimate_raw"] is not None for p in points)
    positive = sum((p["net_estimate_raw"] or 0) > 0 for p in points)
    return {
        "schema": "oasis-metric-screen-v1",
        "scenario": plan.scenario,
        "evidence_class": "synthetic_fixture" if plan.scenario == "synthetic" else "provider_claim",
        "chain_id": plan.chain_id,
        "reporting_asset": plan.tokens[plan.start_token].model_dump(),
        "pool_issues": issues,
        "planned_points": len(points),
        "depth_complete_points": depth_complete,
        "cost_complete_points": cost_complete,
        "decision": (
            "positive_provider_estimate"
            if positive
            else "incomplete_evidence"
            if cost_complete != len(points)
            else "no_positive_provider_estimate"
        ),
        "points": points,
        "positive_estimates": positive,
        "costs": [c.model_dump() for c in plan.costs],
        "pool_costs": "included_in_provider_depth",
        "incremental_cost_unit": "reporting_asset_raw_per_hypothetical_fill",
        "alternatives_are_mutually_exclusive": True,
        "first_seen_time_independently_verified": False,
        "block_identity": None,
        "contract_identity_verified": False,
        "provider_update_age": None,
        "ordering": "unverified_update_before_trade_risk",
        "exact_caller_simulation": "unavailable",
        "window_realized_pnl_raw": None,
        "execution_eligible": False,
    }
