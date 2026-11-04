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

This work performs zero production RPC, original-volume access, key access,
configuration application, service starts, signatures or broadcasts. The USD
500/200/500 capital/loss caps and both 1800-second feed ceilings are unchanged.
Real USD/NAV, runtime fingerprints and migration, simulation, receipts and
unwind/restart acceptance remain required. The existing 197-request read-only
authorization question is still pending; this diagnostic creates no new scope.
