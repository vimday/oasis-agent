# Active LP and v4 Hook Research Playbook

**Status:** Phase 1 research doctrine

The detailed Robinhood Chain implementation and promotion sequence is defined in
[`ROBINHOOD_V4_AUTOLP_BLUEPRINT.md`](ROBINHOOD_V4_AUTOLP_BLUEPRINT.md). This playbook supplies the
general LP doctrine; the blueprint controls Phase 1 pool access, routing, fee/range experiments,
keeper behavior, and release gates.

## 1. LP is active inventory trading

A concentrated-liquidity position is a state-dependent inventory and limit-order schedule, not a
risk-free yield product. Range selection, timing, fee, liquidity density, exits, and hedging express
views about volatility, direction, flow toxicity, and how long price remains in range.

The correct question before entry is:

```text
Will expected fee and incentive income over the holding window
exceed LVR/adverse selection + inventory loss + gas + rebalance/exit cost
with sufficient margin under stress?
```

High displayed APR is neither necessary nor sufficient. It may be annualized from a short burst,
ignore future dilution, precede a regime reversal, or reflect toxic flow that is expensive for LPs.

## 2. Candidate LP modes

- **Balanced market making:** two-sided range around fair value; earns flow while accepting gamma/LVR.
- **Single-sided acquisition:** place quote inventory so a range crossing accumulates the target asset
  according to a predeclared purchase plan.
- **Single-sided distribution:** use the range as a staged sale plan.
- **Directional LP:** choose asymmetric ranges/inventory while measuring the embedded delta separately
  from fee alpha.
- **Event-window LP:** enter only for a bounded high-flow period with predetermined exit and capacity.
- **Portfolio LP:** diversify across pools only after modeling common factor, chain, token, and liquidity
  exit correlation. More pools do not guarantee lower drawdown.

Each mode has a different benchmark. Directional PnL must not be credited to the fee strategy.

## 3. Exact concentrated-liquidity math

Never encode folklore such as “a 10% one-sided range always loses 5% when crossed” as a universal
rule. Loss depends on token orientation, square-root-price endpoints, entry price, terminal price,
inventory benchmark, fees, and exit action.

For liquidity `L` and square-root price `sqrt(P)` inside `[sqrt(Pa), sqrt(Pb)]`, use canonical v3/v4
amount formulas and integer rounding matching the contracts. Evaluate values at entry, every boundary,
range exit, and stressed terminal prices against the correct hold/acquisition/distribution benchmark.

Required tests:

- both token orientations and price inversions;
- below/inside/above range;
- exact boundary ticks and tick rounding;
- decimal changes and valuation-currency changes;
- zero fees versus fee accrual;
- range crossing then reversal versus permanent crossing;
- exit gas/slippage and inability to withdraw immediately.

## 4. Entry selection

Estimate over a declared holding horizon:

- executable fee yield at expected liquidity share, not pool-wide headline APR;
- volume persistence and fee elasticity versus competing pools;
- expected LVR/adverse selection and jump/gap risk;
- price/range transition probabilities and terminal inventory distribution;
- token contract, issuer/admin, holder, liquidity, oracle, bridge, and chain risk;
- gas, priority fee, rebalance/exit latency, incentives, dilution, and capacity;
- the value and downside of intentionally acquiring or distributing inventory.

For new or meme assets, require sellability tests, permission/admin analysis, transfer-tax and callback
behavior, liquidity concentration, mint/blacklist/freeze/proxy powers, and stressed exit depth. A high
fee rate cannot compensate for an asset that cannot be sold or safely transferred.

## 5. Range and exit policy

Ranges are chosen from a loss budget and expected flow distribution, not an arbitrary percentage.
Every position has before entry:

- intended LP mode and inventory target;
- lower/upper ticks and why they are economically meaningful;
- maximum size, concentration, and capital-at-risk time;
- expected break-even fee time under base and stress scenarios;
- range-exit, drawdown, toxicity, data-quality, and contract-change kill conditions;
- hold/withdraw/recenter/hedge/unwind action for each boundary;
- minimum executable exit liquidity and gas reserve.

“Immediately withdraw when out of range” is a testable candidate policy, not a universal truth. It can
avoid unwanted inventory but may crystallize adverse selection and pay repeated gas. Compare it with
hold, hysteresis, cooldown, hedge, wider/nested range, and no-rebalance counterfactuals.

## 6. Rebalancing doctrine

Automatic recentering can mechanically sell after declines and buy after rises, realizing LVR and
paying gas. It is allowed only when out-of-sample tests show incremental net benefit after:

- price path and reversal behavior;
- lost fee time outside range;
- swap and liquidity-modification gas;
- adverse price impact and MEV;
- inventory objective and tax/accounting consequences;
- hysteresis, cooldown, minimum benefit, and daily rebalance caps.

The default comparison for every auto-rebalance strategy is an identical position without rebalancing.
The agent must be able to choose “do nothing” when action has negative marginal value.

## 7. v4 dynamic-fee hook policy

The hook attempts to price liquidity according to observable risk without discriminating by trader
identity. Candidate inputs include lagged volatility, direction-aware inventory, recent price impact,
flow toxicity proxies, competing executable liquidity, and time/event regimes.

Controls:

- exact v4 fee encoding and `MAX_LP_FEE` validation;
- deterministic public preview using the same inputs/rounding as `beforeSwap`;
- min/max fee, maximum fee change per block/time, stale-input fallback, and manipulation bounds;
- no private hook-delta extraction or wallet/balance-based price discrimination;
- parameter changes are bounded, timelocked where appropriate, evented, and replayable;
- every effective fee is attributable to a rule version and state snapshot;
- quote compatibility and fee-volume/routing effects are tested end to end.

## 8. Portfolio LP controls

Aggregate exposure by token, issuer/admin, chain, PoolManager/hook, oracle, bridge, stablecoin,
liquidity venue, and correlated narrative. Small positions in ten pools can still be one concentrated
risk if they share the same quote token, deployer, bridge, or exit liquidity.

Reserve uncoupled gas and liquid inventory for exit. Do not allocate emergency reserves to headline
APR opportunities. Cap the number of positions by operational capacity demonstrated in chaos drills,
not by how many a human or bot can nominally open.

## 9. Social-field-report treatment

The referenced LP thread offers useful hypotheses: fee must cover range/inventory loss within a finite
window; LP can implement directional acquisition/distribution; exit timing matters; automatic
rebalancing can be costly; private submission and minimum-output protection may reduce some MEV risk.

It also contains claims that must not become constants without evidence: universal APR entry cutoffs,
“almost no drawdown,” fixed range-loss shortcuts, and automatic withdrawal rules. Each pool, token,
range, fee regime, chain, and holding horizon needs exact math and independent data.

## 10. Phase 1 acceptance

The first hook strategy advances only if it demonstrates on pinned data from the selected chain (Robinhood remains the initial benchmark):

- correct per-token replay and accounting;
- incremental net benefit over static-fee and same-range controls;
- robust results across both directions, volatility/flow regimes, and realistic fee elasticity;
- bounded loss when price exits range or inputs become stale/manipulated;
- no critical/high contract finding and passing fuzz/invariant/fork suites;
- shadow/canary reconciliation, halt, and safe unwind within the declared SLO.
