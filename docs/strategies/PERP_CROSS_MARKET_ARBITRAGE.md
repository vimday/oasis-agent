# Perpetual and Equity–Crypto Cross-Market Arbitrage Playbook

**Status:** Research specification; no authorization to trade  
**Use:** Phase 2+ after the Phase 1 safety foundation is reusable

## 1. Strategy families

### Spot–perpetual basis and funding

Hold offsetting spot and perp exposure to collect expected funding and/or basis convergence. Net edge:

```text
expected funding received
+ expected basis convergence
- spot and perp execution costs
- borrow/financing and collateral yield foregone
- hedge rebalance and transfer costs
- liquidation, ADL, venue, oracle, and collateral risk premium
```

Funding is stochastic. A displayed or predicted rate is not locked revenue, and the position may be
liquidated before funding/convergence is realized.

### Perp–perp cross-venue arbitrage

Take opposite positions in economically equivalent perpetuals when executable basis plus expected
funding differential exceeds all costs and risk reserves. Contract multipliers, settlement assets,
oracle definitions, mark prices, margin tiers, funding intervals/caps, and ADL/liquidation rules must
be normalized explicitly.

### Equity/equity-future–equity-perp arbitrage

Quote or take liquidity on the crypto venue against executable fair value from a regulated equity or
futures venue, then hedge confirmed fills. This is not atomic. It is a latency-sensitive inventory
strategy with market-hours, borrow, FX, dividend, corporate-action, settlement, and regulatory risk.

### Atomic on-chain arbitrage

Prefer atomic execution when all legs share a transaction and revert semantics truly remove leg risk.
Profitability still requires state-current simulation, gas/builder payment, approval/transfer behavior,
MEV competition, inclusion probability, and post-state reconciliation.

## 2. Opportunity equation

For a sell leg `S` and buy leg `B`, calculate on executable depth, not mids:

```text
edge_net(q) = proceeds_bid_S(q)
            - cost_ask_B(hedge_ratio * q)
            + expected_funding_and_carry(q, horizon)
            - venue_fees_and_rebates(q)
            - borrow_and_financing(q, horizon)
            - gas_and_priority_cost
            - FX_and_conversion_cost
            - expected_hedge_slippage
            - latency_and_gap_risk_reserve
            - capital_and_tail_risk_charge
```

Entry requires positive conservative edge under partial fills and stressed hedge prices, not only
positive expected edge. Capacity is the largest size whose marginal stressed edge remains positive.

## 3. Fair value and market state

Maintain separate estimates for:

- executable hedge bid/ask by size;
- crypto venue executable book;
- venue index/oracle and mark price;
- expected dividends, funding, borrow, FX, and futures carry;
- session state: regular, extended, overnight, auction, halt, closed, stale;
- confidence and disagreement among independent sources.

For equity-linked products, map every contract to the exact economic claim. Adjust for share splits,
special/ordinary dividends, mergers, symbol changes, futures rolls, currency, taxes, and settlement.
Disable trading until a corporate action is reflected consistently across every leg and ledger.

## 4. Two-leg execution policy

A common pattern is maker-first on the less efficient venue, hedge-after-fill on the deeper venue.
It earns spread but creates unhedged latency. Required controls:

- quote size and distance vary with executable hedge depth, volatility, inventory, latency, margin,
  session quality, and capital availability;
- confirmed primary fills create hedge demand; acknowledgements do not;
- hedge small fills may accumulate to avoid minimum commissions, but accumulation is bounded by
  notional, delta, volatility, and time—not only quantity;
- child hedge orders have maximum size/impact and hard slippage bounds;
- cancel/requote thresholds include queue-position loss and adverse selection;
- exposure-increasing quotes stop before the hedge venue closes, halts, disconnects, reaches price
  limits, loses borrow, or becomes stale;
- unknown status on either leg forces reconciliation and blocks new risk;
- emergency unwind chooses the venue/path minimizing stressed total loss, not nominal spread.

The system must simulate primary partial fill, hedge partial fill, hedge rejection, both fills in
opposite order, late fills after cancel, duplicate execution reports, and position disagreement.

## 5. The stale-data/phantom-delta incident class

The referenced CBB field report describes an equity-perp bot whose IBKR data stopped refreshing. The
bot repeatedly treated a false position difference as real and accumulated roughly $120M of gold
futures shorts before shutdown, with a reported $1.1M loss. Treat this as an unverified field report,
but the failure class is fundamental.

Mandatory defenses:

1. **Per-symbol freshness:** validate exchange event time, receive time, update cadence, sequence, and
   expected session activity. A live socket or account heartbeat is insufficient.
2. **Frozen-value detection:** a repeatedly delivered but unchanged value may be stale. Compare book,
   trades, account snapshots, and independent reference feeds.
3. **Independent position truth:** reconstruct from fills and periodically replace/verify with an
   authoritative account snapshot. Track snapshot version and reconciliation age.
4. **Idempotent correction:** a discrepancy gets an incident/reconciliation ID. Do not send another
   correction until prior order/fill state is known or its worst-case fill is included.
5. **Monotonic exposure guard:** while any relevant state is stale/unknown, gross exposure cannot
   increase. Only verified reduce-only actions are permitted.
6. **Three-way limits:** hard maximum gross position, maximum net hedge delta, and maximum
   unhedged-notional-seconds. Any one can halt the instrument.
7. **Independent kill switch:** a separate process computes positions and margin from authoritative
   sources and can disable the strategy/signing capability.
8. **Restart safety:** after reconnect or process restart, recover snapshots, open orders, fills, and
   nonces before quoting. Never resume from local memory alone.

## 6. Perp-specific risk

- Use the venue's exact oracle, mark, maintenance-margin, tier, and liquidation formulas.
- Continuously stress mark/oracle/book divergence, funding jumps, collateral depeg, OI caps, ADL,
  isolated versus cross margin, venue halt, withdrawal delay, and socialized/backstop liquidation.
- Funding PnL is reconciled from ledger events, not estimated rates. Track interval boundaries and the
  price used to compute notional.
- Model rate limits and reserve cancel capacity. Market making that cannot cancel is not controlled.
- Isolate API wallets/signers by process/account where nonce domains or permissions require it. Never
  reuse credentials in ways that enable replay after pruning/deregistration.
- Prefer isolated/capped accounts for canaries. Cross/portfolio margin can hide strategy losses and
  propagate liquidation across otherwise unrelated positions.

## 7. TradFi-specific risk

- Confirm trading permissions, live market-data subscriptions, contract definition, exchange, route,
  multiplier, currency, and session before enabling a symbol.
- Consume order status and executions; brokers may emit duplicate status events or omit intermediate
  callbacks. Reconciliation must be based on executions and authoritative open-order/position queries.
- Encode regular/extended/overnight sessions, holidays, early closes, auctions, regulatory halts,
  limit-up/limit-down, futures price limits, and broker maintenance windows.
- Short hedges require available borrow/locate and may face recalls, fees, price-test restrictions,
  buy-ins, or inability to increase the short. Compliance requirements are hard eligibility inputs.
- Model dividends, withholding/tax treatment, corporate actions, settlement timing, cash availability,
  margin interest, FX, futures expiry/roll, and weekend/holiday basis risk.
- Do not infer legal permission from technical API capability. Jurisdiction, entity/account status,
  market-data license, exchange rules, and broker terms require explicit review.

## 8. Capital-aware quoting

The field report's useful idea is to make entry thresholds conditional on scarce hedge capital:

- when hedge liquidity/margin is scarce, widen entry thresholds, reduce quotes, prioritize closing
  positions, and charge an internal shadow price for capital;
- when capacity is abundant, thresholds may tighten only within pre-approved limits;
- capital transfer time and banking/bridge/custody limits are part of capacity;
- never assume profitable open positions can be liquidated or collateral moved instantly.

This control must be deterministic and bounded. An AI may recommend parameters but cannot silently
widen risk limits or consume emergency liquidity.

## 9. Latency engineering

Measure latency by stage: source event -> receive -> normalize -> strategy -> risk -> submit -> venue
ack -> fill -> hedge submit -> hedge fill -> reconcile. Optimize the p95/p99 critical path only after
correctness and profiling.

Use direct/licensed feeds where economically justified, but implement heartbeats, slow-reader alarms,
gap recovery, snapshots/replay, clock synchronization, schema/version monitoring, and deterministic
failover. Faster stale data is still stale data.

## 10. Promotion criteria

Before any live capital:

- venue adapters pass conformance and chaos tests;
- historical replay includes real depth, trading sessions, funding, borrow, fees, halts, and outages;
- shadow mode reconciles positions/orders/fills with zero material unexplained residual;
- the hedge coordinator survives forced disconnects and every partial-fill permutation;
- risk/kill-switch drills demonstrate bounded halt and unwind time;
- canary uses isolated credentials, accounts, collateral, instruments, and minimal limits;
- net returns remain positive after latency/tail-risk reserves and multiple-testing correction.

## 11. Lessons accepted and rejected from social posts

Accepted as hypotheses or controls:

- move quickly in discovering temporary market-structure edges;
- maker-first/hedge-after-fill can capture spread when leg risk is explicitly bounded;
- minimum hedge sizes and maximum child sizes can balance commissions and impact;
- vary entry threshold with hedge liquidity/capital availability;
- analyze complete trade/fill history to locate loss sources;
- stale state and repeated correction need independent hard controls.

Rejected as engineering practice:

- testing material risk directly in production;
- treating past reported profit as evidence that an edge persists;
- assuming the fastest feed is a source of truth without freshness/reconciliation;
- continuing to scale after a loss without an independent incident review and staged recovery;
- competing on speed before correctness, risk isolation, and recovery are proven.

