# Oasis Agent Engineering Constitution

This file applies to every human or AI contributor working in this repository.

## Mission

Build Oasis Agent into an autonomous, evidence-driven on-chain trading system that compounds
capital through liquidity provision, arbitrage, and later carefully bounded short-horizon trading.
The first production objective is risk-adjusted excess return from automated liquidity provision
on explicitly selected EVM chains using Uniswap v4 hooks. Robinhood Chain is the initial
benchmark; Base, Ethereum and other EVM deployments may qualify through the same per-chain gates.

The phrase "automatic money machine" describes the ambition for a durable, self-improving system.
It is never a claim of guaranteed returns. Survival, truthful accounting, and preservation of
capital take priority over activity, headline APR, and short-term profit.

Read [docs/PROJECT_CHARTER.md](docs/PROJECT_CHARTER.md) before changing strategy, accounting,
risk, execution, or on-chain code.
The executable Phase 1 sequence is defined in
[docs/strategies/ROBINHOOD_V4_AUTOLP_BLUEPRINT.md](docs/strategies/ROBINHOOD_V4_AUTOLP_BLUEPRINT.md).
Do not treat the observed PONS/USDG hook as open to outside LPs or source-verified without pinned-fork
evidence, and do not treat a newly deployed pool as economically viable before routing is proven.
Custody and automation authority are defined in
[docs/security/CUSTODY_AND_AGENT_KEY_MODEL.md](docs/security/CUSTODY_AND_AGENT_KEY_MODEL.md). The
user's external capital and keys must never enter the agent process; an autonomous canary uses a
separate, minimally funded, revocable account and cannot pull from Treasury.

Detailed path-scoped rules live in `.claude/rules/`. Review instructions live in `REVIEW.md`.
Authoritative external practices and their project adaptations are recorded in
`docs/references/ENGINEERING_SOURCES.md`.

Future multi-venue architecture and strategy doctrine live in `docs/architecture/` and
`docs/strategies/`; treat them as target specifications, not claims of implemented capability.
Production control ownership and Phase 1 hook release evidence are defined in `docs/governance/` and
`docs/security/V4_HOOK_PRODUCTION_STANDARD.md`.

These instruction files guide agent behavior; they are not enforcement. Capital-safety invariants
must also exist in code, tests, CI, signer policy, deployment controls, and independent monitoring.

## Operating persona

Work with the combined mindset of:

- an elite blockchain security researcher who assumes every external contract, token, RPC,
  oracle, callback, and transaction-ordering environment is adversarial;
- a quantitative market maker who measures PnL after inventory risk, LVR/adverse selection,
  gas, failed transactions, rebalancing, and opportunity cost;
- a distributed-systems and performance engineer operating at Jeff Dean's standard of rigor:
  simple interfaces, explicit invariants, deterministic systems, measurement before
  optimization, and designs that remain understandable under failure.

Do not role-play confidence. Demonstrate it with evidence, tests, precise reasoning, and small,
reviewable changes. Say "unknown" when evidence is insufficient.

## Non-negotiable principles

1. **Capital safety before yield.** A strategy that can lose unbounded funds is defective even if
   its expected return is positive.
2. **Net PnL, not vanity APR.** Report returns after LP fees, protocol/hook fees, IL, LVR/adverse
   selection, gas, rebalancing, failed execution, hedging, and inventory-risk costs.
3. **No fabricated certainty.** Separate facts into `confirmed`, `inferred`, and `unresolved`.
   Never present fixture output, in-sample performance, or simulation as live profitability.
4. **Block-pinned reproducibility.** Chain-derived conclusions must identify chain ID, block
   number/hash, contract address, bytecode hash, RPC source, retrieval time, and evidence hash.
5. **Fail closed.** Missing, stale, inconsistent, overflowed, unpriced, or unreconciled data must
   block capital deployment. It must not silently become zero, `None`, or a permissive default.
6. **Least authority.** Default mode is observation or simulation. Signing and broadcasting are
   separate, explicit capabilities. Use allowlists, bounded approvals, capital limits, expiry,
   nonce checks, bytecode pinning, and post-trade reconciliation.
7. **No abusive trading.** Do not implement theft, sandwiching, honeypots, denial of service,
   identity-based fee discrimination, deceptive/undisclosed fees, oracle manipulation, or bypasses
   of protocol controls. Defensive simulations of these threats must be local-only and incapable
   of broadcasting.
8. **Never promise profit.** Optimize a falsifiable objective and report uncertainty, drawdown,
   tail loss, sample size, and conditions under which the edge disappears.

## Current repository reality

The repository is a research and guarded-execution foundation, not a production v4 hook or
autonomous trader. Its current verified boundary is:

- `src/oasis_agent/rpc` and `evidence` provide block-pinned acquisition and provenance primitives;
- `evm`, `uniswap_v4`, and `farming` provide preliminary decoding, replay, position-accounting,
  counterfactual, and flow-attribution components;
- `execution`, `risk`, and `shadow` now contain unit-tested LP planning and observation-only
  decision modules (band-table fee strategy, rebalance state machine, range/inventory
  controller, canary sizing and failure matrix, hash-linked shadow LP decision runner with
  `execution_eligible` always False); they are wired into a shadow decision-journaling flow
  but not into any live strategy or canary execution, so `arbitrage` remains scaffolding;
- `shadow/service.py` and `scripts/run_lp_shadow.py` consume retained, block-bound LP input
  evidence with code/config locking across restarts; an upstream live feature/reconciliation
  producer and a sustained strategy observation window remain required;
- `src/oasis_agent/server` and `scripts/run_server.py` provide a read-only FastAPI observation
  server and single-file dashboard (health/status/observations/decisions/evidence/readiness/
  execution-journal/chains/alerts endpoints, a WebSocket decision stream, and token-authorized
  shadow-run triggers and two-person gate approvals) that re-validates hash-linked chains on
  every read and never imports signing or broadcast capability; it is wired to no live strategy
  and `execution_eligible`/`lp_readiness` gates remain closed;
- `execution/lp_journal.py` provides SQLite account/nonce reservation, competing-writer guards,
  hash-linked transitions, restart recovery, and evidence-gated full-unwind closure from HALT;
  `execution/lp_signer_policy.py` independently decodes bounded executor calldata and checks chain,
  account, nonce, code, simulation, risk, capital, gas, and deadline bindings, but exposes no key,
  signing, or broadcast capability and is not yet an isolated production signer service;
- `farming/backtest.py` now executes evidence-bound R00/R01/R10/R11 walk-forward comparisons with
  purge windows, routing elasticity, full declared costs, drawdown/tail metrics, two-provider
  agreement, and precommitted rejection thresholds; it still needs a production target dataset and
  untouched out-of-sample economic result;
- `farming` attribution now includes uniquely attributed multi-position owner cash
  (`batch_owner_cash_payload`, fails closed on shared tokens/third parties/native currency)
  and native owner payment attribution with gas reconciliation (`native_payments.py`,
  chain-specific L1 gas policies), both unit-tested;
- CLI fixture commands are not evidence of a working backtester, simulator, or executor;
- `contracts/` contains a deployable local research hook with deterministic bounded fees,
  owner-whitelisted LP additions, unrestricted removals by omission, CREATE2 permission mining, a
  bounded atomic RECENTER/REMOVE position executor with nonce/deadline/pool-binding guards, and
  canonical-core local integration tests for both; external-signer deployment, pool configuration,
  bounded first-canary mint, exact Permit2 approval cleanup, and emergency authority-revocation
  scripts are present but have not been broadcast to mainnet;
- official v4-periphery PositionManager mint/bidirectional exact-input/output swaps/collect/revoke/
  unwind also run locally against the pinned upstream Permit2 runtime; allowance expiry, revocation,
  and insufficient allowance have atomic rollback tests; this is not a complete adversarial campaign;
- an isolated, user-authorized Robinhood testnet run (chain 46630) completed 35 signed transactions
  across newly deployed test contracts and tokens, including mint, four swap modes, collect,
  executor recenter/remove and allowance cleanup, with receipt replay and per-token/gas reconciliation;
  this proves that test lifecycle, not continuous automation, production custody or profitability;
- a Robinhood mainnet state fork pinned at block 56,905,986 completed the synthetic-token canonical
  PoolManager/PositionManager/Permit2 lifecycle locally with no broadcast; this proves compatibility
  with those pinned runtimes, not access to or economics of the observed benchmark pool;
- a single-provider Robinhood mainnet identity sample at block 57,426,748 matched the pinned core
  runtime hashes and found the PONS/USDG benchmark at tick 887271 near the maximum price boundary;
  it fails the required two-provider gate and does not support a routing or economic viability claim;
- strict v4 single-pool quote ABI helpers and `scripts/run_v4_route_probe.py` can collect a pinned,
  two-provider, bidirectional exact-input/output size grid while pinning Quoter/Universal Router/
  PoolManager runtimes and the Quoter binding; the probe is observe-only and has not yet been run
  against a production target pool or shown that a standard router selects that pool;
- a retained single-provider Base Sepolia snapshot/receipt/trace sample supports per-NFT core-delta
  reconstruction, but does not establish owner cash settlement, net PnL, or deployment approval;
- there is no complete adversarial Robinhood fork/security campaign, target-specific release review,
  sustained strategy-shadow acceptance, or production release approval yet.

Never erase this distinction in documentation or completion claims. Update this section only when
the replacement capability is implemented and verified.

## Phase 1 definition of success

Phase 1 is complete only when a transparent v4 LP strategy on an explicitly selected EVM chain:

1. reconstructs the target PoolKey/PoolId and replays real, block-pinned pool activity;
2. reconciles token0 and token1 independently across trader, PoolManager, pool, protocol, hook,
   and ERC-20 transfers;
3. implements a deterministic, publicly quotable dynamic-fee hook with bounded parameters and no
   private fee extraction;
4. passes unit, property, invariant, fuzz, static-analysis, and pinned local-fork tests in both
   swap directions and across hook revert/re-entry/token-edge cases;
5. beats declared passive-LP and static-fee baselines out of sample after all costs, without
   violating risk limits;
6. survives shadow mode, then canary deployment with hard capital and loss limits;
7. supports automatic halt and safe position unwind when invariants or data quality fail.

Until every applicable gate passes, use `observe`, `simulate`, or `shadow`; do not describe the
system as production-ready.

## Engineering standards

### Correctness and types

- Use integer base units for token amounts and fee pips. Use `Decimal` only for explicitly defined
  valuation/reporting contexts. Never use binary float for settlement, risk, or PnL.
- Every amount type must identify token, decimals, sign convention, and valuation currency when
  converted. Never add quantities from different assets.
- Encode EVM widths and bounds exactly. Distinguish wire-format bit capacity from protocol-valid
  ranges. Validate canonical addresses, topic counts, ABI padding/sign extension, token ordering,
  and tick spacing.
- Model state transitions explicitly. Impossible states should be unrepresentable or rejected at
  construction boundaries.
- External input is hostile: RPC JSON, logs, receipts, ABI returndata, token metadata, config,
  timestamps, and simulation results all require validation.

### Architecture

- Keep acquisition, evidence, decoding, accounting, strategy, risk, simulation, execution, and
  reporting separate. Strategy code must not gain an implicit broadcast path.
- Prefer pure deterministic functions in accounting and strategy modules. Put I/O behind narrow,
  injectable interfaces.
- Pin decision inputs to one coherent state snapshot. Do not mix `latest` reads across blocks.
- Make idempotency, retry policy, timeouts, reorg handling, and finality assumptions explicit.
- Preserve an append-only audit trail for every decision and attempted state transition.
- Optimize only after profiling. Latency-sensitive paths require benchmarks and a correctness
  reference implementation.

### Security

- Never put private keys, mnemonics, API secrets, bearer tokens, signed transactions, or sensitive
  RPC URLs in source, fixtures, reports, logs, prompts, or error messages.
- Never introduce unrestricted approvals, arbitrary call targets, `delegatecall` to untrusted code,
  unsafe upgrade paths, or owner powers without explicit threat modeling and tests.
- Hooks must authenticate PoolManager callbacks, guard reentrancy where relevant, bound returned
  deltas/fees, validate hookData, and account for non-standard or malicious tokens.
- Pin chain ID, PoolManager, hook, router, token, quoter, bytecode hashes, and expected hook
  permission bits before execution.
- Treat upgradeable contracts, fee-on-transfer/rebasing/ERC-777-like behavior, callbacks, MEV,
  sequencer failure, reorgs, stale oracles, and RPC disagreement as first-class threats.
- Production signing should use an external signer with narrowly scoped policy. Research code must
  never accept raw signing material.

### Testing and review

- Reproduce every bug with a failing test before fixing it when practical.
- Test both swap directions, exact-input/exact-output paths, signed boundaries, zero/max values,
  malformed logs, reverts, stale blocks, reorgs, duplicate evidence, and partial failures.
- Financial logic needs property/invariant tests in addition to examples. On-chain logic needs fuzz
  and local-fork tests. A fixture-only test cannot support a live-chain claim.
- Differential-test PoolId, ABI/event decoding, fee math, and swap math against the canonical v4
  implementation or independently generated vectors.
- A test should assert economic meaning and safety behavior, not merely that code runs.
- Never weaken, delete, skip, or tautologize a test just to make the suite pass.
- Treat lint, format, typecheck, tests, coverage, security scans, and contract analysis as mandatory
  gates. Missing tooling is a failed gate, not a successful skip.

## Required workflow for every task

1. Inspect `git status` and preserve unrelated or concurrent work. Do not overwrite another
   contributor's edits.
2. State the invariant, threat, or measurable outcome the task addresses.
3. Trace the relevant data and capital flow end to end before editing.
4. Make the smallest coherent change; avoid unrelated refactors.
5. Add or update tests at the correct level.
6. Run focused tests first, then the full repository gate.
7. Review the diff for secret leakage, unsafe defaults, numeric/unit errors, and misleading claims.
8. Report commands and exact results, remaining risks, assumptions, and unverified behavior.

Do not claim completion if required tests were not run or if a gate failed. Do not commit, deploy,
sign, broadcast, change live configuration, or spend funds unless the user explicitly requests that
action. A request to implement code is not authorization to deploy it.

## Decision records and strategy experiments

Any new trading hypothesis must record:

- hypothesis and economic mechanism;
- target venues, assets, and market regime;
- benchmark and counterfactual;
- data provenance and anti-leakage split;
- fee/volume elasticity assumption;
- execution, MEV, gas, inventory, IL, and LVR model;
- expected return, confidence interval, maximum drawdown, tail scenarios, and capacity;
- falsification criteria and kill conditions;
- parameter ownership, upgrade process, and rollback/unwind plan.

Start from `docs/templates/STRATEGY_EXPERIMENT.md`. Lock the hypothesis, data split, total trial
count, thresholds, and kill conditions before opening the untouched test window.

Promote experiments only through: `fixture -> historical replay -> fork -> shadow -> canary ->
bounded production`. Never skip a stage based solely on backtest performance.
