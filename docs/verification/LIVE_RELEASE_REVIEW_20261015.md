# Direct-loop release review and engineering delivery — 2026-10-15

## Outcome

The engineering changes are validated for commit, but the direct loop is not
released for new live transactions. The four remaining implementation gaps and
closure criteria are in [LIVE_RELEASE.md](../operations/LIVE_RELEASE.md). The
owner's existing canary authority is preserved; no funded execution, new signature,
mainnet transaction or profitable strategy result was produced by this work.

## Included fixes

- Raw two-provider receipt costs include Base L1/operator gas and bind to the
  actual retained signed transaction metadata. LP outcome proofs replay position
  ownership, identity and liquidity at the receipt block.
- Pre-sign wallet capture and approval/revert cash proofs preserve a verified
  balance baseline. Successful swap/mint/burn cost/position proofs alone now refuse
  settlement and preserve pending, counters and prior position.
- CLI and internal-action release gates precede signing. Recovery can inspect an
  already pending transaction without constructing a signer or starting new work.
  Absent receipt/unused nonce is insufficient to forget a signed live transaction.
- Strict trial-1 configuration bounds reject widened authority, wrong PoolId/token
  decimals, duplicate keys and coercion. State persistence rejects malformed risk
  values and uses unique temporary files with file/directory fsync.
- A bounded read-only API and dashboard expose timestamps, pending transactions,
  recorded losses/gas and release gaps. Missing/stale data never implies zero PnL
  or execution permission. The browser showed all four blockers with missing local
  state; no fake live results were inserted for the screenshot.

## Validation

The full checks ran on 2026-10-14, with browser/repository finalization on
2026-10-15 (Asia/Shanghai). The code was unchanged after those checks apart from
an explanatory HTML sentence, separately covered by the frontend syntax gate.

| Check | Result |
| --- | --- |
| `make check` | exit 0; Python 2,860 tests + 138 subtests; 1 existing websockets deprecation warning |
| Ruff / format / mypy | pass; 488 formatted files, 216 typed source files |
| Frontend syntax | 8 local modules pass |
| Foundry CI | 70 tests pass across 9 suites; no failures or skips |
| `make security-check` | exit 0; no known locked dependency vulnerabilities; secrets gate passes |
| Bandit | 0 medium/high; 70 low findings remain |
| `make contract-security-check` | exit 0; Slither 80 contracts / 102 detectors / 0 findings |
| `make live-check` | exit 0 for operational observation; `execution_eligible: false`, signer not contacted |
| Browser | four release blockers displayed on the actual missing-state view |

Local raw logs, screenshot and final source/log digests are retained under
`reports/review-20261014/release-closure/`. Operational observation is a diagnostic
snapshot, not retained raw-RPC replay or financial acceptance. The chain observation
and account-value diagnostic remain local rather than being published with code.

The tests that previously accepted a successful receipt without owner cash were
changed to require refusal. Raw cost/position replay assertions remain, and the
existing full approval/revert positive paths remain tested. Inner pre-sign tests
stub the outer release gate only to exercise their isolated boundary; separate
CLI and internal-action tests verify the actual gate refuses every new action.

## Scope still unresolved

Complete successful-action cash attribution; independent transaction-policy
signing; exact complete NAV/fee valuation; and append-only/reorg-safe execution
recovery remain engineering work. This delivery closes specific fail-open bugs
and prevents reaching those incomplete paths. It does not claim to complete the
user's entire autonomous live-trading objective.
