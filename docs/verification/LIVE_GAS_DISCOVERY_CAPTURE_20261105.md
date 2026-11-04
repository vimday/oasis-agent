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
