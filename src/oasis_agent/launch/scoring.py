"""Versioned, deterministic meme candidate research; no signing or order authority."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from ..models.common import canonical_json, sha256_hex
from .models import Address, Digest, Positive, StrictModel, UInt

Bps = Annotated[int, Field(ge=0, le=10000)]
Check = Literal["pass", "fail", "unknown"]


class TrendSignal(StrictModel):
    provider: Literal["gmgn", "okx"]
    chain_id: Positive
    token: Address
    rank: Annotated[int, Field(ge=1, le=100)]
    window_seconds: Positive
    observed_at: UInt
    retrieved_at: UInt
    raw_sha256: Digest


class ChainFactors(StrictModel):
    chain_id: Positive
    token: Address
    block_number: Positive
    block_hash: Annotated[str, Field(pattern=r"^0x[0-9a-f]{64}$")]
    observed_at: UInt
    evidence_sha256: Digest
    holder_coverage: Check
    holder_count: Positive
    holder_top10_bps: Bps
    common_control_bps: Bps
    common_control_analysis: Check = "unknown"
    contract_analysis: Check
    sell_simulation: Check
    mint_or_freeze_authority: Check
    # pass means bounded/absent authority was established, not that an authority exists.
    liquidity_exit: Check
    quote_impact_bps: Bps
    round_trip_cost_bps: Bps
    executable_depth_usd_micros: UInt
    liquidity_usd_micros: Positive
    fdv_usd_micros: Positive
    unique_buyers: UInt
    unique_sellers: UInt
    wash_trading: Check


class PositionSignal(StrictModel):
    entry_cost_usd_micros: Positive
    net_exit_value_usd_micros: UInt
    high_water_exit_value_usd_micros: UInt
    opened_at: UInt
    observed_at: UInt
    evidence_sha256: Digest

    @model_validator(mode="after")
    def coherent_high_water(self) -> "PositionSignal":
        if self.high_water_exit_value_usd_micros < self.net_exit_value_usd_micros:
            raise ValueError("high water cannot be below current net exit value")
        return self


class LpScenario(StrictModel):
    """Declared counterfactual estimate, never realized PnL or raw routing proof."""

    expected_fee_usd_micros: UInt
    all_costs_and_inventory_loss_usd_micros: UInt
    hold_opportunity_usd_micros: UInt
    routing: Check
    observed_at: UInt
    evidence_sha256: Digest


class Candidate(StrictModel):
    schema_version: Literal["oasis.candidate.v1"]
    chain_id: Positive
    token: Address
    evaluated_at: UInt
    trends: Annotated[tuple[TrendSignal, ...], Field(max_length=2)]
    chain: ChainFactors
    position: PositionSignal | None = None
    lp: LpScenario | None = None


class ScorePolicy(StrictModel):
    version: Literal["oasis.meme-score.research.v1"] = "oasis.meme-score.research.v1"
    max_age_seconds: Positive = 30
    trend_window_seconds: Positive = 300
    max_top10_bps: Bps = 4000
    max_common_control_bps: Bps = 1500
    max_impact_bps: Bps = 100
    max_round_trip_cost_bps: Bps = 300
    min_unique_traders: Positive = 100
    min_holders: Positive = 100
    min_depth_usd_micros: Positive = 100_000_000
    max_fdv_liquidity_ratio: Positive = 100
    min_score: Bps = 7000
    stop_loss_bps: Bps = 1000
    take_profit_bps: Bps = 2000
    trailing_drawdown_bps: Annotated[int, Field(gt=0, le=10000)] = 800
    max_hold_seconds: Positive = 3600
    # heat, flow breadth, depth, holder dispersion, FDV/liquidity proxy.
    weights: tuple[Bps, Bps, Bps, Bps, Bps] = (2000, 2000, 2500, 2000, 1500)

    @model_validator(mode="after")
    def weights_sum(self) -> "ScorePolicy":
        if sum(self.weights) != 10000:
            raise ValueError("score weights must sum to 10000")
        return self


class ScoreResult(StrictModel):
    schema_version: Literal["oasis.candidate-score.v1"] = "oasis.candidate-score.v1"
    chain_id: Positive
    token: Address
    input_sha256: Digest
    policy_sha256: Digest
    score_bps: Bps
    components: tuple[Bps, Bps, Bps, Bps, Bps]
    blockers: tuple[str, ...]
    action: Literal["reject", "watch", "simulate_entry", "simulate_exit", "halt_review"]
    exit_reason: str | None
    lp_action: Literal["requires_separate_lp_evaluation", "simulate_lp"] = (
        "requires_separate_lp_evaluation"
    )
    evidence_level: Literal["normalized_import_not_raw_attestation"] = (
        "normalized_import_not_raw_attestation"
    )
    execution_eligible: Literal[False] = False


def score_candidate(candidate: Candidate, policy: ScorePolicy) -> ScoreResult:
    now, c = candidate.evaluated_at, candidate.chain
    blockers = []
    if candidate.token == "0x" + "0" * 40:
        blockers.append("zero_token")
    if (c.chain_id, c.token) != (candidate.chain_id, candidate.token):
        blockers.append("chain_identity_mismatch")
    if not 0 <= now - c.observed_at <= policy.max_age_seconds:
        blockers.append("chain_data_stale_or_future")
    providers = [t.provider for t in candidate.trends]
    if len(providers) != 2 or set(providers) != {"gmgn", "okx"}:
        blockers.append("distinct_gmgn_okx_required")
    for trend in candidate.trends:
        if (trend.chain_id, trend.token) != (candidate.chain_id, candidate.token):
            blockers.append("trend_identity_mismatch")
        if (
            not trend.observed_at <= trend.retrieved_at <= now
            or not 0 <= now - trend.observed_at <= policy.max_age_seconds
        ):
            blockers.append("trend_stale_or_future")
        if trend.window_seconds != policy.trend_window_seconds:
            blockers.append("trend_window_mismatch")
    for name in (
        "holder_coverage",
        "common_control_analysis",
        "contract_analysis",
        "sell_simulation",
        "mint_or_freeze_authority",
        "liquidity_exit",
        "wash_trading",
    ):
        if getattr(c, name) != "pass":
            blockers.append(name + "_" + getattr(c, name))  # noqa: PERF401
    for value, limit, name in (
        (c.holder_top10_bps, policy.max_top10_bps, "holder_concentration"),
        (c.common_control_bps, policy.max_common_control_bps, "common_control"),
        (c.quote_impact_bps, policy.max_impact_bps, "price_impact"),
        (c.round_trip_cost_bps, policy.max_round_trip_cost_bps, "round_trip_cost"),
    ):
        if value > limit:
            blockers.append(name)
    if c.holder_count < policy.min_holders:
        blockers.append("insufficient_holders")
    # Either side may contain the same wallets: use a conservative union lower bound.
    if max(c.unique_buyers, c.unique_sellers) < policy.min_unique_traders:
        blockers.append("insufficient_trader_observations")
    if c.executable_depth_usd_micros < policy.min_depth_usd_micros:
        blockers.append("insufficient_executable_depth")
    if c.fdv_usd_micros > c.liquidity_usd_micros * policy.max_fdv_liquidity_ratio:
        blockers.append("fdv_liquidity_ratio")
    heat = min(((101 - t.rank) * 100 for t in candidate.trends), default=0)
    flow = c.unique_buyers * 10000 // max(1, c.unique_buyers + c.unique_sellers)
    depth = min(10000, c.executable_depth_usd_micros * 10000 // (policy.min_depth_usd_micros * 5))
    holders = 10000 - max(c.holder_top10_bps, c.common_control_bps)
    value = min(10000, c.liquidity_usd_micros * 10000 * 10 // c.fdv_usd_micros)
    components = (heat, flow, depth, holders, value)
    score = sum(x * w for x, w in zip(components, policy.weights, strict=True)) // 10000
    action: Literal["reject", "watch", "simulate_entry", "simulate_exit", "halt_review"] = (
        "reject" if blockers else "simulate_entry" if score >= policy.min_score else "watch"
    )
    exit_reason = None
    p = candidate.position
    if p is not None:
        # Popularity is an entry filter; disappearing from rankings must not suppress exits.
        blockers = [
            reason
            for reason in blockers
            if not reason.startswith("trend_") and reason != "distinct_gmgn_okx_required"
        ]
        if p.opened_at > p.observed_at or not 0 <= now - p.observed_at <= policy.max_age_seconds:
            blockers.append("position_stale_or_future")
        if blockers:
            action, exit_reason = (
                "halt_review",
                "risk_or_data_failure_requires_fresh_exit_simulation",
            )
        else:
            pnl = (
                (p.net_exit_value_usd_micros - p.entry_cost_usd_micros)
                * 10000
                // p.entry_cost_usd_micros
            )
            drawdown = (
                (p.high_water_exit_value_usd_micros - p.net_exit_value_usd_micros)
                * 10000
                // max(1, p.high_water_exit_value_usd_micros)
            )
            exit_reason = (
                "stop_loss"
                if pnl <= -policy.stop_loss_bps
                else "trailing_stop"
                if drawdown >= policy.trailing_drawdown_bps
                else "take_profit"
                if pnl >= policy.take_profit_bps
                else "time_stop"
                if now - p.opened_at >= policy.max_hold_seconds
                else None
            )
            action = "simulate_exit" if exit_reason else "watch"
    lp = candidate.lp
    lp_action: Literal["requires_separate_lp_evaluation", "simulate_lp"] = (
        "requires_separate_lp_evaluation"
    )
    if (
        not blockers
        and p is None
        and score >= policy.min_score
        and lp is not None
        and lp.routing == "pass"
        and 0 <= now - lp.observed_at <= policy.max_age_seconds
        and lp.expected_fee_usd_micros
        > lp.all_costs_and_inventory_loss_usd_micros + lp.hold_opportunity_usd_micros
    ):
        lp_action = "simulate_lp"
    return ScoreResult(
        chain_id=candidate.chain_id,
        token=candidate.token,
        input_sha256=sha256_hex(canonical_json(candidate.model_dump(mode="json"))),
        policy_sha256=sha256_hex(canonical_json(policy.model_dump(mode="json"))),
        score_bps=score,
        components=components,
        blockers=tuple(sorted(set(blockers))),
        action=action,
        exit_reason=exit_reason,
        lp_action=lp_action,
    )
