# Multi-chain automation: design before implementation

Date: 2026-09-11. Status: implementation in increments; no live release approval.

## Problem and intended behavior

LP already has evidence-bound decisions, bounded executor intents, a durable nonce journal and an
external-provider signer sidecar. These are not yet a continuously reconciled autonomous trader.
Launch has two different boundaries: retained-input diagnostics, and a historical Base monitor with
an in-process key path. The latter is not a reusable custody model. Broadening its chain literal
would silently reuse Base venue addresses, approval assumptions and ETH-denominated policies.

The intended platform runs independently configured LP and Launch campaigns per EVM chain. Each
campaign acquires a coherent snapshot, retains evidence, proposes bounded actions, obtains independent
risk authorization, simulates exact calldata, reserves account/nonce authority, and reconciles receipts.
Unknown submission outcome blocks new exposure; it never means a transaction was not sent.

## Ownership and identity

- Chain identity is explicit and positive. Token identity is `(chain_id, address)`, never symbol.
- Protocol deployments belong to reviewed per-chain adapters; router ABI versions, runtime hashes,
  gas accounting, finality and sequencer assumptions cannot inherit from another chain.
- Strategy parameters are versioned separately from deployment configuration and signing policy.
- LP and Launch share evidence and execution invariants, not financial position types or settlement.
- Each `(chain, account)` has a single reservation authority across strategies. No cross-chain bridge,
  automatic Treasury refill or shared private key is introduced.
- Services and strategies must not import the historical monitor's signing path. A later migration
  must replace it with an independently bounded external signer before general live availability.

## Execution sequence

`observe -> retained evidence -> deterministic proposal -> risk/reservation -> pinned simulation
-> external signer policy -> submission -> receipt reconciliation -> finality -> closed`

HOLD creates no transaction. An LP RECENTER uses the existing bounded executor ABI. REMOVE needs
verified ownership and settlement bounds; a HALT is not blanket permission to submit an unwind.
Launch requires both entry protection and realistic sellability/exit-liquidity evidence. Pending events
may wake discovery but cannot replace a canonical two-provider decision snapshot.

Reservation is durable before submission. Retries preserve economic intent and nonce. Timeout after
submission creates UNKNOWN/HALTED and requires receipt/account reconciliation. A successful receipt
alone is not token-level settlement proof. Every promotion remains chain/asset/strategy specific.

## Increment order and acceptance

1. **Explicit multi-chain research inputs.** Keep legacy Base target serialization and parameters;
   introduce an explicit v2 target policy with required chain, asset, supply and budget. Generalize
   retained snapshots, event identity and token reconnaissance. Reject cross-chain observations,
   bind FDV to the selected policy, retain target identity in raw evidence. No venue support claim
   follows from accepting an EVM chain ID. Native and hookless LP observations must be representable.
2. **Continuous acquisition and replay.** Per-chain acquisition workers, bounded backpressure,
   durable input cursors and atomic cycle/decision completion. Crash between health and decision
   writes must recover deterministically. Never checkpoint before evidence and decision are durable.
3. **LP proposal-to-intent adapter.** Bind retained decision, reconciled NFT/range, exact amount
   bounds, code hashes, nonce and deadline. Differential-test executor calldata. A research decision
   with `execution_eligible=False` cannot become an authorized intent by renaming fields.
4. **Launch per-chain venue adapters.** Start with a pinned canonical v3 deployment, then v4.
   Require independent adapter conformance, two-provider quotes, continuous buy/sell simulation,
   native/stable routing, token control checks and per-chain gas accounting. Base-only Aerodrome and
   Slipstream remain scoped to Base until another deployment is independently verified.
5. **Unified runtime and console.** Per-campaign discovery, input freshness, decision, simulation,
   intent, receipt, residual and halt reasons. Distinguish configured, supported and release-approved.
6. **Local fork then external-signer canary.** Adversarial and recovery tests, untouched economic
   results, sustained shadow acceptance and separately authorized deployment/broadcast.

## Performance and verification

Keep Python as the correctness reference. Measure acquisition, validation, decision, journal,
simulation, queue and reconciliation separately. Connection pooling and bounded concurrency cannot
replace independent provider agreement. Journal verified-head caching requires competing-writer,
payload-tamper and restart tests. Rust remains conditional on representative CPU profiles and parity.

Tests must cover identical addresses on different chains, wrong-chain RPC, mixed snapshots,
zero/max/boolean chain IDs, explicit v2 parameters, legacy policy stability, provider disagreement,
reorg, custom supply/FDV boundaries, native LP currency and hookless pool representation. Later
execution increments additionally require crash/nonce/unknown-submission and settlement properties.

No network endpoint or deployment address is inferred by this design. No new trading hypothesis,
threshold tuning, live configuration change, signing or broadcast is authorized by implementation.

## Implemented increment and use

The v2 research target, generalized snapshot/event chain identities, parameter-bound FDV checks,
chain-aware token reconnaissance and retained Launch API reads are implemented. v2 has no default
chain, asset, symbol, reference supply, FDV cap or principal budget. Load it using `parse_target`;
`Target` remains the historical Base policy. Supply and USD limits are positive integers; missing
risk/proof thresholds still block qualification. No non-Base venue adapter or live entry is enabled.

For reconnaissance, pass `--target /absolute/path/target.json` to `scripts/run_launch_recon.py` with
an explicit `--block`, new `--report` path and `--evidence` database. Set endpoint values only in
`OASIS_LAUNCH_RPC_PRIMARY` and `OASIS_LAUNCH_RPC_SECONDARY`; source aliases and target identity are
retained in evidence. Without `--target`, historical Base defaults remain. Replay retained observations
using `scripts/run_launch_shadow.py --target ... --input ... --journal ...`; each chain/target requires
its own journal. The selected journal is already readable through the Launch observation API.

LP `LpShadowService.consume_once` atomically retains health and decision, with the canonical full-cycle
hash inside the decision audit chain. It returns a typed result with `duplicate`, historical `record`,
and current `halted` status. A repeated input does not get a fresh timestamp or renewed eligibility.
There is no separate mutable checkpoint. `BEGIN IMMEDIATE` serializes competing connections; nested
journal operations use savepoints, and only the outer cycle commits. Runtime interruption rolls back
the entire in-flight cycle. Validation failure retains a HALT after rollback.

`run_lp_shadow.py` uses this path for both bounded files and optional continuous streams:

```sh
.venv/bin/python scripts/run_lp_shadow.py \
  --policy /absolute/path/policy.json --input /absolute/path/cycles.jsonl \
  --evidence /absolute/path/evidence.db --journal /absolute/path/new-shadow.db \
  --follow --cycles 100 --poll-ms 500 --idle-seconds 60 \
  --stop-file /absolute/path/shadow.stop
```

The producer must persist evidence before completing each newline-terminated input. Follow mode
waits for partial lines, rejects inode replacement/truncation, and halts on idle deadline. The stop
file ends consumption cleanly; it never clears an existing HALT. `--cycles` counts newly consumed
cycles, so historical duplicate inputs do not exhaust the budget after restart. No producer, capital
reservation or signing interface is created by following a file. Managed server campaigns continue
to use isolated bounded input copies; they do not silently become live followers.

Preserve old journals: code/config locks intentionally reject restarting them under changed code.
Start a separately reviewed journal for this implementation. New-format journals support idempotent
restart under the same code/config. Full chain validation and duplicate lookup still scan history;
this increment makes no throughput claim and does not implement verified-head caching. Same-inode,
same-size rewrites of the input file are unsupported; evidence/decision integrity remains separately
checked. Pool observation now permits native currency0 and a zero hook without claiming settlement,
routing or economic viability for such a pool.

Remaining work includes a two-provider live LP feature/reconciliation producer, exact proposal-to-intent
binding, per-chain Launch route/simulation adapters, cross-strategy account reservation ownership and
external-signer migration. Those are necessary before autonomous on-chain trading can be enabled.

A subsequent [LP execution-preparation increment](LP_EXECUTION_PREPARATION.md) adds two-source
position capture, explicit NFT/pool scope and deterministic simulation-only executor calldata.
It now includes exact two-source eth_call simulation with intent-derived, role-bound runtime pins and
executor dependency-getter checks; independent risk, settlement and live-release gates remain unresolved.

## Verification record

- New multi-chain tests first failed at the missing v2-policy import; atomic-cycle tests failed
  for the missing `consume_once`; the native/hookless regression failed on the zero-address guard.
- `make check RUNNER='.venv/bin/python -m coverage run'`: passed, 1,036 Python tests,
  108 subtests, 70 contract tests, no contract failures/skips, frontend syntax and all static gates.
- After adding isolation tests and the typed consumption result, `make python-check` with the same
  coverage runner passed: 1,038 tests, 108 subtests, 33.33 s; aggregate coverage 89%.
- Final restart/stream hardening: 23 focused tests passed; Ruff and formatting passed (261 files).
- `make security-check`: initial sandbox package-index access failed; network-enabled retry passed.
  Dependency audit found no known vulnerabilities; Bandit found no medium/high issues (44 low
  findings remain); secret gate passed.
- `make contract-security-check`: passed, 80 contracts / 102 Slither detectors / zero results.
- One existing websockets deprecation warning and existing Forge lint/cache warnings remain.
  Synthetic tests establish these software behaviors, not real-chain economic or execution results.
