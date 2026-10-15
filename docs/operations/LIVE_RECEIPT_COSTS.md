# Retained receipt costs in the live loop

Live pending resolution requires a `RetainedReceiptCostReader`. The operator
CLI `scripts/run_live_lp.py --mode live` wires it to the same two endpoint
configurations used by the live loop, under aliases `live_primary` and
`live_secondary`. Identical endpoint strings are refused; different endpoint
strings alone do not establish independent providers.

After receipt identity, canonical-block and confirmation-depth checks, the cost
reader acquires fresh raw receipt/header evidence from both providers. It reuses
`collect_receipt_cost_quorum` and `TransactionCostCapture`: on Base the total is
execution gas plus L1 data fee plus the block-hash-pinned operator fee. A revert
spends gas too and follows the same path. Raw receipt identity, gas fields and
logs must still match the receipt the live loop is about to apply.

Files under the configured state directory:

- `live.receipts.db`: the existing `transaction_cost` evidence schema plus
  `live_position_outcome` mint/burn proofs, `live_wallet` snapshots and
  `live_revert_cash` interval proofs and `live_approval_cash` successful approval
  proofs, with raw request/response envelopes,
  provider aliases and block-bound references.
- `live.receipt-budget.db`: a persistent acquisition budget of 4096 requests
  per source, at least 3000 ms apart. A restart preserves usage and the frozen
  policy. This budget covers wallet, reverted/approval cash, cost and position readers, not other
  live RPC polling.
- `live.state.json`: existing pending/position state, with `native_gas_units`
  and full evidence references on each resolved action's history entry. A live
  pending item also retains both pre-sign wallet references in its v2 plan hash.

The cost proofs are persisted and read back before gas, action count or pending
is changed. A missing receipt, provider disagreement, changed block, missing
Base L1 fee, exhausted acquisition budget or persistence failure keeps pending
unresolved. A legacy pending item without its target address also refuses;
do not clear it simply to permit a new transaction.

If publication of the state file fails after evidence retention, restart from
the unchanged state file and reacquire the proofs. The retained records do not
themselves increment the gas counter. This preserves exactly-once counting
across that crash boundary. The state file remains the existing atomic-replace
store; this change does not provide a hash-linked live execution journal.

Older live states may contain only execution gas in `gas_spent_wei`. Loading
an older state with a nonzero total sets `legacy_gas_cost_incomplete=true`.
The old total is preserved, new complete costs are added, and both report paths
expose that flag. No historical fee is guessed or backfilled. The counter covers
resolved actions recorded by this loop, not every transaction the account sent.

Shadow mode has no live cost collector. Paper gas remains an estimate. This
integration alone does not authenticate transaction calldata/value/nonce; the
additional signed commitment and position binding are described in
`LIVE_START_CHECK.md`. Reverted transactions additionally require exact wallet
cash conservation through a bounded, parent-linked two-provider interval; their
receipt balances become the next wallet baseline. Successful bounded approvals
now require the same conservation plus the exact intended allowance outcome.
Successful swap/mint/burn still lack complete wallet/LP cash reconciliation.
Neither path values fees accrued by a
position or updates the global account risk journal. Receipt depth is not
Ethereum finality. These remain separate
requirements; a gas proof is not an execution permit or a profitability result.
