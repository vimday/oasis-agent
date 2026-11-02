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
