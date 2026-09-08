Oasis Agent production-readiness evidence package (Phase-1 LP system).
Date: 2026-09-08 (Asia/Shanghai)
Scope: deployment identity checklist tool + service integration interface document,
full repository gate re-run.

Contents:
  gates/python-check.txt    python-check: 744 passed + 108 subtests; ruff/mypy/format clean (204 files, mypy 102 source files)
  gates/security-check.txt  security-check: pip-audit clean, bandit 0 medium/0 high severity, secret gate passed
  gates/contract-check.txt  contract-check: 68 passed / 8 suites; forge fmt/build/lint clean

New deliverables verified in this round:
  - src/oasis_agent/deploy/checklist.py   DeployCheck / DeploymentChecklist pure classification;
      ten canonical check names must match exactly; PASS/FAIL require complete block-pinned
      evidence; identities_verified only when every check passes; execution_eligible always False.
  - scripts/run_deploy_checklist.py       Observe-only CLI: per-provider chain id / runtime keccak /
      hook permission bits / token metadata / PoolId initialization at an explicit block pin;
      evidence persisted with request/raw/interpretation hashes; exits 0 only when identities verified.
  - tests/unit/test_deploy_checklist.py   Validation negative cases + fail-closed checklist semantics.
  - docs/architecture/SERVICE_INTEGRATION_INTERFACES.md
      Integration contract for the service framework / frontend: data layer, evidence model,
      shadow service, readiness gates, execution layer (observe-only), deployment CLI contract,
      server P0-P3 API contract, and integration conventions.

Gate status unchanged: execution_eligible: false; lp_readiness WP0-WP8 not authorized. This
package is evidence for the designated third-party reviewer; it does not replace that signature
or authorize any production release, canary, or fund execution.

Verification: shasum -a 256 -c MANIFEST.sha256
