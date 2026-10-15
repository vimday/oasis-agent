# Live receipt quorum continuation — 2026-10-13

The current baseline is commit `19b9cdb`. The October 8 amendment in
`docs/strategies/PHASE1_LP_EXPERIMENT_LOCK.md` records a one-day shadow gate
for the USD 500 canary; it supersedes the older economic-window requirements
for that canary only. This continuation changes neither that policy nor capital
limits, and makes no profitability or release claim.

## Confirmed local defects and repair

Seven new constructed counterexamples failed before the repair: disagreement
on effective gas price, payer, target, receipt height or chain-specific cost
presence still cleared pending; a missing secondary provider and a secondary
head with insufficient confirmation depth also allowed confirmation.

`LiveLoop._verified_receipt` now requires a secondary provider in live mode,
compares required receipt identity and execution-cost fields, compares optional
chain-specific cost fields including their presence, and requires confirmation
depth at both providers. Required fields and canonical nonnegative integer
quantities are checked before interpreting the receipt. Invalid status and
non-list logs refuse. Refusal preserves pending, gas counters and action counts;
restart coverage verifies persisted pending for each quorum disagreement.

Five further malformed-receipt cases cover absent height, negative gas price,
noncanonical gas quantity, status outside zero/one and malformed logs. The
focused suite passes 44 tests. These are local RPC stubs, not real-chain evidence.

## Validation

- Final Python suite: 2689 passed, 138 subtests passed; one existing websockets
  deprecation warning. Ruff, format and mypy pass.
- Frontend syntax: seven modules pass. Solidity tests: 70 pass; formatting,
  build and lint pass in the separate contract check.
- `make security-check`: dependency audit reports no known vulnerabilities;
  Bandit reports no medium/high findings (70 low findings); secret gate passes
  without credential verification. `make contract-security-check`: Slither
  analyzes 80 contracts with 102 detectors and reports zero findings.
- Initial restricted full check retains six Unix-socket permission failures;
  restricted dependency audit could not build its temporary environment. Final
  checks use automatically approved unrestricted execution to exercise local
  synthetic socket authority and public dependency services.
- Logs are retained in `reports/review-20261013/receipt-quorum/`, including
  initial failures. No independent external audit is claimed.

## Remaining limits and next work

Receipt consensus alone does not prove the signed transaction's nonce,
calldata, value or chain, complete native cost, finality, wallet settlement or
LP cash/NAV conservation. The direct live path still does not consume the
independent account observer or policy-controlled sidecar admission boundary.
Its NAV uses floats and principal-only position valuation. These require
separate engineering closure before claiming complete accounting.

The optional cost comparison is deliberately not a cost calculator: absent
cost fields on both providers are not evidence of zero cost. Complete gas
reconstruction remains the role of `execution/lp_receipts.py` and
`farming/transaction_costs.py`.

Only shadow files were present in the inspected `data/live` directory; no
`live.state.json` was present. This does not establish chain wallet state or
whether another process is running. No RPC acquisition, key access, signing,
broadcast, service startup, halt reset or live configuration change occurred.

Next integration should preserve the pending intent until the existing retained
receipt/cost evidence and independent account observation jointly establish its
outcome. It must preserve prior loss/reservations and refuse unexplained cash;
it must not initialize a replacement zero-loss journal to bridge the systems.
