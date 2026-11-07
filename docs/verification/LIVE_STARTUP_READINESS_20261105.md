# Shared local startup diagnostics — 2026-11-05

## Observed diagnostic gap

The default Docker readiness command previously returned exit 0 when the owner
release was open, even for the committed five-pin configuration that actual
`--start` refuses. Its documentation correctly limited that response to the
release decision; it did not bypass the real startup gate. The missing local
runtime diagnostic still made the default result less useful to an operator.
A controlled five-pin regression reproduced exit 0 before this change and failed
its expected refusal assertion. The original failing log is retained.

`reporting/live_startup.py` now supplies one bounded, redacted local declaration
used by the default CLI and the live-status API/dashboard. It reads only the
public local start configuration, never Docker, production state, STOP, keys,
RPC environment values or chain data. Missing, malformed, oversized, symlinked
and non-regular files refuse rather than becoming a valid policy. Parsing uses
the same `LiveConfig` validation and gas-pin predicate as execution. Neither
configuration nor owner release is written.

The declaration distinguishes the original five-pin policy from a full declared
eight-role policy. An open owner release remains accurately recorded and does
not override a missing runtime policy. A closed release refuses before reading
the local configuration. The existing one-shot acceptance-permit semantics are
preserved; this work creates or restores no permit.

CLI output retains the release fields and adds `startup`. Exit 3 now includes
missing gas pins and invalid/missing configuration, even with an open release.
Exit 0 means only `local_guards_passed: true`. The declaration always carries
`startup_prerequisites_passed: false`, `execution_eligible: false`,
`evidence_replayed: false` and `rpc_requests: 0`. Complete configured hashes are
not observed or approved deployed runtime evidence.

The `/api/v1/live/status` response includes the same `startup` schema, alongside
its unchanged local state declaration. The dashboard renders the policy hash
summary, 5/8 or 8/8 declared-role count and missing gas roles in a separate card.
It lists image identity, original service state/configuration migration, STOP,
fresh USD/NAV, dual-source runtime/simulation, real receipt/fund recovery and
record/configuration binding as unverified. These are not inferred from recent
ticks, recent state-file writes, unit tests or the owner's historical release.
The local configuration is not asserted to be the running container's policy.

Frontend validation rejects a forged success flag, incomplete/duplicate missing
roles, omitted live checks, inconsistent release/configuration shapes and
malformed hashes. Invalid data clears previous diagnostics and state numbers.
API ETags change when the local policy changes even if the state record does
not. No new endpoint, POST action, start/rearm button or network acquisition was
added. Backend additions are compatible with readers ignoring new fields; the
new frontend refuses older payloads lacking the required startup declaration.

## Read-only import boundary and validation scope

Parsing `LiveConfig` used to import the live RPC/signing module indirectly through
`v4lp`'s two-function calldata dependency. `v4lp` now uses an I/O-free ABI utility.
The original `live/chain.py` helpers and signing/broadcast implementation remain
byte-identical. A canonical ERC-20 vector compares the pure utility with those
unchanged public helpers; existing protocol encoding tests remain applicable.
An isolated fresh Python process forbids importing signing/broadcast modules and
still reads the status and local startup declaration successfully.

The first focused development run retained four failures because tests called a
committed five-pin fixture a complete eight-pin policy. Explicit synthetic gas
pins were added only to those test configurations; no production gate, pin or
configuration was changed. Initial lint findings were corrected and retained.
The corrected focused suite passed 158 tests, including actual Node presentation
logic, FastAPI response/ETag updates, malformed-data clearing, read-only methods,
closed-release ordering, legacy/full declared policy behavior and import isolation.

The first complete development gate passed Python/frontend checks (4363 tests,
138 subtests, 248 checked Python source files and eight frontend modules), then
failed secret scanning. The candidate was the deliberately invalid, non-hex
`synthetic-private-content` field value in the parser rejection/redaction test,
not a private key. Review added only its exact path/type/value fingerprint to the
existing non-secret baseline (`87a590cd7ae585256fb3b170d6182ab5fb2724dc`, the
scanner's SHA-1 fingerprint). Scanner rules, exclusions and failure behavior are
unchanged. The original failed gate remains at
`reports/review-20261105/startup-readiness-quality-v1/report.json`, evidence hash
`6d5fb808d41e5df6cd4172138f714ad35d87b88ca198a046c3c4875a890f0dd2`.
The reviewed standalone scan then exited 0; its log SHA-256 is
`053b0787a6717b1633093b62d5a99040a9bcab2b95f7d4f569bf2f64ee1f3295`.

The complete rerun at
`reports/review-20261105/startup-readiness-quality-v2/report.json` passed all four
planned checks with exit 0 and no timeout/launch failure. Its canonical evidence
hash is `3ec9704404c7ecbae4ee1e1865e2d38e30f65228feab2415664a80389ccadd5f`.
Python/frontend again passed 4363 tests, 138 subtests, 248 typed source files and
eight frontend modules, with nine existing deprecation warnings (WebSockets and
multithreaded `fork`). Bandit reported zero medium/high findings and 98 low
findings; the configured medium/high gate passed, not an assertion of no findings.
Secrets performed no credential verification requests. Compose verification was
offline and used absent/synthetic RPC inputs only.

`startup-readiness/quality-reverification.json` records independent verification
of each check's exit, log hash and canonical report hash, plus all 16 locked
code/test/configuration hashes. The enclosing shell-session result was unavailable
after context compaction; it is not represented as an observed exit 0. The
independent reverification itself exited 0. `start-path-review.json` also compares
ASTs for every non-`main` CLI function against the previous commit and confirms
the actual `start` implementation is unchanged. This is internal self-review,
not an external audit or a new operator release approval.

## Committed Docker candidate

Implementation commit `f4b972efa4511ac5bd943791648b3fa21bdf4432` was archived as
the exclusive build context and built with `--pull=false`; the hash-locked
dependency-install layer was cached. Build, stopped-image source verification
and the Linux smoke harness each exited 0, as did the enclosing builder.

- Image: `sha256:5ed0028b471a05c3ae0cf5dfa72daab361a435b0126bc395d2154c9cd947f2fe`.
- Application source manifest: `7416e66950fb2a1a9394c07a733f59650fe0239f85106f433e9ed4892f306518`.
- Candidate report: `reports/review-20261105/runtime-f4b972e/summary.json`,
  canonical evidence hash `98cabb05e0f8dcd1f981625e35e001790bcfb0f1cd5caf2db42cfade3fc8d55a`.
- Independent artifact reverification: `runtime-f4b972e/reverification.json`,
  hash `1e252c13950cfce881712270746eff69f9895824073e282582a4dd8627a219ce`.

All 355 application files were independently compared to both committed and
local bytes. All 357 archived files (application plus two Docker build files)
were compared to the implementation commit. Canonical report/manifest hashes,
script/archive hashes, all three log hashes and archive exit 0 were rechecked.
Source verification covers application bytes; installed dependencies, base image
and the local Docker daemon remain trusted inputs.

The smoke process ran as UID 10001 with no network, a read-only root, dropped
capabilities, `no-new-privileges`, a bounded PID count and temporary scratch
space. No source, production state, socket or key volume was mounted. It refused
execution-module imports and confirmed that CLI/API use the same local
declaration. The committed five-pin CLI returned the expected refusal code 3
without creating its output directory; this is an assertion inside the passing
smoke harness, not a successful real startup. A synthetic eight-pin policy in
scratch space passed only local guards, retaining all seven unverified live
prerequisites and false execution eligibility. The canonical ERC-20 calldata
vector also matched. This is a tested image candidate, not deployment or chain
acceptance. No production configuration, STOP, budget or service state changed.

This work performs zero production RPC, original-volume access, key access,
configuration application, service starts, signatures or broadcasts. The USD
500/200/500 capital/loss caps and both 1800-second feed ceilings are unchanged.
Real USD/NAV, runtime fingerprints and migration, simulation, receipts and
unwind/restart acceptance remain required. The existing 197-request read-only
authorization question is still pending; this diagnostic creates no new scope.

## Base startup serialization follow-up

The one-click launcher now acquires the daemon-wide startup guard before
replaying the two original state volumes. It checks for existing project
containers immediately after acquiring the guard and again after replay and
release/source checks, before writing the Compose attempt. A competing launcher
that cannot acquire the guard does not read the original state volumes. If
preflight refuses, the launcher removes only its own guard by container ID;
an uncertain Compose attempt still retains its guard. The guard serializes
this launcher; it does not lock out arbitrary Docker clients.

The initial regression run exited 1: the overlapping and later attempts both
ran original-state replay while the first held the guard (three reads instead
of one). The first repair run exited 1 because the test for a late project
container still keyed its arrival to the old ordering. The final tests cover
both a container present after guard acquisition and one appearing during
preflight; all 65 startup tests passed. Logs, in that order, are retained in
`reports/review-20261107/base-startup-guard-preflight/` with SHA-256 values
`2a061863f2a24f686eb3c9cbc1ad5cd340acca18e68cbf2539178e4ccc221cfa`,
`82331b15ec063738a7f7204994c32d36297da27309eeef980a201dc362ba01b5` and
`8c30356375e82013d43b812d222268e1bb631b9f29bc8870cfb527fdd5c3d5d8`.

The actual Base readiness command still exited 3 with
`gas_oracle_runtime_pins_missing`; current full NAV, dual-source production
simulation and receipt/fund recovery remain unverified by this change.
No production volume, key, RPC, or service was used.

Final `make live-quality` exited 0: 4,763 tests and 138 subtests passed;
Python/frontend, secret scan, Bandit and offline Compose each exited 0 and
all four log hashes matched. The 685 frozen inputs were unchanged. Quality
report hash: `c5324ca20a2d2925aa7e095b2dc35b60c40d4451415ffff75e506021fd1450db`.

## Base Compose policy input binding

The verifier now reads the Compose file once and sends the same bytes to
Docker Compose for both the missing and synthetic RPC environment cases. Its
report hashes those bytes. If the current file differs after either case has
run, the verifier refuses; the one-click launcher still compares the digest
again before starting. Relative paths resolve from the original Compose
project directory. This checks the deployment configuration, not a live
container or chain state.

An atomic file replacement between expansions reproduced a false pass in the
previous verifier. The regression exited 1 before the fix; the corrected
Compose and startup suites passed all 112 tests. The two logs are retained in
`reports/review-20261107/base-compose-snapshot/` with SHA-256 values
`10fca3473c04d43e734f37900d69582c0611b2a805aa633c40f1fa5006a8ac36` and
`533bc5ddf033a67a80f1693684157268ed5dc3998c0dd78372cd879461b1d1d3`.
No production service, volume, key, RPC or release policy changed.

Local Docker Compose produced identical expanded documents before and after
this change for both cases (all four parser invocations exited 0). The offline
Base readiness command still exited 3: `gas_oracle_runtime_pins_missing`.
Final `make live-quality` exited 0: 4,764 tests and 138 subtests passed;
Python/frontend, secret scan, Bandit and offline Compose each exited 0 and
all four log hashes matched. The 685 frozen inputs were unchanged. Quality
report hash: `1d9bddb5ebef6d9d696ea3f1d469df39329184221168983b8e51e5e2f6a54fb6`.

## Base local RPC profile readiness

The launcher already required a valid local RPC quota and pacing profile, but
its offline readiness command and status API could report a static pass
without one. With all eight runtime pins declared, a missing or invalid
profile now blocks both views. The dashboard shows which case was reported and
rejects any payload that claims a static pass while carrying that blocker. The
launcher and reporting read the same sibling profile using the same bounded
regular-file read and typed parser; neither opens a request ledger to do so.
Legacy configurations still report the missing gas-oracle pins first.

The pre-fix regression exited 1: all four missing, malformed, FIFO and
symlink cases incorrectly exited 0 from the readiness command. The initial
focused fix run exited 1 because a tightened import test blocked a transport
module already imported through the existing reporting package. The corrected
test forbids loading the budget module and still checks the existing signing
boundaries; all 90 focused tests passed. The initial full gate exited 2 at
lint; its output remains in `reports/review-20261107/base-rpc-profile-readiness-quality/`.
The regression and both focused logs are in `reports/review-20261107/base-rpc-profile-readiness/`.
Actual local readiness still exited 3 with `gas_oracle_runtime_pins_missing`.
No ledger, key, RPC endpoint or production service was accessed by these checks.

The next full gate also exited 2: the repository disallows future
annotations, so its code-standard test rejected the initial type annotation.
The failed report and test output are retained in
`reports/review-20261107/base-rpc-profile-readiness-final-quality/`. The
type-only change to `Self` and a quoted forward reference preserved the
import boundary. All 94 focused tests, including the code-standard suite,
passed afterward; their log is `focused-standards.log` in the first directory.

Final `make live-quality` exited 0: 4,768 tests and 138 subtests passed.
Python/frontend, secret scan, Bandit and offline Compose each exited 0 and
all four log hashes matched. The 685 frozen inputs were unchanged. Quality
report hash: `e481e578d40ab17ab891b54ae5aa75888df47a1896528a50552c73b5d48a79e2`.

## Base startup policy and image binding

The launcher now hashes the exact bytes it parses for both the trading
configuration and the RPC quota/pacing profile. Both hashes must match the
corresponding entries in the verified image's application manifest. A
difference refuses before RPC environment handling, volume inspection,
original-state replay, or Compose. The canonical configuration hash remains
the journal binding; the raw hashes are used only to establish which files
supplied the host checks. The final manifest, release and Compose checks
remain in place.

Two simulated replacements reproduced the old gap: the initial host policy
differed from the policy subsequently verified in the image and the launcher
nevertheless attempted Compose. The pre-fix regression exited 1. After the
fix, both cases refused at image verification and all 96 focused tests
passed. The logs are in `reports/review-20261107/base-start-policy-image/`,
with SHA-256 values
`996dd1e7253c150b65beafe51f43c9cc7a524d39c4666e0e4be9beb87b3860d1` and
`12be8c0a48a73ab90988cdea29a2b4dfe7a7f998cf74e37de5d949031a490b14`.
Actual Base readiness still exited 3 with
`gas_oracle_runtime_pins_missing`. No RPC request, production volume, key or
service was used.

Final `make live-quality` exited 0: 4,770 tests and 138 subtests passed.
Python/frontend, secret scan, Bandit and offline Compose each exited 0 and
all four log hashes matched. The 685 frozen inputs were unchanged. Quality
report hash: `82870ac096fb393f7cd973db5b0fd9c5645c12c3ab0376e04afc9e1fd2a66016`.
