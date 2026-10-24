# NAV checkpoint replay at restart — 2026-10-23

## Finding and correction

The direct loop's restart check re-derived individual retained NAV proofs and
compared their `AccountValue` objects to the integer risk ledger's last NAV. It
did not require the configured provider pair, compare complete feed rounds/core
state, or bind that observation to the risk ledger's timestamp. It also returned
without checking anything when the reference list was empty, even if the state
already contained a loss basis. Therefore, two copies of one provider's proof,
references mixed from different captures with identical valued amounts, or a
missing proof list could pass that local checkpoint check. The global live
release gate remained closed; this finding is not evidence of an executed trade.

`replay_nav_quorum()` now requires exactly the declared, distinct, ordered source
pair at one block, rebuilds both proofs from retained bytes under the current
configuration, compares their position declarations and complete observed RPC
results, and requires equal exact account values. Rounded NAV equality cannot hide
a different feed round or core fee-growth state. Source aliases are expectations,
not independent evidence that two endpoints have unrelated operators.

`LiveLoop._check_nav_checkpoint()` now uses that replay and binds its value and
block timestamp to `ExactRisk.last_nav` and `last_at`, validating the integer risk
state as well. An existing legacy or exact loss basis without retained NAV proofs
refuses before a new observation. A pristine risk state still permits inception.
The check never resets or migrates a baseline, writes state, signs or calls RPC.
Both strategy and transaction-authority admission already call this check, so the
same protection applies after either service restarts.

These are historical checkpoint checks, not freshness acceptance. A valid old
checkpoint may replay, but the next live decision still needs a new two-source
capture under the existing 120-second head and 1800-second feed limits. The check
does not require the old position to equal the current position: a fully reconciled
transaction can change inventory after that NAV observation. It does require the
two historical proofs to describe the same position.

## Verification

The initial regression run against the old code exited 1 with seven failures:
duplicate source, reversed pair, unexpected source, mismatched ledger timestamp,
different valid feed rounds with identical answers, missing proof list, and legacy
loss basis without proofs. The original failure output remains at
`reports/review-20261023/nav-checkpoint-before.log`.

The new focused suite has 14 cases using synthetic raw RPC envelopes and local
SQLite evidence. It also checks valid replay after state-journal restart, pristine
inception, and both service admission paths refusing a bad pair before any fresh
RPC or key-provider call. Positive external-flow bookkeeping and precheck loss
detection now use a replayable baseline instead of unproven legacy declarations.
Fixture runtime pins and timestamps are synthetic; none
of these tests is production NAV or receipt recovery acceptance.

An initial full quality run stopped at formatting (exit 2 for its first command).
Its evidence remains at `reports/review-20261023/nav-checkpoint`; the test formatting
was corrected before the next run. That run, retained at
`reports/review-20261023/nav-checkpoint-final`, had three test failures: two old
fixtures expected an unproven baseline to pass, and one unrelated Unix-socket
test encountered `ENOTCONN` at client shutdown. The two fixtures were updated to
expect refusal, with positive bookkeeping/loss coverage retained using raw proof
pairs. The affected NAV/precheck/loop suites then passed, and the socket case
passed in a separate rerun without a change to the daemon or its test. This is
consistent with a timing-sensitive socket failure, not proof of its root cause.
No failed run is relabeled as passed.

The final complete run at `reports/review-20261023/nav-checkpoint-complete`
passed all four commands with exit 0, no timeout and no launch failure. Each log
digest and the report hash were independently verified. Results: 3446 tests and
138 subtests passed, including the socket case; one existing websockets
deprecation warning remains. Compile, Ruff/format, mypy and eight frontend module
syntax checks passed. Secret scanning made no credential verification requests;
Bandit reported zero medium/high findings and 85 low findings. Offline Compose
validation passed seven services without starting containers. Quality evidence
hash: `24c010f658a9859670ae55b5822fd0936f5849bdda3b21dacfa9c10129446840`.

## Remaining release conditions

There was no RPC request, paid API call, key access, production service start or
transaction in this increment. Paid acceptance remains subject to the same
cumulative USD 0.10 ceiling and previously recorded eight requests. This change
establishes neither a funded historical basis nor full-account discovery. Fresh
USD prices, complete dedicated-account NAV, current production two-source
simulation/runtime verification, and real dual-service receipt/cash recovery are
still required. All capital/loss limits and release gates remain unchanged.
The local production Docker image is not rebuilt or deployed by this change.
