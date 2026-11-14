# Durable cycle shadow outbox

2026-11-14. Baseline: `8c91455`. Reviewer: Codex implementation self-review; no independent
review or audit is claimed. Evidence class: **synthetic fixtures and offline replay**.
This extends [per-filter event coverage](EVENT_COVERAGE_20261114.md); it does not supersede or
retry that record's stopped Base RPC experiment.

## Delivery contract

The normal producer `scripts.replay_cycle_shadow --queue-session NAME --chain-store DB` now
freezes the complete cycle config and shadow window under one session ID. Session registration,
verified coverage, the retained report and quote-replay tasks commit in one SQLite transaction.
A process exit during publication leaves no half-published cursor or job. Repeating identical
publication preserves stable task IDs and completed work. Each task binds the config/window hashes,
chain, exact block/parent/time, affected paths, size grid and the loaded strategy/evidence source fingerprint.
A source change in the running process, an unreadable source file, or a new implementation attempting
to consume an old session invalidates that session. This is a local source binding, not build attestation. Only a dispatch with a usable frozen
quote cutoff becomes work; an empty observed window and an unusable cutoff produce no tasks.

These are **historical, offline quote-replay tasks**, not trading proposals or permission to make
network requests. The data's frozen retrieval cutoff remains part of strategy replay. The worker's
processing clock governs only delivery leases; running an archived window today does not make its
prices current. Lease default is 30 seconds, allowed range 1–60,000 ms, at most eight attempts.

Before claim and completion, the consumer replays original range evidence, reconstructs the report
and task identity, and checks every required canonical header and per-filter block coverage. Missing
current inputs cannot be filled from old stored coverage. A processing-clock regression or corrupted
binding/report/task/delivery/result projection latches the session. There is no reset API.

Delivery is **at least once**. A crashed worker's expired lease permits another attempt of the same
job ID. Worker, attempt and lease expiry fence completion; the old worker cannot overwrite a newer
attempt. Identical completion is idempotent, while changed quote inputs cannot replace the prior
result. An expired lease, including one expiring during evaluation, cannot commit. Completion means
the bounded offline evaluation was stored, even when its decision is unavailable; it is not a fill,
settlement, profitable trade or exactly-once external effect.

The consumer calls the existing `evaluate_windows` with the task's exact block and affected routes.
It retains the quote-input manifest digest and result digest. There is no signer, RPC client,
arbitrary command execution, state override, broadcaster or background daemon in this worker.
Results and task envelopes keep `execution_eligible=false`.

## Invalidation and recovery

A SQLite trigger also covers canonical reorgs applied through an ordinary `CanonicalChainStore`
connection. It records invalidation and clears every affected session's pending, leased and completed
jobs in the same transaction that removes canonical blocks. Original reports and result bytes remain
historical records, but the current queue view hides invalidated results. A formerly completed result
therefore cannot remain a current candidate after a reorg.

If a producer batch itself encounters a canonical conflict/reorg, its coverage changes roll back and
all existing sessions on that chain are conservatively invalidated. The rejected branch must be
reviewed and recovered through retained canonical evidence separately; the queue does not silently
reanchor or resurrect old work. A missing quote-filter input after a ready report, a changed frozen
config/window, or normal-entry integrity failure also invalidates its session. Optional LP-discovery
coverage continues to follow the existing separate quote/discovery gate; `observed` in the outbox
means the quote filters are complete, not that missing discovery evidence has been invented.

Producer output-file failure is separate from publication: committed jobs survive and can be inspected
or consumed normally. A crash after durable completion but before exporting a worker report also leaves
the result in the queue; rerunning the worker does not repeat completed work. Exported JSON files are
historical snapshots, not a live queue view. Inspect the original queue again before consuming work.
SQLite and local hashes provide durable consistency checks, not independent provider honesty or a
host-compromise-resistant ledger. This is not a production release-control store.

## Retained normal-entry demonstration

Artifacts: `reports/research/shadow-outbox-20261114/synthetic-cli-final/`. The fixture uses artificial chain
`87654321`, blocks 100–101, one event wakeup and three mutually exclusive size points. It is generated
from the existing cycle-research fixture; it is not replayed market history or an untouched evaluation.

| Step | Observed result |
| --- | --- |
| Producer CLI | exit 0; one durable pending task |
| Worker CLI | exit 0; one completed task, three conditional quote points |
| Separate canonical writer changes block 101 | canonical reorg commits |
| Queue `show` CLI | exit 3, state invalidated, prior result hidden |
| Network and capital | zero RPC requests, no signing or broadcasting |

The fixture's `positive_conditional_quote` is an expected synthetic calculation, **not evidence of edge**.
The stable job ID is `fa24a9ab422d47665202c1dddbde448304e69937b9af5abad6774bc7aba6738f`.
Artifact SHA-256:

- producer: `712ebad20217fe421d9007b2a1772bf87d2055ef381d42b785946c31a69b5328`;
- completed worker: `734cd2593d9abccb59223b04f84f67fac71b08913532073c8febc85d45dda982`;
- invalidated view: `89635a80c927827f520e726f43fe40f3a4c21b789ac5904b33fb636d18598855`.

## Regressions and remaining work

Thirty-three new regressions exercise ordinary producer/consumer CLIs, quote replay, quiet/unready windows,
partial-to-complete coverage, missing-input regression, duplicate publication/completion, changed
quote inputs, process exits during publication and after claim, competing workers, expiry, stale
workers, eight-attempt exhaustion, clock regression, corrupt projections and evidence, and both direct
canonical-writer and producer-path reorgs. Source upgrade/unavailability regressions were added during self-review. Focused suite: 80 tests, exit 0.

The first regressions found JSON list/tuple representation differences causing false integrity faults
and inconsistent duplicate-return values. Canonical comparisons and returning the stored JSON form
fixed them. An early missing-cutoff test violated the existing all-window cutoff policy and was
corrected to a consistently unset fixture. Original failing logs remain `focused-first.log`,
`focused-second.log`, `focused-fourth.log` and `focused-final.log`; the passing focused log is
`focused-final-4.log`. The earlier `synthetic-cli/` demonstration and `make-check.log`
passed before source binding was added; they remain retained and are superseded by the final
source-bound demonstration and `make-check-final.log`. Raw fixtures and check logs remain ignored in `reports/`.

The queue is now integrated and synthetically verified. Real prospective windows, independent source
and time evidence, maker/oracle triggers, an equal-budget event-versus-polling comparison, exact
transaction prestate/settlement and untouched net-NAV evaluation remain incomplete. Existing stopped
RPC campaigns and capital authority are unchanged. The next research step returns to Metric's retained
builder-state/canonical-state relationship and the precise two-venue simulation requirements, while
keeping the Aave historical ledger and Elysium prestate gaps visible in the execution checklist.

While final checks ran, the original Titan capture was replayed again without network access.
`metric-next-dependencies.json` records the next dependency review: all 17 storage slots in each
selected snapshot belong to the Metric oracle; the other four accounts have balance/nonce fields
only. Canonical account roles, the parent hash and the meaning of those changes remain unverified.
No overrides were applied. The earlier Base campaign's original ledger was read back at **14/32**
with STOP intact; six retained capture/design/STOP hashes are unchanged.

## Final checks

- Focused regression: **80 tests**, exit 0; `focused-final-4.log`.
- `make check`: exit 0; **5,953 Python tests, 138 subtests, 9 frontend modules,
  70 Foundry tests**; `make-check-final.log` also retains lint, format and mypy results.
- `make security-check`: exit 0; dependency audit, Bandit and secret gate passed;
  `security-check-final.log`.
- Documentation: four documents, 38 local links, four shell snippets and both CLI help interfaces
  checked, exit 0; `docs-check.log`. Original stopped Base capture hashes and 14/32 ledger readback
  are retained separately; no RPC was made.
- Local environment remains Python 3.12.4, Node v23.11.0 and Foundry 1.8.4 Homebrew. CI pins Node
  22.14.0 and Foundry 1.8.1; local checks do not assert that CI has run.
- `make contract-security-check`: exit 0; 80 contracts, 102 Slither detectors, **0 findings**;
  `contract-security-check-final.log`.
- `git diff --check`: exit 0; reviewed paths contain only source, synthetic tests and documentation.
  No raw captures, credentials, key material or production state is staged.
