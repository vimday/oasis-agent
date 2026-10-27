# Guarded Docker startup entry — 2026-10-25

`make live-docker-readiness` exposes the existing offline release decision, and
`make live-docker` provides one guarded startup attempt for the owner's selected
local Docker deployment. The current release still refuses before Docker, RPC
environment reads, output creation or state access. The direct CLI was executed
with `--start` and exited **3**, without creating its requested attempt directory;
its refusal is retained in
`reports/review-20261025/docker-start-development/closed-release.json`.

After a separately completed production release, the entry verifies the local
Unix-socket context, rejects Docker environment overrides, validates the expanded
deployment policy and runtime application bytes, and requires existing local
state/socket/key volumes without driver options. It refuses existing project
containers rather than implicitly restarting/recreating them. It constructs only
the authority and strategy services with the verified immutable image ID, fixed
commands, confinement and private volume layout. No build, pull, setup profile,
budget initialization, key import or arbitrary Compose override is available.

Only the declared RPC variables reach those services. The generated document goes
to Compose over stdin; endpoints and arbitrary Docker output are never retained
in reports. Immediately before startup, release/source/config checks run again.
An exclusive, flushed and fsynced attempt file precedes the single bounded Compose
command. Timeout or a nonzero exit leaves the attempt unresolved and is never
automatically retried. A zero command exit is not service health, receipt recovery
or trading approval: `services_ready` and `execution_eligible` remain false in the
launcher report. Service journals remain the execution authority and recovery truth.

The Docker daemon, host, local context configuration and filesystem remain trusted;
this launcher is not an OS isolation boundary or proof of external data quality.
Unverified fresh prices, provider independence and dedicated-account receipt/cash
acceptance still block production. The accepted startup path has been tested only
with synthetic Docker responses; no production service was started.

## Validation records

Sixteen focused tests passed, including release-first refusal, immutable images,
fixed service inventory, absent/remote volumes, remote Docker, changed application
sources, preexisting containers, endpoint redaction and one-attempt handling of
timeout/partial startup. A generated document with synthetic endpoints parsed
successfully through the real local Compose parser without daemon/service access:
`docker-start-development/compose-plan-validation.log` under the review directory.

The first full run passed Python/frontend and secrets, then stopped at Bandit
(exit 1): B108 interpreted the container-private `/tmp` tmpfs specification as a
host temporary-file path. A narrowly scoped B108 annotation documents why no host
temporary file is opened. The attempt file itself is exclusively created in the
private per-attempt directory. The initial failed report is preserved at
`reports/review-20261025/docker-start-quality/report.json`.

A second full-check attempt stopped at Ruff because the revised redacted error
message exceeded the line-length limit. The message was shortened, Ruff and format
passed, and a fresh full check was run; the failed attempt remains at
`reports/review-20261025/docker-start-quality-final/report.json`.

The final full quality run passed all four command groups with exit 0, no timeout
and no launch failure: **3504 tests, 138 subtests**, Python compile/Ruff/format/mypy,
eight frontend modules, secret scanning, Bandit (89 low, zero medium/high findings)
and expanded Compose verification. Each log hash and the canonical report hash was
independently checked. Report:
`reports/review-20261025/docker-start-quality-verified/report.json`; evidence hash
`4a4a541491d72a89098327cc03c5a9db032ac8952f107ccbbed5718751c28626`.

The runtime image build exited 0 using the cached locked dependency layer. Both
`oasis-live:local` and `oasis-live:docker-start-20261025` identify
`sha256:93b5fd5829277896c82edc2d422b5b0755a41d97e57e25db7b38c55872fb1394`.
A stopped-container inspection verified **329 tracked application files** against
the reviewed checkout, without starting any process or mounting a production volume.
Its `docker-start-development/image-sources.json` report under the review directory
has evidence hash `f12e1a95095d724a5fbc809fbfe6fa0093a9708eec5708727dc1e2cd846c2e92`
and manifest hash `bd037e254273773b8238472c272651a50bda0eb0d84b1608ba5c4d357be93117`.
This is source identity and development validation, not a new production acceptance.

## Synthetic report cleanup

Archived and removed ten loose files from these explicitly synthetic diagnostics:

- `reports/review-20261003/position-account-expenses/synthetic-only`
- `reports/review-20261004/lp-flow-classification/synthetic-cli-only`

Their retained assessment/CLI records identify synthetic-only inputs, no real keys,
no new RPC requests and no execution eligibility. Every archived member's SHA-256
was compared with the source, then the source hash was checked again before removal.
Tracked files, symlinks and key/budget/nonce/journal-named inputs were excluded.
The surrounding historical checkpoint manifests, scripts and failure logs remain
unchanged; recreating those old paths requires the verified archive members.

Original size **1666251 bytes**, archive **110738 bytes**, net reduction before the
small manifest **1555513 bytes**. Archive:
`reports/archives/synthetic-inputs-20261003-04.tar.gz`; SHA-256:
`4a69e5531c8ca7045158a2725b8f41ad66886198b50b2a81da26bcfd89d499e8`.
Per-file mappings are retained beside it as
`synthetic-inputs-20261003-04.tar.manifest.json`.

All real chain evidence and request budgets are preserved. This work made zero
RPC requests, did not read/reimport a production key, and did not sign or broadcast.
The old one-shot free current-state authorization was not reused.

## Signing-boundary release enforcement — 2026-10-26

The startup CLI already refused a closed release, but the core
`TransactionAuthority.sign()` method relied on its caller to perform that check.
A caller constructing the authority directly could therefore reach its admission
and signing flow without independently enforcing the project's release decision.
The authority now checks release before state/RPC access and again immediately
before its key-provider call, after observation, simulation and durable reservation.
Status lookup and the separate keyless recovery paths remain available.

Regression tests restore the real closed release gate after constructing only
synthetic fixtures. They verify that an initially closed gate prevents state/RPC
access and key calls, and that closing it after reservation still prevents key use
while preserving the reservation and allowing status inspection. Existing synthetic
admission tests explicitly stub release so they continue exercising later controls;
this does not add a production override. The focused suite passed 88 cases.

Two redundant files were removed only after byte-for-byte and SHA-256 comparison
with retained originals: the duplicate position-account-expenses check log and
the synthetic account-risk-gas-binding repeat report. Their paths, original
timestamps, hashes and retained replacements are recorded in
`reports/review-20261026/authority-release-cleanup.json`. This removes 189435 bytes
without discarding unique historical outcomes or chain evidence.

Unrelated dependency-manifest and lockfile edits in the shared checkout are excluded
from this increment. No runtime image is rebuilt from those unreviewed edits; the
earlier image/source verification above describes the earlier revision only and
does not cover this new authority check. Current source verification must still pass
before startup. This change is not fresh-price/NAV, production dual-source or
dedicated-account receipt/cash acceptance, and does not open release. No production
key access, RPC, signing, broadcasting or service startup is performed.

Full development validation in the shared checkout passed **3506 tests and 138
subtests**, with the existing websockets deprecation warning. All four command
groups exited 0 without timeout or launch failure: Python/frontend checks, secret
scanning, Bandit (89 low, zero medium/high findings), and offline Compose validation.
Every raw log hash and the canonical report hash was independently checked:
`reports/review-20261026/authority-release-quality/report.json`, hash
`428caba1f5baaf25287b66bf5a499de17d77fc74ff61455e67eb8ec2013ee0a1`.
This does not attest a clean locked-environment rebuild of the unrelated dependency
edits. After validation, 817 regenerable cache files were removed (30946146 bytes);
the per-file record is `reports/review-20261026/authority-release-cache-cleanup.json`.
Together with the two duplicate reports, this increment removes 31135581 bytes
before its small cleanup manifests. Earlier cleanup totals are not added again.

## Committed-revision image follow-up — 2026-10-26

The image rebuild deferred above is now complete **for committed revision
`56911bcbf56e8b86b49243e863ff65dc2b317677`**, without adopting the unrelated pending
dependency upgrades. An allowlisted `git archive` supplied only committed runtime
sources, the committed hashed dependency lock, two runtime configs and Docker build
files. No checkout environment file, key, ledger, report or untracked file entered
the build. The build exited 0; dependencies were installed with `--require-hashes`.

Both `oasis-live:committed-56911bc` and `oasis-live:local` now identify
`sha256:24b83a7903e0f8cf5f34fba8920a714aa6fc21a06e142da85e093f6285a61ea4`.
Stopped-container inspection matched all **329 application files** to a freshly
re-derived archive of that same commit. Manifest hash:
`6614ed30c910019fe1aa593703b9d6df7ffa5ae00d1ff05d5c53b6a9d0b94267`;
source-evidence hash:
`d33e419767e5ecfd7ba31caa51084c03ad6d5bc7af83c388533d7164add08c58`.
The temporary inspection container had no mounts and was never started.

A separate non-root, read-only, network-disabled, unmounted temporary container
passed `pip check` and verified that calling the actual authority signing method
with release closed raises its release refusal before any authority state or key
provider is constructed. Its process exited 0; the read-only filesystem produced
only a pip-cache-disabled warning. This is an offline runtime check, not a full
Linux regression suite, dependency vulnerability assessment or production-service
acceptance. No strategy, production key volume, RPC or broadcaster was used.

Build/source/offline-check exits and log hashes are retained in
`reports/review-20261026/committed-runtime/summary.json`, with input manifest,
verification scripts and raw logs in the same directory. The 3092480-byte temporary
build archive was removed after verification; it can be reproduced from Git and is
not counted as additional cleanup of pre-existing files. Eight empty cache
directories were also removed. Remaining real evidence, budgets and unique
diagnostics are preserved.

The launcher's working-tree source comparison is unchanged: the candidate image
matches committed requirements, while the checkout still contains uncommitted
requirements edits, so those differing bytes must be resolved through reviewed
work before that comparison can pass. No comparison is bypassed. Fresh NAV,
current production two-source proofs and dedicated-account receipt/cash recovery
remain unaccepted; all four release blockers remain closed.

## Strict-feed committed runtime — 2026-10-27

The candidate runtime was rebuilt from allowlisted Git archive inputs at
`1d22118bd0c0530b4f9d8814ed68c3dd3bdb3f74`, including the committed dependency
upgrade and restored 1800-second ceilings. No working-tree environment, keys,
reports, state or untracked files entered the build. The build exited 0 with
hash-checked dependency installation. Both `oasis-live:committed-1d22118` and
`oasis-live:local` now identify
`sha256:9e87656bf7b28adb4846cc9853e330c85d7e7f2c72aef91f0ba2279df75bf80b`.

The standard stopped-container source verifier exited 0 and matched all 329
application files to the current tracked runtime files; the comparison was also
checked against the committed input manifest. Manifest hash:
`b85dedebedb7bfbf8b5026f49117bb7267c66ecb76bf17056f88ca53596d62f1`.
This resolves the prior application-source mismatch, without bypassing the
launcher's checks or claiming production acceptance.

An unmounted, non-root, read-only, network-disabled temporary container exited 0
for `pip check`, the actual authority's closed-release rejection before state/key
construction, both configured 1800-second feed ceilings, and rejection of 1801
and 90000-second ceilings. A pip-cache-disabled warning is retained. No key volume,
strategy service, signing socket, RPC or broadcaster was used. No full Linux test
suite or dependency vulnerability audit was performed in this runtime check.

Raw build/offline logs, input hashes, source verification and summary are in
`reports/review-20261027/committed-runtime/`. Summary evidence hash:
`6e576e410c1d6c0146094f01bcf9b3f75ef8f3a269151dc7d21787d4613a82ff`.
The temporary build archive was removed after hash verification; Git reproduces
it. Previous images and all old evidence/budget ledgers remain available. The four
release blockers remain closed pending actual production evidence.
