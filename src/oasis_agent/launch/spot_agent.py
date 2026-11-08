"""Live shadow spot agent: discover -> enrich -> decide -> paper-execute -> manage exits.

One `tick()` is one bounded decision cycle over fresh OKX onchainos claims. It returns
append-only decision records; the caller journals them. Without an executor the book is
paper-only and nothing here can sign or broadcast. With an operator-gated `LiveExecutor`
the same decisions trade the Agentic Wallet: cash is the wallet's own USDC balance and
positions come from observed fills. `execution_eligible` stays False on every record; a
record is never a permit.
"""

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from fractions import Fraction
from typing import Any, Literal

from .discovery_slot import DiscoveryProvider, nominated_markets
from .gmgn_source import GmgnSource, cross_check, parse_token_info
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
    merge_price,
    merge_security,
    merge_signals,
    security_flags,
)
from .outcomes import OutcomeTracker, Pending, parse_prices
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
from .spot_executor import LiveExecutor, LiveRefused
from .spot_lifecycle import TRADE_KINDS, run_status

Record = dict[str, Any]
MAX_MISSED_MARKS = 3
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


def _rows(capture: Capture) -> list[Any]:
    return capture.data if isinstance(capture.data, list) else []


def _first(capture: Capture) -> dict[str, Any] | None:
    data = capture.data
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        return data[0]
    return data if isinstance(data, dict) else None


class SpotAgent:
    def __init__(
        self,
        source: OnchainosSource,
        config: AgentConfig,
        state: AgentState | None = None,
        clock: Callable[[], float] | None = None,
        gmgn: GmgnSource | None = None,
        executor: LiveExecutor | None = None,
        nominators: Sequence[DiscoveryProvider] = (),
    ):
        self.source = source
        self.gmgn = gmgn
        self.executor = executor
        self.nominators = tuple(nominators)
        self.mode = "shadow" if executor is None else "live"
        self.config = config
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

    def _manage(self, now: int, out: list[Record]) -> None:
        for token, position in list(self.state.positions.items()):
            flags: tuple[str, ...] | None
            try:
                row = self._security(token, now)
                flags = None if row is None else security_flags(row)[0]
                value, quote_hash = self._sell_quote(position, now)
            except (SourceFailure, ValueError):
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
                    continue
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

    def _discover(self, now: int) -> tuple[list[Market], list[Any]]:
        hot = self.source.hot_tokens(self.config.chain_id)
        markets: dict[str, Market] = {}
        for row in _rows(hot):
            if isinstance(row, dict) and (m := market_from_hot(row, now)) is not None:
                markets.setdefault(m.token, m)
        try:
            signal_rows = _rows(self.source.signals(self.config.chain_id))
        except SourceFailure:
            signal_rows = []  # Signals only raise a score; absence never admits a token.
        # Smart-money buys are a discovery source of their own: tokens they name join the
        # pool with only the fields the signal states. Unstated liquidity and age stay
        # unknown and block entry unless enrichment (price-info/advanced-info) fills them.
        for row in signal_rows:
            if (m := market_from_signal(row, now)) is not None:
                markets.setdefault(m.token, m)
        # Pluggable providers (e.g. FOMO) only nominate addresses; a failing provider
        # costs nothing but its own nominations.
        for nominator in self.nominators:
            try:
                addresses = nominator.nominate(self.config.chain_id)
            except SourceFailure:
                continue
            for m in nominated_markets(addresses, now):
                markets.setdefault(m.token, m)
        merged = [
            merge_signals(m, signal_rows, now, self.config.signal_window_seconds)
            for m in markets.values()
        ]
        return merged, signal_rows

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

    def _try_entry(self, market: Market, assessment: Assessment, now: int) -> Record:
        size = entry_size(self.state.cash_usd_micros, market.liquidity_usd_micros, self.config)
        if self.executor is not None:
            # The gate's hard per-trade cap bounds the size, so a large wallet still trades.
            size = min(size, self.executor.gate.max_trade_usd_micros)
        if size < 1_000_000:
            return self._record(now, "skip_entry", token=market.token, reason="size_below_1_usd")
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

    def tick(self, now: int) -> list[Record]:
        """One bounded cycle. Exits are managed before any new entry is considered."""
        out: list[Record] = []
        calls = self.provider_calls()
        self._label_outcomes(now, out)
        self._manage(now, out)
        if any(r["kind"] == "halt_review" for r in out):
            out.append(self._summary(now, calls, entries_blocked="held_position_unpriceable"))
            return out
        try:
            markets, _ = self._discover(now)
        except SourceFailure as error:
            out.append(self._record(now, "discovery_failed", category=error.category))
            out.append(self._summary(now, calls))
            return out
        ranked = sorted(
            markets,
            key=lambda m: (-(m.smart_wallets), -(m.liquidity_usd_micros or 0), m.token),
        )
        open_slots = self.config.max_positions - len(self.state.positions)
        enriched = 0
        for listed in ranked:
            if enriched >= self.config.enrich_top:
                break
            if (
                listed.token in self.state.positions
                or self.state.cooldown_until.get(listed.token, 0) > now
            ):
                continue
            pre = assess(listed, self.config)
            # Cheap gates first: a blocker that is already a known failure cannot be
            # rescued by enrichment, so it must not cost provider calls.
            if pre.action == "skip" and decided(pre.reasons):
                out.append(self._candidate(now, listed, pre))
                continue
            enriched += 1
            try:
                market = self._enrich(listed, now)
            except SourceFailure as error:
                out.append(
                    self._record(now, "enrich_failed", token=listed.token, category=error.category)
                )
                continue
            assessment = assess(market, self.config)
            out.append(self._candidate(now, market, assessment))
            if assessment.action == "flip" and open_slots > 0:
                submitted = self.submissions()
                try:
                    entry = self._try_entry(market, assessment, now)
                except (SourceFailure, ValueError):
                    if self.submissions() != submitted:
                        # A swap was handed to the wallet: this is not a refused quote.
                        raise
                    entry = self._record(
                        now, "skip_entry", token=market.token, reason="quote_refused"
                    )
                out.append(entry)
                open_slots -= entry["kind"] in TRADE_KINDS
        out.append(self._summary(now, calls))
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

    def _candidate(self, now: int, market: Market, assessment: Assessment) -> Record:
        self.state.outcomes.register(
            market.token,
            market.symbol,
            assessment.action,
            assessment.score_bps,
            now,
            market.price,
        )
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
        )

    def _summary(self, now: int, calls_before: int, **extra: Any) -> Record:
        s = self.state
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
