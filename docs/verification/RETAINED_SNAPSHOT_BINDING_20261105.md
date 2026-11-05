# Retained snapshot approval binding

## Confirmed defect and boundary

A journal snapshot written before `last_light_check` existed can omit that
optional field. `StateStore._decode()` supplies its default when reconstructing
the state. Runtime-transition review and application previously hashed
`asdict(state)`, but journal replay correctly checks the original canonical
snapshot. The resulting normalized approval hash differed from the raw source
hash. Strict-feed review had the same mismatch against its transition record.
Valid retained evidence could therefore pass review but fail migration.

Three disposable synthetic regressions failed against the prior implementation:
both supported five-pin runtime source configurations and the single-service
strict-feed transition. Their original log remains at
`reports/review-20261105/retained-snapshot/regression-before.log`. This establishes
the code defect; it does not establish that any real migration has been attempted
or that fresh production evidence is available.

## Change and preserved controls

`StateStore.retained_payload()` verifies the existing journal through a separate
read-only reader, requires its configuration and revision to match the caller's
loaded snapshot, and compares the decoded state with the supplied state using
canonical JSON. It returns the original bytes. It does not advance the original
writer's compare-and-swap revision on success or refusal. Even a competing write
with the same decoded values must be rejected as a different revision.

Runtime review and application now hash those retained bytes. Strict-feed review
requires an explicit canonical raw payload that decodes to the reviewed state;
its CLI, single-service writer and paired writer obtain it from the same helper.
The journal's original raw-source hash check, configuration transition whitelist,
fresh proof requirements and locked paired DELETE/FULL transaction are unchanged.
Old journal rows are never rewritten or backfilled with newly added defaults.

Thirteen additional cases cover both runtime configurations, single and paired
feed migrations, malformed or mismatched raw payloads, read-only preservation,
unloaded/missing/shadow inputs, changed financial fields and a semantically equal
competing snapshot. The competing-writer case also verifies that a subsequent
stale save still fails, so the verification helper cannot refresh its authority.

## Evidence scope

This work uses synthetic retained proofs and disposable local journals only.
No original state/evidence/budget or key volume was opened; no production HALT,
migration, rearm, service start, signing or broadcast occurred. RPC requests are
zero. ETH/USD and USDC/USD remain at 1,800 seconds; total capital USD 500, daily
loss USD 200 and cumulative loss USD 500 remain unchanged.

The separate authority maintenance-HALT plan remains pending explicit approval:
`reports/review-20261105/quiesce-original-plan/plan.json`, hash
`c317a743407dc9dfb998510c61362d5b3c1ab78a740de300a6ec493809885a49`.
Neither this fix nor its tests approve that write or a new RPC collection.
Fresh production USD/NAV, dual-source runtime/simulation and real receipt/fund/
unwind/restart acceptance remain unresolved. The existing five real historical
transactions and owner release are not replaced by these synthetic results.

The first post-change targeted run failed fourteen tests because the adapted
continuity tests lacked imports. Those imports were corrected; the failing log
is retained as `targeted-v1.log`. An ad hoc Mypy invocation mixed file and package
module names and exited 2; the canonical `python -m mypy src` invocation passed
all 251 source files. Its original failure and corrected logs are retained.
The first standalone Linux-fixture generator rejected a noncanonical `/tmp`
symlink path before exporting anything. Its tool result is referenced in
`fixture-generation-v1-exit.json`; the corrected generator resolves the physical
path. These failed attempts are not passing acceptance results.

## Completed development checks

The full `make live-quality` run exited 0. Its four planned commands all exited
0 without timeout or launch failure: Ruff/format/Mypy/Pytest/frontend, secrets,
Bandit and Compose verification. Results: **4,585 tests and 138 subtests passed**,
ten known deprecation warnings, 251 Mypy source files and eight frontend modules.
All **668** frozen runtime/test/config files remained byte-for-byte unchanged.
A separate local verifier checked every command exit, log hash and report hash;
this is local engineering verification, not independent/external audit approval.

- Quality report: `reports/review-20261105/retained-snapshot-quality-v1/report.json`,
  hash `9ac31bb5424bce59dd9b907d8a450c908795cf6a49253f3c92b2ebe4b63adc1e`.
- Exit/log/frozen-input verification:
  `reports/review-20261105/retained-snapshot/quality-review.json`, hash
  `08238764e5ca50574194640152bb506bd1dec454ea28c04b6bad92cc66b04750`.
- Retained earlier failures and corrected local checks:
  `reports/review-20261105/retained-snapshot/local-checks.json`, hash
  `f94cd5d3650a070c8d9efba1dfb6cbde7eb6fc71a8e2dcf0be678d6381e43d70`.

## Verified candidate image

Implementation commit: `cc6ea8406493b4799edcfb901854597b81857198`.
Candidate image:
`sha256:be6143dda7e57c13baf777e7d715376614be6c74a881ca22e14b633fc06ea856`.
The stopped-image verifier and separate archive checker matched all **362**
application files and **364** archive inputs to Git and local bytes. Manifest:
`41a8cc9ec1379e361ca86f0565824f887c7f1315278bac18cc0b3ee8a40063ef`.

Build, source verification and corrected Linux smoke each exited 0. The UID
10001 worker ran with networking disabled, a read-only root, dropped capabilities,
no-new-privileges and no mounts. Both strict-feed and historical 90,000-second
source configurations migrated to the same strict 1,800-second target in
**disposable synthetic paired journals**. Normalized-hash approval was rejected;
old journal rows, retained proofs and untouched synthetic budget markers were
preserved. A distinct raw snapshot with equal decoded values caused a revision
conflict, and verification refusal did not refresh the stale writer's revision.
No budget ledger was exported or copied in the synthetic fixture packet.

The first Linux smoke exited 1 because its competing writer appended exactly
the existing bytes, which the journal correctly treats as an idempotent no-op.
The corrected harness removes the optional default field to create distinct
bytes with equal decoded values and asserts that the tip actually changes.
The original failed script, exit record and log are retained; log hash
`12e4c1e45d4957f60c76f28e3dc7775af5d443d4894422e79ba27afc1e7a99e8`.
Only the changed smoke was rerun, using the already verified image; the successful
build and source checks were not repeated or relabeled.

- Candidate report: `reports/review-20261105/retained-snapshot-cc6ea84/summary.json`,
  hash `b742638fcefebc3e489feaaa3c7264989e465a4f2e8fb9cf309422457682317a`.
- Separate local result check:
  `reports/review-20261105/retained-snapshot-cc6ea84/reverification.json`, hash
  `822c8b493949d95308e7522355950707bc0352ea2fcde14f33cb7d9d643eacff`.

These are development and image-compatibility results. The candidate was not
installed on either production service, and the original authority maintenance
HALT approval and fresh production acceptance requirements remain outstanding.
