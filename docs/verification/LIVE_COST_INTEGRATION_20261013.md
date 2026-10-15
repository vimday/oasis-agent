# Live pending costs — 2026-10-13

## Confirmed change

The live loop previously accumulated only `gasUsed * effectiveGasPrice` for
confirmed and reverted actions. It now uses the existing raw-RPC native-cost
producer before completing either transition. Base L1 and operator fees are
included. Two raw proofs are retained and bound to the applied receipt's
identity, block, logs and gas fields; disagreement or failed persistence leaves
pending and account/position counters unchanged.

The shared collector was extracted from `observe_receipt`; that manifest-based
producer still binds payer/native units before retention. Both entry points
now check that the factory's source aliases are the requested providers.
No new gas formula, loss policy, strategy parameter or capital limit was added.

`run_live_lp.py` constructs the reader, its evidence database and durable
request budget for live mode. It loads none of these for shadow mode. Older
execution-only gas history is preserved and explicitly flagged incomplete.

## Constructed validation

New tests exercise the actual pending state machine and CLI with local
JSON-RPC response fixtures and a signer that raises on any signing attempt.
They cover success/revert whole cost, mint/burn state transitions, raw proof
replay, different operator/L1 costs, foreign chain, moved/missing block/receipt,
receipt-to-proof substitution, persistence failure redaction, restart after
state-publication failure, missing Base L1 fee, missing reader, frozen request
budget, duplicate endpoints, and old gas-history preservation.

In the accepted Base fixture, execution gas is 200 wei, L1 fee is 7 wei and
operator fee is 11 wei. The stored total is 218 wei for success and revert.
This is synthetic engineering evidence, not a live transaction or measured
profit. The CLI test uses two local raw-response fixtures and makes no network
or signer call. Validation logs are retained in
`reports/review-20261013/live-cost-integration/`.

Final checks:

- Focused live/cost tests: 81 passed, including 21 new cases.
- `make check`: 2710 Python tests and 138 subtests pass, with one existing
  websockets deprecation warning; Ruff, format, mypy (207 files), frontend
  syntax (seven modules), Solidity build/format/lint and 70 Solidity tests pass.
- `make security-check`: no known dependency vulnerabilities, no medium/high
  Bandit findings (70 low findings), and secret gate passes without credential
  verification. These checks used automatically approved execution for local
  test sockets and public dependency data; no real authority was invoked.
- The first focused run correctly exposed the old expectation that live receipt
  quorum alone cleared pending; it was changed to require cost evidence, and new
  accepted-path tests exercise full raw retention rather than bypassing that gate.
  An import-order lint issue was corrected before the final checks.

No contract code changed. The same-day Slither result (80 contracts, 102
detectors, zero findings) remains prior validation, not a new run here.

## Unresolved integration

The direct live path still has no full transaction-envelope binding or
independent wallet/LP settlement, and it does not consume the persistent
global account-risk observer. The approved canary's checkpoint/net-loss policy
also differs from that observer's conservative gross-loss and exact-midnight
policy; those cannot be silently treated as equivalent when connecting them.
Native LP cash and opening/closing position inventory need their own raw proof
coverage before an independent account frame can close the transaction.

This work performs no acquisition against real endpoints, service startup,
real key access, signing or broadcast, and grants no new release authority.
Operator behavior and recovery limits are documented in
`docs/operations/LIVE_RECEIPT_COSTS.md`.
