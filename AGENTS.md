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

## Project-owner release policy update (2026-09-15)

An external/independent security audit is not required by the project owner. This supersedes
older external-audit prerequisites; it is not an audit pass. Internal code review, executable
security validation, raw evidence checks, capital/loss limits and operator release approval remain
required. The existing `independent_reviewer` field is engineering/promotion review metadata,
not proof of an external audit, and must never contain a fabricated reviewer.

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
- `farming/position_quorum.py` reconstructs two agreeing raw-RPC position snapshots; scoped LP
  cycles can be combined with explicit bounds by `execution/lp_preparation.py` into canonical
  RECENTER/REMOVE simulation-only calldata. `execution/lp_simulation.py` adds two-source hash-pinned
  eth_call with intent-derived, role-bound runtime pins and retained v3 envelope/deployment-bound
  evidence; signing and release authority remain absent from this preparation flow.
- Launch retained-input diagnostics and token reconnaissance accept explicit v2 multi-chain target
  policies and v3 holder-risk inputs, with raw two-provider holder capture/replay. The historical
  execution monitor remains Base-specific and retains a separate raw-key live path, not the unified
  production custody model. This is not multi-chain live trading.
- `shadow/service.py` and `scripts/run_lp_shadow.py` support atomic, idempotent cycle consumption and
  optional bounded JSONL following, with partial-line waits, truncation/rotation checks and idle HALT.
  They consume retained, block-bound LP input evidence with code/config locking across restarts; an upstream live feature/reconciliation
  producer and a sustained strategy observation window remain required;
- Bounded pool-state observation now has persistent RPC budgets, raw two-provider market-window
  replay, mature/deferred sample handling, and hash-linked checkpoint recovery (at most 1000 windows).
  This is not a complete unattended backfill/reconciliation service or a sustained acceptance result;
- `src/oasis_agent/server` and `scripts/run_server.py` provide a read-only FastAPI observation
  server and modular dashboard (health/status/observations/decisions/evidence/readiness/
  execution-journal/chains/alerts endpoints, a WebSocket decision stream, and token-authorized
  shadow-run triggers and two-person gate approvals), plus versioned LP shadow / Launch observe
  campaigns with session RPC profiles, isolated retained inputs, idempotent start, process-group
  stop and restart recovery. It checks SQLite data versions on every journal read and fully
  revalidates changed hash-linked chains in read transactions, and never imports signing or
  broadcast capability; it is wired to no live strategy
  and `execution_eligible`/`lp_readiness` gates remain closed;
- `execution/lp_journal.py` provides SQLite account/nonce reservation, competing-writer guards,
  hash-linked transitions, restart recovery, and evidence-gated full-unwind closure from HALT;
  `execution/lp_signer_policy.py` independently decodes bounded executor calldata and checks chain,
  account, nonce, code, simulation, risk, capital, gas, and deadline bindings, but exposes no key,
  signing, or broadcast capability and is not yet an isolated production signer service;
- `execution/lp_signer_sidecar.py` adds durable nonce reservation, external-provider signature
  verification, and an optional independent account-risk admission boundary: when a guard is
  configured it must reserve reconciled capital before any provider call, an attempt that produces
  no transaction releases its reservation, and a successful signature keeps it until an independent
  reconciliation releases it. `socket_key_provider.py` implements a bounded local Unix-socket digest
  adapter. It is not a key daemon, and the default CLI configures neither a production provider nor a
  risk guard, so it always denies. Default shadow children receive a minimal environment, which is
  not OS-level isolation from wallet files;
- `farming/backtest.py` now executes evidence-bound R00/R01/R10/R11 walk-forward comparisons with
  purge windows, routing elasticity, full declared costs, drawdown/tail metrics, two-provider
  agreement, and precommitted rejection thresholds; it still needs a production target dataset and
  untouched out-of-sample economic result. Its v2 protocol also requires per-fold excess return
  over hold/R00/R01/R10 and strict calibration/evaluation block separation;
- `farming` attribution now includes uniquely attributed multi-position owner cash
  (`batch_owner_cash_payload`, fails closed on shared tokens/third parties/native currency)
  and native owner payment attribution with gas reconciliation (`native_payments.py`,
  chain-specific L1 gas policies), both unit-tested;
- `farming/owner_quorum.py` verifies native/ERC-20 owner cash from raw two-provider snapshots,
  with token identity binding; this does not establish complete account NAV or sustained net PnL;
- `farming/cash_window.py` reconstructs a whole position window from two-source retained raw
  snapshots, complete core/NFT event ranges and every modification payment, deriving the owner
  at each action. It rejects gaps at successive boundaries and never claims full-account NAV,
  valuation, all gas costs, finality or live eligibility;
- `risk/canary_instructions.py` parses owner USD limits exactly and checks explicit capital/loss
  inputs; the owner confirmed a dedicated wallet and USD 200 daily / USD 500 cumulative loss caps.
  The owner subsequently approved USD 500 total capital. `risk/capital_journal.py` now persists
  an account/policy-bound loss latch across restarts and days, with idempotent observations and
  hash-linked history checks. Independent NAV valuation and signer/broadcast integration remain
  unconnected; this is not yet a deployed production kill switch;
- `risk/capital_reservations.py` supports shared cross-chain USD reservations with serialized
  writers, idempotency, retained pending commitments and explicit settlement release. Base and
  Robinhood share the owner-approved USD 500 total; per-chain losses are reconciled by the global
  ledger below, which is not yet wired to this component. Neither legacy execution nor signer is
  wired to this component;
- `risk/account_risk.py` is a single cross-chain reconciled-account ledger: one append-only,
  hash-linked SQLite state carrying each account's NAV, per-asset movement reconciliation,
  non-trading-flow explanation, conservative gross loss (a gain in one account never offsets a loss
  in another), day/cumulative loss latch, and capital ceiling over deployed investment plus pending
  commitments. A commitment settles only against investment units its own account gained between two
  consecutive frames, valued at the new frame's prices and floored, at most one unit of pending per
  observed unit, so capital already spent is not charged against the ceiling twice; an appreciation
  of units already held settles nothing, a settled commitment is refused by `release()` as
  unproduced, and no caller can request a settlement. It timestamps by a trusted integer clock,
  requires an exact-midnight reconciliation across day boundaries, and latches the halt without
  weakening; `current_state()` is a pure read-only baseline that never appends an event.
  `execution/account_risk_guard.py`
  binds it to the signer as an admission boundary over an injected `AccountRiskReader`.
  `risk/portfolio_manifest.py` declares the reconciled accounts/assets and must exactly cover one
  `GlobalRiskPolicy` inventory, and may declare v4 LP positions: a `ManifestPosition` pins the
  position manager, state view, pool manager, ERC-721 token id, pool id and both pool currencies
  plus the state-view and pool-manager runtime hashes, and `ChainInventory.asset_ids` covers
  positions too, so a declaration can never add an asset the ledger does not count.
  `farming/account_inventory.py` and `farming/account_flows.py`
  capture and replay two-provider, block-pinned balances/feeds, `ownerOf` for every declared
  position and raw `Transfer`/ERC-721 movements
  (unexplained residuals become `unresolved_operations`, never zero; every transfer-driven token
  delta is credited, including transfers between monitored accounts, while gas and undeclared
  native movement are never credited). `execution/retained_position.py` values one declared position
  from retained proof only — two agreeing position snapshots, the pool's slot0 window covering the
  position's own block and one fresh feed round per pool currency — floored leg by leg.
  `execution/account_risk_reader.py`
  (`EvidenceReplayAccountRiskReader`) is the first `AccountRiskReader` implementation: it replays
  those retained proofs into one complete `PortfolioFrame`, carries each declared position as an
  `investment` asset whose value is that floor (units must be exactly 1 and both capture paths must
  agree on ownership), and prices a signing request's worst-case
  commitment with ceiling rounding, raising on any missing, stale, disagreeing, undeclared-provider
  or unexplained input.
  `scripts/run_account_risk_reconcile.py` is an observe-only CLI that never opens the risk journal,
  reserves capital or admits a commitment; it binds retained sources to the manifest's declared
  providers, chains its own prior `RiskState` (`--state`) so successive runs accumulate day and
  cumulative loss, and takes native gas/non-gas units only as explicit operator declarations
  (`--native`). `risk/shadow_acceptance.py` adds the shadow precondition with no I/O: a
  `ShadowWindow` derives every summary — block range, span, last cumulative loss — from its own
  accepted runs, refuses a window with too few runs, a discontinuous sequence, a halted or
  unresolved run, a repeated ledger state, a regressed block or time, a gap beyond the requirement
  or a span below it, and `canary_admission()` refuses a window observed under another manifest or
  policy, an account outside the policy inventory, a future or stale window, and a canary cap above
  the approved total capital, the daily loss budget or the remaining cumulative loss budget; its
  default window requirements are stated operator minima, not chain facts, and neither function
  authorizes a transaction. `scripts/run_account_risk_shadow.py` is the observe-only loop that
  produces one such window, running the reconciliation observer in an isolated subprocess at a
  freshly pinned block, retrying a capture failure at a new block, halting the loop on a refused
  capture, and never opening the risk journal, holding a key, signing or broadcasting;
  `scripts/check_canary_admission.py` re-derives a retained window byte for byte against the manifest
  it claims to describe and prints the admission or refusal reasons, always with
  `execution_eligible: false`. One live run of that loop against the two public Base providers the
  example manifest declares completed a three-run window (blocks `51760279`/`51760358`/`51760414`,
  270s span, cumulative loss 0, no unresolved operations, seven attempts because the public providers
  dropped two attempts of each of the first two runs), and the admission check admitted a USD 50 cap
  while refusing a USD 300 cap against the owner's USD 200 daily budget; that window is retained only
  under `reports/evidence/` (not version controlled), its requirements are the repository minimum, it
  covers one native-only account, and no canary path consumes the admission, so it is a mechanism
  demonstration rather than canary acceptance. `configs/account-risk.base.example.json` is the only
  committed
  manifest: the
  owner-confirmed dedicated wallet on Base 8453, native-only, declaring no position, with an ETH/USD
  feed pin verified from two Base mainnet providers before it was written and re-parsed by a unit
  test so it cannot rot silently. The default signer CLI loads no reader or guard and still
  denies; no reconciliation run or shadow window is archived in version control, no Robinhood or
  ERC-20
  manifest pin is
  verified, no position is declared anywhere in a committed manifest, and no all-asset chain indexer
  or production signer/broadcast integration exists, so this is not a deployed production kill
  switch;
- Native wallet balances now support bounded two-provider capture and offline raw-RPC replay;
  real Base and Robinhood pinned samples agree. A same-block Base ETH/USD reference also replays
  from two providers with explicit integer valuation rounding. This is native-only historical
  reference valuation, not complete NAV, independent economic price consensus, or live risk approval;
- Base public dRPC/Alchemy samples now agree on six v4/token runtimes and three ETH/USDC
  pool states at block 51,410,447; the hookless fee=500 pool passed all 12 two-source direct
  quotes. A separate CLI observer sample at block 51,410,550 passed raw state replay. This
  establishes a Base quote benchmark, not Oasis-hook routing, profitability or execution authority;
- Base mainnet block 51,410,447 now passed the local canonical protocol lifecycle with synthetic
  tokens: mint, four swap modes, collect, revoked-whitelist exit and Permit2 cleanup, with
  pre/postflight block identity checks. This is local fork compatibility, not funded trading
  of the benchmark pool or an economic acceptance result;
- Base block 51,410,447 also passed a local real ETH/USDC hookless-pool lifecycle: mint,
  four exact swap modes, collection/removal, per-currency cash conservation, approval cleanup
  and missing-allowance atomic rollback. Artificial fork balances and generated flow do not
  establish organic LP returns, Universal Router selection or funded production execution;
- `launch/scoring.py` and `trend_import.py` provide GMGN/OKX retained-rank imports and a
  deterministic, fail-closed research score combining supplied holder, contract, depth and valuation
  assessments, with net-exit and LP-counterfactual proposals. CLI, API and console share the same
  score and retain `execution_eligible=False`; inputs are explicitly normalized imports, not raw
  attestations. `launch/holder_scoring.py` now binds two-source retained holder replay to the same
  token/runtime/decimals/block/time, derives holder metrics and fails closed on unresolved common
  control; its CLI retains the proof-binding hash. It is not a live holder indexer or contract scanner.
  Bounded authenticated provider capture exists separately (see below); automatic chain-contract
  enrichment, persistent position reconciliation, out-of-sample profitability and trading integration
  remain unimplemented;
- `launch/discovery.py`, `discovery_import.py` and `batch_scoring.py` provide bounded multi-token
  discovery, rank/time/provenance binding and shared batch research scoring without a fixed token.
  The shared batch API and console expose missing evidence, exclusion counts and independent
  held-position exit proposals; imported acquisition failures remain distinct from empty markets.
  These are user-triggered retained-input evaluations, not live feed polling. Public profile and OKX
  unauthenticated access tests returned HTTP 403 locally. `scripts/capture_token_research.py` now
  captures authenticated five-minute GMGN/OKX rankings on Base/Robinhood and bounded enrichment
  with response hashes, source-specific claims, unknown fields, failure records and offline replay.
  A Base run passed all nine requests, including GMGN security/pool/20-holder and OKX advanced
  risk/100-holder/cluster data. Version-two normalization now preserves holder share units and GMGN boolean aliases,
  rejecting duplicate holder rows and marking conflicting/invalid aliases unknown. Complete v1
  archives still replay; replay checks scope, selection, units and provider halt order. A fresh
  September 22 Base run captured all four OKX routes including 100 holders; GMGN CLI failed
  and its subsequent requests were halted. Provider claims are not chain attestations. Robinhood OKX cluster capture failed with HTTP 400. FOMO is authorization-required;
  sustained discovery, complete enrichment and execution remain unverified or unimplemented.
- CLI fixture commands are not evidence of a working backtester, simulator, or executor;
- `contracts/` contains a deployable local research hook with deterministic bounded fees,
  owner-whitelisted LP additions, unrestricted removals by omission, CREATE2 permission mining, a
  bounded atomic RECENTER/REMOVE position executor with nonce/deadline/pool-binding guards, and
  canonical-core local integration tests for both; external-signer deployment, pool configuration,
  bounded native/ERC-20 or ERC-20/ERC-20 first-canary mint, exact Permit2 approval cleanup,
  persistent revocable bounded-executor NFT operator authority, and emergency authority-revocation
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
- a two-provider Robinhood mainnet identity sample at block 57,634,560 matched the pinned core/token
  runtime hashes and the complete PONS/USDG PoolId/slot0 state; project hook/executor roles remain
  unconfigured, so the full deployment checklist remains ineligible for execution;
- strict v4 single-pool quote ABI helpers and `scripts/run_v4_route_probe.py` can collect a pinned,
  two-provider, bidirectional exact-input/output size grid while pinning Quoter/Universal Router/
  PoolManager runtimes and the Quoter binding; a real two-provider PONS/USDG run agreed on all
  identities and all 12 quote failures at the maximum price boundary, so that benchmark is not a
  viable production target and no standard-router selection has been established;
- the hookless native ETH/STOCKHOOKS pool from Uniswap position `2071232` was reconstructed as PoolId
  `0xaa82ac6c0cca2c126161a16b1bff0d820eb4cb81701df81258633b9fe57536d6`; at block
  `57,747,350`, two public providers agreed on pinned identities and all 12 exact-input/output quotes
  in both directions succeeded. This establishes a direct-quote benchmark, not routing or economics
  for the distinct pool that an Oasis hook would create;
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
