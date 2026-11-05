# Missing maintenance HALT review

## Original-state finding

The verified startup-history candidate was used in a local, network-disabled,
UID 10001 container with only the two original state volumes mounted read-only.
Both project services were exited. Default joint gas/NAV planning refused with
exit 3: `joint discovery requires both original services halted`. At least the
strategy journal has no HALT, even though the existing STOP file is present.
This is the previously retained original state, not a newly cleared halt.

The initial refusal and its diagnostic remain separate under
`reports/review-20261105/joint-original-plan/` and
`reports/review-20261105/joint-original-plan-diagnostic/`. Diagnostic report hash:
`bf4d36efdbec348cccb3f22319e6195271cd419311e9192ad2680b4cf0cd795d`.
All twelve original file identities/metadata and all sampled non-budget content
hashes were unchanged; directory entries were unchanged. Budget files were not
copied or hashed by the worker; the original read-only planner does not reset or
replace them. Both attempts made zero RPC requests and accessed no key/socket
volume or private `.env`. No production state write or service start occurred.

The prior explicit production-write permission was **only to create two STOP
markers**. That is not permission to append a new journal snapshot. No production
HALT append is part of this change's development acceptance.

## Narrow operator entry

`python -m scripts.quiesce_live_services --strategy-dir /strategy
--authority-dir /authority` is read-only review. It requires the original STOP
and lock files, private owned state/journal files, distinct original artifacts,
recognized original configuration digests and fully verified journal chains.
It does not open evidence or budget files. Existing owned lock files may be
readable, as the actual 0644 lock files are; no permission rewrite is required.

The retained `oasis.live-quiesce-plan.v1` names each original tip, configuration,
source/target state hash, whether a HALT is missing and the exact file identities.
It proposes only `operator maintenance stop; runtime transition review required`.
An existing nonempty HALT is preserved without exposing its text. Only a missing
HALT changes; every other raw state field, including absent legacy default
fields, remains identical. No claim that migration has already happened is used
as a halt reason.

Application requires a separate approval of that exact plan, a retained plan
file and a new attempt directory outside both originals:

```sh
python -m scripts.quiesce_live_services \
  --strategy-dir /strategy --authority-dir /authority \
  --apply --plan-file /review/quiesce-plan.json \
  --approved-plan-hash EXACT_REVIEWED_HASH --out /review/new-attempt
```

Before any journal write the CLI fsyncs an uncertain-attempt marker and its
directories. The library re-derives the plan under both original service locks.
Each needed append is a single existing-file SQLite transaction, checks DELETE
and FULL under its write lock, compares the original tip/state, and appends only
the new HALT snapshot. It never converts WAL or removes recovery artifacts.
STOP, original JSON projections, configuration, financial state, old journal
rows, evidence and budgets remain untouched. Journal-backed readers immediately
see the HALT despite the intentionally older JSON projection.

This is **not paired atomic financial migration**: each halt append is atomic
and partial completion only removes authority. A failure after the first commit
retains that halt and the two STOP files, reports an unknown result, and requires
inspection. An old plan cannot be replayed at changed tips; no retry is automatic.
Existing HALTs produce no additional append.

The maintenance reason is recognized by the separate evidence-constrained rearm
path, whose raw receipt/cash/NAV/nonce/capital checks, exact approved review and
STOP preconditions remain required. It is not permission to migrate, rearm,
start, sign, broadcast, acquire RPC or change any capital/price limit.

## Development evidence

The full corrected quality run exited 0: **4,572 tests, 138 subtests**, ten known
deprecation warnings; 251 Mypy source files; eight frontend modules; Ruff,
format, secrets, Bandit and Compose checks all exited 0. A separate local script
verified all exits/log hashes and **667** frozen runtime/test/config inputs.

- Quality: `reports/review-20261105/quiesce-quality-v2/report.json`, hash
  `7d1953446fac88503e24fe9421bac8b7402c9503da4345c018a0cce229b7b09b`.
- Local result check: `reports/review-20261105/quiesce/quality-review.json`, hash
  `52f2568381272d528f8b3b2d61a9d6703e947d6ab8808ddf27da0ac7741de7f5`.

The first quality wrapper omitted the three new untracked files from its frozen
inventory. It was stopped, not accepted: `python_frontend` exit -15, outer exit
2. Its 664-file inventory and logs remain under `quiesce-quality-v1/`; report
hash `d6c86488f965b47646004773becdfb76137b2fb9f773dc7b82aa9acfe11e2753`.
The v2 inventory includes all three files before the checks began.

Earlier targeted assertions accidentally compared the other intentionally
appended journal against its old whole-file bytes; they were corrected to check
only authorized journal changes while preserving all other files. That failed
log (`quiesce/targeted-v1.log`) and the initial missing-type-annotation Mypy log
remain retained. Neither was a passing result. Tests additionally cover old
raw field preservation, existing halt/no-op, wrong approval, source changes,
missing STOP, shared files, locks, WAL/sidecars, a real second-connection mode
flip, snapshot size/type bounds, partial commit/unknown reporting and the rearm
proof requirement. All application tests use disposable synthetic originals.

Current real USD/NAV, gas/runtime/simulation, receipts/funds/unwind and restart
acceptance remain outstanding. The USD 500 / 200 daily / 500 cumulative and
both 1,800-second feed limits are unchanged; production RPC remains zero here.
