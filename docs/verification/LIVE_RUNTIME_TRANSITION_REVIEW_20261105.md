# Read-only runtime upgrade preflight — 2026-11-05

`python -m scripts.review_live_runtime_transition` now joins the original two
service journals, fresh retained NAV and a retained gas-oracle quorum into one
offline preflight. It prints JSON only. It never creates a directory or output
file, opens a budget ledger, acquires RPC, changes configuration, appends an
epoch, removes STOP, clears HALT, starts a service, signs or broadcasts.

The target must be a plain `LiveConfig` document with all eight declared pins
and both feed ceilings exactly 1800 seconds. Each original journal must still
have the corresponding five-pin binding, optionally its exactly recognized
historical USDC 90000-second policy. Account, pool, original five pins, risk,
gas and all other policy fields must match by hash. Historical proof replay
uses that original policy; new NAV must use the strict target's own hash.
An already expanded original is not treated as a new upgrade. No historical
configuration or proof is relabeled.

Both private owned service directories must have their existing state,
journal, evidence, lock and STOP files. The review holds shared nonblocking
service locks, refuses aliases/hardlinks and recovery sidecars, and rechecks
file/directory identity, ownership, mode and change metadata. It validates each
hash-linked journal in read-only mode, repeats the final journal-tip check and
never initializes missing artifacts. The source journals remain bound to their
old policy even after a successful preflight.

The existing checkpoint verifier independently replays the original account
anchor, holdings/nonce continuity, strict fresh NAV and remaining loss budgets.
The two services must agree on current wallet/value, raw main NAV observations,
position identity, funded basis, external flows, native gas totals and retained
settlement anchors. Risk previews retain the original basis and external flows.
This does not replay the complete settlement history or establish an unwind.

The gas verifier now exposes its already-validated full agreed header alongside
the observed pins. Its fingerprint-only API still uses the same verifier and
returns the same dictionary; acquisition templates and payloads are unchanged.
Gas and both service NAV proofs must have identical complete headers, including
parent hash, at the same block. Three observed gas pins must equal the declared
target. An agreeing hash with a different parent refuses. The deadline is the
minimum of all input deadlines, and the CLI checks current time again before
printing. Declarations and reference hashes are checked again before success.

Exit 0 means this narrow preflight passed. `execution_eligible`,
`runtime_approved`, `migration_applied` and `state_write_supported` always remain
false. Missing, corrupt, changed, stale or foreign input exits 3 with a fixed
redacted refusal. A result is not a source-code approval, RPC grant, state-write
permission, migration/rearm permit, production runtime result or release.

## Operator inputs

The command requires `--strategy-dir`, `--authority-dir`, `--config`,
`--references` and `--gas-evidence`. The config is the proposed **plain config**,
not a `candidate.json` wrapper. The reference document has exactly three keys:
`schema: oasis.runtime-transition-inputs.v1`, `nav` containing exactly
`strategy` and `authority` reference pairs, and `gas` containing one shared
two-provider reference pair. Every reference is re-read from retained raw proof.
The original provider aliases are `live_primary` and `live_secondary`.

Current independently head-selecting acquisition commands do not guarantee
that the gas and NAV captures land on the same block. This review deliberately
refuses that mismatch; a coordinated pinned-block acquisition step is still
needed before real acceptance. No additional 13- or 197-request scope has been
consumed here. A history-preserving, paired five-to-eight epoch writer also
remains unimplemented. Existing startup, signing and journal-binding gates
continue to refuse an unapplied policy. The committed production config remains
five-pin, and the owner's USD 500/200/500 limits and 1800-second ceilings remain.

Subsequent implementation: the [joint acquisition
mode](JOINT_RUNTIME_RECOVERY_CAPTURE_20261105.md) now addresses that acquisition
coordination gap in code. Its separate 209-read scope has not been executed
against production. The original validation evidence below remains unchanged;
the paired runtime epoch writer and real acceptance are still outstanding.

Later implementation: the [paired epoch
command](PAIRED_RUNTIME_EPOCH_20261105.md) now supplies an explicitly approved,
halt-preserving writer. Its mechanism validation does not apply a production
epoch or establish real acceptance. This preflight still writes nothing.

## Validation

Twenty-two new tests cover both original feed epochs, five/eight-pin scope,
target pin changes, missing/foreign proof, stale publication, unsettled or
unhalted state, basis/gas disagreement, recovery sidecars, artifact aliasing,
full-header disagreement, changed originals and an active service lock.
Successful review is tested with state-save, budget initialization and RPC
methods forbidden. Every original synthetic file, including fixture budgets,
remains byte-identical. The related suite passed 108 tests, exit 0.

The initial lint exit 1 (export ordering and two unused imports) was corrected
without changing rules. A fixture-packet generator initially refused the host's
`/tmp` alias under the existing canonical private-path rule; its observed exit
is recorded, and its initial traceback remains in the tool transcript rather
than a retained stdout file. Two host harness failures remain in their original
logs: a missing project-root import path and a missing synthetic feed-pin
override. Neither was a product acceptance pass. The corrected host harness
exited 0 with explicit synthetic feed/clock substitutions.

The full gate at `runtime-transition-quality-v1/report.json` passed all four
checks with exit 0, no timeout or launch failure, and outer Make exit 0:
**4437 tests, 138 subtests**, 248 typed library source files and eight frontend
modules. Nine existing WebSockets/multithreaded-fork deprecation warnings remain.
Bandit found zero medium/high and 98 low findings. Secret scanning performed
no credential verification requests; the scanner baseline/rules are unchanged.
Compose checks used absent/synthetic RPC inputs with zero network calls.

Canonical quality evidence hash:
`c62c89c2d157fc9aec5fc9d0c20d3bf58aa4c5ec23e1960d4dbb6e95c417d8df`.
Independent exit/log/canonical-report and 625-file lock verification hash:
`c9bbd1747f4892c25bebe4c7ed205880a0692e6427a65368a6141dc2f727af06`.
This is internal self-review, not external audit or capital-moving approval.
No original production volume, canary key or repository `.env` was accessed.
Production RPC, signatures, broadcasts and production state changes were zero.

## Committed image verification

Implementation commit `2c6af6eeedad16d4aaa01225034fa87e3dbd2cc5` was archived
as the exclusive Docker build context, using `--pull=false` and the cached
hash-locked dependency layer. Build, stopped-image source verification, Linux
smoke and the enclosing builder each exited 0.

- Image: `sha256:009c9c58a8785ff255c016aac565e604bd7309f0daea48073672de4d2f9c3a64`.
- Source manifest: `0310e755f7c1bd7cf87689261ab7ca29f64407a7fad65998fe689064d1daeb2e`.
- `reports/review-20261105/runtime-2c6af6e/summary.json` evidence hash:
  `406a06cbf0794dda8223f843c9346c573b9fcb567cb2bd7ae5b625f7ea4a3f7c`.
- Independent reverification hash:
  `158b8f36b743b8bc845e0fe9e2885d076d7f5cd3c7256db853ad3e29ab74470e`.

All 356 application files matched committed and local bytes; all 358 archive
files, including the two Docker build files, matched the implementation commit.
Log, script, archive, canonical-report and manifest hashes were rechecked along
with all 625 locked inputs. Dependencies, base runtime and the local Docker
daemon remain trusted; this verifies application bytes only.

The UID 10001 smoke used no network, a read-only root, dropped capabilities,
`no-new-privileges`, a bounded PID count and scratch space. No source, production,
socket, key or budget volume was mounted. Two synthetic packets each contained
exactly eleven journal/proof/control files and omitted every budget ledger.
They covered strict and historical-USDC originals. With state save, epoch
transition, evidence put, budget initialization and RPC request methods forbidden,
both read-only reviews and actual CLI calls passed (CLI exit 0); stale and
missing proofs refused, and original synthetic bytes remained unchanged.

Only the disposable verifier process overrides its synthetic feed pin and clock.
Those substitutions are explicit in the output. The packet SHA-256 is
`415e658e9bb6b8660613947005e9f61bb7a3d1000f3664c36711cdcafed15018`.
This is keyless image validation, not real NAV, runtime approval, migration,
deployment or live acceptance. All execution/approval/write flags remain false.
