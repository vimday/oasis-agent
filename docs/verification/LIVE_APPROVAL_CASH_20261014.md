# Successful bounded approval cash and allowance outcome — 2026-10-14

## Confirmed implementation

The direct loop no longer clears a successful approval from receipt status alone.
`live/approval_cash.py` requires a raw two-provider `live_approval_cash` proof that
binds the actual retained transaction metadata to the bounded approval intent,
the pre-sign wallet proofs, complete receipt gas and the closing wallet balances.

Only USDC-to-configured-Permit2 and Permit2-to-configured-PositionManager/router
approvals qualify. The native value must be zero, target and calldata hash exact,
amount within the configured allowance cap, and Permit2 expiry equal the decision
block time plus the configured duration. The closing USDC allowance must equal
the intended amount. Permit2's amount and expiry must match, its nonce must remain
unchanged from the opening allowance and both endpoint runtimes must match its
configured hash. A receipt-block-expired approval refuses.

The nonce behavior follows the checked-out canonical dependency:
`contracts/node_modules/@uniswap/v4-periphery/lib/permit2/src/AllowanceTransfer.sol`
calls `updateAmountAndExpiration` in
`src/libraries/Allowance.sol`; the latter modifies amount and expiry, not nonce.
Zero expiry has special block-timestamp semantics there; this adapter explicitly
requires the positive configured duration. These source files and the package
lock are hashed in the validation snapshot, not treated as moving upstream code.

Reverts and approvals share `live/cash_interval.py` primitives: exact EOA nonce
consumption, unchanged USDC, native debit equal to full gas, empty endpoint EOA
code, unchanged USDC runtime/implementation, and a complete transfer-free interval
of at most 128 parent-linked blocks. Receipt target and gas envelope are also
rechecked from retained cost bytes. Reverts still require status 0; successful
approval proofs require status 1 and their additional intent/outcome checks.
The existing revert payload schema and raw request order remain unchanged.

Only after both proofs retain and replay does the loop count gas, clear pending,
install the observed receipt wallet baseline and invalidate pre-action NAV.
The frontend's existing action-reference counts consume the resulting history
without claiming that the server independently replayed those proofs.

## Constructed validation

All new tests use synthetic raw RPC fixtures and a signer that must never be
called. They exercise USDC plus both configured Permit2 spenders, exact complete
gas, wallet conservation, code changes, wrong approval amount/expiry/nonce,
delegated-account code, provider disagreement, owner transfers, broken linkage,
wrong native value/calldata/spender, excessive amounts, missing baselines,
altered retained interpretation, foreign-action refusal and crash recovery with
one gas count. The API projection is checked against a resulting actual loop
state, including invalidated NAV and recorded cash-reference counts.

Logs and final source/log digests are retained under
`reports/review-20261014/approval-cash/`. No funded execution, live signer contact,
mainnet transaction, new chain observation or economic return is claimed.

Final validation passed: `make python-check` completed with 2,815 tests and 138
subtests, Ruff checks/formatting and mypy across 215 source files. The focused
regression completed with 141 tests, including 23 new approval cases. The frontend
syntax gate passed all eight local modules. Bandit reported zero medium/high
findings (70 low findings remain); the secrets gate passed without credential
verification requests. One existing `websockets.legacy` deprecation warning remains.

## Remaining boundary

Successful swap/mint/burn still need complete owner cash attribution, including
native refunds and internal calls. Their existing successful-action path still
resets wallet units. The separate transaction-policy signer and complete NAV/fee
valuation are not integrated into this direct loop. Passing an approval cash
proof does not establish strategy edge or authorize another transaction. Owner
capital and loss limits are unchanged.
