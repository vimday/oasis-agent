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
  They consume retained, block-bound LP input evidence with code/config locking across restarts;
  `execution/lp_cycle_assembly.py` is the deterministic `LpCycle` producer they were missing: it
  replays the two slot0 captures behind a market window, requires inventory and reference-price
  inputs to be two-provider agreed values at the decision block from that window's own providers,
  derives volatility/toxicity/marginal value from the verified feature window (a declaration can
  never override them), and retains the operator's declaration as its own block-pinned record, so
  every check precedes the first write. `execution/lp_position_inventory.py` derives a declared
  position's own decision-block token amounts from two agreeing retained position proofs and the
  window's own agreed pool price (principal plus reported uncollected fees, retained as
  `derived_position_integer` records that borrow each provider's own wire envelope).
  `execution/lp_reference_price.py` supplies the other input the feature window always demanded and
  nothing wrote: it replays each pool currency's own pinned USD feed observation through the window's
  own two providers at the decision block, re-reads that currency's on-chain `decimals()` beside its
  feed, requires both providers to agree on the q192 ratio and the ratio to be a plausible price of
  that pool, and retains one `derived_reference_integer` per provider that borrows that provider's own
  wire envelope. The source is the owner's declared feed pin per currency, and the value is derived
  from two observed feed answers rather than asserted beside the request; it is not a stablecoin peg,
  a settlement price or an economic estimate. `scripts/assemble_lp_cycle.py` is the operator step
  that rebuilds the window, derives that inventory and reference price and appends one cycle line,
  appending nothing when any check refuses. Its candidate terms, thresholds and `pin_simulated_ok`
  are explicit operator declarations, not an estimated edge: the fee/LVR/inventory model is still
  unlocked, no cycle has been produced against a funded account or a live market window, and a
  sustained strategy observation window across real markets remains required.
  `scripts/capture_lp_position.py` and `scripts/capture_reference_price.py` are the producers of the
  retained inputs those steps consume: the first reads one declared NFT and the second each pool
  currency's own USD feed through a two-provider quorum at one explicit block, both derive their
  scope or block from the captures themselves, take each provider as an explicit `{alias, env}` pair
  so the aliases can be the window's own two, refuse a request naming another chain, height, identity,
  feed or alias pair before their first write, and hold no key;
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
  signing, or broadcast capability and is not yet an isolated production signer service; it and the
  preflight producer share one public `preflight_payload()` definition, so the bytes a producer retains
  are exactly the ones this gate re-derives;
- `execution/lp_signer_sidecar.py` adds durable nonce reservation, external-provider signature
  verification, and an optional independent account-risk admission boundary: when a guard is
  configured it must reserve reconciled capital before any provider call, an attempt that produces
  no transaction releases its reservation, and a successful signature keeps it until an independent
  reconciliation releases it. `socket_key_provider.py` implements a bounded local Unix-socket digest
  adapter, and `execution/lp_signer_daemon.py` (started by `scripts/run_lp_signer_daemon.py`) is a
  bounded *reference* isolated signing authority for it: one owner-only 0700 directory with a 0600
  endpoint, one allowlisted account, one canonical 32-byte digest request per connection, a signature
  budget, a time to live, and an append-only audit of addresses, digests, signature hashes and fixed
  refusal reasons that never contains key material or echoes client input. It signs digests, never
  transactions, and a returned signature is not an execution permit. Its key comes from one
  owner-only 0600 file or one environment variable of that process; a local EOA key file is the least
  isolated configuration the same socket contract supports, and the daemon has no restart supervision
  or OS-level confinement beyond file ownership and socket permissions. The operator CLI can now wire
  both that socket authority and the independent admission boundary, so a request that clears the
  static policy, the fee caps, the nonce journal and the live reconciled-account reservation does
  produce a signature; with either authority absent it still denies, and the default path loads
  neither. A configured authority additionally requires the canary precondition: `--canary-window`
  must name a retained shadow window for the same account, manifest and policy whose requirements are
  at least `CANARY_WINDOW_REQUIREMENTS`, and `--canary-cap-usd` a cap `canary_admission()` admits, or
  the CLI refuses to start; a window without an authority to consume it is itself an error rather than
  a silently ignored input. The boundary is built from a single-chain `PortfolioManifest` by
  `execution/account_capture.py` and `execution/live_account_reader.py`, so the frame it reserves
  against is captured at the signing request's own block; a multi-chain manifest is refused because
  one block cannot pin two chains, a declared position must already exist and be owned at that block,
  and native gas spent by an earlier signed transaction must be declared explicitly or the next frame
  stays unresolved. The CLI still never broadcasts and a produced signature is not an execution
  permit. Default shadow children receive a minimal
  environment, which is not OS-level isolation from wallet files;
- `execution/lp_broadcast.py` is the only place that may put an already-signed transaction on chain:
  it decodes one EIP-1559 envelope, requires every field to equal the sidecar request that produced
  it, applies the operator's own chain/target/fee/value allowlists, pins the endpoint chain id and
  submits once, journaling request/transaction hashes, the recovered signer, outcomes and reasons in
  an append-only hash-linked SQLite ledger that never contains raw bytes, a signature or a key; an
  unknown result is journaled `unresolved` and never retried. `execution/lp_receipts.py` is the
  producer that closes the loop the ledger left open: `observe_receipt()` reads a journaled
  transaction's receipt from two independent providers at the block the receipt itself names (never an
  operator-supplied height), reconstructs the transaction's whole native cost through
  `farming/transaction_costs.py` (execution gas plus OP L1/operator or Nitro `gasUsedForL1` or
  Ethereum blob gas), and refuses a missing receipt, a provider that does not see that block, any
  disagreement on payer/status/cost, a payer that is not the reconciled account and a native asset
  whose decimals differ from the cost policy, retaining nothing until every check passes.
  `gas_declarations()` then emits the `--native` file the reconciliation observer consumes, summing
  per manifest account and never deriving non-gas native movement.
  `scripts/run_lp_receipts.py` (run as `python -m scripts.run_lp_receipts`) requires every named
  transaction to already appear in the operator's broadcast journal, closes each open broadcast from
  that same observed receipt, and writes `declarations.json`, cost `evidence.db` and a
  `report.json` that always carries `execution_eligible: false`; a refused receipt leaves only a
  redacted report. Nothing here signs, holds a key or schedules itself, the shadow loop does not
  consume its declarations, and an account that also sends native value still keeps an unexplained
  residual that refuses its next frame;
- `execution/lp_preflight.py` produces the other evidence kind the signer gate has always demanded and
  nothing wrote: `collect_lp_preflight_quorum()` observes one explicit head from two independent
  providers and pins every read to it by EIP-1898 block hash, issuing exactly `eth_chainId`, the
  opening header, one `eth_getCode` per role-bound runtime, the executor's own `lastNonce()`, the
  signing account's `eth_getTransactionCount` and a closing re-read of the same header. Each capture
  re-derives every field from its retained bytes and refuses a chain other than the policy chain, a
  header that moved before the close, live code that differs from its pin, a word that is not a
  canonical uint64 and an address that cannot be the signing identity; a quorum refuses a provider that
  disagrees on the head, either nonce or the pins. `persist_lp_preflight_evidence()` retains both
  providers as `kind = "execution-preflight"` records whose payload, interpretation and content hash are
  the same bytes `authorize_lp_signing` re-derives, and `replay_lp_preflight_evidence()` rebuilds one
  from its retained envelopes alone, refusing a payload its own bytes contradict. It holds no key and
  cannot sign or broadcast and it does not reserve an account nonce;
- `execution/lp_risk_decision.py` supplies the `kind = "risk-decision"` evidence the frozen gate always
  demanded and nothing wrote: `decide_lp_risk()` accepts only an `EvidenceReplayAccountRiskReader`,
  refuses calldata that does not hash to the intent, a gas envelope other than the intent's own
  `max_gas_units`, an untimestamped or foreign simulation block and fewer than two distinct simulation
  providers, prices by reusing that reader's own `reconcile_and_price` under a
  `current_block = simulation_block` envelope with zeroed nonces and no evidence (so a second valuation
  can never disagree with the ceiling the signer guard reserves against), and refuses to build a
  decision whose `approved` contradicts its reasons, which are only an unresolved operation in the
  reconciled account and a worst case above the capital ceiling. `persist_lp_risk_decision()` retains
  exactly one record at the simulation block through one payload definition shared with
  `replay_lp_risk_decision()`, so the bytes a producer retains are the ones the gate re-derives.
  `execution/lp_signing_request.py` then assembles a request: `parse_prepared_simulation()` rebuilds one
  plan and its two simulation references from a report's exact bytes (refusing a report that still lists
  `pinned_simulation` as unresolved, another schema, a non-simulation plan, a nonzero value, a mismatched
  intent hash or calldata and evidence that is not two distinct sources at the plan block), and
  `assemble_lp_signing_request()` re-reads every submitted reference from the store (the gate matches by
  hash alone, so an unretained reference could otherwise be smuggled in), requires the retained
  simulation payload to be byte-for-byte the envelope this plan's own simulation must consist of,
  requires the preflight providers to be exactly the manifest's declared pair observing the policy's own
  account, executor and runtime pins, requires the manifest to describe exactly one chain and the plan's
  account, and requires the intent's pool and token id to be a position that manifest declares. It
  returns the sidecar request with `authorize_lp_signing`'s own verdict, holds no key and cannot sign,
  reserve a nonce or broadcast. `scripts/assemble_lp_signing_request.py` is the operator command: it
  keeps the gas envelope out of the operator's hands (the fee caps are the sidecar config's own and the
  gas limit is the plan's own intent), re-captures the manifest's two-provider account proofs at the
  intent's own state block, prices the decision, collects a fresh two-provider preflight at a later head
  and writes `--out` exclusively only when the gate authorizes — a refusal exits 3 and writes no file, so
  an unauthorized request never becomes a path a signer could be pointed at. No request has yet been
  assembled against a funded live account, so the accepted path is exercised by synthetic retained proofs
  only, `run_lp_signer_sidecar.py` still needs an operator-supplied request file plus an admitted shadow
  window, and a retained request is not an execution permit;
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
  `execution/account_capture.py` extracts the two-provider acquisition path into a library
  (a fresh one-shot session pair per declared provider, one block-pinned observation, an inception
  baseline that refuses a native declaration) and `execution/live_account_reader.py`
  (`CaptureAccountRiskReader`) is the `AccountRiskReader` a signing request needs at admission time:
  it captures the manifest's declared proofs at the request's own block, replays them through the
  evidence-bound reader and prices the worst case, refusing a multi-chain manifest, a capture at
  another height or hash, and native declarations outside the manifest.
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
  capture, refusing a schedule its own window requirements cannot satisfy and stopping as soon as an
  accepted run lands beyond the maximum gap, and never opening the risk journal, holding a key,
  signing or broadcasting;
  `scripts/check_canary_admission.py` re-derives a retained window byte for byte against the manifest
  it claims to describe and prints the admission or refusal reasons, always with
  `execution_eligible: false`. Live runs of that loop against the two public Base providers the
  example manifest declares have completed accepted windows, including three runs over 270s (blocks
  `51760279`/`51760358`/`51760414`, seven attempts because the public providers dropped two attempts
  of each of the first two runs), four runs over 676s (blocks `51817579`–`51817917`) and five runs
  over 924s (blocks `51822505`–`51822967`, cumulative loss 128188 USD micros, no unresolved
  operations, every gap under the 300s maximum), while longer attempts produced no window at all
  because one public provider dropped every attempt of a single run. The admission check re-derived
  immediately after that last window admitted exactly USD 200.000000 and refused USD 200.000001 as
  above the owner's USD 200 daily budget, and it refuses the same window once it is older than its
  maximum staleness, so a window supports only an admission derived in its own session; every window
  is retained only under `reports/evidence/` (not version controlled), their requirements are the
  repository minimum, they cover one native-only account that has never broadcast, and they are still
  too short and too narrow to satisfy a real canary, so they are a mechanism demonstration rather than
  canary acceptance. `configs/account-risk.base.example.json` is
  the only
  committed
  manifest: the
  owner-confirmed dedicated wallet on Base 8453, native-only, declaring no position, with an ETH/USD
  feed pin verified from two Base mainnet providers before it was written and re-parsed by a unit
  test so it cannot rot silently. The signer CLI only admits through flags an operator supplies — a
  reader and guard, plus an admitted shadow window once a signing authority is configured — so its
  default invocation loads no reader or guard and still denies; no reconciliation run or shadow window
  is archived in version control, no Robinhood or
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
