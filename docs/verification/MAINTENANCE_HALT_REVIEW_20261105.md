# Missing maintenance HALT review

## Original-state finding

The verified startup-history candidate was used in a local, network-disabled,
UID 10001 container with only the two original state volumes mounted read-only.
Both project services were exited. Default joint gas/NAV planning refused with
exit 3: `joint discovery requires both original services halted`. The subsequent
original-volume quiesce plan identifies **authority** as missing HALT, with the
strategy's existing HALT preserved. Early commentary inferred the strategy role
from the refusal location; that inference was incorrect and is superseded by the
explicit per-service plan below. The existing STOP files are present.
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

## Checked image and original approval plan

Implementation commit: `b0f4c596705bf3f91a86940b8d7b0d349ad96a8c`.
Candidate image:
`sha256:cb6d5d7ee931d1f9f9fcf8be9c4cc3585e1fe04fb9dc306a073c8f0ea964b3dc`.
All **362** application files and **364** Git archive inputs match that commit
and local bytes. Source manifest:
`9478a33395073f459d3d210347bfea088656cc08daf1e8bb3d6b15aea996a1db`.
Build, stopped-image source verification and synthetic Linux smoke wrapper each
exited 0. The mount-free UID 10001 smoke used a network-disabled/read-only root,
dropped capabilities and no-new-privileges. Six actual CLI exits were
**[0, 0, 0, 3, 3, 3]** for missing, existing, no-op, wrong approval, partial
commit and a second-connection WAL race. Only missing HALTs changed; projections,
STOP and locks stayed unchanged; existing halts were not replaced. Uncertain
outcomes were retained, and the injected WAL mode was not converted.

The first smoke failed because its expected Python dataclass dictionary retained
tuples while journal JSON contains arrays. The corrected harness compares the
canonical JSON form; checked application bytes were unchanged. The initial smoke
script/log remain under `quiesce-b0f4c59/`, failure log hash
`ca545614b36941f8220efa550e1a493544d57dabf3b189f967e3fe86867a7952`.
Accepted candidate report: `reports/review-20261105/quiesce-b0f4c59-v2/summary.json`,
hash `d4016c2a08f000096383517b806c5d118e900ac73068a0d562fa9cc2978503e7`.
Separate local verification of every exit/log/source/archived Git blob and all
667 frozen inputs: `quiesce-b0f4c59-v2/reverification.json`, hash
`7462405a7910ce07cc1f48a05c3534e79edb90e833ea0796f2b25756e36aef98`.
These are local retained-result checks, not another reviewer or external audit.

Only the review operation was then run against the two original read-only
volumes, with SQLite restricted to exactly two immutable read-only journal
connections. It opened no evidence/budget database, key, socket or `.env`, and
all eight scoped original files and directory entries were unchanged.

- Review plan: `reports/review-20261105/quiesce-original-plan/plan.json`.
- Exact plan hash:
  `c317a743407dc9dfb998510c61362d5b3c1ab78a740de300a6ec493809885a49`.
- Host verification report hash:
  `71abaa0c1f033c79175619d49dd2da541849796e93c03f6172886a51b0f508d7`.
- Original-volume worker report hash:
  `7ccf86c0fd33ae993ec4fd71e96c372af8ab4e393153a90bf551d380b1d1237a`.

**Only authority needs one append.** Its source tip is
`e8b87f1bbe8b9abbc4574101cd3d6c73c5f3bc929e6769eb51ebcb6bc21f2c0a`;
source raw state hash `075e717ebed9db42a3f75a00ae0216f2e9f906da787f55be475994c19377fd5f`
would become `3c38e307141d7dc389e44ad005368cea7cbdffac6120822038c2c780ec5604b7`.
The strategy's source and target hashes are identical and its journal receives
no append. Original configuration bindings stay at their exact historical hash;
no historical 90,000-second policy is made live by this maintenance operation.

The production plan remains **pending separate owner authorization**. No
production `--apply` was invoked, and no new RPC authorization was inferred.
The documentation-only follow-up changes no checked code/test/config bytes;
the four unrelated untracked documents remain unmodified and uncommitted.
