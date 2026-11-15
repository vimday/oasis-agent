# Oasis Agent Long-Term Profit Roadmap

**Status:** Strategy dependencies and promotion order

**Updated:** 2026-11-14

## 1. Portfolio thesis

Oasis Agent should become a portfolio of independently measurable edges on one shared safety and
execution platform. It must not depend on one token, pool, chain, exchange, market regime, or model.
The long-run objective is durable compounded net asset value, not maximum trade count or displayed
APR.

Strategy priority follows four rules:

1. prefer atomic or naturally hedged payoffs over exposed multi-leg execution;
2. prefer edges explained by market structure over pure price prediction;
3. reuse verified data, ledger, risk, signer, and reconciliation infrastructure;
4. fund a strategy only when its marginal portfolio return exceeds its new tail and operational risk.

Every strategy begins in research, graduates through shadow and isolated canary, and can return to
zero allocation when capacity, competition, or regime changes remove the edge.

## 2. Ordered strategy program

| Stage | Strategy family | Candidate edge | Main reason for order |
|---|---|---|---|
| 1 | Robinhood v4 automated LP | Dynamic fee, range, routing, and reliable automation | Phase 1 benchmark for the core chain ledger and risk loop |
| 2 | Same-chain atomic arbitrage | Price inconsistency across pools/routes after all costs | Atomic settlement bounds leg risk and reuses v4 state |
| 3 | Crypto spot-perp/perp-perp basis | Funding, basis convergence, maker spread | Market-structure edge, but introduces venue and liquidation risk |
| 4 | On-chain inventory-aware market making | Quote spread plus rebates/fees net of adverse selection | Reuses fee/toxicity and execution models across venues |
| 5 | Equity/perp/tokenized-equity arbitrage | Session, oracle, basis, funding, and conversion dislocations | Valuable but non-atomic, regulated, calendar-sensitive, and capital intensive |
| 6 | Short-horizon meme trading | Attention/liquidity/flow regime signals | Highest contract, gap, manipulation, and model-decay risk |
| 7 | Cross-strategy allocator | Diversification and scarce-capital allocation | Useful only after multiple strategies have honest live distributions |

Cross-chain arbitrage is not an early target. Bridge latency, finality, message failure, fragmented
inventory, and correlated bridge risk often dominate an apparent price gap. Begin with inventory
already isolated on each chain and never make a bridge completion part of an atomic-profit claim.

This does not serialize research by chain. Ethereum, Base, Robinhood and Elysium should use shared
EVM research interfaces concurrently, with separate deployment identities, inventory, evidence and
promotion gates. The [multi-chain and lending experiment](MULTICHAIN_LENDING_EXPERIMENT.md) adds
bounded directory discovery and synthetic oracle/spot divergence accounting. A public API refusing
a chain is an availability gap, not evidence that the chain lacks a deployment or opportunity.

## 3. Stage 1 — Robinhood v4 automated LP

The detailed normative plan is
[`ROBINHOOD_V4_AUTOLP_BLUEPRINT.md`](ROBINHOOD_V4_AUTOLP_BLUEPRINT.md). Its reusable outputs are more
valuable than a single pool:

- exact EVM/v4 event and state reconstruction;
- per-token double-entry accounting and markout/LVR attribution;
- executable quote and route comparison;
- toxicity, volatility, inventory, and fee-elasticity features;
- deterministic capital/risk policy and external signer boundary;
- idempotent transaction lifecycle, finality, and reorg handling;
- shadow/canary promotion, evidence manifests, and incident response.

The benchmark PONS/USDG pool is not automatically the production pool. Pair selection must account
for token authority, reference-price quality, organic route demand, competing depth, exit liquidity,
and whether outside liquidity is permitted.

## 4. Stage 2 — same-chain atomic arbitrage

Research price inconsistencies among v2/v3/v4 pools, hook pools, aggregators, and liquid staking or
wrapper conversions on the same chain. Profit is evaluated from executable state:

```text
worst_case_net = guaranteed_outputs
               - guaranteed_inputs
               - pool/protocol/hook fees
               - L2 execution and L1 calldata gas
               - builder/relay/payment cost
               - state-drift and revert buffer
```

Requirements:

- all legs settle atomically or the opportunity is classified as inventory-backed, not atomic;
- route targets, callback callers, tokens, code hashes, approvals, and loan sources are allowlisted;
- the exact transaction is simulated at a pinned head and expires quickly;
- reserve and transfer-tax tokens are rejected unless exact behavior is modeled;
- private submission is treated as an assumption, not a privacy or inclusion guarantee;
- failed/reverted transaction cost and competition-induced inclusion probability enter expected PnL;
- no sandwiching, deceptive flow, oracle manipulation, griefing, or fake volume.

Builder and inclusion-cost rules:

- The private-submission bid cap is an explicit owner-approved parameter. Do not infer a default from
  a third-party observed margin or builder payment.
- The cap applies per attempt and covers priority fee, direct coinbase/validator transfer, and any
  bundled payment; compare the total against the conservative gross edge, not a midpoint estimate.
- Reverted, dropped, replaced, and non-included attempts are expensed at realized cost; they do not
  restore or enlarge the next attempt's cap.
- Any builder/relay payment is conditional on the exact output check and an allowlisted beneficiary.
  The signer compares the broadcast bytes to the authorized envelope before release.
- Undisclosed last-look, selective session cancellation, or any practice that disadvantages
  counterparties without disclosure is not permitted.

Named market-maker and searcher analyses are hypotheses about competition and informed flow, not
evidence of our edge. Before they affect pool selection or LP economics, verify code, balances,
approvals, and flow at a pinned block with declared sources.

Start with two-pool cycles and a small token universe. Add graph search only after the exact reference
implementation and pruning rules are tested against false-positive/revert rates.

## 5. Stage 3 — perp and basis arbitrage

Candidate modes:

- spot-perp cash-and-carry or reverse cash-and-carry;
- perp-perp funding and mark divergence across decentralized venues;
- dated future versus perp basis when a liquid expiry venue exists;
- maker on one venue with a taker or inventory hedge on another;
- delta-neutral LP inventory hedging, only when hedge cost improves total LP tail-adjusted return.

The edge is not the displayed funding rate. Model executable spread, funding timing/caps, maker fill
probability, taker fees, borrow, collateral yield, margin, liquidation, oracle/mark divergence, API
failure, withdrawal delay, and the cost of a partial hedge.

Every non-atomic trade is a hedge-group saga. Only confirmed fills create hedge demand; unknown order
state blocks new risk; unhedged notional multiplied by time has a hard budget. Position truth comes
from independent account reconciliation, not repeated hedges against one possibly stale endpoint.

Promotion begins with one pair, two venues, isolated margin, no cross-collateral assumption, and a
pre-funded unwind path on both venues. See
[`PERP_CROSS_MARKET_ARBITRAGE.md`](PERP_CROSS_MARKET_ARBITRAGE.md).

## 6. Stage 4 — inventory-aware on-chain market making

Generalize the v4 fee/range controller into a venue-independent quote engine:

```text
reservation_price = robust_fair_value - inventory_skew - risk_buffer
half_spread       = fees + volatility + toxicity + latency + hedge_cost + profit_margin
```

Quote sizes and spreads respond to depth, inventory, fill probability, markout, and hedge capacity.
The engine must distinguish price alpha from spread capture and rebates. It cancels or widens when
reference state, inventory, hedge venue, sequencer, or account state is stale.

The research question is whether active quoting improves on passive LP after gas, selection, failed
cancels, hedge slippage, and operational burden. The system must never spoof, layer, wash trade, or
create misleading liquidity.

## 7. Stage 5 — equity–crypto cross-market arbitrage

Research only after the multi-venue state machine has survived crypto canaries. Candidate relationships
include U.S. equity or future versus a tokenized-equity/perpetual product, and related basket/ETF
dislocations.

Economic equivalence must be proven rather than inferred from a ticker. Normalize legal/economic
rights, multiplier, currency, dividend and corporate-action treatment, oracle/index construction,
trading hours, settlement, redemption, borrow/locate, margin, funding, expiry, and issuer/custody risk.

The major edge windows may occur exactly when a hedge is unavailable: equity close, halt, limit state,
borrow recall, venue maintenance, oracle freeze, or crypto weekend. Entry therefore requires a stressed
unhedged-loss bound and pre-funded exit on each side. A large apparent spread with a closed or
non-executable hedge leg is exposure, not arbitrage.

The supplied practitioner report is incorporated as a failure lesson: stale position data can cause
repeated over-hedging. The permanent control is idempotent fill-ledger reconciliation and an
unhedged-notional-time budget, not a faster polling loop.

## 8. Stage 6 — bounded meme trading

This is last because apparent alpha is entangled with contract control, thin exit liquidity,
adversarial order flow, gaps, social manipulation, and extremely short half-life.

Allowable research signals include verified liquidity/holder changes, executable order flow, price
impact, route quality, contract events, and public attention measures with point-in-time timestamps.
No strategy may trade on fabricated promotion, coordinated pumps, private victim targeting, or market
manipulation.

Before any position:

- simulate both buy and sell with the exact token and route;
- inspect proxy/admin, mint, freeze, blacklist, tax, pause, callback, and liquidity-removal powers;
- cap exposure by independently executable exit depth and stress token value to zero;
- use exact approvals, short deadlines, maximum slippage/impact, and isolated capital;
- define time stop, liquidity stop, authority-change stop, loss stop, and signer kill switch;
- attribute PnL after failed transactions, MEV, tax, price impact, and unsold residual inventory.

Prediction models remain proposal generators. They cannot change limits, choose arbitrary contracts,
or bypass deterministic sellability and execution policy.

## 9. Stage 7 — portfolio allocator

Capital allocation is a constrained optimization over honest live distributions:

```text
maximize expected compounded net return
subject to drawdown, expected shortfall, liquidity, capacity, concentration,
          operational, venue, chain, oracle, bridge, signer, and model-risk limits
```

Allocation uses marginal contribution, not standalone Sharpe or APR. Correlate strategies by common
token, stablecoin, issuer, chain, sequencer, PoolManager/hook, oracle, bridge, venue, custodian,
collateral, and failure mode. LP unwind ETH, perp margin reserve, and incident cash remain segregated.

Scale-up is slow and reversible; scale-down is fast and automatic. A strategy receives zero capital
when its lower-bound edge is negative, evidence is stale, reliability budget is exhausted, or its risk
duplicates a better strategy.

## 10. Shared research and production standards

All stages use the same promotion doctrine:

```text
idea -> falsifiable experiment -> historical replay -> adversarial simulator/fork
     -> live shadow -> isolated canary -> bounded production -> gradual scaling
```

Every report includes:

- exact economic hypothesis and invalidating observations;
- point-in-time dataset/code/config hashes and untouched evaluation window;
- executable rather than midpoint prices;
- all protocol, venue, gas, funding, borrow, hedge, impact, failure, and opportunity costs;
- confidence interval, sample size, drawdown, tail loss, turnover, capacity, and regime dependence;
- complete PnL attribution and unexplained residual;
- security, legal/venue-rule, model, and operational assumptions;
- halt/unwind test evidence and a named capital owner for the decision.

The two practitioner threads supplied by the project owner are captured as hypotheses and incident
lessons: finite LP fee windows, directional/single-sided LP, range-exit and rebalance costs, direct
data/latency, maker/hedge thresholds, and stale-state over-hedging. Their headline returns and fixed
rules are not treated as evidence. Official protocol/venue documentation, pinned chain state, and our
own adversarial tests control production decisions.

## 11. Near-term focus rule

Robinhood remains the Phase 1 LP research benchmark. Base is the current acceptance target for
the shared data, valuation, risk, signing, settlement, and recovery path. Keep three work streams
explicit:

1. **Base LP operational acceptance.** Reproduce bounded start and stop under a current source and
   budget scope. Record fresh NAV, exact simulation, and dual-ledger settlement when an eligible
   action actually occurs. Separately evaluate the economics of the strategy and target pool.
2. **Base autonomous spot research.** Develop point-in-time multi-source discovery, independently
   priced entries and exits, and net performance comparisons in shadow. Measure the incremental
   contribution of GMGN, OKX, and any authorized FOMO inputs on an untouched window. Exercise
   slippage, capacity, stale input, and untradeable exits. Share custody, limits, and evidence
   services before proposing any spot canary.
3. **Later strategy research.** Advance atomic arbitrage, hedging, and allocation as the required
   evidence and operational controls become reusable. A new venue or chain needs its own
   independent admission and recovery evidence.

The ordering above governs capital promotion and dependencies. It permits bounded offline and
shadow experiments while operational gates are being completed. Engineering improvements should
leave a reusable path from proposal to reconciliation, with independent risk and custody controls.
See the [current evidence index](../CURRENT_IMPLEMENTATION.md) for observed results.


## 12. Next research increment — event-driven cycles and Metric

The [2026-11-14 research design](METRIC_EVENT_RESEARCH.md) prioritizes Elysium exact-prestate and
prospective event evidence, Metric same-chain executable quote comparisons, and independent LP
economics. A bounded public API capture and offline two-pool depth screen now exist; they produce
provider estimates only. Conditional maker updates, empty or one-sided depth, caller extensions,
unknown costs, and outstanding debt must not become executable-profit claims.

The [pinned Metric/V3 quote experiment](METRIC_V3_QUOTE_EXPERIMENT.md) now composes both venue
orders from retained block-bound inputs. Its first Ethereum observation stopped on a Metric
quoter revert before any complete cycle. Feed state, exact-caller atomic simulation, independent
providers and prospective coverage remain evidence requirements; runtime pins alone do not prove
source equivalence or executable edge.

Aave-funded dislocations and CEX/bridge legs remain separate non-atomic research until repayment,
health-factor stress, transfer delay, exact simulation, and reconciliation are demonstrated. This
increment does not change the capital promotion order or grant live authority.

## 13. 2026-11-15 external audit response

An external report based on public material argues that three current strategy families are
structurally negative EV at small size: custom v4 LP without aggregator routing, blind launchpad
sniping, and generic-router two-hop atomic arbitrage. Treat the report's specific returns, weights,
and latency numbers as hypotheses; adopt only the controls that survive our evidence gates.

- **Custom v4 LP.** Router/aggregator whitelist evidence is now a required pool-access check. Absence
  of that evidence keeps the pool unresolved and not execution-eligible. Base LP remains paused under
  the owner instruction; Robinhood LP remains a research benchmark.
- **Launchpad sniping.** No live launchpad buy without launchpad-safety evidence covering the current
  buy/sell tax, sellability, anti-snipe or lock window, honeypot simulation, and creator context.
  Spot remains shadow.
- **Atomic arbitrage.** Generic-router two-hop cycles remain research only. Any candidate needs direct
  allowlisted pool adapters, exact pinned simulation, revert and builder-cost accounting, and the
  existing Stage 2 gates.
- **CEX-DEX lead-lag and ERC-1271 intent/RFQ.** These are later research priorities, not authorized
  execution. They require non-atomic hedge accounting, external venue data, independent fill and
  inventory reconciliation, and a separate release decision. The report's capital weights and return
  targets are not adopted; any allocation is an owner decision.
- **Base Denim.** Base headers now expose `timestampMs`; evidence parsing validates it against the
  whole-second header timestamp. Do not infer seconds from `block.number` or treat a missing
  millisecond field after activation as valid.
