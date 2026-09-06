# Robinhood Chain v4 Auto-LP Phase 1 Blueprint

**Status:** Normative Phase 1 design; implementation and live profitability are not complete  
**Updated:** 2026-09-04  
**Scope:** Proprietary-capital automated LP on Robinhood Chain, using a transparent Uniswap v4 hook

This document retains the Robinhood benchmark and its chain-specific assumptions. The shared LP
workflow may also target Base, Ethereum or another explicitly selected EVM deployment under
`docs/architecture/MULTICHAIN_LP_EVIDENCE.md`. Robinhood-only sequencer, gas and finality assumptions
must not be copied to another chain; each deployment passes its own promotion gates.

## 1. Outcome and boundaries

Phase 1 must produce a system that can answer, with reproducible evidence:

```text
Did our fee policy and LP automation earn more net value than comparable alternatives,
after LVR, inventory change, gas, rebalance, failed execution, and routing elasticity,
while staying inside a precommitted loss budget?
```

The deliverable is not merely a deployed hook. It is the complete loop:

```text
chain + market evidence
  -> reconciled pool/position state
  -> deterministic fee and range proposals
  -> independent risk decision
  -> quote-compatible hook + bounded position executor
  -> receipts and token-level reconciliation
  -> attribution against baselines
  -> halt, unwind, and promotion decision
```

Phase 1 uses only project-owned capital. It does not accept public deposits, promise yield, hide fees,
discriminate by wallet, manufacture volume, or assume an observed third-party hook is safe or open to
outside LPs. Public vault accounting, transferable shares, leveraged hedging, and cross-chain capital
movement are later capabilities with separate security and legal review.

## 2. Current Robinhood benchmark

### 2.1 Block-pinned reconnaissance

The following is a reconnaissance snapshot, not yet a retained production evidence bundle. Every
field must be reacquired from two providers, content-hashed, and revalidated before simulation or
deployment.

| Field | Observed value | Evidence status |
|---|---|---|
| Chain | Robinhood Chain mainnet, chain ID `4663` | Confirmed by official docs and RPC |
| PoolManager | `0x8366a39cc670b4001a1121b8f6a443a643e40951` | Confirmed by official Uniswap material and RPC |
| Pool ID | `0x374531f25cfb1b92f6d03b946598d53dd934fba6e138bcece153a52113aa73a5` | Confirmed from `Initialize` log |
| Currency 0 | `0x39dbed3a2bd333467115de45665cc57f813c4571`, on-chain symbol `PONS`, 18 decimals | Confirmed by RPC |
| Currency 1 | `0x5fc5360d0400a0fd4f2af552add042d716f1d168`, on-chain symbol `USDG`, 6 decimals | Confirmed by RPC |
| Pool fee field | `0x800000`, v4 dynamic-fee flag | Confirmed from `Initialize` log |
| Tick spacing | `60` | Confirmed from `Initialize` log |
| Hook | `0x1191Ac2561817686EFE4296B37BA5a5419Eaa880` | Confirmed from `Initialize` log |
| Initialize block | `51,590,255`; hash `0x6f0471ce629a4fac3716c5a8c5dcf2506465ec364cf22d082813979dcdf8ec10` | Confirmed from RPC log |
| Hook permissions | `beforeInitialize`, `beforeAddLiquidity`, `beforeSwap`; no return-delta flags | Confirmed from address bits |
| Hook code | 9,775 bytes; keccak `0x7873387b1de6d2c9c216162c51e432f22ab61ef4fa9bc1c55f1ab2fb788908b0` | Confirmed at block `53,576,331` |
| Hook owner/registrar | Both returned `0x99b5d4a51598cdb2a8cd3a115202bb88afe0c767`; pending owner was zero | Confirmed at block `53,576,331` |
| PositionManager | Hook getter returned `0x58daec3116aae6d93017baaea7749052e8a04fa7` | Confirmed by RPC |
| Snapshot block | `53,576,331`; hash `0xa439ac5045cdaf5f86b0d55a8d80769f24377e4fc2e4e7bfa6b8dbb5cbc2d74b` | Confirmed by RPC |

A deliberately narrow activity probe found 23 swap logs across blocks `53,538,587`–`53,539,342`
(`2026-09-03 15:58:06`–`15:59:28 UTC`): 22 transactions, all `zeroForOne`, approximately
`2,862.4212 PONS` and `1,595.695927 USDG` of absolute event amounts, and an effective LP fee of
`2,000` pips (0.20%) on every swap. No target-pool swap appeared in the final 10,000 blocks ending at
the snapshot block. This confirms bursty, one-sided activity in one small sample; it does not establish
daily volume, toxic flow, the fee algorithm, expected return, or whether 0.20% changes in other states.
The full-history replay must determine those facts.

The hook source and exact fee function have not been verified. However, the whitelist evidence is
stronger than selector presence alone: historical events set four addresses to `true`; their current
`isWhitelisted(address)` reads returned true; and the `beforeAddLiquidity` bytecode path calls the
pinned PositionManager's `ownerOf(uint256)` and `getPoolAndPositionInfo(uint256)` before consulting
whitelist storage. This strongly indicates a beneficial LP-NFT-owner whitelist. Until a pinned-fork
add/remove-liquidity lifecycle proves every relevant branch, the exact semantics remain inferred and
this pool is observe-only.

### 2.2 The strategic consequence

A v4 hook is bound into a pool's `PoolKey` at initialization. We cannot attach our hook to the
existing benchmark pool or replace its fee logic. There are only three legitimate paths:

1. obtain documented permission to LP in the observed pool and use it only as designed;
2. use the pool as a read-only benchmark while deploying our own transparent hook and distinct pool;
3. reject the pair if token, routing, liquidity, or access-control risk fails the gate.

The default plan is path 2. A new pool does not earn fees merely because its hook is better. It must
win executable order flow from routers while offering enough depth and price quality. Routing is
therefore a Phase 1 product requirement, not a later marketing task.

### 2.3 Chain-specific constraints

- Production uses two authenticated providers plus an archive source; Robinhood's public RPC is
  rate-limited and explicitly unsuitable for production.
- Subscribe to the sequencer feed for low-latency heads, but reconstruct truth from RPC receipts,
  logs, and independent snapshots.
- `block.number` in Solidity is an L1-height estimate on this Arbitrum Nitro chain. Use
  `ArbSys(0x64).arbBlockNumber()` only when actual L2 height is required. Use `block.timestamp` and
  monotonic off-chain clocks for elapsed-time rules; never assume a fixed number of L2 blocks equals
  a fixed wall-clock interval.
- `block.prevrandao` is constant and cannot be used as randomness.
- Ordering is first-come, first-served by sequencer arrival; raising priority fees does not jump the
  queue. Latency still matters, and this does not eliminate toxic flow or sequencer risk.
- Soft confirmation is suitable for monitoring. Deployment, governance changes, large capital
  accounting, and irreversible decisions require posted-to-L1 or Ethereum-finalized evidence under
  the configured finality policy.
- Gas includes L2 execution and L1 calldata cost. Minimize hook and rebalance calldata, measure both
  components, and retain an isolated ETH unwind reserve.
- The canonical bridge's approximately seven-day L2-to-L1 challenge period means emergency capital
  cannot be assumed immediately movable.

## 3. Alpha thesis and falsification

### 3.1 What may create excess return

The candidate edge has three separable sources:

1. **Fee-policy alpha:** lower fees attract benign flow; higher fees price expected adverse selection,
   inventory worsening, volatility, and price impact.
2. **Range alpha:** capital is placed where expected fee density exceeds expected LVR, inventory, and
   rebalance costs.
3. **Operational alpha:** faster reliable state, fewer failed transactions, better routing, and safer
   automation reduce leakage relative to manual LP.

The main risk is that a higher fee loses routing share while a lower fee subsidizes toxic arbitrage.
Observed volume cannot be replayed unchanged under a counterfactual fee and called profit.

### 3.2 Primary null hypothesis

```text
H0: after all costs and realistic routing response, the dynamic-fee policy has no positive
    out-of-sample net contribution over the identical pool, capital, and range policy
    using the best static fee selected without test-set leakage.
```

Rejecting `H0` requires multiple market regimes, both directions, declared sample-size/confidence
rules, and sensitivity to flow elasticity. A profitable total strategy does not prove fee alpha if
the profit came from holding PONS or choosing a narrow range.

### 3.3 Factorial attribution

Every evaluation runs the same capital and time window through four cells:

| | Fixed range | Active range |
|---|---:|---:|
| Static fee | `R00` | `R01` |
| Dynamic fee | `R10` | `R11` |

Report:

```text
fee contribution at fixed range  = R10 - R00
range contribution at static fee = R01 - R00
combined strategy excess         = R11 - R00
interaction                      = R11 - R10 - R01 + R00
```

Also compare every cell with buy-and-hold inventory. Directional inventory PnL is reported separately
and never relabeled as fee income.

## 4. System components

### 4.1 Evidence and state reconstruction

The indexer must ingest and retain:

- block headers, hashes, timestamps, L1 posting/finality state, and reorg lineage;
- PoolManager initialization, swap, modify-liquidity, donate, protocol-fee, and dynamic-fee events;
- transaction input, receipt, gas, logs, status, and traces when available;
- hook, PoolManager, router, quoter, PositionManager, and token bytecode at pinned blocks;
- `slot0`, liquidity, tick bitmap/ticks, fee growth, position state, balances, allowances, and native
  gas balance;
- competing same-pair pools, executable quotes by size/direction, and route selected;
- token metadata and authority: proxy implementation/admin, owner, roles, mint/freeze/blacklist/tax,
  pause, callbacks, transfer behavior, holders, and executable exit depth;
- reference prices with source, event/receive time, confidence, market status, and staleness.

Decision snapshots are coherent block-versioned objects. Reads from `latest` at different moments
cannot be silently combined. All token quantities remain integer base units until explicit valuation.

### 4.2 Fast path and truth path

```text
sequencer feed + provider A websocket ----> low-latency state candidate
                                              |
provider A/B RPC + archive + receipts ----> reconciled truth ----> risk gate
                                              |
                                      immutable evidence store
```

The fast path may propose. Only reconciled state may authorize capital. Provider disagreement, a
sequence gap, stale head, changed code hash, or unexplained ledger residual disables new exposure.
Risk-reducing removal may continue only through a separately bounded and simulated path.

### 4.3 Contract set

The minimum initial deployment is intentionally small:

- `OasisDynamicFeeHook`: immutable PoolManager, exact permission flags, bounded per-swap fee override,
  public preview, rule-version events, per-pool state, and no arbitrary call or token-withdraw power.
- `OasisHookFactory`: mines and verifies the CREATE2 address flags, deploys known bytecode, and proves
  the runtime code hash before pool initialization.
- `OasisPositionExecutor`: allowlisted PoolKey and PositionManager only; performs atomic
  decrease/collect/optional bounded swap/increase flows from explicit intents. It is not a general
  wallet or arbitrary-call proxy.
- governance: delayed multisig for bounded parameter changes; a separate guardian can only pause or
  tighten limits. A new hook and pool are preferred to an upgradeable proxy.

Do not build a public vault in Phase 1. Project capital should remain in a narrowly permissioned
multisig/signer arrangement, with position ownership and emergency-removal authority documented in
`docs/security/CUSTODY_AND_AGENT_KEY_MODEL.md`.

### 4.4 Liquidity access model

The recommended Version 1 pool is **public for swaps and permissioned for liquidity additions**.
Liquidity belongs only to allowlisted LP NFT owners; removal is never blocked by the hook. This makes
the managed-inventory fee signal coherent, prevents outside JIT liquidity and uncontrolled fee
dilution, and keeps every LP unit under the project ledger. It also reduces composability and must be
disclosed clearly to routers and users.

The intended permissions are therefore the minimum set needed for guarded initialization,
liquidity-add authentication, per-swap fee selection, and post-swap state update. Do not enable
return-delta permissions or liquidity-removal callbacks in Version 1. The exact address flag set is
mined and asserted against `Hooks.Permissions` before deployment.

Because a standard PositionManager is the PoolManager-facing sender, the add gate cannot mistake
`sender` for the beneficial LP. Version 1 pins the canonical PositionManager, derives the exact token
ID from its position salt convention, verifies the callback position against
`getPoolAndPositionInfo(tokenId)`, resolves `ownerOf(tokenId)`, and checks that owner against hook
whitelist storage. First-mint ordering and every return shape are pinned-commit and fork tested.

Rollout begins with the user's external whitelisted LP owner and human-signed position actions. The
agent has no key. Only after strategy maturity is a separately funded, small agent account added to
the whitelist for autonomous canaries. Treasury, hook governor, emergency guardian, manual LP owner,
and agent keys remain distinct. The agent account never receives pull authority over external funds.

Permissioned additions are not hidden trader discrimination: fee selection remains independent of
swapper address and fully previewable. If router policy or market evidence shows that permissioned
liquidity destroys useful order flow, run a separate public-LP pool experiment rather than weakening
authentication in place.

The dedicated executor remains useful in the later policy-controlled stage, but it must operate for a
whitelisted owner through verified PositionManager semantics; it does not replace ownership checks
with a broad caller allowlist.

## 5. Dynamic-fee controller

### 5.1 Version 1 inputs

Version 1 uses only bounded, deterministic inputs available to both execution and quote paths:

- lagged timestamp-based EWMA volatility from prior pool/reference observations;
- current active liquidity and estimated tick/price impact for the requested size;
- lagged direction-aware net-flow/toxicity statistics, including future-price markout from completed
  observations;
- the managed inventory skew published under a bounded, stale-aware rule;
- swap direction, exact-input/exact-output mode, and amount;
- liveness, stale-state, and circuit-breaker status.

Do not use sender identity, wallet balance/history, private classifications, `tx.origin`, randomness,
or a value that a quoter cannot reproduce. External signed data may be researched later, but a PONS
feed must not be assumed to exist merely because Chainlink Data Streams is available on the chain.

### 5.2 Direction and units

Define inventory skew in a declared valuation currency:

```text
I = (value(token0 held) - target_value(token0)) / position_NAV
```

`I > 0` means excess token0. A `zeroForOne` swap sends token0 into the pool and removes token1, so it
worsens positive `I`; the opposite direction improves it. The implementation must preserve this sign.
Taking the absolute value before selecting a directional fee is a correctness bug.

All fees use v4 pips (`1e-6`) as bounded integers. Token values never enter settlement math as binary
floats.

### 5.3 Candidate policy

```text
fee_pips = clamp(
    base_fee
  + volatility_band_premium
  + expected_impact_premium
  + lagged_toxicity_premium
  + max(0, direction_worsening_inventory_premium)
  - benign_flow_discount,
    min_fee,
    max_fee,
)
```

Each term is capped separately. The total change is rate-limited by elapsed seconds and rule version.
`max_fee` must be below both the protocol-valid maximum and the point at which routing tests show the
pool has no useful demand. Fee override flags and fee value bits are constructed with canonical
`LPFeeLibrary` semantics and tested at every boundary.

Start with a discrete monotone band table, not an unconstrained learned on-chain model. A model may
estimate thresholds off-chain, but the deployed policy is compact, reviewable, deterministic, and
fully replayable. Parameter promotion uses walk-forward evaluation and cannot train on the final test
window.

Every size band is denominated separately in token0 and token1 integer base units. For v4,
`amountSpecified` names the input asset on exact-input swaps and the output asset on exact-output
swaps; the four direction/mode paths must select the correct token threshold. A single raw threshold
shared by assets with different decimals is invalid even when its Solidity integer type is safe.

### 5.4 State timing

The `beforeSwap` decision reads only state established before the current swap and the current swap
parameters. The `afterSwap` callback may update bounded EWMA statistics for the next swap. This avoids
charging a fee based on future information and makes replay order exact.

All decay uses timestamp deltas with maximum-gap clamping. Same-timestamp bursts, timestamp jumps,
zero liquidity, extreme amounts, exact output, price-limit truncation, and overflow boundaries have
explicit behavior. Stale input returns a declared conservative fallback fee or pauses new swaps; it
must never silently return zero.

### 5.5 Quote contract

Expose a view function equivalent to:

```text
previewFee(PoolKey, SwapParams, hookData)
  -> (feePips, ruleVersion, stateHash, validUntilTimestamp)
```

The production quoter uses the same library and rounding as `beforeSwap`. For routing, also provide a
bounded-gas indicative quote/swap-to-price surface where the selected router requires it. A quote is
valid only for its state hash and deadline. Fork tests assert quoted and settled amounts across both
directions and exact-input/output paths.

### 5.6 Fee-policy kill criteria

Disable or roll back the candidate rule when any precommitted condition occurs:

- quote-versus-settlement mismatch above base-unit tolerance;
- fee override outside bounds or unexpected rule version;
- route share/volume collapses beyond the elasticity envelope;
- fee-policy cell underperforms the best static control beyond its loss/error budget;
- manipulation test moves the fee more cheaply than the expected value extracted;
- hook gas or revert rate breaches its SLO;
- reference/position state is stale or inventory sign cannot be reconciled.

## 6. Automated range and inventory controller

### 6.1 Separate experiments first

Fee and range policies must not be introduced simultaneously. The order is:

1. static fee + fixed range control;
2. dynamic fee + identical fixed range;
3. static fee + candidate active range;
4. dynamic fee + candidate active range.

This prevents a lucky directional position or frequent recentering from disguising a bad fee rule.

### 6.2 Candidate range representation

Use a bounded set of non-overlapping or intentionally nested tranches, each with:

- lower/upper tick aligned to `tickSpacing`;
- capital and liquidity in integer token units;
- purpose: core market making, outer inventory absorption/distribution, or idle reserve;
- maximum loss if the volatile token goes to zero;
- expected fee density, LVR, time in range, and exit depth;
- independent position identifier and accounting ledger.

The optimizer chooses weights; values such as 60/30/10 are experiment candidates, not production
constants. Total exposure remains bounded even when nested ranges become simultaneously active.

### 6.3 Proposal objective

For a candidate action `a` over horizon `H`:

```text
marginal_value(a) = expected_incremental_fees
                  - expected_incremental_LVR
                  - inventory_risk_charge
                  - swap/rebalance/gas/MEV_cost
                  - failure_and_model_uncertainty_buffer
```

Act only when the lower confidence bound of `marginal_value(a)` exceeds a precommitted minimum benefit.
“Do nothing” is always a valid candidate and often the correct one.

### 6.4 Rebalance triggers

A rebalance proposal requires all applicable conditions:

- price has left the inner buffer or target inventory band for a minimum wall-clock dwell time;
- expected benefit exceeds total cost and hysteresis threshold;
- minimum cooldown and daily rebalance limits have not been hit;
- reference price, route depth, nonce, balances, approvals, and gas reserve are fresh;
- volatility/toxicity is not above the no-recenter circuit-breaker threshold;
- the post-action position satisfies token, range, concentration, and worst-case loss limits;
- pinned simulation succeeds with minimum amounts and a short timestamp deadline.

A range crossing does not automatically mean “withdraw immediately.” The controller evaluates hold,
remove, recenter, reduce, and unwind counterfactuals. Repeated buy-high/sell-low recentering is treated
as realized LVR, not operational success.

### 6.5 Atomic action and state machine

Where supported by the pinned periphery contracts, one action bundles removal, fee collection,
bounded inventory swap, and new liquidity placement atomically. Otherwise, Phase 1 rejects the action
rather than carrying an unbounded naked leg.

```text
OBSERVED -> PROPOSED -> RESERVED -> PINNED_SIMULATED -> POLICY_APPROVED
         -> SIGNED -> SUBMITTED -> SOFT_CONFIRMED -> RECONCILING
         -> L1_POSTED/FINALIZED -> CLOSED

Any state -> EXPIRED
Any uncertain state -> HALTED; only verified risk reduction may proceed
```

Intent identity includes chain, position, old/new ranges, exact amounts, maximum swap input, minimum
output, fee/gas bounds, nonce, state block/hash, deadline, config hash, and code hashes. Retries cannot
create a second economic rebalance.

## 7. Routing and liquidity bootstrapping

### 7.1 Route acquisition is part of alpha

For a newly initialized hook pool, continuously sample executable quotes for both directions and a
log-spaced size grid against every material same-pair route. Record:

- selected route and split;
- quoted/settled output, price impact, all fees, hook gas, and latency;
- our active-liquidity share and depth at each size;
- benign-flow retention and toxic-flow avoidance;
- reason for router exclusion or quote failure.

Integration with a standard router or UI must be demonstrated, not inferred from protocol
compatibility. If a custom quoter/router is required, its trust, allowance, calldata, and audit surface
becomes part of Phase 1.

### 7.2 Bootstrap rules

- Initialize only through an allowlisted factory after checking the pool does not already exist and
  the initial price is within an independently sourced bound.
- Prevent third-party front-running of initialization where the chosen callback design requires it.
- Begin with a wide, low-concentration position and no external deposits.
- Verify that unauthorized additions revert, authorized additions reconcile, and every authorized
  position can be removed when governance, keeper, and fee logic are paused.
- Do not pay incentives until organic quote competitiveness is measured; never count circular volume
  or self-paid incentives as revenue.
- Cap capital by two-sided executable exit depth, not reported TVL.
- Reject launch if sampled router quotes cannot reach the pool or if fee logic makes exact quoting
  unreliable.

## 8. Accounting and research protocol

### 8.1 Token-level conservation

For every swap, liquidity action, and accounting period, reconcile independently:

```text
wallet delta + PositionManager delta + PoolManager delta + hook delta
+ protocol fee + LP fee + ERC-20 transfer delta + native gas delta = residual
```

The real implementation uses correctly signed per-asset equations rather than summing unlike units.
Residual tolerance is defined in token base units and may not be hidden by USD rounding.

### 8.2 Net PnL decomposition

```text
net_pnl = collected_fees
        + uncollected_fee_change
        + inventory_mark_to_market
        - hold_benchmark_change
        - LVR/adverse_selection_markout
        - rebalance_swap_cost
        - L2_execution_and_L1_data_gas
        - failed_transaction_cost
        - incentives_and_routing_cost
        - financing/opportunity_cost
```

Report gross fees, net PnL, and excess return separately. Use reference-price markouts at several
wall-clock horizons, such as 1 s, 5 s, 30 s, and 5 min, when data quality supports them. If no
independent PONS reference is defensible, label LVR/toxicity unresolved and block profitability claims.

Also report fee ownership explicitly:

```text
Oasis fee capture = total LP fees * Oasis active liquidity / total active liquidity
```

A permissioned pool can keep this share near 100%, but it must fund competitive depth itself. That is
a capital-efficiency tradeoff, not free excess yield, and routing response remains part of net PnL.

### 8.3 Counterfactual routing

Historical replay has two layers:

1. **Accounting replay:** reproduce what actually happened, including effective swap fee.
2. **Counterfactual model:** simulate changed fee/liquidity against competitor routes and demand
   elasticity. At minimum publish pessimistic, base, and optimistic elasticity scenarios.

It is invalid to multiply a new fee by unchanged historical volume. The worst credible routing model,
not the most profitable model, drives the capital gate.

### 8.4 Required dashboard

At minimum expose:

- NAV, inventory by token, delta, range utilization, time in range, and exit depth;
- effective fee distribution by direction/size/rule version;
- volume, route share, fee capture, markout/LVR, and fee elasticity;
- gas per swap and rebalance, hook revert rate, quote mismatch, and simulation drift;
- PnL attribution for `R00/R01/R10/R11` and buy-and-hold;
- drawdown, expected shortfall, loss velocity, capacity, and capital utilization;
- RPC/feed age, sequence gaps, provider disagreement, finality lag, and reconciliation residual;
- current config/code/evidence hashes and every halt reason.

## 9. Risk budgets and automatic responses

### 9.1 Canary sizing

Before launch the operator sets absolute caps. The calculated canary cap is no larger than the minimum
of:

- the approved absolute experiment loss amount;
- 5 basis points of liquid portfolio NAV;
- 1 basis point of independently verified two-sided exit depth at the approved maximum impact;
- the amount whose complete loss leaves all gas, operations, and unwind reserves untouched.

These are conservative initial ceilings, not evidence the trade is safe. PONS exposure is stressed to
zero and USDG is separately stressed for depeg/freeze/issuer risk.

### 9.2 Failure matrix

| Trigger | Automatic response |
|---|---|
| RPC/feed stale, gap, or disagreement | No new exposure or rebalance; reconcile from independent source |
| Sequencer outage or abnormal finality lag | Halt submissions; preserve ETH; resume only after grace period |
| Reference price stale/manipulable | Conservative fee fallback; disable range growth/recenter |
| Quote/settlement mismatch or hook revert | Disable route and fee-rule version; retain evidence |
| Position/balance/allowance/nonce mismatch | Halt executor; no compensating trade from uncertain state |
| Token bytecode/admin/transfer behavior changes | Reject token and prepare bounded removal |
| Hook/PoolManager/router/quoter code hash changes | Fail closed pending new audit and fork run |
| Range exit during toxic/jump regime | Do not reflexively recenter; evaluate reduce/hold/unwind |
| Daily loss, drawdown, residual, or gas budget breach | Independent kill switch; cancel growth and unwind per runbook |
| Governance/security incident | Freeze parameter changes and follow incident-response runbook |

Emergency logic must remain usable when the strategy process, model, primary RPC, or ordinary signer
is unavailable. It may reduce exposure only and cannot raise limits.

## 10. Implementation work packages and gates

### WP0 — Correctness foundation

Finish canonical event/topic cardinality and sign-extension validation, protocol-valid tick spacing,
finite numeric validation, per-token integer accounting, collision-resistant opportunity identity,
method-aware RPC retry policy, and evidence provenance. Fix the current direction-erasing dynamic-fee
prototype before using it in any result.

**Gate:** property/unit tests fail on malformed inputs and demonstrate both fee directions; no float,
fractional base-unit, or cross-token settlement path remains.

### WP1 — Benchmark dossier and pool-access decision

Produce a two-provider evidence manifest for PoolKey, all deployments/code hashes, hook permissions and
control plane, token authority/sellability, historical events, active LP ownership, executable depth,
router/quoter support, and a pinned-fork add/remove test.

**Gate:** explicit `accessible`, `permissioned`, or `unresolved` decision. Only `accessible` permits an
LP experiment in the observed pool; all other outcomes keep it read-only.

### WP2 — Exact replay and ledger

Implement historical backfill, reorg handling, v4 state reconstruction, per-position fee growth,
gas, reference markouts, and token conservation. Reproduce sampled transactions against a pinned
local fork.

**Gate:** zero unexplained residual above base-unit tolerances and receipt/state agreement for both
directions and liquidity lifecycle events.

### WP3 — Research harness

Implement `R00/R01/R10/R11`, walk-forward splits, fee-volume elasticity, competitor routing, stress
paths, capacity, and experiment registry. Precommit parameters and rejection thresholds before the
untouched evaluation window.

**Gate:** positive lower-bound net excess return in eligible regimes without relaxing thresholds after
seeing test results. Otherwise publish rejection and iterate in research mode.

### WP4 — Minimal hook and executor

Pin v4-core/periphery commits, implement preview-identical fee logic, exact address flags, factory,
bounded executor, governance, events, and emergency behavior. No upgradeable proxy or public vault.

**Gate:** source/runtime reproducibility, independent review, Slither with justified findings, unit,
fuzz, invariant, symbolic/manual review where useful, and gas budgets.

### WP5 — Adversarial fork verification

Test both directions, exact input/output, price limits, same-timestamp bursts, stale state, maximum
amounts/fees, reentry, malicious/nonstandard tokens, direct initialization, unauthorized callbacks,
JIT liquidity, sandwich/backrun scenarios, sequencer/RPC faults, duplicate keeper runs, and emergency
removal.

**Gate:** no critical/high issue, all capital-safety invariants pass, and a fresh environment can
reproduce the artifacts and deployed address.

### WP6 — Live shadow and routing proof

Run decisions without signing. Compare predicted fee, route, post-swap state, markout, and hypothetical
positions to live outcomes. Exercise restart, failover, stale-data, and kill-switch drills.

**Gate:** SLO/error budgets hold for a predeclared duration and sample count; the pool is actually
quotable/routable across the required size grid.

### WP7 — Testnet and mainnet canary

Testnet proves lifecycle correctness but cannot prove economics. Mainnet canary uses the sizing rule,
isolated signer policy, daily loss/gas caps, human-visible alarms, and rehearsed unwind.

**Gate:** full accounting/finality closure, no policy bypass, no unresolved incident, and net results
inside the precommitted confidence/risk envelope before any scale increase.

### WP8 — Bounded production

Scale only one step at a time using marginal capacity and tail risk. Any code, token, router, market
regime, or material parameter change returns to the appropriate earlier gate.

## 11. Phase 1 release criteria

Phase 1 is successful only when all of the following are true:

- the selected production pool, not merely the benchmark, has a complete evidence dossier;
- the hook is transparent, reproducible, publicly quotable, minimally permissioned, and independently
  reviewed;
- the position controller can choose no action, halt, restart, and unwind safely;
- `R11` beats buy-and-hold and comparable static/passive LP controls out of sample after all costs;
- fee-policy contribution is separately positive under the accepted elasticity envelope;
- routing supplies real external volume without hidden incentives or self-trading;
- every token unit and transaction state is reconciled;
- stress loss, drawdown, concentration, gas, finality, and reliability remain within fixed budgets;
- shadow and canary gates complete without unresolved critical/high findings.

If these conditions do not hold, the valid result is “no deploy” or “do not scale.” The agent's edge
includes declining bad liquidity opportunities quickly and preserving capital for a better regime.

## 12. Immediate ordered backlog

1. Repair the WP0 correctness blockers in the current Python foundation.
2. Persist the reconnaissance above as a two-provider, block-pinned evidence bundle.
3. Prove or reject benchmark-pool liquidity access on a local fork.
4. Backfill the full target-pool lifecycle and build the exact token ledger.
5. Measure effective fee by swap direction, size, volatility, markout, and routing context.
6. Build static-fee/fixed-range controls before optimizing any policy.
7. Implement the discrete bounded dynamic-fee simulator and fee-elasticity scenarios.
8. Select the production pair using token safety, reference price, route demand, and exit depth; do not
   assume PONS/USDG wins.
9. Extend the implemented Solidity invariants and official PositionManager lifecycle into pinned
   Robinhood fork, malicious-token, Permit2, reentry, and emergency-path campaigns.
10. Complete fork, shadow, routing, and canary gates in order; never deploy from a backtest alone.
