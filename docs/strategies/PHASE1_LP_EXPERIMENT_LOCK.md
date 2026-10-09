# Phase 1 LP Strategy Experiment Lock

**Status: DECIDED under owner delegation (2026-09-27). Structure locked; every numeric input is
materialized by a named producer, never guessed.**

This file is the design lock the blueprint requires before any strategy window is opened. Two kinds of
content appear in it and they must not be confused:

- **Decided** items are policy, procedure, experiment identity, budgets and threshold *rules*. They are
  closed: changing one re-opens this lock and must be recorded here before the untouched test window is
  opened.
- **Materialized** items are chain and dataset facts (target pool, block ranges, dataset digest, the
  calibrated token amounts). They are computed by the producer named next to them from the decided rule.
  A number is never written here by hand to fill one in.

It is not a strategy result and not authorization to trade. The system stays `observe`/`shadow` and
`execution_eligible` remains False.

The doctrine this lock must be consistent with is [`ACTIVE_LP_PLAYBOOK.md`](ACTIVE_LP_PLAYBOOK.md); the
Phase 1 pool access, promotion sequence, risk budgets and failure matrix are in
[`ROBINHOOD_V4_AUTOLP_BLUEPRINT.md`](ROBINHOOD_V4_AUTOLP_BLUEPRINT.md). Where this file and the blueprint
disagree, the blueprint controls.

## Delegation record

- Owner instruction: *"你按照最佳实践替我做决定即可"* (2026-09-27), delegating every open decision in the
  previous draft of this file to the agent.
- **Decided by:** agent, under that delegation. **Approver of record:** the project owner. A delegation is
  not a review, and this document records no review.
- **What delegation cannot set:** a chain fact, a dataset, a pool identity, or a reviewer's name. Those are
  either materialized by a producer from a decided rule, or left explicitly empty. Fabricating one would
  violate the constitution's "no fabricated certainty", so none is invented here.
- **Amendment rule:** any change to a *Decided* value, and every additional parameter set evaluated,
  increments `trial_number` and must be recorded in this file before the next run.

## Identity

- **Experiment ID (decided):** `OASIS-P1-LP-FACTORIAL-01`. The four cells and their order are the
  blueprint's §6.1 order and the cell names `farming/backtest.py` already evaluates:
  1. `R00` — static fee, fixed range (the control);
  2. `R10` — dynamic fee, identical fixed range (isolates fee-policy alpha);
  3. `R01` — static fee, active range (isolates range alpha);
  4. `R11` — dynamic fee, active range (the combined strategy).
  A cell is promoted only after the cell before it passes. Fee and range policies are never introduced
  together.
- **Owner:** the project owner. **Reviewer:** *not delegable.* This field must name a real reviewer or stay
  empty; the constitution forbids inventing one. **Decided requirement:** a reviewer must be named before
  `R11` is promoted beyond `shadow`. Internal engineering/promotion review is required; the owner's
  release policy requires no external audit, and this field is not proof of one.
- **Code commit (decided):** pin `1f8d78e` now, and re-pin the commit and a working-tree hash at
  materialization. `reports/` is not version controlled and changes between runs, so a commit alone is not
  a sufficient pin for a reproduced result.
- **Dataset/evidence manifest hash (materialized):** produced by the replay producer as
  `observation_dataset_hash()` over the ordered observation payload — the exact digest
  [`ExperimentLock`](file:///Users/metaverse/funspace/oasis-agent/src/oasis_agent/farming/backtest.py#L214-L225)
  already enforces. No window may be opened before it exists.
- **Chain, pool, tokens (decided as a rule; see below).**
- **Created:** 2026-09-26. **Decided:** 2026-09-27. **Materialized/locked timestamp (materialized):** the
  chain time of the block recorded in `locked_before_block`.

## Target selection rule (decided)

Chain and pool are facts, so the lock fixes the *rule* that selects them rather than a name.

- **Chain:** Robinhood Chain mainnet (chain 4663, matching blueprint §2.1; 46630 is the
  repository's testnet configuration) remains the initial benchmark. Base (8453) and other EVM
  deployments are eligible only under the identical gate below; they rank after Robinhood only because
  Robinhood is the declared initial benchmark, not because it is expected to be better.
- **Pool admission — every item mandatory, two-provider and block-pinned:** WP1 outcome is `accessible`
  (not `permissioned`, not `unresolved`); outside LPs may add and remove liquidity; hook permission bits
  are exactly the dossier's declared set; a pinned-fork add/remove round trip succeeds; both swap
  directions quote at three sizes through a supported standard router; and the pool's 1-basis-point
  two-sided exit depth at the approved maximum impact exceeds the canary cap's binding term (blueprint
  §9.1).
- **Choice among admitted pools:** greatest verified two-sided exit depth at the approved maximum impact at
  the same pinned block; ties broken by lower measured all-in cost per action, then by lower token risk.
- **Current state of the rule:** no pool is admitted. The observed Robinhood PONS/USDG hook pool is not
  treated as open to outside LPs and failed all twelve two-provider quotes at the maximum price boundary;
  the hookless ETH/STOCKHOOKS pool that passed twelve direct quotes is a quote benchmark whose §9.1 cap
  evaluates to `0.0000001 ETH`, too small to mint after gas and minimum useful liquidity. The first
  eligible pool has therefore not yet been established, and the experiment is blocked at WP1.
  This is an evidence status, not proof that no eligible pool exists. The subsequent
  [2026-09-27 screening](../verification/LP_TARGET_PROGRESS_20260927.md) verified Base
  ETH/USDC and USDC/cbBTC direct quotes at block 51826740 and ETH/USDC's local
  standard-router settlement and LP lifecycle. Complete two-sided admission depth,
  cost and economic evidence remain unresolved; neither candidate is promoted by
  those checks. The selection rule and experiment trial are unchanged.

## Hypothesis

- **Economic mechanism:** a concentrated-liquidity position on one selected v4 pool earns its fee share of
  the flow that crosses it, and the position is reopened or resized only when the expected marginal value
  of doing so exceeds its cost after inventory risk, LVR/adverse selection, gas and rebalancing.
- **Why the edge should exist:** the pool's fee rate is a price for liquidity, and a range/fee rule that
  responds to observable volatility and flow toxicity can be paid more per unit of inventory risk than a
  fixed rule — if routing actually selects the pool and the flow is not exclusively toxic.
- **Who pays for the edge and why it persists:** flow that crosses the range pays the fee. It persists only
  while the pool keeps routing share, which is itself a hypothesis the quote benchmark does not establish.
- **Conditions where it disappears:** routing share collapses below the elasticity envelope; realized
  volatility and adverse selection exceed the range's fee density; rebalancing cost exceeds the avoided
  inventory risk; input data becomes stale or manipulable.
- **Primary falsification test (decided):** the minimum per-fold `R11` excess over each of `R00`, `R01` and
  `R10`, and over buy-and-hold, fails to reach the precommitted materiality floor. There is no "positive
  but not significant" acceptance path: the repository's confidence rule is the worst fold, not a bootstrap
  interval, and the floor is a measured cost, not zero.

## Frozen design (decided)

The structure below is enforced by the current repository; the values are now fixed as experiment
parameters. A freely chosen threshold is a precommitment, not a claim about the market — no evidence
attaches to the choice, only to the result measured against it.

- **Fee and range policy per cell:**
  - *static fee* = the selected pool's own `PoolKey.fee`; no fee override is applied.
  - *dynamic fee* = one discrete monotone band table per blueprint §5.3, with every parameter fitted on the
    discovery and calibration segments only.
  - *fixed range* = chosen once, from the discovery segment alone: the tick window spanned by that
    segment's own observed `sqrtPriceX96` path, aligned inward to `tickSpacing`, never changed afterwards.
  - *active range* = the controller may choose among HOLD/REMOVE/RECENTER/REDUCE/UNWIND, subject to the gate
    below. Repeated buy-high/sell-low recentering is accounted as realized LVR, never as success.
- **Observable inputs and lookback windows (decided):** markout horizons `{1, 5, 30, 300}` s — the horizons
  blueprint §8.2 already names — with the **primary horizon `H = 300 s`**; `max_lateness_seconds = 60`;
  price window span ≥ 600 s with ≥ 20 observations, which fixes the observation cadence at ≤ 30 s and
  constant for the whole experiment. Under the owner's 2026-09-28 instruction, current research uses two
  public RPC providers; credentials are not a research prerequisite. Capability is measured from actual
  budgets, latency, complete captures and retained gaps, rather than inferred from authentication. The
  cadence and data acceptance bounds are unchanged. Derived from the window and never declared:
  `volatility` (mean relative move of `sqrtPriceX96 ** 2`), `toxicity` (= `adverse_selection`, mean adverse
  markout ratio), `oracle_deviation`, `inventory_imbalance`.
- **Parameter search space and total trials (decided):** `maximum_trials = 12` — three unidirectional
  promotion steps at up to four parameter sets each. `trial_number` starts at 1 and increments for every
  parameter set evaluated, including discarded ones. The blueprint's 60/30/10 tranche weights and its
  optimizer weights are experiment candidates, never production constants (playbook §9).
- **Capital and sizing rule (rule decided, amount materialized):** `initial_capital1` is the §9.1 minimum
  converted to token1 base units at the decision-block reference price and floored: the least of the
  approved absolute experiment loss amount, 5 basis points of liquid portfolio NAV, 1 basis point of
  independently verified two-sided exit depth at the approved maximum impact, and the amount whose
  complete loss leaves gas, operations and unwind reserves untouched. Owner-approved limits already on
  record: **USD 500 total capital, USD 200 daily loss cap, USD 500 cumulative loss cap.** At the current
  evidence the 1-basis-point exit-depth term binds, so a canary is blocked until an isolated LP owner is
  funded inside that budget and the selected pool proves two-sided exit depth.
- **Execution/MEV model (decided):** atomic submission only; an action that cannot be atomic is rejected
  rather than carrying a naked leg (blueprint §6.5). Minimum-output protection and a short deadline are
  mandatory (`pin_simulated_ok`). Priority fees may not be counted as a routing advantage: ordering on this
  chain is sequencer-arrival first-come-first-served (blueprint §2.4). No code path in this repository
  signs or broadcasts, and this lock cannot authorize one.
- **Gas assumptions (decided):** per action, measured from the pinned-fork simulation of the exact atomic
  action (L2 execution plus L1 calldata components) and retained; the gate refuses an action without a
  fresh measured gas reserve. Gas is never estimated from a price guess.
- **Price source and valuation currency (decided earlier by the owner, unchanged):** each pool currency's
  own pinned USD feed, read through the market window's own two providers at the decision block and
  combined into the q192 ratio `reference_price_x192` in the same fixed point as `sqrtPriceX96 ** 2`. Decided
  now: feed freshness bound `max_age_seconds = 3600` per feed; `max_reference_deviation = 0.01`, evaluated
  in exact integer arithmetic — this is also the first manipulation detector, since a pool whose own price
  deviates from the independently observed feed ratio by more than the bound refuses the window instead of
  pricing it.
- **Freshness bounds (decided):** reference price ≤ 3600 s (the feed's own bound); route depth, nonce,
  balances and approvals ≤ 60 s; gas reserve ≤ 300 s. These are the numeric thresholds behind the
  `*_fresh` flags the declaration carries today as operator declarations; until the producing step measures
  them, the flags remain the operator's assertion and are named as such in the retained interpretation.
- **Trigger parameters (rule decided, values materialized on calibration data only):**
  - `min_benefit` = the 95th percentile of single-action all-in cost observed in the **calibration**
    segment.
  - `hysteresis_threshold` = the **maximum** single-action all-in cost observed in the calibration segment,
    so the dead band is at least one worst-case action and the binding act threshold is
    `max(min_benefit, hysteresis_threshold)`.
  - `max_volatility` and `max_toxicity` = the 95th percentile of the calibration segment's own derived
    volatility and toxicity distributions. The intended meaning is exact: do not recenter in the most
    extreme 5% of the calibration regime. Reading them as universal market constants is forbidden
    (playbook §9).
  - cooldown `= 3600 s` per position; daily rebalance limit `= 6` actions per position. Stated experiment
    parameters.
  - inner buffer for `price_left_buffer` = the fixed range's bounds contracted inward by 20% of each side's
    half-width. Stated experiment parameter; the flag is set only when the decision-block price is outside
    those inner bounds.
  - `dwell_met` requires the price to have stayed outside the inner buffer for ≥ `H = 300 s`.
- **Market sessions, halts, maintenance, reconnect (decided):** the blueprint's failure matrix (§9.2)
  governs; any RPC/feed staleness, gap or provider disagreement stops new exposure and rebalancing. Every
  halt is fail-closed and may only reduce exposure; no emergency path may raise a limit.
- **Token behavior assumptions (decided):** a token exhibiting any non-standard behavior (fee-on-transfer,
  rebasing, ERC-777-like callbacks, mint/blacklist/freeze powers, unrenounced admin or proxy) is excluded
  before it is used; sellability and transfer-tax behaviour must be demonstrated, not assumed; and every
  pinned runtime hash is re-verified before each action, with a change rejecting the token and preparing a
  bounded removal.
- **Funding/borrow/dividend/FX/corporate-action assumptions:** none in Phase 1; the canary is unlevered and
  holds no borrow. If any of these later apply, this lock must be reissued.
- **Partial-fill, order-status and hedge-state model (decided):** atomic-only, so there is no naked leg and
  no partial-fill inventory. No hedge leg is modeled, no hedge PnL may be credited to the strategy, and the
  embedded directional inventory PnL is always reported separately from fee alpha.

### Owner decisions the code already has named fields for

Every item below is a field the current declaration model accepts; the constraint column is what
`LpCycleDeclaration`, `CandidateDeclaration` and `TickRange` enforce at construction. Units are exact:
integer base units for amounts, `Decimal` for ratios and values.

| Field | Meaning | Constraint enforced by code |
| --- | --- | --- |
| `direction` | swap direction the cycle is evaluated for | must be a typed v4 swap direction |
| `amount_specified` | swap size evaluated | nonzero int256 |
| `exact_input` | exact-input vs exact-output | bool |
| `benign_flow` | whether the observed flow is considered benign | bool |
| `min_benefit` | precommitted minimum benefit to act | finite `Decimal`; a non-hold action is selected only when its lower confidence bound exceeds this |
| `hysteresis_threshold` | dead-band above the benefit threshold | finite, nonnegative `Decimal`; the gate requires `marginal_value > hysteresis_threshold` |
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
| `max_reference_deviation` | largest relative gap between the derived ratio and the pool's own price | declaration pattern admits `0.` plus 1–18 digits or `1.0…`; the consumer re-parses it as a finite `Decimal` in `[0, 1]` with at most 18 places and compares in exact integer arithmetic |

The five candidate terms compose exactly the blueprint's objective:

```text
marginal_value(a) = expected_incremental_fees
                  - expected_incremental_lvr
                  - inventory_risk_charge
                  - swap_rebalance_gas_mev_cost
                  - failure_and_model_uncertainty_buffer
```

Note what the repository's v1 "lower confidence bound" actually is: `lower_confidence_bound()` returns
`marginal_value()`, with the fifth term acting as the model-uncertainty allowance. The statistical
confidence of the result is enforced at the backtest level by the minimum per-fold excess, not by a
distributional bound on a single decision.

## Where each candidate term comes from (decided)

The question this section answers is the one the previous draft left open: for each of the five terms, is
its value estimated from the frozen dataset or asserted by the operator, and on what evidence. None is a
free-form operator number at decision time.

| Term | Decided source | Where it comes from |
| --- | --- | --- |
| `expected_incremental_fees` | estimated from the frozen dataset | `gross_routable_volume1 × fee_pips × liquidity share`, with the elasticity-scaled routed volume — the arithmetic [`_fee_income`](file:///Users/metaverse/funspace/oasis-agent/src/oasis_agent/farming/backtest.py#L228-L236) already applies. Never a headline APR. |
| `expected_incremental_lvr` | estimated from the frozen dataset | realized adverse markouts (`adverse_selection_fixed1` / `adverse_selection_active1`) at the named horizons |
| `inventory_risk_charge` | estimated from the frozen dataset | the 95th percentile of the calibration segment's own `fixed_inventory_pnl1` / `active_inventory_pnl1` dispersion at the primary horizon |
| `swap_rebalance_gas_mev_cost` | measured, not estimated | `gas_cost_active1 + rebalance_cost_active1`, measured per action by the pinned-fork simulation of the exact atomic action |
| `failure_and_model_uncertainty_buffer` | asserted as a precommitted policy floor | the 95th percentile of single-action all-in cost measured in calibration, so one failed action cannot flip the gate |

Three terms are estimated from the frozen dataset, one is measured, and one is a policy floor applied to
calibration-only data. The fee/LVR/inventory model is therefore stated, and the estimator that implements
it — a producer of `LpReplayObservation` records — is the implementation step that follows this lock. Until
that producer exists, the terms remain what the code already forces them to be: explicit operator
declarations, and no cycle may be treated as an economic estimate.

## Data split (decided)

- **Segments:** the retained timeline is partitioned by block into four contiguous segments with shares
  50% discovery, 20% calibration, 10% validation, 20% untouched test, rounded outward to whole observation
  windows. Every boundary carries a purge of at least `max(window span, largest declared feed freshness
  bound, finality depth expressed in chain time)`.
- **Minimum coverage:** discovery + calibration + validation ≥ 21 days with ≥ 4 purged folds; the untouched
  test ≥ 7 days and ≥ 20% of the span. Both swap directions must be present in every fold. These are stated
  operator minima, not chain facts.
- **Walk-forward:** the purged four-cell evaluation already implemented in
  [`backtest_strategy`](file:///Users/metaverse/funspace/oasis-agent/src/oasis_agent/farming/backtest.py#L272-L324),
  with `minimum_folds = 4`, `calibration_size = evaluation_size`, and
  `purge_size = ceil(max input horizon / observation cadence)`. Calibration and evaluation must cross a
  strict block boundary, and the per-fold control excesses are compared with the same materiality floor as
  the headline result.
- **Untouched final test window:** opened once. `locked_before_block` = its first block; the lock record's
  evidence must be pinned at or before that block.
- **Missing-data/reorg policy:** fail closed. A missing, stale, disagreeing or reorged input blocks the
  decision; a gap is never zero-filled; any unexplained residual refuses the evaluation outright.

## Baselines

The executable minimum market-coverage precheck is now
`python -m scripts.check_lp_dataset_coverage`: it replays raw two-provider market
inputs, checks the locked horizon/lookback/cadence, applies the 50/20/10/20 split
and a 3600-second minimum purge, and refuses insufficient 21-day development or
7-day test coverage. This does not seal an untouched test, implement the economic
estimator, prove chain finality or a longer finality-driven purge, or establish
economic acceptance. Its `out_of_sample_accepted` remains false. See
[price/account/coverage verification](../verification/PRICE_ACCOUNT_OOS_20260927.md).

- Buy-and-hold the volatile token for the identical window at the same valuation (`hold_pnl1`).
- `R00` — static fee, fixed range: the primary control.
- `R01` — static fee, active range: isolates range alpha.
- `R10` — dynamic fee, fixed range: isolates fee-policy alpha.
- Passive LP: a declared widest-range position at the same fee rate with no rebalancing.
- Same policy without the candidate feature under test, so a lucky directional position cannot be credited
  to the feature.
- Competing pools/routes: the same pair on the best alternative venue, measured with a two-provider quote
  grid at the same pinned block.

`R11` is accepted only against all of them, and the interaction term `R11 - R10 - R01 + R00` is reported
rather than assumed to be zero.

## Precommitted acceptance and kill criteria (decided)

- **Confidence and materiality:** for each of `R00`, `R01` and `R10`, and for the fold lower bound against
  hold, the minimum per-fold `R11` excess must reach `minimum_r11_excess1`, where that floor is the
  **largest single-action all-in cost observed in the calibration segment**, floored at 1 base unit (the
  evaluator additionally floors at 1, so a non-positive result can never be accepted). The rule is
  deliberate: the strategy must beat the control by at least one whole action's cost, so a single lucky
  avoided action cannot pass the test.
- **Drawdown and tail loss:** `maximum_drawdown1 = maximum_expected_shortfall1 = initial_capital1`, i.e. the
  §9.1 canary cap; `tail_fraction_ppm = 50_000` (the mean of the worst 5% of event-level loss increments).
  The USD 200 daily and USD 500 cumulative loss latch is an independent and stricter halt that the canary
  gate refuses to cross.
- **Minimum sample and regime coverage:** `minimum_evaluation_events = minimum_folds × evaluation_size`
  from the retained dataset, with both swap directions in every fold and at least two realized-volatility
  terciles represented across the evaluation span, and routing share inside the declared elasticity
  envelope.
- **Maximum unexplained accounting residual:** zero. Any unexplained native or token movement blocks the
  next frame instead of being recorded as zero, and any residual refuses the evaluation.
- **Gas and reliability:** gas per action and revert rate are reported per cell; the hook revert rate SLO is
  **zero admitted reverts** in the untouched test window. A revert disables the rule version (blueprint
  §9.2), retains the evidence and re-opens this lock. A route whose quote and settlement disagree above
  base-unit tolerance is disabled the same way.
- **Directional exposure:** directional inventory PnL is reported separately from fee alpha and may never
  be relabeled as fee income; unhedged-notional-time is bounded by `initial_capital1` held for no longer
  than the primary horizon between decisions.
- **Margin/liquidation and capital-transfer thresholds:** not applicable in Phase 1 (unlevered, no
  cross-account transfers); if they become applicable, this lock must be reissued.
- **Security and reliability gates:** no critical or high contract finding; passing fuzz, invariant and
  pinned-fork suites; bounded hook deltas; authenticated PoolManager callbacks; every pinned runtime hash
  re-verified before execution.
- **Immediate kill conditions:** every trigger in the blueprint's failure matrix (§9.2), plus a nonlinear
  loss-velocity breach, a daily or cumulative loss-cap breach, any unreconciled accounting residual, a
  provider disagreement on the decision block, or any input whose freshness bound is exceeded. A kill
  halts and unwinds; it never raises a limit.

## Results — fill only after the lock is materialized

- Net PnL and excess return by baseline: not measured.
- Attribution: fees / inventory / IL / LVR / gas / rebalance / failures / residual: not measured.
- Confidence, multiple-testing adjustment, and trial ledger: empty; `trial_number` has not been incremented.
- Drawdown/tail/regime/sensitivity/capacity: not measured.
- Deviations from the frozen design: none recorded, because no run has been made.
- Decision: blocked at materialization, not pending a further choice.
- Independent reviewer sign-off: none; no reviewer is named and none may be fabricated.

## What is blocked, and by which producer

None of these is an undecided policy; each is a fact or a measurement that does not exist yet.

1. **Target pool** — the WP1 `accessible` dossier under the target selection rule. Blocked, as recorded
   above: no observed pool currently passes.
2. **Dataset digest and block ranges** — the replay producer emitting `LpReplayObservation` records and
   `observation_dataset_hash()`, including the token-level conservation ledger.
3. **The five-term estimator** — the producer implementing the table above, so that the candidate terms
   stop being operator declarations and start being estimates bound to retained evidence.
4. **`initial_capital1`** — the §9.1 sizing rule applied at a funded, isolated LP owner within the
   owner-approved USD 500 budget, plus the two-sided exit depth of the selected pool.
5. **Demonstrated two-provider capture reliability** at the decided ≤ 30 s cadence. Public endpoints
   are allowed for current research under the owner's 2026-09-28 instruction; paid credentials are not
   required to proceed. Existing public capture failures remain evidence of unresolved reliability,
   not proof that every public endpoint is incapable. This infrastructure amendment changes no economic
   parameter, opens no trial and grants no production release.
6. **A named reviewer** before `R11` is promoted beyond shadow. This is the one item delegation cannot
   supply.

Until these exist, no window is opened, no cycle carries an estimated edge, and `execution_eligible`
remains False everywhere, including in every retained record this repository produces.

## Amendment 2026-10-08: owner-authorized live canary (trial_number = 1)

- **Owner instruction (2026-10-08):** confirmed `0xe653df20a8c525edf381942ffc47704b7bdfe97d` as the
  project's dedicated wallet, approved LP-first on it, and decided: *"shadow一天就全面实盘，我们不能总是
  犹豫错失时机"* — one day of forward shadow, then full live.
- **What this overrides, on the owner's authority:** the 21d dev + 7d untouched OOS gate, the named
  reviewer requirement (item 6 above) and the replay-producer gates (items 2–3) no longer block the canary.
  They remain the bar for raising capital above the USD 500 cap. This is an owner risk acceptance, not a
  review, and it is recorded as such.
- **Parameters (trial 1, `configs/live.base-eth-usdc.json`):** Base ETH/USDC fee-500 hookless v4 pool
  `0x96d4…8c0a`; symmetric range ±490 ticks (≈ ±5%, the best band in the 2026-10-08 36-day replay);
  recenter only when out of range, ≥ 600 s after open; ≤ 12 actions/day; swap slippage 50 bps against the worse of pool
  and Chainlink ETH/USD; pool vs oracle deviation ≤ 40 bps; oracle age ≤ 1800 s; pool and oracle reads
  block-pinned and cross-checked on a second provider; 0.004 ETH gas reserve.
- **Unchanged hard limits:** USD 500 committed capital, USD 200 daily and USD 500 cumulative loss latch
  (mark-to-market wallet NAV at Chainlink, UTC+8 day). Daily loss is gross: decreases between hourly /
  per-action / day-boundary NAV checkpoints lock in and later gains never subtract them; a missing
  midnight observation charges the new day. Cumulative is net loss against funded basis; any wallet
  change not produced by the loop's own verified actions halts until the operator books it as an
  external flow (`--acknowledge-flow`). All state, balances and runtime code are read at one block hash
  and must match on two providers; a submission resolves only from a receipt for exactly that hash,
  sender and target, ≥ 3 blocks deep on a canonical block both providers agree on (a burn also needs
  the NFT burn log). Per-action runtime-hash and USDC-implementation check against
  hashes pinned in config and confirmed on two providers 2026-10-08 (change ⇒ halt), key isolated in the
  digest signer process. A halt does not unwind the LP; the operator decides.
- **Shadow result (2026-10-08 23:21 → 2026-10-10 00:06 UTC+8):** 2759 ticks, one paper swap/mint, NAV
  USD 545.94 → 546.94 (range USD 534.30–549.47), no halt. Gross 1-hour-checkpoint decreases were
  USD 16.64 over the day, far below the USD 200 daily cap. This is paper (no fees, no impact), not
  economic evidence.
- **Executor:** `scripts/run_live_lp.py` (`oasis_agent.live`). Fork e2e on a Base fork passed
  approve → swap → mint → forced out-of-range → burn → swap → mint before any mainnet signature.
- **Expectation on record:** the replay put the ±5% edge at tens of USD per month at this size; the canary
  tests execution and accounting, not a money machine.
