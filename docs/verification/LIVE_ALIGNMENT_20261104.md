# Live boundary alignment — 2026-11-04

## Confirmed implementation and offline verification

The current change connects four previously inconsistent boundaries:

1. The historical USDC 90000-second journal policy can explicitly migrate to the
   owner-required 1800 seconds without deleting history or resetting funded basis.
   Both service locks, original journal tips, raw two-provider fresh NAV and risk
   continuity are required. Every committed migration remains HALTed. Partial
   two-journal application is recorded as unresolved and cannot restart trading.
2. Read-only precheck can replay original retained checkpoint proofs through a
   read-only evidence snapshot while writing new diagnostic captures only into
   its temporary store. Corrupt retained proof cannot be hidden by that overlay;
   a running strategy's exclusive lock refuses the diagnostic before RPC use.
3. Backend and dashboard both require a complete account observation within 120
   seconds. A newer tick-only range check no longer renews stale NAV status.
4. The optional OKX price guard requires an explicit free-only grant and its
   original persistent ledger before any HTTP request. It batches the two prices,
   bounds response/decimal inputs, rejects redirects and non-rational prices, and
   stops after failure. Grant loading also refuses symlinks and nonregular files
   without waiting on a FIFO. It supplies neither block-pinned NAV nor OKX execution.
   Provider free allowance remains an operator declaration requiring verification.

`make live-quality` exited 0. Its four subprocesses each exited 0 without launch
failure or timeout: Python/frontend, secret scanning, Bandit and Compose checks.
Python results: **3857 tests passed, 138 subtests passed**; mypy accepted 238 source
files; frontend syntax accepted eight modules. The report and every referenced
log hash were re-derived after completion:

```text
reports/review-20261104/live-alignment-quality-locked/report.json
evidence_hash: a0fb3df9c8cbf62f2670ecb29b32f9b6353882fc68a0cdf29789171cd6b3e877
```

`make contract-check` also exited 0: 70 contract tests passed, followed by
`forge lint`. Its explicit exit record is
`reports/review-20261104/feed-migration/contract-exit-verified.json`; the log SHA-256
is `d67bf02fc10dc6d94cba387b0986765b13bc3f40d090a60e6b4193eff6f8998e`.
Foundry warned that the sandbox could not persist its external signature cache;
the tests and lint completed successfully. The original earlier contract log is
retained; the repeated check captures an otherwise unavailable tool exit result.

Focused migration/precheck/CLI checks passed 91 tests. Independent internal review
covered state continuity, old-writer refusal, proof expiry under lock, projection
interruption, shared two-service scope and partial failure. This is engineering
review and synthetic regression evidence, not an external audit or production
acceptance. Original focused logs remain under
`reports/review-20261104/feed-migration/`.

## Production evidence boundary

A keyless, network-disabled Docker read replayed both original volume journals
and historical checkpoints. All 13 inspected files, including the original
budget ledgers, were byte-identical before and after. Both service reviews still
refuse with `fresh_strict_nav_required`. Neither service was migrated, started,
signed through or broadcast through during this work.

The retained review report has evidence hash
`332545f1bd0cd431581ee0324986cc4f1cadd488321241fd3bf76302c0b9353f`.
The original artifact is
`reports/review-20261104/feed-migration/retained-review.json`.
An explicit exit-recording repeat used the same read-only/no-network boundary and
again left all 13 files unchanged. It exited 3 as expected for missing fresh NAV,
with no stderr. Its report hash is
`1040e50964aabdd5994c29008fcfae398273d1d5fa08b57774c896901b30f235`,
under `retained-exit-verified.json`; `retained-exit-status.json` records the exit.

Current production limitations remain fresh complete USD/NAV under both
1800-second ceilings, current two-source acquisition/runtime acceptance, real
receipt recovery including full unwind, and a reviewed operational restart.
Historical receipt/checkpoint replay does not prove present NAV, availability,
profitability or unwind acceptance. Paid RPC requests made by this change: zero.
No existing request grant or budget was reset, copied or replaced.

The original production audit already records five real settlements in both
services: USDC approval, two Permit2 approvals, swap and mint. NFT 3183370 still
has liquidity. This supersedes the earlier statement that the dedicated account
had no real transactions; it does not establish a complete unwind or current
receipt finality. The existing October 31 USD 500 canary approval is recorded in
`reporting/live_release.py`; no new general trading approval is implied or needed
merely to recognize it. Current acquisition scope and verified service costs are
still required, and cannot be inferred from leftover request counts.

The OKX guard is optional for the native Base path and remains disabled until its
own query credentials and verified free entitlement are available. This change
does not implement OKX swap/broadcast, custody or payment capabilities.

## Corrections and retained failures

The failed `focused-pair.log` compared prices whose rounded NAV was unchanged;
the corrected case changes a priced USDC leg. The failed `focused-cli.log` reused
an old-policy reader after a successful migration; rejection is the intended
behavior, and the corrected assertion uses a new reader. Their original logs
are retained, followed by passing `focused-pair-fixed.log` and
`focused-cli-fixed.log`. Earlier intermediate lint and integration failures are
also retained; only the final verified reports above establish passing checks.

The October 17 release document is now explicitly labeled a historical snapshot.
The recorded October 31 owner approval remains distinct from today's closed
runtime gates. No prior failed or stale evidence has been promoted to acceptance.
Final internal review additionally requested persistence of the attempt directory's
parent before migration; that improvement was implemented before final validation.

## Committed Docker candidate

Implementation commit `30f13db80692265e39ca4bd15688c75dbd842bd5` was pushed to
`main` by SSH with exit 0. Its clean `git archive` runtime-only context produced
candidate image
`sha256:41355e93da5059ac7aae2709b0d136ce615827e544c86aab11a4c94dc41b6fd7`.
All **340** application files in the stopped image matched committed local bytes;
source verifier exit 0, manifest hash
`32eac214c96f6f0a98a1041c270f0324c184b089f0ae1a6abdd07bd978cdb008`.

A keyless, network-disabled, read-only smoke container exited 0. It checked UID
10001, no effective capabilities, no-new-privileges, absent production/key/socket
mounts, dependency consistency, the 1800/1800-second ceilings, USD 500/200/500
limits and the expected HALT CLI refusal (child exit 3). This does not exercise
the live signing path.

The same unmodified image then replayed the original two service volumes read-only,
without source overlays, key or network. The historical checks passed and all
13 files remained unchanged; missing fresh NAV correctly produced exit 3. Its
review hash is `fc0492cf6365f88d10c0fa7586744dbf92c6f7312d644b30aa3be50aa4c8e9c3`.
The production containers were still exited on image `0b4f8cb42deb`; the candidate
was not deployed. Artifacts are under `reports/review-20261104/runtime-30f13db/`.

The first build with `--network=none` exited 1 because its dependency-install layer
had no cache for that network mode. Its original log is retained. The second build
used the same context and hash-locked dependencies under the normal build mode;
the dependency layer was cached and the build exited 0. No paid RPC was called.
Installed dependencies, base image and Docker daemon remain trusted inputs; the
source comparison verifies application bytes, not their entire supply chain.

## Follow-up: preserve original runtime state before startup

The one-click launcher previously checked that its named Docker volumes existed,
while the service entry points could initialize missing state or request ledgers.
An existing volume with lost files could therefore become a new accounting or
request-budget baseline. Production Compose commands now require
`--require-existing-runtime`; that requirement persists through later `StateStore`
rebinding, evidence opens and every budgeted acquisition. Missing originals are
refused rather than created, and an existing evidence schema is checked without
automatic migration.

Before Compose starts either production service, the verified candidate runs
`scripts.inspect_live_startup` with no network, RPC environment, key or socket,
and only the two original state volumes mounted read-only. Both existing locks
are held shared; a service writer refuses, while concurrent read-only inspections
may coexist. Missing/empty/corrupt/shared files, a foreign configuration, STOP,
HALT, pending signing/transactions, unresolved accounting or exhausted budgets
refuse. Each production service repeats its check under its exclusive lock before
RPC or key access. Opening and locking a replaced lock file is also refused by
comparing the original path, locked descriptor and current path identities.

This validates retained prerequisites, not current NAV or available dollar spend.
Existing host/volume identity remains trusted. Manual research initialization
remains separate; the production flag cannot be silently ignored by reporting,
readiness, recovery or state-edit commands. A missing JSON projection requires
explicit journal recovery before production startup.

Two-provider acquisition now cooperatively cancels remaining calls after a worker
observes the other worker's failure. A request already past its cancellation check
may finish; both workers are joined and no partial quorum is accepted. The change
adds no retry and does not refund, recreate or expand any budget.

Internal focused checks passed 281 cases, followed by two additional lock-race
cases; the cancellation suite passed 16 cases. The initial focused failures were
test defects: two corrupt-evidence assertions omitted the production `ChainError`
type, and a lock-replacement injector recursively invoked its patched `os.open`.
Both were corrected without weakening the refusal behavior; failed and passing
logs remain under `reports/review-20261104/startup-preservation/`. Its reviewed
summary hash is `9c9ad9cdafdaa9af4966f16c98b80d7d93d2c3d80a42bef3c66de9b5689f092f`.

An additional remaining implementation gap is explicit authority rearming after
the strict-feed migration: migration deliberately sets/preserves HALT, and the
authority CLI currently has no evidence-constrained rearm operation. The existing
strategy clear-halt command is not a substitute for that authority operation.
This follow-up implements neither rearming nor automatic STOP/HALT removal.

The follow-up full `make live-quality` gate exited 0: **3940 tests and 138
subtests passed**, with mypy, eight frontend modules, secret scanning, Bandit and
Compose checks also successful. Each subprocess exit and retained log hash was
verified. Report: `reports/review-20261104/startup-preservation-quality/report.json`,
evidence hash `fab1b7446cc140fb47b95588e9a6f86ec56be8a9ec03d58d97a410f1bee2ef57`. The only source edit after this gate started
clarified a lock docstring (shared readers may coexist); its Ruff check also exited
0. No contract source changed; the earlier 70-contract-test result remains the
contract validation for this unchanged tree.
