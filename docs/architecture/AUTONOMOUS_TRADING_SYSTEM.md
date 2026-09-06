# Oasis Autonomous Trading System Architecture

**Status:** Target architecture; most components are not yet implemented  
**Priority:** Phase 1 LP first, reusable foundation for later strategies

## 1. Design objective

Build one capital-safe trading platform that can host different strategy families without giving
strategy code direct control of keys or unrestricted venue APIs. The platform must support:

- Uniswap v4 automated LP and active range management;
- atomic on-chain arbitrage;
- perp-perp, spot-perp, and funding/basis arbitrage;
- equity/equity-future versus on-chain equity-perp arbitrage;
- tightly bounded on-chain short-horizon trading;
- portfolio-level capital and risk allocation.

Speed is an edge only after correctness. The architecture therefore uses a fast path for current,
validated state and a separate independent path that continuously reconstructs truth and can halt
the fast path.

## 2. Authority boundaries

```text
raw venue/chain feeds
        |
        v
normalization + validation ---> immutable evidence/event log
        |
        v
reconciled market/account state <--- independent snapshot reconciler
        |
        v
strategy proposal (no keys, no venue client)
        |
        v
deterministic risk engine + capital reservation
        |
        v
execution coordinator / hedge-group state machine
        |
        v
venue adapter ---> external signer policy ---> venue/chain
        |
        v
fills/receipts ---> ledger + reconciliation ---> SLO/kill switch
```

No model or strategy process can directly reach the signer. The risk service and signer independently
enforce target allowlists, action types, expiry, nonce/idempotency, and amount limits.

## 3. Canonical domain model

All decision records are immutable and schema-versioned.

### Instrument identity

`InstrumentId` must distinguish:

- economic underlying and quote/settlement currency;
- venue and venue-native symbol/contract ID;
- product type: spot, token, perpetual, dated future, equity, FX, LP position;
- multiplier, tick/lot size, decimals, expiry, settlement method, margin mode;
- corporate-action and roll lineage;
- chain ID and contract address where applicable.

Symbol equality is never proof of economic equivalence. A tokenized equity, HIP-3 perp, U.S. share,
ADR, CFD, and future can have different rights, hours, currencies, multipliers, funding, dividends,
settlement, and oracle rules.

### Time and sequence

Every observation carries:

- `event_time`: when the venue/source says the event occurred;
- `receive_time`: when our boundary received it;
- `process_time`: when strategy/risk processed it;
- venue sequence/version or explicit `none`;
- source clock quality and maximum accepted skew.

Freshness is evaluated per instrument and data role. A healthy connection or recent heartbeat does
not make an unchanged quote fresh.

### Price roles

Prices are not interchangeable:

- executable bid/ask plus size and depth;
- local mid/last trade;
- index/oracle price used by a protocol;
- mark price used for margin/liquidation/PnL;
- valuation price used by our ledger;
- theoretical fair value produced by a model.

Arbitrage entry uses worst-case executable prices. Margin monitoring uses the venue's exact mark and
maintenance rules. Accounting uses an independently declared valuation policy.

### State objects

Minimum typed records include:

- `MarketSnapshot` with coherent version vector across legs;
- `AccountSnapshot`, `PositionSnapshot`, `BalanceSnapshot`, `MarginSnapshot`;
- `StrategyProposal` with edge decomposition and expiry;
- `RiskDecision` with hard-limit utilization and reasons;
- `OrderIntent`, `VenueOrder`, `OrderEvent`, `Fill`;
- `HedgeGroup` linking every leg and residual exposure;
- `CapitalReservation` preventing competing strategies from double-spending;
- `LedgerEntry` by venue/account/instrument/currency;
- `ReconciliationResult` and `IncidentTrigger`.

## 4. Data plane

Each venue/source adapter implements:

1. authenticated/public connectivity with explicit version and rate-limit policy;
2. snapshot bootstrap;
3. incremental update handling with duplicate/out-of-order/sequence-gap detection;
4. three-clock timestamps and latency metrics;
5. heartbeat/hung-connection detection;
6. bounded reconnect with replay or snapshot recovery;
7. slow-consumer backpressure behavior;
8. immutable raw evidence before normalization where feasible;
9. independent health status per stream, symbol, and account endpoint.

The reconciler regularly queries authoritative positions, balances, open orders, fills, nonces, and
margin. It compares them with the event-built state. Differences above tolerance halt exposure
growth; they are never auto-corrected by repeatedly trading against an untrusted local delta.

## 5. Strategy interface

A strategy is a pure proposal engine over a coherent snapshot:

```text
propose(snapshot, portfolio_state, config) -> zero or more StrategyProposal
```

Every proposal specifies:

- all legs, side, quantity, order constraints, and hedge relation;
- gross executable edge and each expected cost;
- residual factor exposure under partial-fill scenarios;
- expiry in venue time/block and maximum acceptable state version drift;
- required capital, margin, borrow, approvals, and rate-limit capacity;
- worst-case loss before hedge, after hedge, and during unwind;
- evidence references and confidence;
- kill/unwind action.

Strategies cannot mutate limits, submit orders, query secret-bearing clients, or mark themselves
successful.

## 6. Risk hierarchy

Risk is enforced from narrowest to widest scope:

```text
order -> hedge group -> instrument -> venue/account -> strategy -> chain -> portfolio -> session/day
```

Hard controls include:

- gross and net delta/notional; factor and basis exposure;
- unhedged quantity multiplied by time and stressed volatility;
- margin utilization and distance to liquidation under venue-exact formulas;
- collateral, stablecoin, bridge, custodian, oracle, sequencer, and smart-contract concentration;
- order count/rate, cancel rate, API quota, gas, slippage, market impact, and daily turnover;
- realized/unrealized loss, drawdown, expected shortfall, and loss velocity;
- maximum unknown/stale state duration and reconciliation residual;
- capital transfer and withdrawal latency.

Limits only become tighter automatically. Raising limits or enabling a new capability requires a
versioned reviewed configuration and explicit operator approval.

## 7. Execution coordination

Atomic routes should execute atomically where the chain/protocol truly guarantees it. Everything
else is a distributed saga that can partially fail.

Generic hedge-group states:

```text
PROPOSED -> RESERVED -> QUOTING -> PRIMARY_WORKING
         -> PARTIALLY_FILLED -> HEDGE_REQUIRED -> HEDGING
         -> HEDGED -> RECONCILING -> CLOSED

Any state -> CANCEL_PENDING -> UNWINDING -> CLOSED
Any state -> UNKNOWN -> HALTED (only verified risk reduction allowed)
```

Rules:

- transitions are idempotent and append-only;
- only confirmed fill deltas create hedge demand;
- unknown order status is not equivalent to cancellation;
- retry reuses or safely relates idempotency keys;
- the coordinator handles partial fills, hedge minimums, maximum child size, and residual dust;
- every group has an unhedged notional-time budget and deadline;
- risk-reducing orders have a separately constrained path, never an unrestricted bypass;
- completion requires venue/account/ledger reconciliation, not merely acknowledgements.

## 8. On-chain execution

On-chain proposals additionally carry exact chain/block/hash, state overrides if any, calldata hash,
value, gas bounds, access list, nonce, deadline, minimum output, approval delta, target bytecode hashes,
and expected balance/event/state changes.

Required stages:

```text
build -> static policy -> pinned simulation -> adversarial simulation
      -> sign-policy authorization -> submit -> inclusion tracking
      -> finality/reorg tracking -> post-state reconciliation
```

Use private transaction/bundle paths where supported and beneficial, but treat relay/builder privacy,
inclusion, and atomicity as explicit assumptions. Public fallback must be separately risk-scored; do
not leak a profitable or vulnerable transaction merely because a private submission timed out.

## 9. Portfolio capital allocator

The allocator consumes validated strategy return/risk distributions, current positions, liquidity,
capacity, transfer time, and correlated stress losses. It allocates against marginal portfolio
utility—not standalone APY.

Capital buckets are isolated so a failure in meme trading or a perp venue cannot consume LP unwind
capital. Maintain operational reserves for gas, margin top-ups, emergency hedging, and withdrawals.
Do not count unrealized cross-margin profit or slow-to-transfer collateral as immediately fungible.

## 10. Observability and reliability

Core SLIs include:

- feed age and sequence-gap duration per instrument;
- event-to-decision and decision-to-ack/fill latency distributions;
- order/fill/position reconciliation lag and residual;
- hedge completion time and unhedged notional-seconds;
- simulation-to-inclusion state drift;
- reject, cancel failure, reconnect, rate-limit, revert, and dropped-message rates;
- risk-service, signer, kill-switch, and unwind availability;
- PnL attribution completeness.

Burning a reliability, loss, or reconciliation error budget freezes strategy promotion and capital
scaling. Alerts must prescribe an action: page now, ticket with deadline, or retain as diagnostic log.

## 11. Testing architecture

- Deterministic venue simulators with controllable clocks, gaps, duplicates, reordering, partial fills,
  disconnects, halts, rate limits, and contradictory snapshots.
- Adapter conformance tests shared across every venue.
- Property tests for exactly-once economic effects under at-least-once messages.
- Model-based tests for order and hedge-group state machines.
- Historical event replay and counterfactual executable fill models.
- Chaos tests for dead feeds, frozen values, clock jumps, corrupt messages, signer refusal, RPC forks,
  chain reorgs, and process restarts at every state transition.
- Shadow/canary tests with independent reconciliation and automatic rollback.

## 12. Implementation sequence

1. Finish Phase 1 canonical v4 decoding, per-token ledger, evidence, replay, and hook verification.
2. Generalize immutable event, clock, instrument, ledger, and reconciliation primitives.
3. Implement one read-only venue adapter and its conformance simulator.
4. Implement portfolio truth and risk engine before any cross-venue executor.
5. Add shadow-only perp basis/funding analysis.
6. Add the hedge-group coordinator and simulated two-leg failures.
7. Add minimal-capital isolated canaries only after recovery drills.
8. Add equity bridge and regulatory/calendar/borrow controls as a separate capability.

