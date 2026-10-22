# Unknown signing outcome in the dashboard — 2026-10-22

## Defect and behavior

The read-only live-status projection previously ignored `signing_attempt`. A
locally retained signing reservation with no returned transaction hash could
therefore appear as a recent observation and "no pending transaction", despite
the execution/recovery layer preserving an unknown signing outcome. This was a
reporting defect, not a release-gate bypass.

The bounded local-state parser now recognizes a non-null reservation, including
an empty object, without copying its contents to the response. The API adds
`signing_unresolved` (`true`/`false` for a parsed state, `null` for missing/invalid/
unconfigured state) and a `signing_unknown` status with the
`signing_outcome_unresolved` blocker. Unknown signing takes display precedence
over halt, pending and observation freshness; their applicable blockers remain.

The dashboard renders "签名结果未知，等待恢复核验" and explains that a missing reply
does not establish that the transaction was unsigned or unsubmitted. Its next
step is to check the authority commitment and receipt, never automatically re-sign
or re-broadcast. The pending panel no longer says "无待处理交易" for this state.
It still offers no signing, broadcasting or recovery mutation action.

The frontend requires the status, boolean flag and blocker to agree; contradictory
responses clear displayed results rather than silently dropping the unknown
state. Missing/unconfigured/invalid responses clear the flag to `null`. Older
local files without `signing_attempt` remain supported as local declarations.
An older API response lacking the new flag is refused by the updated frontend for
parsed states until the backend/assets are updated consistently.

## Verification

Focused reporting and actual JavaScript presentation tests passed 28 cases before
the final API-cache assertions were added. Coverage includes nonempty and empty
reservations, reservations alongside a halt, malformed reservations clearing
numbers, private payload redaction, return to ordinary status after the reservation
is cleared, conflicting frontend flags, pending-panel text and removal of stale UI
messages. The API regression checks a new ETag and updated unknown-signing response
when the underlying state changes, followed by clearing on corruption.

The JavaScript tests execute the real module with a test DOM in Node; they verify
rendered content and transitions, not browser screenshot/layout acceptance. No
CSS or layout was changed.

The final `make live-quality QUALITY_OUTPUT=reports/review-20261022/signing-status`
passed all four commands with exit code 0, no timeout and no launch failure:
Ruff/format/mypy, 3,268 tests and 138 subtests (including the final API assertions),
eight frontend syntax checks, no unreviewed secret candidates, no medium/high
Bandit severity findings and seven offline Compose service expansions. One
dependency deprecation warning and 85 low severity Bandit findings remain.
Raw logs and per-command hashes are retained in the output directory. Report
evidence hash:
`abdcd10b5a1bd19677655db3874bf964e539d0f3c7c50dee716628c83d81d7a2`.

## Scope and remaining conditions

This remains a bounded **local JSON declaration**, not authoritative journal replay,
chain evidence or proof that a service is running. No signing reservation is
cleared, no state file is modified and no recovery operation is started by reading
the endpoint. `execution_eligible` and `evidence_replayed` remain false and net
profitability remains unresolved. No raw signing request is returned.

No real RPC or oracle request, key-volume access or production service start was
performed. The same cumulative USD 0.10 budget still has eight recorded paid
requests; no ledger was opened or replaced in this work. Capital/loss limits and
production release blockers are unchanged. Fresh production prices/NAV, current
dual-source simulation/runtime evidence and real-canary receipt/cash recovery
remain unaccepted. The existing Docker image was not rebuilt for this change.
