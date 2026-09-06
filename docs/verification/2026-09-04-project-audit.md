# Phase 1 Project Audit and Next-Work Guidance

**Date:** 2026-09-04  
**Updated:** 2026-09-05  
**Scope:** current dirty working tree on `main` at `14ed921`  
**Decision:** `RESEARCH` only; no deploy, signer, broadcast, or capital authorization

The subsequent [evidence-binding review](2026-09-05-evidence-binding-review.md) records additional
replay/provenance defects, their regression fixes, and newer verification results. The counts below
are the earlier audit snapshot and do not attest concurrently changing fee-controller work.

## 1. Executive assessment

The repository now has a coherent Phase 1 doctrine, deterministic replay/accounting foundations, and
a minimal research-grade Solidity hook. The documentation correctly treats risk-adjusted excess
return, rather than fee APR, as the objective and separates Treasury, manual LP ownership, hook
governance, agent canary authority, and emergency recovery.

The implementation remains `RESEARCH`: it has not completed a real two-provider chain backfill,
position-scoped settlement reconciliation, pinned Robinhood fork lifecycle, policy signer, live
shadow runner, audit, or out-of-sample profitability proof. Local Python, canonical-core, and
official PositionManager tests authorize continued research only.

## 2. Document authority and reading order

Use this order when documents overlap:

1. `AGENTS.md` — mandatory engineering, security, financial, and verification invariants.
2. `docs/PROJECT_CHARTER.md` — mission, objective function, promotion gates, and Phase 1 definition of
   success.
3. `docs/strategies/ROBINHOOD_V4_AUTOLP_BLUEPRINT.md` — executable Phase 1 work-package sequence and
   economic research design.
4. `docs/security/CUSTODY_AND_AGENT_KEY_MODEL.md` and
   `docs/security/V4_HOOK_PRODUCTION_STANDARD.md` — authority boundaries and release blockers.
5. `docs/architecture/AUTONOMOUS_TRADING_SYSTEM.md` and
   `docs/governance/PRODUCTION_CONTROL_FRAMEWORK.md` — shared target architecture and enforceable
   production controls.
6. Other `docs/strategies/` playbooks — later strategy doctrine, not implemented capability.
7. `docs/runbooks/`, `docs/templates/`, and `docs/references/` — operational procedures, experiment
   records, and source rationale.
8. `docs/superpowers/` and dated `docs/verification/` records — historical design/evidence snapshots;
   they do not override newer normative documents.

`CLAUDE.md` is intentionally a thin agent entry point into this hierarchy. `.claude/rules/` adds
path-scoped constraints; it must not become an independent source of economic truth.

If two normative documents conflict, apply the safer and more restrictive rule, stop capital-moving
work, and record an explicit decision before changing either document.

## 3. Current code-change map

| Area | Current improvement | Maturity and remaining gap |
|---|---|---|
| Configuration | strict unknown-key, secret, boolean, finite-limit, placeholder-RPC, and ambiguous-chain-form rejection | Research ready; capital/gas units and production config schema remain undefined |
| RPC | chain-ID check, bounded retries, complete block snapshots, a bounded contiguous range collector, raw exchange capture, and provider-agreement model | No completed production-provider/archive backfill, adaptive batch policy, finality tracker, or live disagreement drill |
| Evidence | separate request/raw-response/interpretation bodies, composite provenance, canonical range store, transactional reorg replacement, and provider-quorum enforcement | No WORM/remote retention, signed manifest, complete live dataset, or operational restore/finality drill |
| EVM analysis | PUSH-aware opcode/address/selector scanning and bounded metadata extraction | Heuristic only; CBOR parsing and selector meaning are not canonical source verification |
| v4 primitives | protocol-valid bounds, canonical PoolId/fee math, Python vectors, and npm-locked canonical core/periphery Solidity dependencies | Release-source/artifact attestation and broader differential/fork vectors remain incomplete |
| v4 events | exact ABI validation and deterministic Initialize/Swap/ModifyLiquidity reduction over a reorg-aware canonical store | No donate/protocol-fee/transfer trace reducer, full tick crossing/fee-growth reconstruction, or fork reconciliation |
| LP accounting | block-pinned PositionManager reads, per-token ledgers, exact range principal math, structured PnL, and four-cell comparison identity | No complete real position settlement, fee-growth replay, valuation/markout evidence, IL/LVR attribution, or executable OOS four-cell run |
| Dynamic fee | timestamp-based stale/deviation/rate guards in Python plus a bounded publicly previewable Solidity band rule | Python research signals and Solidity rule are not one promoted model; fee elasticity, manipulation resistance, and OOS edge remain unproved |
| Arbitrage/risk | finite immutable models, explicit expiry/worst-case result, collision-resistant IDs, unresolved-evidence default, and basic risk blocking | Snapshot identity lacks block/evidence references; no allowlist, simulation, accounting-closure, reservation, portfolio, or signer enforcement |
| Execution | observe/approve modes fail closed on disabled mode, expiry, and denied risk | Guard stub only; a caller-supplied boolean is not an independent risk authorization |
| CLI/reporting | explicit fixture mode, non-broadcasting commands, truthful fixture labels, corrected dynamic-fee sentinel and tick spacing | `backtest`, `scan`, and `simulate` are labels/stubs, not engines |
| Solidity hook | one-pool binding, public swaps, owner-whitelisted NFT LP adds, removal callbacks disabled, token-specific size bands, 10% hard cap, guardian pause/revoke, CREATE2 flags | Real Robinhood fork, Permit2, malicious-token/reentry, symbolic/static-analysis, audit, routing, and economics remain open |
| Tooling | local Python/Solidity gate, full-SHA GitHub Actions, exact npm lock, 10,000-case fuzz and 512-run stateful invariants | Python dependency lock, coverage, secret/dependency scans, SBOM/artifact provenance, Slither, and fork gate remain open |

## 4. Defects closed in this audit

- The dynamic-fee direction test now asserts different effective fees, not merely different diagnostic
  inputs; unsigned volatility/adverse-selection/rebalance features reject negative values.
- Token settlement amounts now require Python integer base units. Integral `Decimal` values and booleans
  no longer pass as settlement quantities, and asset pairs require canonical token order.
- `PoolKey.tick_spacing` enforces the protocol range `1..32767`, separately from the ABI `int24` wire
  width; PoolKey currencies must be canonical.
- Initialize/Swap decoding enforces exact topic counts, canonical signed ABI extension, and `uint160`
  price width.
- Block, transaction, receipt, log, and evidence identities reject malformed hashes and inconsistent
  transaction/block relationships.
- Evidence retrieval re-hashes stored payloads. Legacy rows now receive a deterministic migration
  timestamp so their persisted key matches the reconstructable reference.
- Arbitrage snapshots require a positive expiry and explicit worst-case profit. Opportunity IDs hash
  the complete available snapshot instead of colliding on route text, and missing evidence defaults to
  `UNRESOLVED` rather than `CONFIRMED`.
- Configuration rejects simultaneous flat and nested chain definitions and rejects non-string RPC or
  PoolManager fields instead of coercing them.
- `analyze-hook` once again requires explicit fixture mode.
- The benchmark report no longer labels tick spacing `60` as a `0.006%` pool fee; it records fee field
  `0x800000` as the dynamic-fee sentinel and tick spacing separately.
- Pool-flow forensics excludes affiliated and unknown gross fees from beneficial-owner PnL, preserves
  negative external fee alpha, and requires claim-specific classification/amount/cost provenance.
- The Solidity fee band now selects token0/token1 thresholds using canonical exact-input/output
  semantics instead of comparing incompatible raw units; an unused `minimumFee` field was removed.
- The hook is permanently bound to its first PoolId, so a global LP whitelist cannot silently gain
  authority over additional pools. The official PositionManager first-mint timing and unrestricted
  post-revocation removal path are exercised locally.

## 5. Open findings and deployment blockers

### P1 — complete before trusting research output

1. Bind the Python signal research and deployed Solidity rule through one versioned experiment and
   quote-equivalence specification; do not imply the current amount bands implement volatility,
   inventory, toxicity, or fee elasticity.
2. Attest canonical v4 source commits, npm provenance, compiler settings, and release artifact hashes.
   Expand differential tests for event ABI, fee override, protocol fee, swap signs, rounding,
   exact-input/output, and 100% fee restrictions.
3. Run the bounded collector against two independent production providers plus archive access and
   retain the raw envelopes. Reconcile finality/reorg behavior before calling the range `CONFIRMED`.
4. Expand property/invariant campaigns to exact per-token conservation, fee growth, malformed external
   input, retry/idempotency, malicious tokens, reentry, duplicate evidence, and deep reorg behavior.
5. Make every fixture value traceable to a source record. The current default-fee array and heuristic
   bytecode interpretation must remain `INFERRED` until pinned-fork behavior or verified source proves
   their meaning.

### P1 — complete before any hook implementation is promoted

6. Produce the WP1 machine-readable identity dossier for PoolManager, hook, PositionManager, routers,
   quoters, tokens, pool key, code hashes, roles, whitelist state, and deployment blocks.
7. Reproduce authorized/unauthorized first mint, later add, NFT transfer, revocation, collect, and
   removal on a pinned Robinhood fork. The observed hook remains read-only and `permissioned` or
   `unresolved` until this passes.
8. Prove real executable routing and two-sided exit depth. A newly deployed hook/pool with no routed
   flow cannot earn excess fees regardless of fee sophistication.

### P2 — engineering and operational blockers

9. Protect the new mandatory CI checks and add Python dependency locking, coverage, secret scanning,
   dependency review, SBOM, artifact provenance, Slither, and pinned-fork jobs.
10. Replace the execution stub with typed proposal, simulation, risk-decision, intent, signer-policy,
    submission, unknown-state, finality, and reconciliation records. Never accept a bare caller-supplied
    `risk_allowed` boolean at a capital boundary.
11. Tighten local Claude tool grants. Broad `Bash(curl *)` and `Bash(python3 *)` permissions exceed the
    least-authority posture even though the local settings file is correctly ignored by Git.

## 6. Ordered next-work queue

### Step A — close WP0

- Unify the promoted fee-rule specification across replay, quote, and Hook implementation.
- Expand canonical v4 differential vectors and accounting properties.
- Execute the raw-envelope/provider-agreement path on a retained real range.
- Complete the remaining CI supply-chain and coverage gates.

Exit condition: all WP0 gate language in the Phase 1 blueprint is executable in tests; no fractional
base-unit, malformed ABI, route-ID collision, unpinned fact, or direction-erasing fee path remains.

### Step B — execute WP1 benchmark dossier

- Create the identity manifest schema and block-pinned collector.
- Backfill Initialize, liquidity, swap, whitelist, ownership, and bytecode/role history.
- Run two-provider comparison and pinned-fork LP lifecycle tests.
- Publish exactly one pool-access decision: `accessible`, `permissioned`, or `unresolved`.

Exit condition: only `accessible` permits an experiment in the observed pool. Otherwise use the
benchmark read-only and evaluate a separately deployed permissioned pool only after routing proof.

### Step C — build WP2 exact replay

- Reconstruct pool/position state and fee growth from events plus authoritative state checkpoints.
- Reconcile trader, PoolManager, LP, protocol, hook, ERC-20 transfers, gas, and balances per currency.
- Reproduce sampled transactions on a pinned fork in both directions and for liquidity lifecycle events.

Exit condition: no unexplained residual above explicit base-unit tolerance and no receipt/state mismatch.

Only after Steps A-C should WP3 economic experiments select a fee/range policy. The current Solidity
hook is a WP4 research artifact, not a release candidate. Executor and autonomous signing remain
later work, with human-custodied manual LP first and a separately funded, revocable agent canary only
after shadow gates pass.

## 7. Verification

Commands run on the audited working tree:

```text
make report
make check
git diff --check
```

Result:

```text
169 Python tests passed, 28 subtests passed
18 Solidity tests/invariants passed
20,000 fuzz cases and 65,536 invariant calls passed
Ruff: passed
Ruff format check: passed
mypy: 58 source files, passed
compileall: passed
Foundry strict build and lint warning gate: passed
git diff --check: passed
```

These results verify the current offline boundary behavior only. They are not chain evidence,
profitability evidence, a contract audit, or production approval.
