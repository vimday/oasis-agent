---
paths:
  - "src/oasis_agent/{market_data,venues,strategies,portfolio,execution,risk,live,launch}/**/*.py"
  - "src/oasis_agent/{arbitrage,farming}/**/*.py"
  - "contracts/**/*.sol"
  - "tests/**/*{trading,market,venue,execution,arbitrage,portfolio}*.py"
---

# Autonomous trading-system rules

Read `docs/architecture/AUTONOMOUS_TRADING_SYSTEM.md` and the relevant strategy playbook before
changing these paths. Production-facing work also follows
`docs/governance/PRODUCTION_CONTROL_FRAMEWORK.md`; prose is not evidence that a control is enforced.

## Separate truth from intent

- Market data, account/position state, strategy signals, risk decisions, order intents, venue
  acknowledgements, fills, and ledger entries are different immutable types. Do not pass generic
  dictionaries or reuse one as another.
- A price must state venue, instrument, role (`bid`, `ask`, `mark`, `index`, `oracle`, `last`, or
  valuation), event time, receive time, sequence/version, size/depth, and staleness status.
- Mark/index/oracle prices are risk and settlement inputs, not proof of executable arbitrage.
- Position truth comes from independently reconciled authoritative snapshots plus fills. Never let a
  strategy's expected position become the risk engine's actual position.

## Cross-venue execution invariants

- Model non-atomic arbitrage as a hedge group with a state machine, not two unrelated orders.
- Use deterministic client order IDs/idempotency keys. Duplicate events must not duplicate state or
  orders; missing acknowledgements must become `UNKNOWN`, never presumed rejected or filled.
- Only confirmed incremental fills create hedge demand. Reconcile cumulative fills and positions
  before retrying. A hedge correction must not recursively create another correction from stale state.
- If any hedge venue, price, position, margin, borrow/locate, calendar, or order state is stale or
  uncertain, block exposure-increasing orders. Permit only explicitly verified risk reduction.
- Enforce gross, net, basis, venue, instrument, collateral, liquidation-distance, unhedged-time, and
  order-rate limits independently. `maxDelta` is necessary but not sufficient.
- Size against executable depth and worst-case partial fill. Include fees/rebates, funding, borrow,
  FX, dividends/corporate actions, gas, price impact, failed transactions, and capital transfer delay.

## Data-plane requirements

- Track exchange/source time, local receive time, and processing time. Heartbeats prove connection
  liveness, not per-symbol price freshness.
- Detect sequence gaps, duplicate/out-of-order events, clock drift, slow consumers, crossed/locked
  books, impossible spreads, unchanged-value feeds, and disagreement with independent sources.
- Recovery requires snapshot plus replay or an explicit gap. Do not resume trading merely because a
  socket reconnected.

## Risk ownership

- Strategy proposes; risk engine authorizes bounded intent; execution adapter submits; independent
  reconciliation observes reality; signer policy is the final capability boundary.
- AI may analyze and recommend parameters, but may not autonomously widen hard risk limits, change
  production credentials, or promote a strategy stage.
- Every venue adapter implements the same conformance suite for reconnect, duplicate/missing events,
  partial fills, cancels, rejects, rate limits, maintenance, and recovery.
