# Stopped-service recovery and NAV alignment — 2026-11-04

This follows [the prior alignment and candidate record](LIVE_ALIGNMENT_20261104.md).
The October 31 USD 500 canary release remains recorded. The work below does not
create a new trading, RPC spending or service-start authorization.

## Confirmed implementation

The feed-policy migration now revalidates the locked file and STOP identity after
locking and immediately before each halted commit. Replacing a lock, replacing
STOP or modifying STOP during proof review refuses. Its two service commits are
still separate, always HALTed; this change does not claim atomic migration.
Ten regression cases fail against the prior committed implementation and pass
with this change. The combined migration/startup suite passed 122 tests.

The new paired rearm is offline and keyless. Default invocation only reviews.
Applying requires an exact prior review hash, both original service locks and
STOP files, current strict-policy journals, original evidence and nonempty,
unexhausted budget ledgers. It only admits the explicitly supported observation
or feed-migration halt, and refuses unresolved signing, pending transactions,
flow acknowledgements, incomplete legacy gas or exhausted loss limits.

It validates all original journal snapshots across the legacy and current feed
epochs, replays each settlement's retained cash and whole native gas cost, and
requires exact coverage of the original settlement anchors and accumulated gas.
New two-provider receipts must match those original receipts at their canonical
settlement blocks. The opening and closing headers recheck one fixed fresh NAV
height; normal production head advancement does not invalidate a pinned snapshot.
Account nonce continuity, wallet units and NFT identity must also agree. A matching
balance alone cannot hide an unrecorded account transaction.

The two rearm entries commit in one SQLite ATTACH transaction over the original
DELETE-mode journals with FULL synchronous settings. Missing files and recovery
sidecars refuse; no database is initialized or journal mode converted. A failed
pre-commit check rolls both entries back. STOP, basis, losses and counters remain;
daily action counters reset only when the original integer risk day changes.
An incomplete JSON projection after a successful commit exits 3 and reports the
committed state instead of inviting a blind retry. SQLite recovery files must not
be deleted. These semantics depend on correct host locking, VFS and durable sync;
see [SQLite ATTACH](https://www.sqlite.org/lang_attach.html) and
[atomic commit](https://www.sqlite.org/atomiccommit.html).

The backend now records each complete NAV's actual feed ceilings and earliest
validity deadline from its retained proofs. Both API and dashboard refuse an
expired feed even when its block is still within 120 seconds. Missing metadata,
legacy valuation and a ceiling above 1800 seconds display as stale; a stricter
ceiling remains valid. This status is an observation, never execution permission.
It trusts runtime-produced metadata; the reporting API does not replay raw proof.

The optional OKX CLI price guard now accepts only 1–200 bps, matching the runtime.
Invalid bounds refuse before credentials, RPC, budget or signer access. The guard
remains a price cross-check, not full NAV, custody, swap routing or broadcast.
An API credential or unused request count does not establish free entitlement.

RPC reservation now supports an optional admission check after obtaining the
SQLite write lock and validating the full original budget history, and again
immediately before commit. Expiry or cancellation rolls the reservation back;
waiting on a lock cannot silently consume a request after the deadline. Existing
callers retain their original behavior. This does not interrupt blocked OS I/O
or replace the collector's separate process deadline.

## Bounded raw-evidence producer

`scripts.capture_live_recovery` defaults to an offline plan. Its online mode
requires an exact plan hash and an unexpired external zero-cost declaration
bound to the two independent HTTPS endpoint hashes, with authorization and
pricing review hashes. The declaration is not a provider billing verification.
The plan binds both original journal tips, configuration epochs, file identities
and budget history tips; consumption invalidates that plan even with a new output
directory. It accepts the actual legacy authority shape (stopped with no HALT)
so fresh acquisition can precede strict-feed migration without a circular gate.

For the retained one-NFT, five-settlement scope, the planned maximum is **197
requests**: 36 NAV and 13 receipt/header reads per provider per service, plus one
shared head read charged to the strategy primary. The service/source maxima are
50/49 and 49/49. Fixed-height capture, exact counts, full-provider agreement and
current risk/holdings continuity are required; this does not acquire simulation
or every execution runtime proof. The original per-service budgets are used
without copying, replacement or policy increases.

The CLI supervises a separate worker process group for at most 120 seconds,
preserves only its two query environment variables and minimal runtime environment,
and refuses late publication. HTTP redirects and retries are disabled. Cancellation
stops further calls; an already in-flight request may finish before group termination.
Each reserved attempt is fsynced to a redacted wire journal before HTTP. Fixed
failure categories and HTTP status integers preserve diagnostics without echoing
credentials. Partial raw evidence remains; no failed run publishes accepted
`references.json` or `fresh-nav.json`. A successful raw capture still requires the
separate offline full-history rearm review and never starts either service.

The 22 collector tests passed using synthetic wire responses and real temporary
SQLite files, including complete 197-request paths for both policy epochs, closed
read transactions before evidence writes, budget reuse refusal, HTTP diagnostics,
expiry and whole-process-group termination. No real provider was contacted. The
original proposal at `recovery-alignment/acquisition-plan.json` remains an earlier
design record, not an executed or newly authorized production plan. A real plan
must still be derived from the original stopped volumes immediately before any
separately authorized capture.

Collector summary: `recovery-alignment/collector-summary.json`, file SHA-256
`9ba5e94404edaf48d7903d6d78ea86d22e00e1fa4bbf61a1cccac54d105e2091`.
It binds the two current source files, each command's actual exit and its log.
This POSIX supervisor is a one-shot CLI boundary, not an embedded server worker.

## Full repository gate

`make live-quality` completed with exit 0 in an environment permitting local Unix
sockets: **4053 tests and 138 subtests passed**, mypy accepted 242 source files,
and eight frontend modules passed syntax checks. Python/frontend, secret scanning,
Bandit and expanded Compose checks each exited 0 without timeout or launch failure.
All four log hashes and the canonical report hash were independently re-derived:

```text
reports/review-20261104/recovery-alignment-quality-local-sockets/report.json
evidence_hash: 68c4ec5db8504a0b780dcd0caaa3c947e1b30018d38177a8758ba4aecd591900
```

The 20 modified/new Python and JavaScript files match their pre-gate SHA-256
snapshot in `recovery-alignment/quality-source-lock.json`. No source changed during
the run. Documentation edits are outside that source snapshot.

The first sandboxed gate is retained in `recovery-alignment-quality/`: 14 failed,
4039 passed and 138 subtests passed; `make` exited 2. Every failure originates in
the sandbox's refusal of local Unix socket bind/connect, including the two child
startup timeouts. A minimal temporary-socket diagnostic also failed with errno 1
inside that sandbox and passed in the permitted environment. No test or safety
check was skipped to get the second result. The successful run retained one
dependency deprecation warning and three fork-from-multithreaded-test-process
warnings; standalone CLI process-group enforcement passed, but this is not proof
that embedding its fork supervisor in a threaded server is supported.

## Evidence and limits

Focused evidence is retained under `reports/review-20261104/`:

- `recovery-alignment/migration-controls-first.log`: 122 passed, exit 0;
  SHA-256 `b0dcea3123e5936b7b6c6c8d04abb9baf725d913ef9599188be55540e771868c`.
- `recovery-alignment/migration-controls-baseline.log`: the ten new regressions
  against the prior source all failed as expected, exit 1;
  SHA-256 `8c9f62bb4e16197198e0cbc5af9e4eb93610153d4bb2c9a83027122b7be5da7a`.
- `paired-rearm/summary.json`: 190 focused tests passed; Ruff, format, mypy and
  Bandit checks passed. File SHA-256
  `9c0a16a34c37bf249156eb114d801e49f65987f484931246fefe810aabe679d4`.
- `ui-feed-deadline-summary.json`: 184 focused tests passed, with source type
  checks, format and eight frontend syntax checks; canonical evidence hash
  `f8675af518f0cc79be3f98a0e148be2d49affac4bf4b7995f8d1907e2ea271c3`.
- `okx-cli-range/summary.json`: 37 tests passed; file SHA-256
  `c7d288c9a5c91f17de0ddf86d49edb52e446be28b2f8f3590bc0d06747f73273`.
- `recovery-alignment/contract-check.json`: `make contract-check` exited 0,
  70 tests passed, then lint passed. Log SHA-256
  `a7fe91d151389d24f1956d5e886b75113dbff6860ef91a824338ab3685a38725`.
- `paired-rearm/budget-admission-summary.json`: 122 focused tests passed, including
  actual SQLite lock contention and rollback after INSERT; type, format, lint
  and Bandit checks passed. File SHA-256
  `4bd2043d2b3cfb1250a120e71e96314e8a1ace6319dc169659f0818e1f924e1d`.

The first rearm epoch test lacked its synthetic feed-pin fixture; the original
failure is retained and the fixture was corrected without relaxing production
pins. Initial format diagnostics and the seven failing-before NAV tests remain
in their original reports. Direct script mypy for the OKX CLI reports five
pre-existing errors also found in the unchanged baseline; this is not reported
as a passed script type check. The repository gate checks its configured source
scope separately. Synthetic tests establish mechanism behavior, not production
acceptance, power-loss recovery, permanent finality or profitability.

No RPC, migration, rearm, STOP removal, key access, signing, broadcast or service
start was performed by this continuation. The earlier original-volume audit
already proves five real settlements in both services, not an empty transaction
history. NFT 3183370 remains invested in that retained evidence. Fresh full USD/NAV,
current dual-source/runtime acceptance, full unwind and an operational restart
are unresolved. The existing eight paid requests and unresolved cumulative cost
remain unchanged; no prior one-shot free grant is renewed by this work.

## Committed candidate and original-volume history

Implementation commit `c845695c9a5d8e02d779e3b84f1cc839db87c75c` was pushed to
`main` over SSH with exit 0. Its runtime-only committed archive built candidate
`sha256:3717c40012f97f8ee7f175328fa14f491082c05fc466df8476c8cf1376dd68ff`
with exit 0 using the cached locked-dependency layer. The stopped-image source
verifier exited 0: all **347** application files matched, manifest hash
`3067abd147069323a3790d093904f01edd4719c3723beaf619f33bd1e084b9ad`.
A network-disabled, keyless, read-only smoke container exited 0, including the
new CLI imports and expected missing-original-state refusals (child exit 3).

The candidate then replayed both original service volumes read-only, with no
network, RPC environment, key or socket mount. The first auxiliary inspector
exited 3: it incorrectly required a standalone NAV record in authority, whose
supported terminal checkpoint is a settled mint. A narrowly scoped read-only
diagnostic confirmed no pending/signing uncertainty or legacy-gas flag; the
authority has complete retained action cash and position evidence, and no
standalone post-mint NAV references. The application's checkpoint/recovery
rules were not changed to satisfy this inspection.

A separate corrected inspector reused the committed collector's checkpoint
review, independently replayed authority's two-source mint cash and NFT position
proofs, then ran `replay_history` over every original journaled settlement. It
exited **0**. Both services agree on all **five** original receipts, wallet units,
account nonce **8**, NFT **3183370** and total native gas **3471950532706 wei**.
The agreed historical receipt hash is
`ca219b0937d5a4334d646fee01bb5c28cf341bc0e5fc9753d3b3c6dff19394ab`.
The strategy's older complete NAV remains historical; authority's action cash
checkpoint is explicitly reported with no complete NAV value. Neither proves
current prices or today's receipt canonicality.

All **13** original file hashes match before/after both inspections and the prior
retained-volume audit, including the active and retired request ledgers. No STOP
file was created or removed. The first inspector, exit-3 report and diagnostic
remain intact beside the corrected v2 output. The v2 evidence hash is
`e2601302d9d2c0f5c5514a88dea82893a108cd7375ef2680b01a34fa65c59d4c`.

Artifacts: `reports/review-20261104/runtime-c845695/`. Verified summary hash:
`399783b81343087174c5966a8a762b0565a541cfff6b4b6bda2304ee22e6e14b`.
Build, source verification, smoke, diagnostic and both inspection exits and their
retained log/report hashes were checked. Independent internal review also
re-derived the full quality report, source lock and candidate source comparison.
Both production containers remain exited on image `0b4f8cb42deb`; this candidate
was not deployed. Fresh strict-feed NAV, current dual-source/runtime evidence,
full unwind and operational restart remain unaccepted. Current provider free
entitlement and the new bounded read-only scope have been requested from the
owner; no RPC was sent while those requirements remain unresolved.

## Follow-up: refuse provider error and endpoint reflections before retention

Offline review reproduced a privacy defect in the recovery collector: an HTTP 200
JSON-RPC error, malformed JSON, mismatched response ID or ambiguous result/error
envelope was written to `wire.jsonl` before the RPC decoder rejected it. Provider
error text could therefore reflect a private RPC endpoint or credential into the
retained hex body despite the outer fixed refusal message. Four new regression
cases failed against the preceding implementation, as did three direct endpoint
reflection cases against the first fix. All stimuli used synthetic credentials;
no real endpoint or original budget was accessed.

The collector now validates a strict, unambiguous request/response envelope before
returning or retaining any response body. A rejected body contributes only its
SHA-256, size, fixed failure category and a validated integer RPC error code when
available. Provider messages and malformed bodies are not retained. A successful
envelope also refuses direct or percent-decoded reflections of its own full URL,
hostname, path components or query values before the recorder or evidence store
can receive it. Short routing components compare as whole fields; longer components
also match inside strings. This covers the tested reflection paths, not arbitrary
obfuscation by a hostile provider. Raw successful bytes keep their exact spacing
and encoding for deterministic evidence replay.

The refusal cancels further calls, preserves the spent original reservation and
does not publish accepted references. The 197-request/120-second limits, no-retry
policy, original-ledger requirement and release gates are unchanged. The expanded
collector suite passed **31 tests**, including both complete 197-request paths,
HTTP failure diagnostics, eight privacy refusal cases and exact-byte preservation.
Initial regression failures and format diagnostics remain under
`reports/review-20261104/recovery-response-privacy/`. The first full-gate attempt
stopped at a newly added test's formatting check; that log and exit 2 are retained.
Only the corrected final gate can establish the current repository result.

The final `make live-quality` exited **0**: **4062 tests, 138 subtests** and eight
frontend modules passed; type checks, secret scanning, Bandit and Compose checks
all exited 0 without timeout or launch failure. All referenced log hashes and the
two-file source lock were re-derived after completion. The four known dependency/
fork warnings remain warnings; no check was skipped.

```text
reports/review-20261104/recovery-response-privacy-quality-locked/report.json
evidence_hash: 1bbe13b071b5a648c35ab978d03470e628246c8f3178016b98d85064c1da7d82
reports/review-20261104/recovery-response-privacy/summary.json
evidence_hash: c925af9fbeca2c38461c90442e3d3f1a7833bf18f08673defa22a1b1fe5d7438
```

This follow-up made zero RPC requests and accessed no original volume, budget or
key. The preceding real historical replay remains a historical result. Current
free entitlement, the new one-shot read scope and fresh production acceptance
remain unresolved; this fix grants no execution or spending authority.

Implementation `42bf5bced7fb92b3f8526bf52546898a49f7cc67` was committed and
SSH-pushed to `main` with exit 0. Its committed runtime-only archive built image
`sha256:4b87e10c1195cc6f77f73eef49abf55a4a679b3985f9e070812f842d5181b2f9`
with the cached locked dependencies. Build and stopped-image source verification
both exited 0: **347** application files matched, manifest hash
`c2d90b18abc69a3bf17a567ed90f1d60bf595bbdd3722d918ee11f6db3f0ca5b`.
The network-disabled, keyless, unprivileged smoke container also exited 0 after
checking exact successful bytes, RPC-error rejection, endpoint-reflection refusal,
strict feed/capital limits and dependency consistency. No original volume was
mounted. The two production containers remain exited on image `0b4f8cb42deb`.

All command exits and referenced log hashes were checked. Candidate artifacts:
`reports/review-20261104/runtime-42bf5bc/`, summary evidence hash
`769906fd7bc1124174acd8b7a75ebc99271b7ad698ff8738ce8a78c08871290c`.
This replaces the older image as the reviewed privacy-fix candidate; it was not
deployed and adds no fresh production acceptance evidence.

## Follow-up: worker deadline survives supervisor termination

Two offline subprocess regressions reproduced a gap in the hard deadline: after
SIGKILL terminated the CLI supervisor, its blocked worker could continue beyond
the authorized window. The second case inherited an ignored and blocked SIGALRM.
Both cases failed against the preceding implementation by observing a late marker
written after supervisor death. Their original exit-1 log remains at
`reports/review-20261104/recovery-worker-deadline/before.log`.

Before constructing any network capability, the forked worker now arms its own
POSIX real-time timer for only the unused part of the supervisor's original
window. It restores SIGALRM's default terminating action and unblocks that signal,
so an inherited handler or mask cannot disable this deadline. Kernel termination
does not depend on a Python handler running after blocked DNS or HTTP returns.
The live supervisor still terminates the worker's process group and publishes
references only after a timely successful exit. The production capture worker
creates no descendant processes; arbitrary caller-created descendants are outside this
orphan-worker safeguard.

The expanded collector suite passed **33 tests**, including forced supervisor
death under both signal states and the existing full captures, cancellation,
privacy and publication checks. No request quota was increased or refunded.
The production 120-second ceiling and its earlier feed/head deadlines remain;
SIGALRM termination is an unsuccessful, possibly partial attempt, never a permit
to repeat a request or open a release gate. This follow-up uses synthetic local
processes only and has made no RPC request or original-state/budget change.

The full `make live-quality` gate exited **0**: **4064 tests, 138 subtests** and
eight frontend modules passed. Type, secret, Bandit and Compose checks all exited
0 with no launch failure or timeout. Their log hashes, report hash and unchanged
two-file source lock were re-derived after completion; the four existing warnings
remain recorded. No contract source changed, and no production acceptance was
inferred from these tests.

```text
reports/review-20261104/recovery-worker-deadline-quality/report.json
evidence_hash: 7237741212790bd4ad5d1ae9be49359f3cae6d3f9904610cc6f6c30a5ddc2107
reports/review-20261104/recovery-worker-deadline/summary.json
evidence_hash: 3248ea1686f670a3c07fd3cd004789520e4283b2d2a21c75910f9cbe85650f35
```

Implementation `8de2e9bc97893763d784e889ce8e77853fb22945` was committed and
SSH-pushed to `main` with exit 0. The committed runtime-only archive built image
`sha256:86db8ac9e24ab26185f8b99e9623bb061b5edd632d028b210c9c743a110e890a`
with the cached locked dependency layer. Build and source verification exited 0;
all **347** application files matched, manifest hash
`42986c2eeb76d57a73ad9ee97ffa3f718725ce27968a4fec8928ab47f431e0fb`.

A network-disabled Linux container, UID 10001 with no capabilities, no key or
original-state mounts and only a temporary tmpfs, executed the PID-1 test harness
`recovery-worker-deadline/linux_orphan_smoke.py`. The harness killed its separate
test supervisor and reaped the orphan worker itself. In both normal and inherited
ignored/blocked-alarm cases, the worker exited by **SIGALRM (-14)** rather than
writing its late marker. The smoke command exited 0. This tests candidate runtime
failure handling; it is not a fresh chain observation or real-account acceptance.

Artifacts: `reports/review-20261104/runtime-8de2e9b/`. Summary evidence hash:
`93561fcef1bb2fbb79308728ae0b3f4b9dc4ef1657aec5c1b3a48429e64ed2ed`.
Every referenced exit and log/report hash was checked. The candidate was not
deployed; both production containers remain exited on image `0b4f8cb42deb`.
Free entitlement and the one-shot acquisition authorization remain unresolved;
there was no external RPC, original-volume, key or budget access in this follow-up.

## Follow-up: strict single-attempt live POST boundary

Offline tests exercised the real urllib redirect/error processors with in-memory
HTTPS responses. The direct live RPC path followed HTTP 301/302/303 redirects;
urllib changes those POSTs to GETs at the Location endpoint. It also accepted
unmatched/noncanonical RPC IDs, ambiguous or missing result fields and arbitrary
send acknowledgments, and could expose a provider rejection message. A malformed
response after submission could escape as a preflight-style error or enter receipt
polling instead of immediately returning to pending reconciliation.

The direct transport now forbids redirects, caps response reads at 1,000,000 bytes
and reuses the strict raw request/response validator. A send acknowledgment must
equal the locally computed hash of the exact submitted bytes. Any rejection,
transport failure, malformed reply or mismatched acknowledgment after the send
attempt is `SubmissionUnknown`, with fixed redacted diagnostics. The commitment
still publishes before the single POST; the caller retains it for independent
pending reconciliation rather than resending or immediately probing nonce/receipt
from inside the failed submission path. Normal receipt waiting after a matching
acknowledgment remains unchanged.

The same adapter now requires canonical hex quantities and distinguishes a
malformed receipt from an explicit null receipt. Legitimate zero balances and
null receipts keep their separate meanings. This rejects malformed inputs without
echoing a returned value into parser errors. The production read adapter continues
to use its existing frozen budget and zero-retry observation transport; this change
does not add budget accounting to the separate broadcast operation or authorize it.

Initial tests retained `17 failed, 4 passed`; after refining the post-attempt
uncertainty contract they retained `21 failed, 1 passed`. The additional typed-read
regressions retained `11 failed, 1 passed`. All original logs remain under
`reports/review-20261104/live-post-boundary/`. The final focused run passed **178
tests**, including all **34** new HTTP/protocol/typed-read cases and existing
authority, nonce, budget, pending-state and LP regressions. Scoped lint, format and
mypy checks exited 0. The HTTPS handler never opens a socket; signatures used by
the commitment tests are ephemeral synthetic fixtures, never the dedicated key.

This is implementation verification. No external RPC, original state volume,
budget or canary key was accessed. Fresh complete USD/NAV, current two-source
runtime/simulation evidence, full unwind and operational restart remain unresolved;
the outstanding free-entitlement and one-shot read authorization are not renewed.

The full `make live-quality` exited **0** with **4098 tests, 138 subtests** and
eight frontend modules passed. All four subprocesses (Python/frontend, secrets,
Bandit, Compose) exited 0 without timeout or launch failure. Report/log hashes and
the unchanged two-file source lock were re-derived after completion. The existing
four warnings remain recorded; no check was skipped and no contract source changed.

```text
reports/review-20261104/live-post-boundary-quality/report.json
evidence_hash: b7eec1f56c149692fd59b5079ea9db30fd22625223e20d6ee07573d42ffafb5d
reports/review-20261104/live-post-boundary/summary.json
evidence_hash: e7d89ef7a9b7f9f6c38f910e962e78da71ab1355e734f5d9339240ac5329e36c
```

Implementation was committed locally as
`b19278649ba68ad8bb0703ec8ee98a2dc499c968`. Its runtime-only committed archive
built image `sha256:2b14d7c80598de9d9fa9ff4d8286ac464cccf726e39114aad663202dfea9f2b6`
with cached locked dependencies. Build and stopped-image source verification
exited 0: **347** application files matched, manifest hash
`9e2ebe10f2e772a1c27dce959ff63a5e8271425cf8b4a595f3fddc927ca384f1`.
The unprivileged, network-disabled Linux smoke container exercised actual urllib
processing over **15 in-memory HTTP requests**, using invalid non-transaction
bytes. It passed redirects, malformed/rejected responses, hash acknowledgment and
typed-read checks with exit 0. No valid transaction, key or original volume was
involved. Candidate summary:
`reports/review-20261104/runtime-b192786/summary.json`, evidence hash
`a002652f634c0624583f96dd080be84020ffeb0aefab945d00c4a89d68677e07`.

**SSH publication is unresolved.** The first push exited 128 with a public-key
refusal while the agent had no identities. A per-command existing Git identity
authenticated, but GitHub rejected that push with Internal Server Error (exit 1).
One bounded retry then timed out connecting on port 22 (exit 128); a read-only
remote-head check using the existing port-443 configuration also timed out (exit
128). All four original logs and exit records remain in `live-post-boundary/`.
No SSH configuration or agent state was changed. The remote head has not been
newly verified; the last locally tracked `origin/main` is
`15a8ed9bf2ab0b6c50345b213bc0afdcd77a1b38`. A later continuation must recheck
remote state before completing the authorized SSH push; no force push is allowed.

Both production containers remain exited on image `0b4f8cb42deb`. This candidate
has not been deployed. Current free entitlement, the one-shot acquisition scope,
fresh complete NAV and production runtime/unwind acceptance remain unresolved.
