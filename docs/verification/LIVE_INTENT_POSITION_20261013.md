# Live intent, position outcome and operational precheck — 2026-10-13

## Confirmed implementation

The direct canary path now derives public commitment fields from the actual
signed EIP-1559 envelope before broadcast. Pending retention binds the recovered
account, chain, nonce, target, value, calldata hash and gas envelope to the exact
configuration, action terms and prior position. State publication fsyncs both the
file and the parent directory after rename before returning to the broadcaster.
Resolution refuses missing legacy commitments or changed inputs. Raw signed
bytes and keys are not retained.

Mint/burn state transitions now require raw two-provider position outcome proofs
at the receipt block. Mint checks the NFT owner, complete pool identity, range,
subscriber flag and manager/core liquidity. Burn checks cleared NFT position info
and zero liquidity at the original core position ID. This follows checked-in
canonical `PositionManager._burn` and `StateView.getPositionLiquidity`, not an
assumption that a Transfer log alone proves economic settlement. Cost and position
proofs are replayed before pending clears; a refusal keeps the prior position and
counter values. Ordinary observations verify NFT identity and halt on unexpected
liquidity changes even inside the range.

Market observation now refuses heads over 120 seconds old or in the future,
future oracle updates, closing-header changes and provider disagreement on block
identity. Live preparation checks both provider chain IDs before signing.

`make live-check` is a read-only operational entry point. It requires no signer,
does not save loop state or reset latches, and exposes redacted blockers. Passing
operational checks is separate from execution permission and economic acceptance.

## Observed environment

The read-only command exited 0 against the configured two Base providers at block
`52555158`, hash
`0xd342fabfa40dfe8e06a45539144072810eee0e01fa8b2f070540a706cac48088`.
Its operational checks passed; `execution_eligible` and `signer_contacted` remained
false. The output is retained in
`reports/review-20261013/live-intent-position/operational-check.log`.
This is an instantaneous diagnostic without retained raw wire envelopes, not a
replayable account-risk frame or durable readiness assertion. No mainnet
transaction, key read or signer contact was performed.

## Constructed validation

Tests use local synthetic JSON-RPC fixtures and ephemeral random test keys. They
exercise actual envelope recovery, callback-before-broadcast ordering, failed
publication, retained pending drift, legacy missing commitments, conflicting
position state, offline raw-proof replay and contradictory raw responses,
restart-to-one-commit behavior, in-range position changes, stale/future/reorg
observations, and observational CLI behavior with no state creation or signer.

The focused live test suite passes 100 cases. Full validation logs and the final
source/log digests are in `reports/review-20261013/live-intent-position/`:

- `check.log`: complete Python, frontend and Solidity gates before the final
  directory-fsync regression; Python 2744 tests and 138 subtests passed.
- `python-final.log`: rerun after directory fsync; 2744 tests passed and one
  pre-existing campaign fixture expired its five-second quote during real
  subprocess startup. The retained decision states `fee_quote_expired`.
- `campaign-regression.log`: campaign and strategy tests after giving only that
  lifecycle fixture a 30-second quote (production defaults are unchanged).
- `python-verified.log`: final full Python gates after the fixture correction;
  2745 tests and 138 subtests passed (one existing websockets deprecation warning).
- `focused-final.log`: final targeted suite.
- `security.log`: dependency audit, Bandit and secret gate.
- `validation.json`: final exit statuses, summaries and source/log hashes.

No Solidity source changed. No fork result is claimed for these new paths; the
new position tests are raw-response synthetic tests, not funded execution.
Ruff, format, mypy (210 source files), seven frontend modules and 70 Solidity
tests pass. Dependency audit found no known vulnerabilities; final Bandit has
zero medium/high findings (70 low findings), and the secret gate passes.

## Explicit limits and next integration

The public commitment hash detects local drift but is not authentication against
an attacker who can rewrite the entire local state. It is not independent signer
policy. Position outcome does not prove owner cash conservation: the direct
loop still resets `last_units` after verified actions, and native refunds/swaps
need complete attribution before that baseline can be replaced by reconciliation.
The loop does not yet consume the global account-risk ledger; its approved
checkpoint/net-loss policy must not be silently replaced with that ledger's
gross-loss/exact-midnight policy. The digest signer remains narrower in isolation
but broader in transaction authority than a policy-enforcing signer.

No claim of net profitability, production acceptance, finality, automated unwind
or raised capital limits follows from this work. Operator usage and recovery
boundaries are in `docs/operations/LIVE_START_CHECK.md`.
