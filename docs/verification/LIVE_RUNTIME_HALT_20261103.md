# Stopped-service evidence review and HALT exit status

Reviewed on 2026-11-03 (Asia/Shanghai), starting from commit
`5242c1f83c34989e611744b04921228efc94ff8b`.

## Confirmed from the local deployment

Docker was available. Both existing services were stopped, using image
`sha256:0b4f8cb42deb23f066bc9a6e8a2adb85d7b7ea2ab94881f24013faf5d2ce3a18`.
Strategy finished at `2026-11-02T04:23:37Z` with exit 0 despite its persisted
`live observation refused; operator review required before polling` halt.
Authority finished at `2026-11-02T09:34:41Z`, approximately 24 hours after start,
with exit 0. This timing is consistent with its configured TTL, not proof of a
new healthy signing session.

A temporary diagnostic container had no network, key or authority-socket mount.
Both state volumes were mounted read-only. The existing journal validators ran
against read-only SQLite connections: strategy's 284 events and authority's 19
events passed hash-chain checks, and both JSON projections matched their tips.
Both describe NFT 3183370 with liquidity 208578797481196 and range
[-199660, -198680); neither has a pending transaction or signing reservation.
This establishes retained state, not the position's current on-chain status.

Both services' retained two-provider cash and native-cost proofs replayed for
five historical transactions: USDC approval, two Permit2 approvals, swap, and
mint. The mint is transaction
`0x61ff6184b17e8f398b9f72e9fb3849d0d24d36da3d0309cd9593aa41008f6ac0`,
Base block 53327238, hash
`0xd3aea3baf43219994f1c8e0e063689cab61fe4094945a6edd17d35feb98e7808`.
Position outcome proofs also replayed. No unwind or complete lifecycle acceptance
is established by these records.

Strategy's last NAV replayed to 499049180 USD micros ($499.049180) at Base block
53400462, hash
`0xf080ec0718c5a9f278cf1d75d0114697068b21651159cf66b6f513c657a7b18b`,
observed `2026-11-02T03:32:13Z`. This is historical, not current NAV or a profit
claim. The image's policy is ETH 1800s / USDC 90000s. Replaying with USDC 1800s
refused with `NAV USDC / USD feed incomplete, stale or future`; therefore these
records do not satisfy the current heartbeat's two-feed 1800s requirement.

All four budget chains passed validation in place. Current strategy counts were
900 primary / 743 secondary and authority 974 / 955, each against its existing
4096-per-source policy. Two older strategy budget files already existed (one
with primary exhausted); their prior retirement/replacement authority was not
established in this review. None was created, copied, reset, replaced or deleted.
Hashes of every mounted state-volume file were unchanged after inspection.
The strategy stop was not current-ledger quota exhaustion. Its redacted errors
do not establish whether the underlying failure was TLS, timeout or another RPC
refusal; that cause remains unresolved.

Raw diagnostic outputs and the exact offline scripts are retained in
`reports/review-20261103/runtime-halt/` (local, not version controlled):
`runtime-audit.json` and `historical-replay.json`. They identify each transaction,
block, proof pair, journal tip and original-file digest. All chain requests,
paid requests, signatures and broadcasts during this review were zero.

## Corrective change

`scripts/run_live_lp.py` now prints its final report and exits 3 whenever the run
ends with a persisted HALT, including a halt loaded at startup. Normal bounded
completion and operator STOP without a halt remain exit 0. The state latch,
report, resource cleanup and Compose `restart: no` policy are preserved.

Eight CLI regression cases exercise both live and shadow setup with injected
offline ticks, real state persistence and the real loop termination path. Before
the fix, the four halt cases failed because no nonzero exit was raised; the four
normal-stop cases passed. After the fix, the focused 169-case selection passed.
This is supervision correctness, not live trading acceptance. Existing containers
were not restarted, their halt was not cleared, and no image was deployed.

Full development quality checks exited 0: 3729 Python tests and 138 subtests,
Ruff/format, mypy, eight frontend modules, secret scan, Bandit and offline Compose
validation. `make contract-check` exited 0 with 70 tests, formatting, build and
lint passing. The existing websockets deprecation and Foundry signature-cache
write warning remain in the logs. Quality report:
`reports/review-20261103/runtime-halt-quality/report.json`; every command exit and
log hash was checked. Diagnostic manifest SHA-256:
`22d31d0c01dd026f689d6497dd5573fc66a5dff95f9e9c6be10bf9fcd4f9dade`.

## Follow-up: enforce the current owner's two-feed ceiling

The subsequent heartbeat again explicitly required a maximum feed age of 1800
seconds, consistent with the owner's direct answer that both ETH and USDC keep
1800 seconds. The committed canary configuration and `LiveConfig.validate()`
now enforce that limit for USDC as well as ETH. A separate USDC field remains
available for a stricter limit; a stable-band check is not a freshness exemption.
The earlier 90000-second observation and replay results above remain historical
evidence and are not reclassified as passing the stricter policy.

Regression coverage rejects operator-file ceilings of 1801, 5261 and 90000 for
either feed, rejects zero/non-integer values, and accepts stricter integer limits.
Synthetic two-provider NAV tests accept an age of exactly 1800 and reject 1801,
as well as the historically observed stale USDC ages. Before the correction,
six cases failed (including the committed-policy assertions); the corrected
151-case focused selection passed with exit 0. Original logs are retained under
`reports/review-20261103/strict-feed-policy/`.

This changes the configuration digest. An additional regression proves that an
existing journal bound to the 90000-second policy refuses binding to the stricter
configuration without modifying its files. No migration, journal reset, budget
replacement, container restart or image deployment is performed. Resuming an
existing position still needs an explicitly reviewed configuration transition
that preserves its history, plus fresh two-provider NAV under both 1800-second
limits. No RPC, signing or broadcasting is part of this correction.

Full follow-up quality checks exited 0: 3748 Python tests and 138 subtests,
Ruff/format, mypy, eight frontend modules, secret scan, Bandit and offline Compose
validation. Contract formatting/build/lint and 70 tests also exited 0. The
existing websockets deprecation and Foundry cache warning remain in raw logs.
Report: `reports/review-20261103/strict-feed-quality/report.json`; all command
exits and log hashes were verified. The focused before/after and contract log
manifest is `reports/review-20261103/strict-feed-policy/regression.json`, SHA-256
`71072817d308a04ee6f14aa514a7b66058d3326cf82124044d774cedf4d74361`.
The stricter parsed configuration digest is
`9e4ddbdf6d566fae6658043138f461a41808f7429c5ec0cd5c4149aab4de6717`.

## Offline inspection entry point

`python -m scripts.inspect_live_state --strategy-state /strategy/live.state.json
--authority-state /authority/state.json` inspects existing service histories from
read-only mounts. The two paths must identify different journals. An optional
`--config` selects the public target configuration; it is validated under the
current policy, never inferred from a legacy journal. Output contains journal
tips, configuration hashes, pending/halt flags and position identity hashes,
not raw signing intents, exception messages or the contents of a halt reason.
Recorded NAV is explicitly historical. The command does not open a budget,
RPC connection, signing socket or key; it cannot migrate, clear or release state.

`LiveJournal.load(read_only=True)` and `StateStore.load(..., read_only=True)` use
SQLite `mode=ro` without writable PRAGMAs. Existing callers retain their default
behavior. Each journal's full hash chain and state validation are still required;
a stale but previously committed JSON projection uses the verified journal tip
without replacing the file. A missing/empty journal or corrupt/foreign projection
is not accepted as a healthy empty account. The two services are separate read
transactions, not one atomic cross-service snapshot; run against stopped services
or independently quiesced volumes.

CLI exit 3 denotes malformed/unavailable evidence or a listed blocker. Exit 0
means only that the inspected histories match the supplied configuration with no
listed blocker. Every report has `execution_eligible: false`, and neither exit
proves current chain state, current NAV, receipt replay or economic acceptance.

The focused tests initially exited 1 because the SQLite test double's parameter
name collided with its `uri` keyword, not because the production read path failed.
The original log was preserved. After correcting the test double, 57 focused
tests passed (exit 0), covering read-only connection/SQL enforcement, byte
preservation, stale projection recovery, missing/deleted/corrupt journals, old
configuration binding, pending/signing/legacy-cost flags and position mismatch.

The new source files were then mounted read-only into the previously verified
candidate image for an integration probe (this was not a rebuilt image). The
temporary container had no network or key/socket mount and read the two original
state volumes read-only. Both journals verified, both position hashes agreed and
both had no pending transaction or signing attempt. The command exited **3** with
`strategy_halted`, `strategy_configuration_mismatch` and
`authority_configuration_mismatch`, as expected. It did not claim readiness.
Its report hash is
`0054a07366ad6616dfc9f2b7986a816b04b89c0fcd7ef9c7dae717bf398116f0`.
Original outputs are under `reports/review-20261103/offline-state/`. This makes
the migration blockers reproducibly inspectable but does not perform a migration
or acquire fresh NAV. No live service, journal or budget was changed.

Full development validation exited 0: 3761 Python tests and 138 subtests,
Ruff/format, mypy, eight frontend modules, secret scan, Bandit and offline Compose
checks. Contract formatting/build/lint and 70 tests also exited 0. Existing
websockets deprecation and Foundry cache warnings remain in the logs. Quality
report: `reports/review-20261103/offline-state-quality/report.json`; all exits and
log hashes were checked. The focused/integration evidence manifest hash is
`1eb2f2a6c6b642ffa0f5fbc3d0d7f434b56bb405a36000ed2a49f9eb6b23bb87`.

## Release authorization is not current runtime readiness

The dashboard previously rendered `金丝雀已放行` whenever the static release
record was open, including when the same response reported a HALT, stale
observation, unresolved signature or pending receipt. A Node execution of the
real renderer reproduced the misleading pending-receipt banner before the fix
(exit 1, retained in `reports/review-20261103/release-status/before.log`).

The banner now identifies that record as a recorded release authorization and
leads with unmet runtime conditions for blocked or unavailable observations.
Even a recent observation explicitly leaves current execution eligibility
unverified. The recorded USD 500 / 200 / 500 limits and release evidence remain
visible. This is presentation only: no release flag, signing/broadcast path,
production service, state journal, RPC budget or feed threshold was changed.
No live acceptance or recovery is established by this correction.

The corrected focused tests exited 0. Full development checks exited 0 with
3761 Python tests and 138 subtests, eight frontend modules, Ruff/format, mypy,
secret scan, Bandit and offline Compose validation; all four quality-check
exits and log hashes were verified. Contract checks exited 0 with 70 tests.
The existing websockets deprecation and Foundry cache-permission warnings remain
in the raw logs. Quality evidence hash:
`49ca0e7a3fbd2f24ea14a9395294f9913295db53c3800cb8565456a0a5c51500`.
Regression summary: `reports/review-20261103/release-status/summary.json`, hash
`d62434dbb7140cffa4deb065e56d02a901f3f455bd4f75e0b1ba376b134c01ef`.

## Operational precheck: preserve journals and verify their configuration

The operational precheck previously loaded an existing journal without supplying
the target configuration digest and used the writable SQLite read path. An empty
retained baseline could therefore pass precheck with a foreign configuration;
the production signing/start path's separate configuration binding still
refused it. A read-only mounted journal also could not reliably use that path.

Precheck now verifies the full journal against the validated target configuration
through SQLite `mode=ro`, before any chain observation. Invalid bindings or
corrupt retained state produce a redacted `retained_state_unresolved` refusal.
No journal creation, migration, latch clearance or budget reset is performed.
Other recovery callers retain their existing default connection behavior.

The initial regression run reproduced the configuration and writable-connection
defects. Its corruption fixture initially violated the digest uniqueness
constraint before reaching the code under test; that fixture was corrected to
corrupt exactly one event. The first focused run also hit sandbox denial of the
existing local Unix socket tests. Both raw failed logs remain under
`reports/review-20261103/precheck-binding/`; the corrected focused run exited 0
for 34 tests with the required local socket permission.

A no-network, read-only, non-root temporary container mounted the original
strategy state volume and four updated source files over the previously verified
candidate image. The real precheck refused the old configuration before an RPC
object was needed, exiting 3 as expected; all original file hashes remained
unchanged. No key, authority socket or writable production volume was mounted.
This was a source-mounted probe, not a rebuilt or deployed production image.
It establishes safe refusal, not current NAV, migration readiness or live release.

Full development validation exited 0: 3764 Python tests and 138 subtests,
Ruff/format, mypy, eight frontend modules, secret scan, Bandit and offline
Compose checks. All four command exits and log hashes were verified; contract
checks exited 0 with 70 tests. Existing websockets deprecation and Foundry cache
warnings remain in the raw logs. Quality report:
`reports/review-20261103/precheck-binding-quality/report.json`, evidence hash
`01712719fa7bd7474ac16761e8fb4cea85a3f98bc4801102638d8acc01493c46`.
The regression and retained-probe summary hash is
`9a6e46a622261c333a62ac56816a3c14e82b7bd2faf9e73968b58c84b35960d8`.

## 2026-11-04: offline evidence review for the strict-feed transition

The old service journals remain bound to their historical USDC 90,000-second
configuration. `scripts/review_live_feed_transition.py` now reviews the evidence
needed before migrating that binding, without changing either journal. It
recognizes exactly that one historical configuration difference; the target
must keep both ETH/USD and USDC/USD at 1,800 seconds. Other configuration
changes are refused. Historical replay does not relax the current live gate.

The command verifies both journal chains through read-only SQLite snapshots,
replays each service's retained NAV or last settled cash checkpoint, and checks
matching position identities. When supplied with new retained NAV references,
it requires current two-provider proofs under the strict target, unchanged
wallet units and position, and the same block and valuation across services.
The loss preview preserves the funded basis and external flows, applies the
existing conservative daily/checkpoint convention, and refuses exhausted loss
budgets. It never clears a halt, applies a migration, acquires data, signs or
broadcasts. A successful evidence review is not a release permit. Current
settlement finality and full receipt/unwind acceptance remain separate gates.

Example, with original volumes mounted read-only at `/strategy` and `/authority`:

```sh
python -m scripts.review_live_feed_transition \
  --strategy-state /strategy/live.state.json \
  --authority-state /authority/state.json \
  --strategy-evidence /strategy/live.receipts.db \
  --authority-evidence /authority/evidence.db
```

Optional `--fresh-nav` names JSON with `strategy` and `authority` reference
arrays, each pointing to raw proofs already retained in its own evidence store.
There is deliberately no apply flag. Missing fresh evidence exits 3.

**Confirmed retained result:** the updated source was mounted read-only over
the previously verified candidate image, in a non-root temporary container with
no network, keys or signing socket. Both production services were stopped.
Strategy's retained NAV checkpoint and authority's last settled cash checkpoint
replayed successfully. The review then exited **3**, with only
`strategy_fresh_strict_nav_required` and `authority_fresh_strict_nav_required`
as its evidence-review blockers. These are not an exhaustive production-release
checklist: configuration application, HALT review, current finality and unwind
remain unresolved. All 13 original volume files, including the budget ledgers,
kept the same hashes. No production image or service was changed. The report
hash is `70ec8dbf24f84d293d95980355057e1bfab63bf7b3398201eaf71cb8c2e8efe3`;
raw outputs are under `reports/review-20261104/feed-transition/`.

The first focused run failed because the new test module did not import the
existing synthetic feed-runtime fixture; it reached the real runtime pin and
correctly refused fixture bytecode. Initial mypy checks also identified missing
annotations. Both were corrected, without changing production feed pins.
The corrected focused run exited 0 for 45 tests, including the retained-state
and checkpoint suites, cross-service head disagreement, same-day/day-boundary
loss continuity, stale proofs, pending signatures and unchanged source files.
The initial failure log remains preserved as `focused-initial.log`. Synthetic
tests establish code behavior, not current USD/NAV or funded live acceptance.

Full development validation exited 0: 3,782 Python tests and 138 subtests,
eight frontend modules, Ruff/format, mypy, secret scan, Bandit and offline
Compose checks. All four quality-check exits and retained log hashes were
verified. Contract formatting/build/lint and 70 tests exited 0. Existing
websockets deprecation and Foundry cache-permission warnings remain in the
logs. Quality evidence hash:
`a98a1bf1be20f9f4c200698d3657a6ba8c99b6e881be0d39c9e1c7bb4765bab5`.
The per-command evidence manifest is
`reports/review-20261104/feed-transition/summary.json`; the initial focused
failure is retained as a failed log with an unavailable original tool exit
code, rather than inventing that exit code. No RPC request, signature or
broadcast was made during this work.
