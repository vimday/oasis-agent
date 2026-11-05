# Full retained settlement history before startup

## Confirmed gap

At commit `0dc9229c9a6b9e1748f800fb873f34bc2d63b3b9`, the production startup
inspection replayed a NAV/cash checkpoint but did not replay all journaled
settlements. Paired startup compared position identity alone. In a disposable
two-service fixture with valid raw two-source NAV, the old inspection accepted
each of these separately appended strategy-state changes: capital basis +1 USD
micro, external-flow total +1 USD micro, gas total +1 native unit with no
settlement, and an added settlement anchor with no historical action.
Both original fixture file sets were unchanged by that inspection.

Retained probe: `reports/review-20261105/startup-history/probe-before-v2.json`,
hash `78ef15e5b1272b0db2d108f2dbad7c8b2285ab1df29660570c2db4ceade11358`.
The four expected-refusal regression tests then failed against the old code;
their complete output remains in `startup-history/regression-before.log`.
This is an offline reproduction of a startup prerequisite gap, not a claim
that a real transaction bypassed the independent signer or current-chain gates.

## Corrected boundary

`live/startup_state.py` now revalidates the complete journal chain in a read
transaction at the previously loaded checkpoint's exact tip and decoded state.
It then passes every original journal row, the retained proof store and the
verified wallet nonce to the existing `rearm_history.replay_history()` verifier.
That verifier reconstructs both providers' raw receipts, payer/status, whole
native cost and action cash under each action's original supported configuration
epoch. It checks exact anchor/event coverage, transaction commitment, contiguous
settled nonce sequence, current checkpoint nonce and total gas. Bounded latest
history lists cannot hide earlier actions preserved by the journal.

Each service's report now includes `settled_history_replayed`, settlement count
and hash, and a financial-continuity hash binding original integer capital
basis, acknowledged external flow, whole gas, verified history, wallet nonce,
units and position to the current configuration. Paired startup requires both
services to agree on these invariants. Independently accumulated daily loss and
observation checkpoints retain their existing per-service loss checks; no lower
value is copied across, no baseline is reset, and no reconciliation is written.

The Docker entry's `state_preflight()` also rejects absent/unverified history,
malformed hashes, boolean/negative/oversized counts and disagreement between
the two service reports before its Compose start command. Both production CLI
paths run the whole-history refusal before reading RPC environment or exposing
RPC, socket or key-provider capabilities. Wire replay errors are redacted.

No price/fee/capital policy, signature boundary, budget, STOP/HALT or raw receipt
was relaxed. This is historical replay of journaled actions, not all-account
transaction discovery, current finality, fresh USD/NAV or live execution approval.

## Test credibility and retained corrections

Tests use two original disposable state/journal/evidence sets and synthetic raw
approval/cost/cash proofs. The accepted path has one approval with 218 native
units of complete gas and succeeds even after the latest bounded event history
is cleared, because the full journal retains it. Negative cases cover missing
and corrupted raw cost evidence, gas/anchor inconsistency, unexplained nonce,
one-sided capital/flow changes, a changed journal tip, malformed Docker reports
and both production CLI paths before network/signing/key access. Original file
and fixture budget bytes remain unchanged by accepted/refused inspections.

The first exploratory probe used the `/tmp` symlink alias; the existing path
identity guard correctly refused it. The v2 probe uses resolved disposable
paths. Both probe scripts are retained. Two intermediate targeted test failures
selected the wrong SQL kind spelling (`transaction-cost` instead of the actual
`transaction_cost`), affecting zero rows; the corrected tests assert that at
least both provider cost records were mutated. A later test incorrectly expected
an exception instead of each production CLI's existing redacted exit 3; that
assertion was corrected without changing the CLI's refusal behavior. The failed
logs remain `targeted-history-v2.log` and `targeted-v3.log`, separate from the
final accepted run. No failed result is counted as a pass.

## Remaining production acceptance

All fixtures and forthcoming Linux smoke packets are explicitly synthetic.
No original production state/key volume, budget or RPC is accessed in this
work. Serialized Linux packets exclude every budget file; the harness creates
new disposable synthetic ledgers and verifies they remain unchanged by reads.
This does not reset, copy, replace or consume the real paid RPC history.

The owner-recorded bounded release remains open since 2026-10-31. Fresh
production ETH/USD and USDC/USD under 1,800 seconds, full NAV, production gas/
runtime/simulation and current dedicated-account receipt/fund/unwind/restart
acceptance remain required. The existing USD 500 capital, USD 200 daily and
USD 500 cumulative limits stay intact. No production migration, rearm, service
start, funding, signing or broadcast is performed by this change's acceptance.

## Development acceptance

The final targeted run exited 0 (139 tests). The required `make live-quality`
then exited 0: **4,553 tests and 138 subtests passed**, with the ten existing
fixture/fork deprecation warnings; Ruff/format, Mypy (250 source files), eight
frontend modules, secret scan, Bandit and Compose checks all exited 0. Bandit
reported the same 98 low findings and zero medium/high findings. The separate
retained-result verification script checked every exit, timeout flag, canonical
report hash and log hash, and all **664** frozen runtime/test/config inputs.
No Solidity or financial policy was changed.

- Quality: `reports/review-20261105/startup-history-quality-v1/report.json`, hash
  `9ad8ec6ce3785c58e83b932ba30384b3559bca1eaf90875d28bb70af03050b97`.
- Separate local verification:
  `reports/review-20261105/startup-history/quality-review.json`, hash
  `8a582444b7e85e5c9f6a2846e17504d2e4f47dda39f9d7524e1e176e826ebad4`.
- Expected-before failure log hash:
  `d7d8004abe00b9009c35514d1ba42fa4e69c7a7db958fbc49612d12794cbf529`.

The separate verification script is a local result checker, not an external
security audit or a claim that another reviewer approved this change.

## Immutable image acceptance

Implementation commit: `88f92889b1d2218ffdfa0c28763d76d2adbba34b`.
Candidate image:
`sha256:099b90f83cf1a9551e91ff71af8db98149de5c3d069e326f00bd0ba3f8484b9f`.
All **360** application files and **362** archived Git inputs match that commit
and local bytes. Source manifest hash:
`f93bac9ff44a0f350ec563c8869743b3c3fc4008922e78f4d3ac645e70e70b2c`.
Build (`--pull=false`, existing hash-locked dependencies), stopped-image source
verification and the Linux smoke wrapper each exited 0.

The smoke ran as UID 10001, with network disabled, read-only root, all
capabilities dropped, no-new-privileges, bounded tmpfs/pids and **no mounts**.
Its six actual inspection CLI cases exited **[0, 3, 3, 3, 3, 3]**: complete
historical proof; then gas, original basis, unexplained nonce, missing raw cost,
and missing anchor. Every original fixture state, journal, evidence file and
new disposable fixture budget remained byte-identical through the inspection.
The accepted report passed the actual Docker adapter parser, while removing its
history-replayed field made that parser refuse before any start command.

This harness explicitly overrides the feed runtime with a synthetic fixture pin;
the override is not a production feed/source approval or a changed image file.
Its six serialized packets contain **zero budget files**. New disposable usage
ledgers are constructed locally for the test and no real ledger is read/copied.
Packet hash: `f8591b4cd36b6ececfa43d3142bf842ddfcf96c6eb3bde59a13458c1befa60e8`.
The full quality gate also retained the existing strict/legacy five-pin receipt
replay under an expanded eight-pin policy and its negative relabeling tests in
`test_live_config_epochs.py`; original cash proofs keep their first policy hash.

- Candidate report: `reports/review-20261105/startup-history-88f9288/summary.json`,
  hash `c30782193b3b7ddc05b571dc498f48025eeb88e2cd5c201b03f8a458ae305ff9`.
- Separate local archive/source/log/exit/smoke verification:
  `reports/review-20261105/startup-history-88f9288/reverification.json`,
  hash `46d3b1fc7e72ac6eaa90a5c1de53d17f37cad434eb907cc0b3935371e75646cb`.

The documentation-only follow-up changes no tested runtime, configuration or
test bytes. The four unrelated untracked verification documents are preserved
and excluded. The candidate was not deployed and none of these synthetic results
substitutes for the remaining current production acceptance.
