# Atomic paired strict-feed migration — 2026-11-05

## Reproduced defect and resulting behavior

The stopped-service feed migration previously committed the strategy journal
before attempting the authority journal. A real SQLite trigger rejecting the
second epoch reproduced an inconsistent result: the strategy retained its new
epoch while authority stayed at the old configuration. Both remained halted,
but the pair required manual investigation before any recovery could continue.
The original regression failed and its log is retained; this was a synthetic
temporary-database test, not an incident in the production volumes.

`feed_transition.apply_pair()` now stages both epochs inside one SQLite ATTACH
transaction and commits once. The original databases must be distinct owned
files, on one filesystem, already using DELETE rollback mode, without recovery
sidecars. Missing originals are never created. Both databases require FULL
synchronization. Both service locks and unchanged STOP identities stay held;
state/journal file identities and both proof deadlines are checked before the
shared commit. Expected journal tips and full hash-linked history remain CAS
preconditions. A second staging failure, SQL commit refusal, expiry or control
identity change rolls back both staged epochs and their metadata tables.

`LiveJournal._transition_in_transaction()` supplies the same epoch validation,
hash commitment and append-only metadata to both paired and single-service
application. It requires an active transaction and accepts only the fixed
`main`/`peer` schema names. This does not add a permitted transition: the sole
supported configuration change remains USDC age tightening from 90000 to 1800
seconds, with ETH age 1800 and every other configuration field unchanged.
The five-to-eight runtime-pin transition is still unimplemented.

Only after both authoritative journals commit are JSON projections written.
Projection failure leaves both new halted epochs readable through their retained
old projections, reports `migration_applied: true`, `atomic_pair: true` and
`projection_complete: false`, and makes the operator CLI exit 3 with status
`both_migrated_halted_projection_incomplete`. This is a request for inspection,
not permission to repeat the migration or replace the journals. An interrupted
or uncertain commit likewise requires both originals and retained sidecars to
be inspected; no automatic retry or sidecar removal was added.

The storage choice follows SQLite's documented atomicity conditions for attached
file databases outside WAL mode. Hardware, filesystem locking, VFS and durable
flush behavior remain assumptions; process-kill tests do not establish physical
power-loss resilience. See [SQLite ATTACH](https://www.sqlite.org/lang_attach.html)
and [SQLite multi-file atomic commit](https://www.sqlite.org/atomiccommit.html).
The same-filesystem requirement is an additional project restriction.

## Verification boundary

Focused tests cover a peer SQL trigger failure, commit refusal, proof expiry
after both stages, lock/STOP replacement, unchanged historical rows, retained
risk basis and external-flow accounting, gas/position/history preservation,
append-only transition metadata on both databases, original budget preservation,
stale expected tips, WAL refusal, recovery-sidecar preservation and incomplete
projection reporting. Existing single-service migration and historical replay
tests remain in place. Previous tests expecting partial commits now assert the
stronger rollback invariant while retaining their failure injections.

Three actual subprocess SIGKILL cases stop after the first stage, after both
stages, and after the shared commit but before projection. The first two retain
both old epochs; the last retains both new epochs. All retain HALT and old
projections. Temporary fixture recovery is explicitly inspected through SQLite
using both original test files; the production entry point itself refuses the
pending sidecars. These are local crash/transaction tests, not chain evidence.

The work performs zero production RPC, budget changes, configuration migration,
key access, signatures, broadcasts or service starts. USD 500 capital, USD 200
daily loss, USD 500 cumulative loss and both 1800-second feed ceilings remain
unchanged. Fresh production USD/NAV, approved gas fingerprints, runtime-policy
migration, simulation, receipts and unwind/restart acceptance remain unresolved.
The existing 197-request authorization question remains pending and separate.

## Development gate

The regression failed before the fix because one new strategy event survived a
peer SQL refusal. After the fix, **153 focused tests** passed. Existing fault
injections were moved to the staging boundary, and their expected outcome was
strengthened from a partial halted commit to rollback of both journals. Fifteen
test cases were added overall, including the CLI projection-failure case.
Six lint findings and one required type annotation were corrected during
development; their initial logs are retained. No test or financial invariant
was skipped or relaxed to pass checks.

Full `make live-quality` exited **0**: **4343 tests, 138 subtests**, 246 mypy source
files and eight frontend modules passed. Python/frontend, secrets, Bandit and
Compose each exited 0 without timeout or launch failure. Nine previously
reported dependency/fork warnings remain. Every check log hash, the canonical
report hash and ten locked source/config hashes were independently verified.
The exact-risk and epoch-validation modules and production configs are unchanged.

```text
reports/review-20261105/feed-pair-atomic/regression-before.log
reports/review-20261105/feed-pair-quality-v1/report.json
passed evidence_hash: 5077351044fb74f7632fb009e7bb8caf43d8b9c1b110c85e73219ee47c2cd21c
```

## Immutable Linux candidate

Implementation commit `cc6ded2b7269edb024bce5b3309ff13847cee5f3` built
candidate
`sha256:36716bfd6c74ca40fee7b68242b10c106f27c2978f17f2e36a93e5c359337323`.
Build, stopped-image source verification and Linux storage smoke each exited 0,
without timeout. All **353** application files matched the committed archive and
local source bytes; manifest hash
`a2c275e04886a506fdbea88adaff662c6e5874a415cfe6b5f66b216ce4274a93`.
Archive, script, log and report hashes were independently re-derived, and all
ten locked source/config hashes still matched after the run.

The unprivileged smoke ran with network disabled, a read-only root, dropped
capabilities and temporary tmpfs, without production/source/key/socket mounts.
It used two synthetic SQLite journals, a deliberately rejected peer insert,
rollback and then a successful paired commit whose two chains reopened halted.
DELETE/FULL settings were checked. This exercises storage staging/replay only:
the raw-proof admission gate was explicitly not exercised by this Linux smoke,
and tmpfs does not establish physical durability of the original production
volumes. Complete raw-proof and SIGKILL cases belong to the host test suite.
Dependencies, base runtime, Docker, filesystem locking and flush behavior remain
trusted. This candidate was not deployed and no production volume was opened.

```text
reports/review-20261105/feed-pair-atomic/manual-review.json
evidence_hash: 04ce3ab42884d9b311437f133ac0be903100523eec8c54c9cb6d086da5f4378d
reports/review-20261105/runtime-cc6ded2/summary.json
evidence_hash: 9188a45be00431f97877c5dc39cd8f8aeb51b5b80dafc118606566a4017b02cd
```
