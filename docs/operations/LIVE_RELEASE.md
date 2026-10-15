# Direct Base canary release boundary — 2026-10-14

**Current result: new live transactions are blocked.** Engineering fixes and a
Git push are not a completed production deployment. `make live-readiness` prints
the same four concrete blockers displayed by the console and enforced before
new actions. It does not read a key, connect to RPC, or create trading state.

## Existing authority and evidence

The owner's 2026-10-08 trial-1 exception remains in force: the dedicated Base
account, USD 500 committed capital, USD 200 daily checkpoint gross loss and
USD 500 cumulative funded-basis net loss. The recorded one-day paper window is
not rescinded or represented as newly observed. The waived long OOS window and
external audit are not reintroduced as canary prerequisites. The exception did
not make absent accounting, arbitrary digest signing or float accounting correct.

## What must actually be completed

| Gap in the direct loop | Required implementation and evidence |
| --- | --- |
| Successful swap/mint/burn owner cash | Two-provider retained native-value/internal-call/refund and ERC-20 flows; bind to the actual signed action, complete bounded block interval and position outcome; zero residual before settlement; both swap directions, mint refund and whole burn tested on a pinned local fork. |
| Independent transaction authority | Decode and authorize the full transaction outside the strategy process; enforce account, target, recipient, calldata, nonce, deadline, code, fee/value/capital/loss limits and fresh simulation. Existing executor sidecar is a different calldata/accounting contract and is not interchangeable with this direct router loop. Keep key material out of agent/server processes. |
| Exact account valuation | Replace binary-float risk/PnL arithmetic with explicit integer/Decimal rounding; include uncollected fees, identify oracle assumptions and native transaction costs; reconcile external flows without erasing losses. Preserve the owner's distinct daily gross and cumulative net conventions. |
| Durable execution and recovery | Append-only configuration-bound journal; recover signatures/nonce reservations across crashes; detect post-settlement reorgs and block subsequent actions on inconsistent history. A three-block receipt depth and atomic JSON snapshot alone do not implement this. |

These are code/integration work, not missing confirmation clicks. They cannot be
closed by setting a JSON `approved` field, changing a loss cap, checking a box,
contacting a digest socket or publishing a commit. A fresh two-provider operational
check and an exact-version local-fork lifecycle remain necessary after integration;
synthetic tests do not prove live operation or economic return. Funded execution
must use the separately provisioned canary account and operator-controlled signer.

## Fixed in this review

- The CLI and internal action path refuse new live execution before signing. The
  offline report and console make these implementation gaps visible.
- Successful cost/position proofs alone no longer clear pending or reset the
  wallet baseline. Existing approval/revert cash reconciliation remains usable.
- `make live-reconcile` runs recovery without creating a signer or proceeding to
  another action. A missing receipt/unused nonce cannot discard live pending.
- Configuration rejects duplicate/extra pool keys, numeric coercion, inconsistent
  PoolId, wrong token units and widening the existing trial's risk parameters.
- State read/write rejects non-finite financial inputs, negative/ambiguous counters,
  malformed wallet checkpoints, duplicate keys, oversized/nonregular reads and
  symlinks. Unique owner-only temporary files replace the predictable `.tmp` path.

The prior receipt cost, signed-intent binding, two-provider position/wallet proofs,
approval/revert reconciliation and observation console changes are included in
this engineering delivery. None of these changes prove profitable live trading.
