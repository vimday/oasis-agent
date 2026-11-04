# Bounded gas-oracle discovery capture — 2026-11-05

## Confirmed implementation boundary

`scripts/capture_live_gas_oracle.py` supplies the acquisition step missing from
the offline gas-oracle discovery reviewer. Default invocation is offline and
reads no RPC environment values. The separate capture mode requires its own
exact approved plan hash and retained, unexpired zero-cost declaration. The
existing 197-request NAV/receipt plan cannot authorize this different scope.
Neither tool verifies provider billing or creates owner authorization.

The new plan first replays the original two stopped service checkpoints under
the existing recovery boundary. This also conservatively requires the full
recovery quota prerequisites, even though discovery spends less. Both original
locks and STOP files stay held throughout capture. The request cap is **13**:
one primary head lookup, then six pinned discovery reads per source, all charged
to the original strategy budget. The authority budget is read only. No ledger
is reset, copied or replaced. Production state, journals, evidence stores and
configuration are never written. Only the new attempt directory holds discovery
wire logs, proposals and evidence.

The shared HTTPS client retains its one-attempt behavior, response bound,
redirect refusal, error-body closure and endpoint-reflection filtering. The
head must be at most 120 seconds old. All state reads use that head's hash and
`requireCanonical: true`. Each source's implementation address is derived from
its own storage reply, never supplied as a caller override. Chain/header/code/
storage failures stop subsequent reads; the closing headers and both complete
raw captures must agree before accepted discovery evidence is retained.

The existing POSIX supervisor now accepts an explicit publication inventory and
parent validator while preserving the original NAV publisher as its default.
The 120-second limit still starts before fork, the child clears unrelated
environment values and carries its own kernel alarm, and the supervisor kills
the request process group before parent validation. A worker exit of zero is
insufficient: the parent independently replays the retained evidence, compares
it with the exact wire/proposal bytes, and rechecks current candidate expiry.
Only timely completion exports `gas-wire.json` and `candidate.json`. Failed or
late publication leaves raw/candidate evidence and a refusal report, removing
this attempt's incomplete publication files. Discovery never publishes NAV
references or claims that NAV, simulation or receipts have been accepted.

The supervisor now receives a private readiness byte only after the child has
installed its alarm, created its own session and reset SIGTERM, before any
acquisition. Before that confirmation it targets only its still-unreaped child
PID. After confirmation it targets that exclusive process group, including an
exited leader's descendants. A permission denial marks cleanup unresolved and
cannot publish success; it is never silently ignored as a successful stop.

The fingerprint proposal keeps `runtime_approved`, `configuration_applied`,
`state_write_supported` and `execution_eligible` false. Both feed ceilings remain
1800 seconds and the USD 500/200/500 capital/loss limits are unchanged.

## Unresolved production acceptance

No production request is authorized or made by this implementation work. The
pending 197-request free NAV/receipt question remains a separate scope; there
is no automatic retry, polling or gas-discovery extension. Real independent
provider fingerprints, source review/approval, a history-preserving five-to-eight
pin transition, fresh complete USD/NAV, runtime/simulation, receipt recovery and
safe unwind/restart acceptance remain required. A successful synthetic test is
not production evidence or a profitability result.

## Verification and retained failures

The focused suite passed **132** tests, including original recovery and offline
discovery replay. The new coverage adds 32 cases: distinct authorization scopes,
zero-spend refusals, exact request accounting, original-budget/STOP preservation,
derived implementation targets, disagreement, stale heads, malformed code/slots,
forged worker artifacts, exclusive publication, missing-original refusal, parent
replay, timeout cleanup and process-group readiness/permission handling.

The first full `make live-quality` exited 2: one new short-deadline test failed
with `PermissionError` at `os.killpg`, while 4325 tests and 138 subtests passed.
Later quality subprocesses were not run after that failure. A deterministic
delayed-`setsid` regression reproduced unsafe unconfirmed-group targeting and
failed before the readiness fix. This establishes that failure class; the
original host's permission mechanism was not independently traced. The original
logs and failed report are retained. No deadline was widened or test skipped.
Three lint findings across development iterations were corrected and retained.

After the fix, full `make live-quality` exited **0**: **4328 tests, 138 subtests**,
246 mypy source files and eight frontend modules passed. Python/frontend,
secrets, Bandit and Compose each exited 0, without timeout or launch failure.
Nine warnings remain: one dependency deprecation and eight fork warnings from
the multi-threaded full-suite test process. They were not suppressed. Both full
report hashes, every check log hash and seven locked source/config hashes were
independently verified after completion. All of this is offline development
validation; it does not accept a production gas-oracle fingerprint or release.

```text
reports/review-20261105/gas-capture-quality-v1/report.json
failed evidence_hash: 9026976881b6a45ac512e33efe18785e57ba68d36aa753df2944bac0db7757b4
reports/review-20261105/gas-capture/unready-regression-before.log
reports/review-20261105/gas-capture-quality-v2/report.json
passed evidence_hash: 4428650d3f6235b69d5e682521acfb2855357833254665d760969d55802c476c
```

## Immutable candidate and original-volume offline plan

Implementation commit `3033895681c53c8700fb73e36c45b0380b2ae563` built
candidate
`sha256:073437286336d840099d426f592193310d922714da17a1ee07c8619fda2cffcb`.
Build, stopped-image source verification and synthetic Linux smoke each exited
0, without timeout. All **353** application files matched both the committed
archive and local source bytes, with manifest hash
`97da50deaede8b26c25851948a8b0b8f492d1e8068d814934eb6bf776094188d`.
All archive/script/log/report hashes were independently re-derived. This checks
application bytes; dependencies, base runtime and the Docker daemon remain
trusted. The smoke exercised process-group readiness, unready-child refusal and
the separate discovery publication inventory. Its artifact bytes were explicitly
synthetic, and it did not simulate or accept a production gas-oracle quorum.
It ran unprivileged with network disabled, a read-only root, dropped capabilities
and temporary tmpfs, without production/source/key/socket mounts, making zero
RPC requests.

A separate read-only container then generated the actual 13-request proposal
from both original service volumes with networking disabled. The first wrapper
failed with `IndexError` because it reused the recovery context's five-element
tuple index for discovery's three-element context. That local wrapper was
corrected; its first failure/logs were retained, and production source was not
changed. The corrected run exited 0. All **12** original file hashes and
device/inode/mode/owner/size/mtime/ctime fields matched before/after and the
previous recovery plan snapshot. Original budgets remain strategy **903/745**,
authority **974/955**. Both original containers were confirmed exited on their
original image. STOP was retained, with no key access, state migration or start.

The discovery plan hash is
`e993306a0ca8c80e71c0bbc2e119d416425d2c7a20d80c3eab9611289dda98f6`.
Its underlying recovery plan hash is still
`62fe2be6f2500a53f47bb93e0b4e07de882c7bbb0f159630cf9eba52af6a60f2`.
Generating either proposal is not authorization to execute it. No real gas
discovery, NAV or receipt acquisition was performed in this follow-up, and this
candidate was not deployed.

```text
reports/review-20261105/gas-capture/manual-review.json
evidence_hash: 4cfe207f6719dfb79bc0bf688d72b7c32d61fc0df90d661d7c1ff6e144755d8f
reports/review-20261105/runtime-3033895/summary.json
evidence_hash: 89ac018614b08e2b239941d15565d48ab6552bce1dd08e57cd1303b34ca5e77e
reports/review-20261105/gas-discovery-plan-v2/plan.stdout
evidence_hash: 587b6cf20b907782a041f8bf001ea8e1e57978a56b91f1d3f9aa27e2d558ce54
```
