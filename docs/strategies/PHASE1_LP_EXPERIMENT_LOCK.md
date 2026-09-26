# Phase 1 LP Strategy Experiment Lock

**Status: DRAFT — NOT LOCKED.** This file is the agent-drafted structure of the design lock the
blueprint requires before any strategy window is opened. It is not a strategy result, not a parameter
set, and not authorization to trade. Every field marked **OWNER DECISION** is a value only the project
owner can set; no value has been invented here. Until the owner approves this file, the system stays
`observe`/`shadow` and `execution_eligible` remains False.

The doctrine this lock must be consistent with is
[`ACTIVE_LP_PLAYBOOK.md`](ACTIVE_LP_PLAYBOOK.md); the Phase 1 pool access, promotion sequence, risk
budgets and failure matrix are in
[`ROBINHOOD_V4_AUTOLP_BLUEPRINT.md`](ROBINHOOD_V4_AUTOLP_BLUEPRINT.md). Where this file and the
blueprint disagree, the blueprint controls.

## Identity

- Experiment ID: **OWNER DECISION** (the blueprint's promotion order in §6.1 implies the first
  experiment is *static fee + fixed range control*, then dynamic fee on the identical range, then
  static fee on a candidate active range, then both).
- Owner: the project owner. Reviewer: internal engineering/promotion review; no external audit is
  required by the project owner's release policy, and no reviewer may be recorded until one is named.
- Code commit: `3c33b14` (*feat: derive the LP cycle reference price from two observed USD feeds*).
  The dirty-tree hash must be recomputed at the moment of lock; `reports/` artifacts are not version
  controlled and change between runs.
- Dataset/evidence manifest hash: **UNRESOLVED.** No production dataset exists. The lock cannot be
  opened until a retained, block-pinned evidence database and its manifest digest exist.
- Chain, pools, tokens, block/time range: **UNRESOLVED (blocked on target selection).** No pool has
  been selected: the observed Robinhood PONS/USDG hook pool is not treated as open to outside LPs and
  failed all twelve two-provider quotes at the maximum price boundary, and the hookless
  ETH/STOCKHOOKS pool that passed twelve direct quotes is a quote benchmark, not routing or economics
  for the distinct pool an Oasis hook would create. Robinhood Chain (chain 46630) remains the initial
  benchmark; Base and other EVM deployments may qualify through the same per-chain gates.
- Venues/accounts/instruments and exact contract mapping: **UNRESOLVED.** Must pin chain id,
  PoolManager, PositionManager, StateView, hook, router, quoter, both token runtimes and the ERC-721
  token ids.
- Created/locked timestamp: created 2026-09-26. Locked timestamp: **OWNER DECISION**.
- Related threat model/decision records: `docs/security/CUSTODY_AND_AGENT_KEY_MODEL.md`,
  `docs/security/V4_HOOK_PRODUCTION_STANDARD.md`,
  `docs/architecture/LP_EXECUTION_PREPARATION.md`.

## Hypothesis

- Economic mechanism: a concentrated-liquidity position on one selected v4 pool earns its fee share of
  the flow that crosses it, and the position is reopened or resized only when the expected marginal
  value of doing so exceeds its cost after inventory risk, LVR/adverse selection, gas and rebalancing.
- Why the edge should exist: the pool's fee rate is a price for liquidity, and a range/fee rule that
  responds to observable volatility and flow toxicity can be paid more per unit of inventory risk than
  a fixed rule — if routing actually selects the pool and the flow is not exclusively toxic.
- Who pays for the edge and why it persists: flow that crosses the range pays the fee. It persists only
  while the pool keeps routing share, which is itself a hypothesis the quote benchmark does not
  establish.
- Conditions where it disappears: routing share collapses below the elasticity envelope; realized
  volatility and adverse selection exceed the range's fee density; rebalancing cost exceeds the
  avoided inventory risk; input data becomes stale or manipulable.
- Primary falsification test: out-of-sample net excess return over the static-fee and same-range
  controls is not positive after all declared costs, or the confidence interval includes zero at the
  precommitted minimum sample.

## Frozen design

The current repository enforces the *structure* below; the *values* are not set.

- Strategy/range/fee policy: **OWNER DECISION.** Fee and range policies must not be introduced
  simultaneously (blueprint §6.1). The first experiment is the control: a static fee and a fixed range.
- Observable inputs and lookback windows: implemented and not owner-selectable in structure — the two
  slot0 captures behind a market window (a strictly block-ordered price series ending at the decision
  block), the declared position's own inventory at that block, the derived reference price at that
  block, and the matured swap markouts behind that window. Derived from them, never declared:
  `volatility` (mean relative move of `sqrtPriceX96 ** 2` across the window), `toxicity`
  (= `adverse_selection`, mean adverse markout ratio), `oracle_deviation`,
  `inventory_imbalance`. The window length, horizon and lateness bounds are **OWNER DECISION**.
- Parameter search space and total trials: **OWNER DECISION**, and must be recorded here *before* the
  untouched test window is viewed. The blueprint's optimizer weights are experiment candidates, not
  production constants.
- Capital and sizing rule: capped by the minimum of the owner's approved absolute experiment loss
  amount, 5 basis points of liquid portfolio NAV, 1 basis point of independently verified two-sided
  exit depth at the approved maximum impact, and the amount whose complete loss leaves gas, operations
  and unwind reserves untouched (blueprint §9.1). Owner-approved absolute limits already on record:
  **USD 500 total capital, USD 200 daily loss cap, USD 500 cumulative loss cap.** The binding ceiling
  for the current authorized account is the 1-basis-point exit-depth term; a canary is blocked until an
  isolated LP owner is funded within budget and the pool itself proves two-sided exit depth.
- Execution/MEV model: **OWNER DECISION** (submission path, priority-fee policy, minimum-output
  protection, private vs public submission). No code path in this repository signs or broadcasts.
- Gas/priority/revert assumptions: **OWNER DECISION**, per chain. Rebalance actions require
  `swap_rebalance_gas_mev_cost` to be declared per candidate and a gas reserve that is itself checked
  fresh (`gas_reserve_fresh`).
- Price source and valuation currency: **owner-selected and implemented**: each pool currency's own
  pinned USD feed, read through the market window's own two providers at the decision block, combined
  into the q192 ratio `reference_price_x192` in the same fixed point as `sqrtPriceX96 ** 2`. The feed
  pin per currency (address, description, runtime hash, max age, token decimals) and the
  `max_reference_deviation` input-quality bound are **OWNER DECISION**; the freshness bound in force is
  declared per feed, not assumed from a heartbeat.
- Event/receive/process clocks and freshness limits: the decision block's own timestamp is the clock;
  `reference_fresh`, `route_fresh`, `nonce_fresh`, `balances_fresh`, `approvals_fresh` and
  `gas_reserve_fresh` are **operator declarations** today, so their numeric thresholds are **OWNER
  DECISION** and remain the fields an operator asserts on their own authority rather than on retained
  evidence.
- Market sessions, halts, maintenance, and reconnect policy: the blueprint's failure matrix (§9.2)
  governs; any RPC/feed staleness, gap or provider disagreement stops new exposure and rebalancing
  (fail closed).
- Token behavior assumptions: **OWNER DECISION.** Any non-standard behavior (fee-on-transfer,
  rebasing, callbacks, admin/blacklist powers) must be excluded or explicitly modeled before the token
  is used, and a token whose bytecode or admin behavior changes is rejected.
- Funding/borrow/dividend/FX/corporate-action assumptions: none in Phase 1; the canary is unlevered and
  holds no borrow. If any of these later apply, this lock must be reissued.
- Partial-fill, order-status, and hedge-state model: Phase 1 is atomic-only — one action bundles
  removal, fee collection, bounded swap and new liquidity placement, and an action that cannot be
  atomic is rejected rather than carrying a naked leg (blueprint §6.5). No hedge leg is modeled and no
  hedge PnL may be credited to the strategy.

### Owner decisions the code already has named fields for

Every item below is a field the current declaration model accepts; none has a value yet. Units are
exact: integer base units for amounts, `Decimal` for ratios and values.

| Field | Meaning | Constraint enforced by code |
| --- | --- | --- |
| `direction` | swap direction the cycle is evaluated for | must be a typed v4 swap direction |
| `amount_specified` | swap size evaluated | nonzero int256 |
| `exact_input` | exact-input vs exact-output | bool |
| `benign_flow` | whether the observed flow is considered benign | bool |
| `min_benefit` | precommitted minimum benefit to act | finite `Decimal`; a non-hold action is selected only when its lower confidence bound exceeds this |
| `hysteresis_threshold` | dead-band above the benefit threshold | finite, nonnegative `Decimal` |
| `max_volatility` | no-recenter circuit breaker | finite, nonnegative `Decimal`; compared with the derived mean relative price move |
| `max_toxicity` | no-recenter circuit breaker | finite, nonnegative `Decimal`; compared with the derived mean adverse markout |
| `price_left_buffer`, `dwell_met`, `cooldown_met`, `daily_limit_ok` | blueprint §6.4 trigger conditions | bool, operator-declared |
| `reference_fresh`, `route_fresh`, `nonce_fresh`, `balances_fresh`, `approvals_fresh`, `gas_reserve_fresh` | freshness conditions | bool, operator-declared |
| `post_action_limits_ok` | post-action position within token/range/concentration/worst-case loss limits | bool, operator-declared |
| `pin_simulated_ok` | pinned simulation succeeded with minimum amounts and a short deadline | bool, operator-declared |
| `expected_incremental_fees` | candidate term | finite nonnegative `Decimal` |
| `expected_incremental_lvr` | candidate term | finite nonnegative `Decimal` |
| `inventory_risk_charge` | candidate term | finite nonnegative `Decimal` |
| `swap_rebalance_gas_mev_cost` | candidate term | finite nonnegative `Decimal` |
| `failure_and_model_uncertainty_buffer` | candidate term | finite nonnegative `Decimal` |
| `target_range` (per candidate) | lower/upper tick aligned to tick spacing | integer ticks inside the canonical v4 bounds, aligned to a positive tick spacing |
| `max_reference_deviation` | largest relative gap between the derived ratio and the pool's own price | decimal string in `[0, 1]`, at most 18 places, evaluated in exact integer arithmetic |

The five candidate terms compose exactly the blueprint's objective:

```text
marginal_value(a) = expected_incremental_fees
                  - expected_incremental_lvr
                  - inventory_risk_charge
                  - swap_rebalance_gas_mev_cost
                  - failure_and_model_uncertainty_buffer
```

The lock must state, for each of the five terms, whether its value is estimated from the frozen
dataset or asserted by the operator, and on what evidence. Until that is stated, the fee/LVR/inventory
model is unlocked and no cycle producer may be treated as producing an economic estimate.

## Data split

- Discovery window: **OWNER DECISION** (blocks and time range, with the evidence manifest hash).
- Calibration window: **OWNER DECISION**.
- Validation window: **OWNER DECISION**.
- Untouched final test window: **OWNER DECISION**, opened once, after the total trial count is
  recorded here.
- Walk-forward schedule: the backtest protocol requires per-fold excess return over hold and the
  R00/R01/R10 controls, with strict calibration/evaluation block separation.
- Purge/embargo rule: **OWNER DECISION** (must be at least the information horizon of every input,
  including the input-quality staleness bounds).
- Missing-data/reorg policy: fail closed — a missing, stale, disagreeing or reorged input blocks the
  decision; a gap is never zero-filled.

## Baselines

- Hold: buy-and-hold the volatile token for the identical window, valued in the same currency.
- Passive LP: a fixed full-range (or declared widest) position at the same fee rate, no rebalancing.
- Static-fee LP: the identical range and inventory with a constant fee rate.
- Same range policy without candidate feature: the same trigger machinery with the candidate term
  under test removed, so a lucky directional position cannot be credited to the feature.
- Competing pools/routes: the same pair on the best alternative venue, measured with a two-provider
  quote grid at the same pinned block.

## Precommitted acceptance and kill criteria

- Minimum net excess return/confidence: **OWNER DECISION**, per baseline, after LP fees, hook fees,
  IL, LVR/adverse selection, gas, failed transactions, rebalancing and inventory risk.
- Maximum drawdown/expected shortfall: **OWNER DECISION**, in the declared valuation currency.
- Maximum inventory/concentration/time at risk: **OWNER DECISION**, bounded by the blueprint's
  portfolio controls and the approved absolute caps (USD 500 total / USD 200 daily / USD 500
  cumulative).
- Minimum sample/volume/regime coverage: **OWNER DECISION**, with both swap directions represented and
  routing share inside the declared elasticity envelope.
- Maximum unexplained accounting residual: zero unexplained operations; any unexplained native or token
  movement blocks the next frame rather than being recorded as zero.
- Capacity and gas thresholds: **OWNER DECISION**, including the maximum gas per action and the hook
  revert-rate SLO.
- Maximum gross/net delta and unhedged-notional-time: **OWNER DECISION**, and the embedded directional
  PnL must be reported separately from fee alpha.
- Margin/liquidation and capital-transfer thresholds: not applicable in Phase 1 (unlevered, no
  cross-account transfers); if they become applicable this lock must be reissued.
- Security/reliability gates: no critical/high contract finding, passing fuzz/invariant/pinned-fork
  suites, bounded hook deltas, authenticated PoolManager callbacks, and every pinned runtime hash
  re-verified before execution.
- Immediate kill conditions: the blueprint's failure matrix (§9.2), plus a nonlinear loss-velocity
  breach, a daily or cumulative loss-cap breach, an unreconciled accounting residual, a provider
  disagreement on the decision block, or any input whose freshness bound is exceeded.

## Results — fill only after design lock

- Net PnL and excess return by baseline: not measured.
- Attribution: fees / inventory / IL / LVR / gas / rebalance / failures / residual: not measured.
- Confidence, multiple-testing adjustment, and trial ledger: empty.
- Drawdown/tail/regime/sensitivity/capacity: not measured.
- Deviations from frozen design: none recorded because the design is not locked.
- Decision: pending owner lock.
- Independent reviewer sign-off: none; no reviewer is named and none may be fabricated.

## What is blocked until this file is locked

- No economic quantity is estimated and no cycle carries an estimated edge; the candidate terms and
  the four thresholds remain explicit operator declarations.
- No canary, signing or broadcast path is admitted: the signer gate additionally requires an admitted
  shadow window for the same account, manifest and policy, and the capital/loss latch remains latched.
- `execution_eligible` remains False everywhere, including in every retained record this repository
  produces.
