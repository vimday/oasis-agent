"""Base spot discovery, paper decisions and position management.

The operator CLI runs a paper book. The executor protocol is the strategy boundary,
and journal records are never transaction permits.
"""

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from fractions import Fraction
from typing import Any, Literal

from ..models.common import canonical_json, sha256_hex
from .discovery_slot import DiscoveryProvider, nominated_markets
from .gmgn_source import GmgnSource, cross_check, parse_token_info
from .launchpad_safety import (
    DecisionStatus,
    LaunchpadSafetyDecision,
    LaunchpadSafetyEvidence,
    LaunchpadSafetyPolicy,
    evaluate_launchpad_safety,
)
from .onchainos_source import Capture, OnchainosSource, SourceFailure
from .opportunity import (
    Assessment,
    DecisionPolicy,
    Market,
    assess,
    count,
    decided,
    market_from_hot,
    market_from_signal,
    market_to_json,
    merge_price,
    merge_security,
    merge_signals,
    security_flags,
)
from .outcomes import REGISTRATION, OutcomeTracker, Pending, parse_prices
from .spot_book import (
    SETTLEMENT,
    Position,
    check_exit,
    entry_cost,
    entry_size,
    exit_value,
    mark,
    parse_quote,
    round_trip_bps,
)
from .spot_executor import LiveRefused, SpotExecutor
from .spot_lifecycle import TRADE_KINDS, run_status

Record = dict[str, Any]
MAX_MISSED_MARKS = 3
# Mutually exclusive fate of every unique discovered token in one cycle; they sum to
# `discovered`. `not_reached_enrich_quota` is deferred, not a skip decision.
CANDIDATE_FATES = (
    "held",
    "cooling_down",
    "rejected_before_enrich",
    "enrich_failed",
    "assessed_after_enrich",
    "not_reached_enrich_quota",
    "not_reached_stop",
)


def _coverage(discovery: str) -> Record:
    return {
        "schema": "oasis.spot-coverage.v2",
        "discovery": discovery,
        # Per discovery source: rows / valid / unique / new (see SpotAgent._discover);
        # empty when discovery did not run or failed before any source answered.
        "discovery_sources": {},
        "discovered": 0,
        **dict.fromkeys(CANDIDATE_FATES, 0),
        "label_registration": dict.fromkeys(REGISTRATION, 0),
        "labels_emitted": {"observed": 0, "unobserved": 0},
    }


LIVE_EVIDENCE = "wallet_balance_delta_and_chain_receipt"


class AgentConfig(DecisionPolicy):
    chain_id: Literal[8453] = 8453
    initial_cash_usd_micros: int = 1_000_000_000
    enrich_top: int = 8
    security_ttl_seconds: int = 900
    cooldown_seconds: int = 3_600


@dataclass
class AgentState:
    cash_usd_micros: int
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl_usd_micros: int = 0
    closed_trades: int = 0
    winning_trades: int = 0
    cooldown_until: dict[str, int] = field(default_factory=dict)
    security: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict)
    outcomes: OutcomeTracker = field(default_factory=OutcomeTracker)
    # Live only: gas is paid in ETH, so it never touches `cash_usd_micros` (the wallet's own
    # USDC). This is the *declared* per-leg estimate, not a receipt-derived fee.
    estimated_gas_usd_micros: int = 0

    def nav(self) -> int:
        """Strategy equity: cash + marked positions - estimated gas (not full-account NAV)."""
        held = sum(p.last_value_usd_micros for p in self.positions.values())
        return self.cash_usd_micros + held - self.estimated_gas_usd_micros


def decision_policy(config: DecisionPolicy) -> DecisionPolicy:
    """Exactly the DecisionPolicy that `assess` reads, without agent-only settings."""
    return DecisionPolicy.model_validate(
        {name: getattr(config, name) for name in DecisionPolicy.model_fields}
    )


def policy_digest(config: DecisionPolicy) -> str:
    return sha256_hex(canonical_json(decision_policy(config).model_dump(mode="json")))


def _rows(capture: Capture) -> list[Any]:
    return capture.data if isinstance(capture.data, list) else []


def _first(capture: Capture) -> dict[str, Any] | None:
    data = capture.data
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        return data[0]
    return data if isinstance(data, dict) else None


def make_launchpad_safety_evaluator(
    policy: LaunchpadSafetyPolicy | None,
    evidence_lookup: Callable[[Market, int], LaunchpadSafetyEvidence | None],
    *,
    fail_closed_on_missing: bool = False,
) -> Callable[[Market, int], LaunchpadSafetyDecision | None]:
    """Build a SpotAgent launchpad safety callback around `evaluate_launchpad_safety`."""
    if policy is not None and not isinstance(policy, LaunchpadSafetyPolicy):
        raise TypeError("policy must be LaunchpadSafetyPolicy or None")
    if not callable(evidence_lookup):
        raise TypeError("evidence_lookup must be callable")
    if type(fail_closed_on_missing) is not bool:
        raise TypeError("fail_closed_on_missing must be a bool")

    active_policy = policy if policy is not None else LaunchpadSafetyPolicy()

    def _evaluate(market: Market, now: int) -> LaunchpadSafetyDecision | None:
        try:
            evidence = evidence_lookup(market, now)
        except SourceFailure:
            return LaunchpadSafetyDecision(
                status=DecisionStatus.REJECTED,
                earliest_safe_entry_ms=None,
                effective_buy_tax_bps=None,
                schedule_fee_bps=None,
                round_trip_loss_bps=None,
                reasons=("launchpad_evidence_unavailable",),
                raw_sha256=sha256_hex(b"launchpad_evidence_unavailable"),
            )
        if evidence is None:
            if not fail_closed_on_missing:
                return None
            return LaunchpadSafetyDecision(
                status=DecisionStatus.REJECTED,
                earliest_safe_entry_ms=None,
                effective_buy_tax_bps=None,
                schedule_fee_bps=None,
                round_trip_loss_bps=None,
                reasons=("missing_launchpad_evidence",),
                raw_sha256=sha256_hex(b"missing_launchpad_evidence"),
            )
        if not isinstance(evidence, LaunchpadSafetyEvidence):
            raise TypeError("evidence_lookup must return LaunchpadSafetyEvidence or None")
        if evidence.token != market.token.lower():
            return LaunchpadSafetyDecision(
                status=DecisionStatus.REJECTED,
                earliest_safe_entry_ms=None,
                effective_buy_tax_bps=None,
                schedule_fee_bps=None,
                round_trip_loss_bps=None,
                reasons=("launchpad_evidence_token_mismatch",),
                raw_sha256=evidence.raw_sha256,
            )
        return evaluate_launchpad_safety(evidence, active_policy)

    return _evaluate


class SpotAgent:
    def __init__(
        self,
        source: OnchainosSource,
        config: AgentConfig,
        state: AgentState | None = None,
        clock: Callable[[], float] | None = None,
        gmgn: GmgnSource | None = None,
        executor: SpotExecutor | None = None,
        nominators: Sequence[DiscoveryProvider] = (),
        launchpad_safety_evaluator: (
            Callable[[Market, int], LaunchpadSafetyDecision | None] | None
        ) = None,
    ):
        self.source = source
        self.gmgn = gmgn
        self.executor = executor
        self.nominators = tuple(nominators)
        if launchpad_safety_evaluator is not None and not callable(launchpad_safety_evaluator):
            raise TypeError("launchpad_safety_evaluator must be callable or None")
        self.launchpad_safety_evaluator = launchpad_safety_evaluator
        self.mode = "shadow" if executor is None else "live"
        self.config = config
        # The decision policy `assess` uses: the DecisionPolicy fields of the config.
        self._policy_digest = policy_digest(config)
        self.state = state or AgentState(config.initial_cash_usd_micros)
        self._clock = clock

    def submissions(self) -> int:
        return self.executor.submissions if self.executor is not None else 0

    def provider_calls(self) -> int:
        extra = (
            (self.gmgn.calls if self.gmgn is not None else 0)
            + (self.executor.calls if self.executor is not None else 0)
            + sum(n.calls for n in self.nominators)
        )
        return self.source.calls + extra

    def _record(self, now: int, kind: str, **body: Any) -> Record:
        return {
            "at": now,
            "kind": kind,
            "mode": self.mode,
            "evidence_level": "provider_claims_not_chain_attestations",
            "execution_eligible": False,
            **body,
        }

    def _security(self, token: str, now: int) -> dict[str, Any] | None:
        cached = self.state.security.get(token)
        if cached is not None and now - cached[0] < self.config.security_ttl_seconds:
            return cached[1]
        row = _first(self.source.security(self.config.chain_id, token))
        if row is not None:
            self.state.security[token] = (now, row)
        return row

    def _sell_quote(self, position: Position, now: int) -> tuple[int, str]:
        capture = self.source.quote(
            self.config.chain_id, position.token, SETTLEMENT, position.units
        )
        sell = parse_quote(
            capture.data,
            chain=self.config.chain_id,
            src=position.token,
            dst=SETTLEMENT,
            amount=position.units,
            raw_sha256=capture.raw_sha256,
            at=capture.retrieved_at,
        )
        return exit_value(sell, self.config), sell.raw_sha256

    def _manage(self, now: int, out: list[Record], should_stop: Callable[[], bool]) -> None:
        for token, position in list(self.state.positions.items()):
            if should_stop():
                break
            flags: tuple[str, ...] | None
            try:
                row = self._security(token, now)
                flags = None if row is None else security_flags(row)[0]
                value, quote_hash = self._sell_quote(position, now)
            except SourceFailure, ValueError:
                missed = position.missed_marks + 1
                self.state.positions[token] = replace(position, missed_marks=missed)
                out.append(
                    self._record(
                        now,
                        "halt_review" if missed >= MAX_MISSED_MARKS else "mark_missed",
                        token=token,
                        reason="exit_quote_unavailable",
                        missed_marks=missed,
                    )
                )
                continue
            reason = "risk_flag" if flags is None or flags else None
            reason = reason or check_exit(position, value, now, self.config)
            if reason is None:
                self.state.positions[token] = mark(position, value)
                out.append(
                    self._record(
                        now,
                        "hold",
                        token=token,
                        symbol=position.symbol,
                        value_usd_micros=value,
                        cost_usd_micros=position.cost_usd_micros,
                        quote_sha256=quote_hash,
                    )
                )
                continue
            live: Record = {}
            if self.executor is not None:
                if should_stop():
                    break
                try:
                    fill = self.executor.sell(token, position.units)
                except LiveRefused as refused:
                    # Nothing was submitted; holding an unsellable position needs a human.
                    out.append(
                        self._record(
                            now,
                            "halt_review",
                            token=token,
                            reason="live_sell_refused",
                            category=refused.category,
                        )
                    )
                    return
                # Cash moves by the observed USDC delta only; the declared gas (paid in ETH)
                # is a separate estimated cost that still counts against the trade's PnL.
                gas = self.config.gas_per_leg_usd_micros
                value = fill.usdc_delta - gas
                live = {
                    "intent_id": fill.intent_id,
                    "tx_hash": fill.tx_hash,
                    "block_number": fill.block_number,
                    "usdc_received_base_units": fill.usdc_delta,
                    "units": str(-fill.token_delta),
                    "estimated_gas_usd_micros": gas,
                }
            pnl = value - position.cost_usd_micros
            del self.state.positions[token]
            if live:
                self.state.cash_usd_micros += fill.usdc_delta
                self.state.estimated_gas_usd_micros += self.config.gas_per_leg_usd_micros
            else:
                self.state.cash_usd_micros += value
            self.state.realized_pnl_usd_micros += pnl
            self.state.closed_trades += 1
            self.state.winning_trades += pnl > 0
            self.state.cooldown_until[token] = now + self.config.cooldown_seconds
            out.append(
                self._record(
                    now,
                    "paper_sell" if self.executor is None else "live_sell",
                    token=token,
                    symbol=position.symbol,
                    reason=reason,
                    proceeds_usd_micros=value,
                    pnl_usd_micros=pnl,
                    held_seconds=now - position.opened_at,
                    quote_sha256=quote_hash,
                    **live,
                    **({"evidence_level": LIVE_EVIDENCE} if live else {}),
                )
            )

    def _discover(self, now: int) -> tuple[list[Market], list[Any], Record]:
        """Unique candidate markets, signal rows, and per-source nomination counts.

        Per source: `rows` returned, `valid` rows naming a token, `unique` tokens among
        them, `new` tokens not already named by an earlier source (hot, then signals,
        then nominators). Sum of `new` equals the unique candidates this cycle.
        """
        sources: Record = {}

        def nominate(name: str, rows: int, found: list[Market], status: str = "ok") -> None:
            tokens = [m.token for m in found]
            fresh = [t for t in dict.fromkeys(tokens) if t not in markets]
            sources[name] = {
                "status": status,
                "rows": rows,
                "valid": len(tokens),
                "unique": len(set(tokens)),
                "new": len(fresh),
            }
            for m in found:
                markets.setdefault(m.token, m)

        hot = self.source.hot_tokens(self.config.chain_id)
        markets: dict[str, Market] = {}
        hot_rows = _rows(hot)
        nominate(
            "hot_tokens",
            len(hot_rows),
            [
                m
                for row in hot_rows
                if isinstance(row, dict) and (m := market_from_hot(row, now)) is not None
            ],
        )
        signal_status = "ok"
        try:
            signal_rows = _rows(self.source.signals(self.config.chain_id))
        except SourceFailure:
            signal_rows = []  # Signals only raise a score; absence never admits a token.
            signal_status = "failed"
        # Smart-money buys are a discovery source of their own: tokens they name join the
        # pool with only the fields the signal states. Unstated liquidity and age stay
        # unknown and block entry unless enrichment (price-info/advanced-info) fills them.
        nominate(
            "signals",
            len(signal_rows),
            [m for row in signal_rows if (m := market_from_signal(row, now)) is not None],
            signal_status,
        )
        # Pluggable providers (e.g. FOMO) only nominate addresses; a failing provider
        # costs nothing but its own nominations.
        for nominator in self.nominators:
            name = f"nominator:{getattr(nominator, 'name', 'unnamed')}"
            try:
                addresses = nominator.nominate(self.config.chain_id)
            except SourceFailure:
                nominate(name, 0, [], "failed")
                continue
            nominate(name, len(addresses), nominated_markets(addresses, now))
        merged = [
            merge_signals(m, signal_rows, now, self.config.signal_window_seconds)
            for m in markets.values()
        ]
        return merged, signal_rows, sources

    def _enrich(self, market: Market, now: int) -> Market:
        price = _first(self.source.price_info(self.config.chain_id, market.token))
        if price is not None:
            market = merge_price(market, price)
        security = self._security(market.token, now)
        if security is not None:
            market = merge_security(market, security)
        advanced = _first(self.source.advanced_info(self.config.chain_id, market.token))
        if advanced is not None:
            first = market.first_trade_at
            if first is None and (created := count(advanced.get("createTime"))) is not None:
                first = created // 1000
            market = replace(
                market,
                dev_rug_count=count(advanced.get("devRugPullTokenCount")),
                first_trade_at=first,
            )
        if self.gmgn is not None:
            # GMGN is an optional second opinion: its absence never blocks, its material
            # disagreement does (via provider_disagreement blockers).
            try:
                info = self.gmgn.token_info(self.config.chain_id, market.token)
                market = cross_check(market, parse_token_info(info.data, market.token))
            except SourceFailure:
                pass
        return market

    def _try_entry(
        self, market: Market, assessment: Assessment, now: int, should_stop: Callable[[], bool]
    ) -> Record:
        size = entry_size(self.state.cash_usd_micros, market.liquidity_usd_micros, self.config)
        if self.executor is not None:
            # The gate's hard per-trade cap bounds the size, so a large wallet still trades.
            size = min(size, self.executor.gate.max_trade_usd_micros)
        if size < 1_000_000:
            return self._record(now, "skip_entry", token=market.token, reason="size_below_1_usd")
        if self.launchpad_safety_evaluator is not None:
            try:
                lp_decision = self.launchpad_safety_evaluator(market, now)
            except SourceFailure:
                return self._record(
                    now,
                    "skip_entry",
                    token=market.token,
                    reason="launchpad_safety_rejected",
                    launchpad_reasons=["launchpad_evidence_unavailable"],
                )
            if lp_decision is not None:
                if lp_decision.status == DecisionStatus.DEFER_UNTIL_DECAY:
                    return self._record(
                        now,
                        "skip_entry",
                        token=market.token,
                        reason="launchpad_defer_until_decay",
                        earliest_safe_entry_ms=lp_decision.earliest_safe_entry_ms,
                        launchpad_reasons=list(lp_decision.reasons),
                    )
                if lp_decision.status != DecisionStatus.APPROVED:
                    return self._record(
                        now,
                        "skip_entry",
                        token=market.token,
                        reason="launchpad_safety_rejected",
                        launchpad_reasons=list(lp_decision.reasons),
                    )
        chain = self.config.chain_id
        buy_capture = self.source.quote(chain, SETTLEMENT, market.token, size)
        buy = parse_quote(
            buy_capture.data,
            chain=chain,
            src=SETTLEMENT,
            dst=market.token,
            amount=size,
            raw_sha256=buy_capture.raw_sha256,
            at=buy_capture.retrieved_at,
        )
        sell_capture = self.source.quote(chain, market.token, SETTLEMENT, buy.amount_out)
        sell = parse_quote(
            sell_capture.data,
            chain=chain,
            src=market.token,
            dst=SETTLEMENT,
            amount=buy.amount_out,
            raw_sha256=sell_capture.raw_sha256,
            at=sell_capture.retrieved_at,
        )
        cost_bps = round_trip_bps(buy, sell, self.config)
        if (
            cost_bps > self.config.max_round_trip_cost_bps
            or max(buy.impact_bps, sell.impact_bps) > self.config.max_impact_bps
        ):
            return self._record(
                now,
                "skip_entry",
                token=market.token,
                reason="round_trip_too_expensive",
                round_trip_cost_bps=cost_bps,
            )
        cost = entry_cost(buy, self.config)
        value = exit_value(sell, self.config)
        units = buy.amount_out
        live: Record = {}
        if self.executor is not None:
            if should_stop():
                return self._record(now, "skip_entry", token=market.token, reason="stop_requested")
            try:
                fill = self.executor.buy(market.token, size)
            except LiveRefused as refused:
                return self._record(
                    now,
                    "skip_entry",
                    token=market.token,
                    reason="live_buy_refused",
                    category=refused.category,
                )
            # Observed fill, not the quote: the cost basis is USDC spent plus the declared gas
            # estimate (paid in ETH), and the units the wallet actually gained. The unwind
            # value is the sell quote rescaled to those units, floored.
            units = fill.token_delta
            gas = self.config.gas_per_leg_usd_micros
            cost = -fill.usdc_delta + gas
            value = value * units // buy.amount_out
            live = {
                "intent_id": fill.intent_id,
                "tx_hash": fill.tx_hash,
                "block_number": fill.block_number,
                "usdc_spent_base_units": -fill.usdc_delta,
                "estimated_gas_usd_micros": gas,
            }
        if live:
            # Wallet USDC moves by the observed delta only; gas is tracked separately.
            self.state.cash_usd_micros += fill.usdc_delta
            self.state.estimated_gas_usd_micros += self.config.gas_per_leg_usd_micros
        else:
            self.state.cash_usd_micros -= cost
        self.state.positions[market.token] = Position(
            token=market.token,
            symbol=market.symbol,
            units=units,
            cost_usd_micros=cost,
            opened_at=now,
            high_water_usd_micros=value,
            last_value_usd_micros=value,
            entry_quote_sha256=buy.raw_sha256,
        )
        return self._record(
            now,
            "paper_buy" if self.executor is None else "live_buy",
            token=market.token,
            symbol=market.symbol,
            cost_usd_micros=cost,
            units=str(units),
            immediate_exit_usd_micros=value,
            round_trip_cost_bps=cost_bps,
            **({"evidence_level": LIVE_EVIDENCE} if live else {}),
            score_bps=assessment.score_bps,
            reasons=list(assessment.reasons),
            smart_wallets=market.smart_wallets,
            quote_sha256=[buy.raw_sha256, sell.raw_sha256],
            **live,
        )

    def tick(
        self,
        now: int,
        *,
        manage_only: bool = False,
        should_stop: Callable[[], bool] | None = None,
    ) -> list[Record]:
        """One bounded cycle; held positions are always checked and exited first.

        Forward-outcome labels and discovery are optional research work: they run only
        after every held position was managed, and a `manage_only` cycle (chosen by the
        runner when the budget covers management but not a full cycle) skips them and
        opens nothing. Deferred labels stay pending; when they are finally queried a
        price outside their lateness window labels them `unobserved`, so a deferral can
        never turn a later price into the decision-time outcome or drop the sample.
        """
        out: list[Record] = []
        calls = self.provider_calls()
        # Paper ticks are deterministic and finish atomically for archive replay.
        stop = should_stop if self.executor is not None and should_stop else lambda: False
        self._manage(now, out, stop)
        if stop():
            out.append(
                self._summary(
                    now, calls, entries_blocked="stop_requested", coverage=_coverage("stopped")
                )
            )
            return out
        if manage_only:
            out.append(
                self._summary(
                    now,
                    calls,
                    cycle="manage_only",
                    deferred=["outcome_labels", "discovery"],
                    coverage=_coverage("deferred_manage_only"),
                )
            )
            return out
        self._label_outcomes(now, out)
        labels = {
            "observed": sum(r["kind"] == "outcome" and r["status"] == "observed" for r in out),
            "unobserved": sum(r["kind"] == "outcome" and r["status"] != "observed" for r in out),
        }
        if any(r["kind"] == "halt_review" for r in out):
            blocked = _coverage("skipped_held_position_unpriceable") | {"labels_emitted": labels}
            out.append(
                self._summary(
                    now, calls, entries_blocked="held_position_unpriceable", coverage=blocked
                )
            )
            return out
        try:
            markets, _, sources = self._discover(now)
        except SourceFailure as error:
            out.append(self._record(now, "discovery_failed", category=error.category))
            failed = _coverage("failed") | {"labels_emitted": labels}
            out.append(self._summary(now, calls, coverage=failed))
            return out
        coverage = _coverage("ok") | {
            "discovered": len(markets),
            "labels_emitted": labels,
            "discovery_sources": sources,
        }
        registration = coverage["label_registration"]
        ranked = sorted(
            markets,
            key=lambda m: (-(m.smart_wallets), -(m.liquidity_usd_micros or 0), m.token),
        )
        open_slots = self.config.max_positions - len(self.state.positions)
        enriched = 0
        for index, listed in enumerate(ranked):
            if stop():
                coverage["not_reached_stop"] = len(ranked) - index
                break
            if enriched >= self.config.enrich_top:
                # Deferred by the quota: never assessed this cycle, never a skip.
                coverage["not_reached_enrich_quota"] = len(ranked) - index
                break
            if listed.token in self.state.positions:
                coverage["held"] += 1
                continue
            if self.state.cooldown_until.get(listed.token, 0) > now:
                coverage["cooling_down"] += 1
                continue
            pre = assess(listed, self.config)
            # Cheap gates first: a blocker that is already a known failure cannot be
            # rescued by enrichment, so it must not cost provider calls.
            if pre.action == "skip" and decided(pre.reasons):
                coverage["rejected_before_enrich"] += 1
                out.append(self._candidate(now, listed, pre, registration))
                continue
            enriched += 1
            try:
                market = self._enrich(listed, now)
            except SourceFailure as error:
                coverage["enrich_failed"] += 1
                out.append(
                    self._record(now, "enrich_failed", token=listed.token, category=error.category)
                )
                continue
            coverage["assessed_after_enrich"] += 1
            assessment = assess(market, self.config)
            out.append(self._candidate(now, market, assessment, registration))
            if assessment.action == "flip" and open_slots > 0:
                submitted = self.submissions()
                try:
                    entry = self._try_entry(market, assessment, now, stop)
                except SourceFailure, ValueError:
                    if self.submissions() != submitted:
                        # A swap was handed to the wallet: this is not a refused quote.
                        raise
                    entry = self._record(
                        now, "skip_entry", token=market.token, reason="quote_refused"
                    )
                out.append(entry)
                open_slots -= entry["kind"] in TRADE_KINDS
        if sum(coverage[f] for f in CANDIDATE_FATES) != coverage["discovered"]:
            raise AssertionError("candidate coverage does not reconcile")  # pragma: no cover
        out.append(self._summary(now, calls, coverage=coverage))
        return out

    def _label_outcomes(self, now: int, out: list[Record]) -> None:
        """Forward-return labels for earlier assessments; failures label `unobserved`."""
        due = self.state.outcomes.due(now)
        prices: dict[str, Any] = {}
        for start in range(0, len(due), 40):
            try:
                batch = self.source.prices(self.config.chain_id, due[start : start + 40])
            except SourceFailure:
                continue
            prices |= parse_prices(batch.data, self.config.chain_id)
        out.extend(
            self._record(now, "outcome", **label)
            for label in self.state.outcomes.label(now, prices)
        )

    def _candidate(
        self, now: int, market: Market, assessment: Assessment, registration: dict[str, int]
    ) -> Record:
        outcome = self.state.outcomes.register_reason(
            market.token,
            market.symbol,
            assessment.action,
            assessment.score_bps,
            now,
            market.price,
        )
        registration[outcome] += 1
        return self._record(
            now,
            "assessment",
            token=market.token,
            symbol=market.symbol,
            action=assessment.action,
            score_bps=assessment.score_bps,
            reasons=list(assessment.reasons),
            liquidity_usd_micros=market.liquidity_usd_micros,
            change_5m_bps=market.change_5m_bps,
            change_1h_bps=market.change_1h_bps,
            smart_wallets=market.smart_wallets,
            label_registration=outcome,
            # The exact input `assess` saw, so the decision can be recomputed offline.
            market=market_to_json(market),
            policy_sha256=self._policy_digest,
        )

    def _summary(self, now: int, calls_before: int, **extra: Any) -> Record:
        s = self.state
        if "coverage" in extra:
            # Label work still pending as this cycle ends, in sample x horizon units: a
            # manage-only or budget-short cycle keeps it here instead of dropping it.
            extra["coverage"] = extra["coverage"] | {"label_backlog": s.outcomes.backlog(now)}
        return self._record(
            now,
            "tick_summary",
            cash_usd_micros=s.cash_usd_micros,
            estimated_gas_usd_micros=s.estimated_gas_usd_micros,
            nav_usd_micros=s.nav(),
            open_positions=len(s.positions),
            # The journal is the source of truth: a kill between journal and state-file
            # writes must never leave a report that disagrees with the retained chain.
            positions=[asdict(p) for p in s.positions.values()],
            realized_pnl_usd_micros=s.realized_pnl_usd_micros,
            closed_trades=s.closed_trades,
            winning_trades=s.winning_trades,
            provider_calls=self.provider_calls() - calls_before,
            **extra,
        )


def state_to_json(state: AgentState) -> dict[str, Any]:
    """Lossless snapshot; prices are exact `numerator/denominator` strings, never floats."""
    return {
        "schema_version": "oasis.spot-agent-state.v1",
        "cash_usd_micros": state.cash_usd_micros,
        "estimated_gas_usd_micros": state.estimated_gas_usd_micros,
        "realized_pnl_usd_micros": state.realized_pnl_usd_micros,
        "closed_trades": state.closed_trades,
        "winning_trades": state.winning_trades,
        "cooldown_until": dict(state.cooldown_until),
        "security": {k: [at, row] for k, (at, row) in state.security.items()},
        "positions": {k: asdict(v) for k, v in state.positions.items()},
        "outcomes": [
            asdict(p) | {"price": f"{p.price.numerator}/{p.price.denominator}"}
            for p in state.outcomes.pending.values()
        ],
    }


def state_from_json(raw: dict[str, Any]) -> AgentState:
    if raw.get("schema_version") != "oasis.spot-agent-state.v1":
        raise ValueError("unknown spot agent state schema")
    tracker = OutcomeTracker()
    for item in raw["outcomes"]:
        pending = Pending(
            **(item | {"price": Fraction(item["price"]), "remaining": tuple(item["remaining"])})
        )
        tracker.pending[(pending.token, pending.at // 60)] = pending
    return AgentState(
        cash_usd_micros=int(raw["cash_usd_micros"]),
        # Additive field: snapshots written before it existed carried no live gas.
        estimated_gas_usd_micros=int(raw.get("estimated_gas_usd_micros", 0)),
        positions={k: Position(**v) for k, v in raw["positions"].items()},
        realized_pnl_usd_micros=int(raw["realized_pnl_usd_micros"]),
        closed_trades=int(raw["closed_trades"]),
        winning_trades=int(raw["winning_trades"]),
        cooldown_until={k: int(v) for k, v in raw["cooldown_until"].items()},
        security={k: (int(v[0]), v[1]) for k, v in raw["security"].items()},
        outcomes=tracker,
    )


def reconcile_with_journal(
    state: AgentState, records: list[Record], schema: str | None = None
) -> AgentState:
    """Make the hash-linked journal authoritative for money state after a restart.

    Refuses exactly when `run_status(schema, records)` is not resumable (interrupted tick,
    unknown or unmatched live orders, persisted or human-review halts, legacy live
    lifecycle), so resume and the report can never disagree. Otherwise cash, positions,
    gas estimate and PnL counters come from the last journaled `tick_summary`; only caches
    (security, cooldowns, pending outcome labels) survive from the state file.
    """
    status = run_status(schema, records)
    if not status["resumable"]:
        reasons = status["blocking_reasons"]
        headline = (
            "journal ends inside an interrupted tick"
            if "interrupted_tick" in reasons
            else "journal has trades but no tick summary"
            if "trades_without_tick_summary" in reasons
            else "live run halted for human review"
        )
        # No later tick summary can clear a live block: reconcile the wallet by hand,
        # then start a new run directory (a fresh live_start reads the wallet's balance).
        raise ValueError(f"{headline} ({', '.join(reasons)}); review before resuming")
    summaries = [i for i, r in enumerate(records) if r["kind"] == "tick_summary"]
    starts = [r for r in records if r["kind"] == "live_start"]
    if not summaries:
        if starts:
            # A live run that never finished a tick starts again from the wallet's cash.
            state.cash_usd_micros = int(starts[0]["cash_usd_micros"])
        return state
    summary = records[summaries[-1]]
    state.cash_usd_micros = int(summary["cash_usd_micros"])
    state.estimated_gas_usd_micros = int(summary.get("estimated_gas_usd_micros", 0))
    state.positions = {p["token"]: Position(**p) for p in summary["positions"]}
    state.realized_pnl_usd_micros = int(summary["realized_pnl_usd_micros"])
    state.closed_trades = int(summary["closed_trades"])
    state.winning_trades = int(summary["winning_trades"])
    return state
