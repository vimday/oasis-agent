# Retained pre-sign wallet and reverted cash — 2026-10-14

## Confirmed implementation

The live loop captures two agreeing raw wallet snapshots before entering the
signing path. Each binds Base chain/account/USDC identity, the decision block
hash, undelegated EOA code, account nonce, native and USDC balances, token runtime,
implementation slot, decimals and opening/closing block headers. Retained
balances must equal the decision's balances. The actual signed transaction's
nonce must equal the observed nonce before its pending callback may publish.
The v2 pending plan hash includes both wallet references. An old pending hash
is refused, not silently rehashed around invented evidence.

`live/revert_cash.py` replays those proofs together with complete Base gas cost
and new receipt-block wallet proofs. It requires exactly one consumed nonce,
exact native gas debit, unchanged USDC, unchanged token runtime and empty EOA
code. Two interval captures cover every intervening block, with consecutive
heights, matching parent hashes, increasing timestamps, matching endpoint headers,
and no inbound/outbound owner USDC or position-NFT Transfer logs. The maximum
interval is 128 blocks. All calls use the existing persistent request budget.

Both raw proofs are retained and replayed before pending/counters change. The
proven receipt balances replace the previous unconditional baseline reset for
reverted actions. A crash before state publication can replay the unresolved
pending without counting the same gas twice. An unrelated subsequent wallet
movement remains visible to the next observation. This narrow adapter infers
native conservation from an undelegated EOA's exact nonce consumption and a
reverted transaction; it does not claim complete internal-call tracing.

Review also found unpinned, primary-only allowance reads in the action planner.
USDC and both Permit2 spender allowances now require equality from both providers
at the canonical decision block hash, including Permit2 expiration and nonce.
These are operational reads; they are not retained allowance outcome proofs.

## Constructed validation

All new evidence is synthetic JSON-RPC data. Tests cover balances, nonce gaps,
delegated-account code, changed token runtime/implementation/decimals, offsetting
token events, broken parent linkage, missing intermediate headers, intervals of
3/128/129 blocks, contradictory interpretation versus raw bytes, pre-sign capture
failure, callback publication and wallet-reference binding, and restart after
state-save failure. Allowance tests exercise actual ABI encoding/decoding and
block-hash parameters, both spenders, missing/expired allowances and disagreements
in allowance amount, expiration or nonce.

Commands, results and source/log digests are recorded in
`reports/review-20261014/revert-cash/validation.json`. The initial full Python
gate passed 2762 tests plus 138 subtests before the interval-boundary and allowance
review additions. Final checks are recorded separately; initial logs are retained.
No Solidity or frontend source changed in this slice.

The final focused live suite passes 128 tests. A sandboxed full run was unable
to bind temporary Unix sockets in six existing signer/socket tests; the
`PermissionError: Operation not permitted` outputs are retained in
`python-final.log`. The authorized run with local socket access is retained
separately as `python-verified.log`. These tests use temporary test keys, not the
operator's signer. Ruff/format and mypy pass; the final static security scan has
zero medium/high findings (70 low findings), and the secret gate passes.
The authorized final full Python run passes 2773 tests and 138 subtests, with one
existing websockets deprecation warning. Mypy checks 212 source files. The
dependency audit reports no known vulnerabilities in the unchanged lockfile.

## Unresolved production boundary

Successful approval/swap/mint/burn actions still need their own complete owner
cash and allowance outcome reconciliation. The direct loop still resets wallet
units after successful actions. It also lacks an independent intent-enforcing
signer integration, complete fee valuation and sustained observed net excess
return. This work changes none of the owner-approved capital/loss limits and does
not substitute the global risk ledger's different loss policy.

No mainnet transaction, live signing, private-key read or new live-market result
is claimed. Reverted-cash conservation and passing tests are not profitability
evidence or release approval. Operational details are in
`docs/operations/LIVE_START_CHECK.md`.
