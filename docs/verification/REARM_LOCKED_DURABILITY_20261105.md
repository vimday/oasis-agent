# Paired rearm durability under write locks

## Confirmed defect

The paired rearm writer checked each attached SQLite database's DELETE journal
mode before `BEGIN IMMEDIATE`. Another connection could persist WAL after those
unlocked probes and before the writer acquired its database locks. Existing
service locks coordinate cooperating processes but cannot make that database
mode probe atomic with a different SQLite connection. The writer would then
clear both service HALTs in mixed journal modes, despite its rollback-journal
atomicity prerequisite. A crash could no longer be treated as a paired atomic
commit under the documented SQLite super-journal contract.

Two disposable synthetic regressions used real separate SQLite connections to
flip the strategy or authority database in that interval. Both old-code tests
failed with `DID NOT RAISE ValueError`, demonstrating that the mode change was
admitted. This is a local code defect, not an observed production incident or a
claim that a real service HALT has been cleared. Original failure evidence is
retained at `reports/review-20261105/rearm-durability/regression-before.log`.

## Change

Rearm now reuses `require_paired_rollback_transaction()`, already used by both
configuration migration paths. It checks the active transaction and both attached
DELETE/FULL settings immediately after `BEGIN IMMEDIATE`, before proof review or
any append, and again immediately before COMMIT. Failure rolls back staged
changes. It never converts WAL to DELETE or removes recovery sidecars.

The approval hash, complete retained receipt/cash/NAV verification, pending-signing
refusals, risk ceilings, STOP files, service locks, budget preservation, old journal
history and projection-failure recovery semantics are unchanged. This patch adds
no release, rearm or transaction authorization.

Four new cases cover the real pre-lock mode change on each service and failed
durability assertions at both locked boundaries. A pre-review failure must stage
nothing; a pre-commit failure after both appends must roll back both journals.
The assertion-failure cases simulate a refused FULL assertion; SQLite itself
forbids changing synchronous within an active transaction. The mode-race cases
actually change persistent WAL mode and check that it is not converted back.
The existing paired-rearm suite also covers ordinary success, loss and history
preservation, stale approval, commit failure, STOP changes and projection failure.

## Evidence and remaining production work

The 46 targeted tests passed. The first full quality run stopped at Ruff because
two new test regex arguments required raw-string prefixes. That run exited 2;
its logs and 669-file snapshot remain under `rearm-durability-quality-v1/`.
The prefixes were corrected before the separate v2 quality run. No failing run
is relabeled as a pass.

Only disposable synthetic state/proofs were exported for the Linux harness;
no budget ledger was exported or copied. Each Linux case creates a fresh
temporary synthetic budget and makes zero network requests. This is a test
fixture, never a replacement or reset of the original production ledgers.

No original state/evidence/budget or key volume was opened in this work. No
production HALT append, rearm, migration, service startup, signing or broadcast
occurred. USD 500 capital, USD 200 daily loss, USD 500 cumulative loss, and both
1,800-second feed ceilings remain unchanged.

The separate authority maintenance-HALT plan remains pending explicit approval:
`reports/review-20261105/quiesce-original-plan/plan.json`, hash
`c317a743407dc9dfb998510c61362d5b3c1ab78a740de300a6ec493809885a49`.
Fresh production USD/NAV, dual-source runtime/simulation and real receipt/fund/
unwind/restart acceptance remain unresolved. Existing historical transactions
and owner release do not substitute for those current proofs.

## Completed development checks

The corrected full `make live-quality` run exited 0: **4,589 tests and 138
subtests passed**, ten known deprecation warnings, 251 Mypy source files and
eight frontend modules. All four planned commands exited 0 without timeout or
launch failure; Ruff/format, secrets, Bandit (no medium/high issues) and Compose
checks passed. A separate local script verified every exit, log hash, canonical
report hash and all **669** frozen runtime/test/config inputs. This is local
engineering verification, not independent/external audit approval.

- Quality: `reports/review-20261105/rearm-durability-quality-v2/report.json`, hash
  `feaeb3cbab6504d7a1eb3499e04eb7373d1715795179acf311b5767f114d1e1e`.
- Result check: `reports/review-20261105/rearm-durability/quality-review.json`,
  hash `dff8c48a8600881a7903eb3912386adc41efc1767d10445f8076d29dc67286af`.
- Original failed Ruff quality report:
  `rearm-durability-quality-v1/report.json`, hash
  `7df06b95853bc3884714f441e69698648075624bc9b1c9c9c730fb9616aceb28`.
- Earlier regression/local checks: `rearm-durability/local-checks.json`, hash
  `2e24864a499211af14bb6229a3ffb497872e33e5e8ea2ff76e57c92d57feb3a6`.

## Verified image and crash boundaries

Implementation commit: `0e3b1b3cb7dfde225be2bd5c20e8dbfd7e6f336f`.
Candidate image:
`sha256:3014497bc83f928cf4948f5d72ccc3ffe94a1e79cc76b06af3d80a38ba835a29`.
All **362** application files and **364** archive inputs match Git and local
bytes; manifest
`b365f43f7c6c30c9099f0590f80c27defdbcf5e044b885be7ac487bffb9cc05c`.
Build, stopped-image source verification and Linux smoke each exited 0.

The mount-free UID 10001 Linux worker used a read-only root, no network, dropped
capabilities and no-new-privileges. Six synthetic cases passed: ordinary paired
rearm, a real pre-lock WAL switch on either journal, and SIGKILL after the first
append, both appends, or the commit before JSON projection. The three killed
children exited **-9**. Before commit both original HALTs survived with no new
committed row; after commit both journals contained the rearm while original JSON
projections remained unchanged. Both WAL switches were refused before staging,
with the changed WAL mode retained rather than converted back. STOP files,
original journal row prefixes, retained proofs and synthetic budget bytes were
preserved in every case.

After a pre-commit SIGKILL, the production entry first refused the hot recovery
sidecars. The harness then explicitly modeled operator inspection with both
**temporary synthetic** originals available so SQLite could recover them. It
did not add automatic recovery, sidecar removal or a production retry. These
specific SIGKILL boundaries do not prove power-loss behavior or an arbitrary VFS;
paired durability still assumes a correct same-filesystem SQLite/VFS/fsync setup.
No real service was rearmed by this test.

- Candidate: `reports/review-20261105/rearm-durability-0e3b1b3/summary.json`, hash
  `b4adb0a60b480ae754133589b6f328afe12b12a6bf18313463392a5dca81ad38`.
- Separate local result verification:
  `reports/review-20261105/rearm-durability-0e3b1b3/reverification.json`, hash
  `4a2150d376d69f1aeee18e70d23a5595d51f5ca91c38a7e00bd20f131706426e`.

The image is a checked candidate only. Fresh production evidence, the separate
maintenance-HALT approval and all subsequent rearm/start gates remain required.
