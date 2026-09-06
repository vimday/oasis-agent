# Oasis Agent Review Policy

Review as if the change can move irreversible on-chain capital. Find root causes, not style trivia.
Trace the complete data and value flow and compare implementation against canonical protocol source.

## Severity

- **P0 — stop/never deploy:** possible theft or unbounded loss; arbitrary signing/calls; leaked keys;
  incorrect asset/fee/delta accounting that can authorize a losing trade; callback authentication or
  reentrancy failure; unsafe upgrade/admin power; fabricated profitability or bypassed risk gate.
- **P1 — must fix before merge:** wrong ABI/topic/sign/bounds/direction semantics; stale or cross-block
  decisions; provenance loss; fail-open parsing/config/retry behavior; backtest leakage; missing costs;
  deterministic replay failure; tests that validate the wrong protocol behavior.
- **P2 — fix before production stage:** poor observability, missing stress/fuzz/fork coverage, scalability
  or reliability weakness, ambiguous units/types, insufficient rollback/incident handling.
- **P3 — improvement:** localized maintainability or clarity issue with no plausible effect on capital,
  security, experiment validity, or operations.

## Required review lenses

1. **Protocol correctness:** hook flags, callback ABI/selectors, PoolKey/PoolId, delta signs, fee packing,
   exact-input/output semantics, settlement, event topics, numeric widths, rounding.
2. **Security:** authority, reentrancy, arbitrary external calls, malicious tokens, MEV/oracle manipulation,
   denial of service, upgrades, pause/unwind, secret and dependency supply-chain exposure.
3. **Financial validity:** per-asset conservation, PnL attribution, LVR/IL, fee-volume response, gas/failed
   execution, benchmark fairness, leakage/overfit/multiple trials, tail loss and capacity.
4. **Distributed systems:** pinned snapshots, reorgs/finality, RPC disagreement, idempotency, timeouts,
   retries, nonce races, simulation-to-inclusion drift, duplicate/partial events, per-symbol freshness,
   venue sequence gaps, order-status uncertainty, and restart recovery.
5. **Test credibility:** would the test fail for the real bug; does it use canonical vectors; are both
   directions and boundaries covered; are mocks hiding protocol behavior; were tools actually run.
6. **Cross-market risk:** instrument equivalence, multiplier/FX/funding/borrow/dividend/corporate-action
   treatment, market sessions/halts, margin/liquidation, partial fills, hedge latency, and phantom delta.

For every finding, give the smallest triggering scenario, capital/security consequence, exact file/line,
and the invariant violated. Do not approve because tests pass if the tests encode incorrect semantics.
Do not spend review space on formatting while P0-P2 findings remain.
