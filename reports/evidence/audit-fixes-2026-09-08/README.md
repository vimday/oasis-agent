Audit fix-round evidence package (Phase-1 LP system).
Date: 2026-09-08 (Asia/Shanghai)
Scope: remediation of all findings in the 2026-09-07 independent AI audit
(5 Medium + 13 Low) plus full repository gate re-run.

Contents:
  audit/2026-09-08-audit-fix-round.md   updated audit report incl. fix-round review section
  gates/python-check.txt                make python-check: 732 passed + 102 subtests; ruff/mypy/format clean
  gates/security-check.txt              make security-check: pip-audit clean, bandit 0 medium/high, secret gate passed
  gates/contract-check.txt              make contract-check: 68 passed / 8 suites; forge fmt/build/lint clean
  gates/contract-security-check.txt     make contract-security-check: slither 80 contracts, 0 result

Remediation summary (all verified in the updated report):
  M1  net-PnL cost coverage: failed_tx_cost + opportunity_cost added to backtest/counterfactual
  M2  release-gate evidence: readiness PASS now requires >=2 providers + single interpretation
  M3  signer nonce serialization: in-process mutex around check->sign->append; concurrency test
  M4  adversarial campaign: hookData / reentrancy / fee + tick boundaries / differential PoolId
      (OasisDynamicFeeHookAdversarial.t.sol) and malicious token classes / atomic rollback
      (OasisPositionExecutorAdversarial.t.sol)
  M5  executor x real Permit2 end-to-end integration suite (OasisExecutorRealPermit2Integration.t.sol)
  L1-L13 closed: evidence hash binding, features.py fingerprint, revert-reason assertions,
      PositionManager integration coverage expansion, HALT reset runbook, etc.

Gate status unchanged: execution_eligible: false; lp_readiness WP0-WP8 not authorized. This
package is evidence for the designated third-party reviewer; it does not replace that signature
or authorize any production release, canary, or fund execution.

Verification: shasum -a 256 -c MANIFEST.sha256
