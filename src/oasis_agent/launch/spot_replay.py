"""Deterministic Base spot decisions and a paper book over retained inputs."""

from collections import defaultdict, deque
from dataclasses import dataclass
from itertools import pairwise
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ..live.exact_risk import ExactRisk
from ..live.state import day_of
from ..models.common import canonical_json, sha256_hex
from .batch_scoring import BatchRequest, score_batch
from .discovery import DiscoveryRequest
from .models import Address, Digest, Positive, StrictModel, UInt
from .okx_spot import PaperQuote, SpotMarket, replay_quote
from .scoring import Candidate, PositionSignal, ScorePolicy, score_candidate

Capital = Annotated[int, Field(gt=0, le=500_000_000)]
TriggerBps = Annotated[int, Field(gt=0, lt=10_000)]
Action = Literal["watch", "paper_buy", "paper_hold", "paper_sell", "halt_review"]
QuoteHistory = dict[tuple[str, int, int], deque[PaperQuote]]


class SpotPolicy(StrictModel):
    score: ScorePolicy
    initial_cash_usd_micros: Capital
    max_trade_usd_micros: Capital
    max_daily_loss_usd_micros: Annotated[int, Field(gt=0, le=200_000_000)] = 200_000_000
    max_cumulative_loss_usd_micros: Capital = 500_000_000
    quote_max_age_seconds: Annotated[int, Field(gt=0, le=120)]
    slippage_bps: Annotated[int, Field(ge=0, le=300)]
    pullback_bps: TriggerBps
    rebound_bps: TriggerBps
    reference_samples: Annotated[int, Field(ge=2, le=20)]
    reference_max_age_seconds: Annotated[int, Field(gt=0, le=3600)]

    @model_validator(mode="after")
    def fresh_assessments(self) -> SpotPolicy:
        if self.score.max_age_seconds > 120:
            raise ValueError("spot assessments must be at most 120 seconds old")
        return self


class SpotFrame(StrictModel):
    discovery: DiscoveryRequest
    candidates: Annotated[tuple[Candidate, ...], Field(max_length=100)] = ()
    markets: Annotated[tuple[SpotMarket, ...], Field(max_length=100)] = ()

    @model_validator(mode="after")
    def verify_scope(self) -> SpotFrame:
        if (
            self.discovery.policy.chains != (8453,)
            or len({c.token for c in self.candidates}) != len(self.candidates)
            or len({m.token for m in self.markets}) != len(self.markets)
            or any(
                c.chain_id != 8453
                or c.evaluated_at != self.discovery.evaluated_at
                or c.position is not None
                or c.lp is not None
                for c in self.candidates
            )
        ):
            raise ValueError("one Base frame with unique tokens and no imported positions required")
        return self


class SpotReplay(StrictModel):
    schema_version: Literal["oasis.spot-replay.v1"]
    policy: SpotPolicy
    frames: Annotated[tuple[SpotFrame, ...], Field(min_length=1, max_length=512)]

    @model_validator(mode="after")
    def chronological(self) -> SpotReplay:
        if any(
            a.discovery.evaluated_at >= b.discovery.evaluated_at
            or a.discovery.policy != b.discovery.policy
            for a, b in pairwise(self.frames)
        ):
            raise ValueError("strictly chronological frames under one discovery policy required")
        return self


class PaperPosition(StrictModel):
    token: Address
    units: Positive
    token_decimals: Annotated[int, Field(ge=0, le=77)]
    opened_at: UInt
    entry_cost_usd_micros: Positive
    high_water_usd_micros: UInt


class PaperDecision(StrictModel):
    at: UInt
    frame_sha256: Digest
    action: Action
    reason: str
    token: Address | None = None
    units: Positive | None = None
    simulated_cash_change_usd_micros: int = 0
    net_nav_usd_micros: UInt | None = None
    quote_sha256: tuple[Digest, ...] = ()
    fomo_observed: bool = False


class SpotReport(StrictModel):
    schema_version: Literal["oasis.spot-replay-result.v1"] = "oasis.spot-replay-result.v1"
    mode: Literal["paper"] = "paper"
    input_sha256: Digest
    decisions: tuple[PaperDecision, ...]
    remaining_frames: UInt
    open_position: PaperPosition | None
    cash_usd_micros: UInt
    simulated_realized_pnl_usd_micros: int
    execution_eligible: Literal[False] = False


def _digest(model: StrictModel) -> str:
    return sha256_hex(canonical_json(model.model_dump(mode="json")))


def _quote(
    market: SpotMarket, side: Literal["buy", "sell"], now: int, policy: SpotPolicy
) -> PaperQuote | None:
    try:
        return replay_quote(
            market,
            side,
            now=now,
            max_age=policy.quote_max_age_seconds,
            max_impact_bps=policy.score.max_impact_bps,
            slippage_bps=policy.slippage_bps,
        )
    except ValueError, RecursionError:
        return None


def _paper_exit(
    candidate: Candidate,
    position: PaperPosition,
    sell: PaperQuote,
    policy: ScorePolicy,
) -> tuple[bool, str]:
    result = score_candidate(
        candidate.model_copy(
            update={
                "position": PositionSignal(
                    entry_cost_usd_micros=position.entry_cost_usd_micros,
                    net_exit_value_usd_micros=sell.usd_micros,
                    high_water_exit_value_usd_micros=position.high_water_usd_micros,
                    opened_at=position.opened_at,
                    observed_at=sell.retrieved_at,
                    evidence_sha256=sell.raw_sha256,
                )
            }
        ),
        policy,
    )
    return result.action != "halt_review", result.exit_reason or "position_within_limits"


@dataclass(frozen=True, slots=True)
class _Entry:
    score: int
    discount: int
    market: SpotMarket
    buy: PaperQuote
    sell: PaperQuote
    fomo: bool

    def priority(self) -> tuple[int, int, bool, str]:
        return -self.score, -self.discount, not self.fomo, self.market.token


def _choose_entry(
    frame: SpotFrame,
    policy: SpotPolicy,
    markets: dict[str, SpotMarket],
    buys: dict[str, PaperQuote],
    history: QuoteHistory,
    available: int,
) -> _Entry | None:
    now = frame.discovery.evaluated_at
    batch = score_batch(
        BatchRequest(discovery=frame.discovery, policy=policy.score, candidates=frame.candidates)
    )
    choices = []
    for item in batch.items:
        token = item.discovery.token
        if item.score is None or item.score.action != "simulate_entry" or token not in buys:
            continue
        market, buy = markets[token], buys[token]
        if buy.usd_micros > available:
            continue
        sell = _quote(market, "sell", now, policy)
        if (
            sell is None
            or sell.token_decimals != buy.token_decimals
            or not 0 < sell.usd_micros <= buy.usd_micros
            or (buy.usd_micros - sell.usd_micros) * 10_000
            > policy.score.max_round_trip_cost_bps * buy.usd_micros
        ):
            continue
        previous = history[token, market.units, buy.token_decimals]
        if (
            len(previous) < policy.reference_samples
            or now - previous[0].retrieved_at > policy.reference_max_age_seconds
            or buy.retrieved_at <= previous[-1].retrieved_at
        ):
            continue
        reference = sorted(q.usd_micros for q in previous)[(len(previous) - 1) // 2]
        last = previous[-1].usd_micros
        discount = (reference - buy.usd_micros) * 10_000 // reference
        rebound = (buy.usd_micros - last) * 10_000 // last
        if discount < policy.pullback_bps or rebound < policy.rebound_bps:
            continue
        fomo = any(
            s.provider == "fomo"
            and s.kind == "social"
            and s.chain_id == 8453
            and s.token == token
            and s.observed_at <= s.retrieved_at <= now
            and now - s.observed_at
            <= min(frame.discovery.policy.max_age_seconds, policy.score.max_age_seconds)
            for s in frame.discovery.signals
        )
        choices.append(_Entry(item.score.score_bps, discount, market, buy, sell, fomo))
    return min(choices, key=_Entry.priority, default=None)


def replay_spot(request: SpotReplay) -> SpotReport:
    """Replay one position at a time. Quotes, timestamps and assessments are research inputs."""
    policy = request.policy
    cash, realized = policy.initial_cash_usd_micros, 0
    position: PaperPosition | None = None
    risk = ExactRisk()
    decisions: list[PaperDecision] = []
    history: QuoteHistory = defaultdict(lambda: deque(maxlen=policy.reference_samples))
    for frame in request.frames:
        now = frame.discovery.evaluated_at
        frame_hash = _digest(frame)
        markets = {market.token: market for market in frame.markets}
        buys = {
            token: quote
            for token, market in markets.items()
            if (quote := _quote(market, "buy", now, policy)) is not None
        }
        if position is not None:
            market = markets.get(position.token)
            candidate = next((c for c in frame.candidates if c.token == position.token), None)
            sell = _quote(market, "sell", now, policy) if market is not None else None
            if (
                candidate is None
                or market is None
                or sell is None
                or market.units != position.units
                or sell.token_decimals != position.token_decimals
                or sell.retrieved_at <= position.opened_at
            ):
                decisions.append(
                    PaperDecision(
                        at=now,
                        frame_sha256=frame_hash,
                        action="halt_review",
                        reason="held_exit_evidence_missing",
                        token=position.token,
                        units=position.units,
                    )
                )
                break
            nav = cash + sell.usd_micros
            position = position.model_copy(
                update={
                    "high_water_usd_micros": max(position.high_water_usd_micros, sell.usd_micros)
                }
            )
            daily, cumulative = risk.observe(day=day_of(now), at=now, nav=nav, checkpoint=True)
            breached = (
                daily >= policy.max_daily_loss_usd_micros
                or cumulative >= policy.max_cumulative_loss_usd_micros
            )
            safe, reason = _paper_exit(candidate, position, sell, policy.score)
            action: Action = (
                "halt_review"
                if not safe
                else "paper_sell"
                if breached or reason != "position_within_limits"
                else "paper_hold"
            )
            decisions.append(
                PaperDecision(
                    at=now,
                    frame_sha256=frame_hash,
                    action=action,
                    reason="risk_limit" if breached and safe else reason,
                    token=position.token,
                    units=position.units,
                    net_nav_usd_micros=nav if safe else None,
                    quote_sha256=(sell.raw_sha256, market.usdc.evidence_sha256),
                    simulated_cash_change_usd_micros=sell.usd_micros
                    if action == "paper_sell"
                    else 0,
                )
            )
            if action == "paper_sell":
                cash += sell.usd_micros
                realized += sell.usd_micros - position.entry_cost_usd_micros
                position = None
            if action == "halt_review" or breached:
                break
        else:
            daily, cumulative = risk.observe(day=day_of(now), at=now, nav=cash, checkpoint=True)
            loss_remaining = min(
                policy.max_daily_loss_usd_micros - daily,
                policy.max_cumulative_loss_usd_micros - cumulative,
            )
            available = min(cash, policy.max_trade_usd_micros, loss_remaining - 1)
            entry = _choose_entry(frame, policy, markets, buys, history, available)
            if entry is not None:
                market, buy, sell = entry.market, entry.buy, entry.sell
                cash -= buy.usd_micros
                position = PaperPosition(
                    token=market.token,
                    units=market.units,
                    token_decimals=buy.token_decimals,
                    opened_at=now,
                    entry_cost_usd_micros=buy.usd_micros,
                    high_water_usd_micros=sell.usd_micros,
                )
                nav = cash + sell.usd_micros
                risk.observe(day=day_of(now), at=now, nav=nav, checkpoint=True)
                decisions.append(
                    PaperDecision(
                        at=now,
                        frame_sha256=frame_hash,
                        action="paper_buy",
                        reason="pullback_recovery",
                        token=market.token,
                        units=market.units,
                        simulated_cash_change_usd_micros=-buy.usd_micros,
                        net_nav_usd_micros=nav,
                        fomo_observed=entry.fomo,
                        quote_sha256=(buy.raw_sha256, sell.raw_sha256, market.usdc.evidence_sha256),
                    )
                )
            else:
                decisions.append(
                    PaperDecision(
                        at=now,
                        frame_sha256=frame_hash,
                        action="watch",
                        reason="no_admissible_entry",
                        net_nav_usd_micros=cash,
                    )
                )
        for token, buy in buys.items():
            points = history[token, markets[token].units, buy.token_decimals]
            if not points or buy.retrieved_at > points[-1].retrieved_at:
                points.append(buy)
    return SpotReport(
        input_sha256=_digest(request),
        decisions=tuple(decisions),
        remaining_frames=len(request.frames) - len(decisions),
        open_position=position,
        cash_usd_micros=cash,
        simulated_realized_pnl_usd_micros=realized,
    )
