# Persistent operational-precheck RPC budget — 2026-10-22

## Defect and correction

`run_live_lp --mode live --check` formerly passed a new temporary directory to
both direct RPC clients and the receipt/NAV acquisition factory. Although each
invocation had a bounded request budget, restarting the diagnostic discarded its
ledger and obtained a new quota. Calling that path read-only did not establish
cost control across invocations. The old historical diagnostic log is preserved;
[its verification note](LIVE_INTENT_POSITION_20261013.md) now records this limit.

The command now passes its existing state directory to every RPC transport.
Direct head/nonce reads and raw evidence acquisition use the same
`live.receipt-budget.db` ledger as other live reads. It requires that ledger to
already exist with the exact frozen policy and valid usage chain. It neither
initializes a budget nor supplies a fresh-quota fallback. The temporary directory
contains only disposable evidence, still labelled ephemeral in the report.

`RpcBudget(require_existing=True)` opens SQLite in `mode=rw`, never creates tables
or inserts a missing policy, and validates the retained policy and usage chain.
`BudgetedPoster` carries this requirement to every reservation, including after
rate-limit waits. Removing the ledger after client construction refuses the next
read rather than silently recreating it. The direct client redacts SQLite errors
alongside other transport errors. Other callers retain their existing default
initialization behavior; this change does not migrate or reset any ledger.

The check is read-only with respect to chain and loop state, while request usage
is necessarily a durable write. It cannot sign or broadcast, save loop state,
clear latches, increase quotas or widen release/capital/loss limits. Request counts
are not dollar prices: these controls do not establish remaining monetary budget
or invoice accuracy. Do not initialize/copy another ledger to bypass a refusal.

## Validation and scope

The focused budget, direct-client, precheck, receipt-cost and authority-recovery
tests passed 96 cases. New cases cover repeated CLI invocations sharing direct
and raw-capture usage, exhaustion before another HTTP call, missing/empty/foreign
policy stores, missing policy rows, deletion after client construction, unchanged
loop-state bytes and absence of signer construction. HTTP is replaced with an
in-memory synthetic transport; no real RPC request is made.

An initial focused lint check flagged a broad exception assertion and long lines;
these were corrected. The first type check rejected an overly broad keyword
dictionary; explicit constructor branches corrected it, and mypy then passed.
Neither failed check is represented as a pass.

The final `make live-quality QUALITY_OUTPUT=reports/review-20261022/precheck-budget`
completed all four commands with exit code 0, no timeout and no launch failure:
Ruff/format/mypy, 3,237 tests and 138 subtests, eight frontend syntax checks, no
unreviewed secret candidates, no medium/high severity Bandit findings, and all
seven offline Compose service expansions. One dependency deprecation warning
and 85 low severity Bandit findings remain. Raw logs and command digests are
retained in that directory. Report evidence hash:
`e01ab5c68f1cbce0e2cf83bd2a4325c226662a7c53202c64075d1bd63634fba0`.

No production budget was opened, copied, reset or changed in this work. Paid
acceptance usage remains eight requests under the cumulative USD 0.10 limit;
this patch does not reclassify them into a new ledger. The owner-selected canary
key volume was not accessed and no real precheck/service was launched. The
existing Docker image has not been rebuilt for this patch.

Fresh USD/NAV, production simulation/runtime evidence and real-canary dual-service
receipt/cash recovery remain unresolved. This is an operational budget defect
fix, not evidence of live readiness or profitability.
