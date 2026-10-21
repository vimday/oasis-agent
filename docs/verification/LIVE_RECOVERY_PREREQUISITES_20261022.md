# Existing-history recovery prerequisites — 2026-10-22

## Defect

The authority's reconciliation command previously called normal state binding on
an empty private directory, created an empty state history and reported no pending
transaction. The strategy reconciliation path could also initialize its history
and create a fresh RPC budget. Neither behavior distinguished first-time setup
from loss of recovery data. This is unsafe evidence for a recovery verdict, even
though recovery has no signing capability and production release remains closed.

The old authority test explicitly accepted the empty-directory behavior. It has
been replaced with a refusal regression and a separate successful case backed by
an existing journal. No historical production receipt evidence is claimed or
rewritten by this correction.

## Confirmed changes

`StateStore.bind(..., require_existing=True)` refuses a missing journal, a legacy
JSON-only state, an empty journal and a different configuration binding. It does
not create tables or migrate a projection into fresh history. Authority status
and reconciliation, and strategy reconciliation, use this mode. The strategy
refuses an absent recovery directory before creating it.

An intact authoritative journal can still recover a missing JSON projection.
Tests retain both the loss accounting and halt latch through this case, with no
journal rewrite. Existing journal reads and appends now open SQLite with `mode=rw`
instead of permitting implicit file creation; removal after object construction
therefore cannot silently create an empty database on the next read. Explicit
normal initialization remains available separately and is not called by recovery.

If the validated journal has no pending transaction or uncertain signature, both
recovery commands return locally without RPC configuration, quota initialization
or evidence creation. This means only that the retained history has no pending
work; it is not a current chain-state assertion or a profit/release verdict.

Pending reconciliation requires the same service's existing, policy-matching RPC
ledger before evidence acquisition or public-status adoption. Both direct reads
and retained receipt/cash acquisition keep `require_existing_budget=True` on every
request. No request-budget reset or increase is provided. Authority SQLite failures
are covered by its fixed redacted top-level diagnostic. Neither service constructs
a key provider for recovery; status remains offline and keyless.

## Verification scope

The focused journal, recovery-prerequisite, authority CLI and receipt-cost suites
passed 82 tests. Coverage includes both services refusing missing/JSON-only history,
missing/empty/foreign-policy budgets, unchanged pending state on refusal, no RPC or
signer construction, valid no-pending recovery with no endpoints, recovery when
only the projection is missing, preserved loss/halt state, and journal deletion
between construction and read. Existing synthetic receipt settlement tests retain
their idempotence and disagreement checks, with explicitly provisioned test-only
journals and budgets. No real transaction or provider is used by these tests.

The first lint run found a nested condition; it was simplified before validation.
Type checking passed after the final implementation.

`make live-quality QUALITY_OUTPUT=reports/review-20261022/recovery-prerequisites`
passed all four commands with exit code 0, no timeout and no launch failure:
Ruff/format/mypy, 3,259 tests and 138 subtests, eight frontend modules, no unreviewed
secret candidates, no medium/high Bandit severity findings and all seven offline
Compose service expansions. One dependency deprecation warning and 85 low severity
Bandit findings remain. The output directory retains raw logs and command hashes.
Report evidence hash:
`863523375c08cf1f9a2c97fcca53bbec0ea7e6e5a2e47e671a54340cf67effb1`.

No production journal or RPC budget was created, migrated, copied, reset or
modified. No key volume was accessed and no Docker production service was started.
This source change has not been rebuilt into the existing Docker image. Real RPC
calls this increment: zero; previously recorded paid calls remain eight within
the same cumulative USD 0.10 authorization, without assuming invoice accuracy.

Fresh USD/NAV, production double-source simulation/runtime evidence and real-canary
dual-service receipt/cash recovery are still unresolved. Capital/loss limits and
release gates are unchanged; passing these tests is not production acceptance.
