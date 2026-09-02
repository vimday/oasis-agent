# Initial Verification Record

**Date:** 2026-09-02
**Repository:** Oasis Agent local project
**Verification mode:** offline, deterministic fixtures only

## Commands run

```text
PYTHONPATH=src python3 -m unittest tests.unit.test_project_bootstrap tests.unit.test_evm_analysis tests.unit.test_v4 tests.unit.test_farming tests.unit.test_dynamic_fee tests.unit.test_arbitrage tests.unit.test_execution tests.unit.test_rpc tests.unit.test_evidence tests.unit.test_config_report -v
```

Result: **19 tests passed, 0 failures**.

```text
python3 -m compileall -q src tests scripts
git diff --check
```

Result: **passed**.

```text
PYTHONPATH=src python3 scripts/analyze_benchmark.py --fixture --output reports/benchmark-hook-analysis.md
cmp -s reports/benchmark-hook-analysis.md /tmp/benchmark.md
```

Result: **deterministic report output matched**.

```text
PYTHONPATH=src python3 -m oasis_agent.cli execute
```

Result: exits non-zero with `broadcast is disabled in the initial release`; no transaction is sent.

## Scope limits

- `pytest`, `ruff`, and `mypy` are not installed in the current environment.
- Editable dependency installation was blocked by local PyPI certificate verification failure while fetching build dependencies.
- Live RPC acquisition, fork execution, Solidity hook deployment, and out-of-sample profitability validation are not claimed by this offline milestone.
- The checked-in benchmark report is generated from deterministic fixture data and retains `confirmed`, `inferred`, and `unresolved` evidence labels.
- Execution remains disabled by default.
