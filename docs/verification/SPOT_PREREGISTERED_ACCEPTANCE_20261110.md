# Pre-registered acceptance of a shadow spot baseline experiment

Scope: offline engineering with synthetic fixtures only. No provider, wallet, RPC, key or
production state was touched. This record does not claim a real-market result, edge, net
return or promotion.

## Problem reproduced at 34864bd

At 34864bd, `scripts.run_spot_experiment` took the window as arguments after capture and
checked only `session_complete`. In the independent probe recorded in
`ceo-readiness-20261109.json`, one archive head produced a report both under its declared
window and under a start moved one day earlier. A zero-cycle `budget_stop` replayed as a
complete session with 0 samples. Both exited 0.

## Change

- `src/oasis_agent/launch/spot_plan.py` defines a strict plan,
  `oasis.spot-experiment-plan.v1`. It names the fixed comparator, a window `[start, end)` and a
  label cutoff. It reuses the experiment's own window contract (`check_window`).
- `scripts/run_spot_agent.py --plan` starts a shadow research run. It binds the normalized
  plan and its digest into `session_start`, which already carries the code identity, the
  strategy config and the run policy digest. The binding happens before the first provider
  call. The run refuses to start when the window is already open, when the plan is malformed,
  when the archive is unavailable, when the record cannot be read back, or when the archive
  already binds that plan. Ordinary shadow runs are unchanged.
- `scripts/run_spot_experiment.py --plan` performs strict acceptance. The window comes only from
  the plan bound to the selected session. Window arguments, `--allow-prefix`, a mismatched,
  moved or duplicated plan, and an edited binding are all refused (exit 2). The record says
  `accepted` or `blocked` (exit 3, still written) and lists every coverage gap. It counts
  cycles by phase and kind, the primary sample denominator, every label state and the embedded
  fixed-baseline report. Every output keeps `execution_eligible: false`.
- Exploration of sessions started without a plan works as before. The report now says
  `window_basis: analysis_time_arguments` (experiment schema v3). Exploration refuses a session
  that has a bound plan.

The decision modules named in the capture code identity are byte-identical to 34864bd, so
scoring, the baseline, positions, exits, call budgets and wallet logic are unchanged.

## Reproduction

```bash
.venv/bin/python -m pytest tests/unit/test_spot_plan.py -q
```

The test runs the real shadow `run_loop` with scripted providers through capture, replay and
the fixed no-smart-wallet-bonus baseline. Two of its tests run the real `main()` entry point.
Each case uses its own temporary directory. Cycles run about 900 s apart from the fixture
time `NOW`. The plan covers window `[NOW+600, NOW+7200)` with label cutoff `NOW+11400`.

| Case | Run exit | Status | Blocking | Cycles | Primary samples | Labels |
| --- | --- | --- | --- | --- | --- | --- |
| 14 ticks | 0 | accepted | none | 14 | 2 | 2 observed |
| budget 1, zero cycles | 0 | blocked | window_start_not_observed, no_discovery_cycle_in_window, label_cutoff_not_reached | 0 | 0 | none |
| 6 ticks, early end | 0 | blocked | label_cutoff_not_reached | 6 | 2 | 2 observed |
| ticks raise from cycle 4 | 3 | blocked | label_cutoff_not_reached | 8 | 2 | 2 unobserved |
| discovery fails in window | 0 | blocked | no_discovery_cycle_in_window | 14 | 0 | none |
| budget for manage-only | 0 | blocked | label_cutoff_not_reached | 14 | 2 | 2 unobserved |

These further cases are refused or blocked in the same test file:

- the original probe's window moved one day earlier (`plan_mismatch`);
- another session's plan, and a plan copied into a second session;
- an edited, stripped or re-digested binding;
- a prefix with its end record removed, and a lost capture write;
- a plan bound after its window opened;
- a binding write failure or read-back mismatch, which makes zero provider calls.

The accepted case repeats to an identical record, and acceptance leaves the archive, journal
and raw files byte-identical.

## Gates

| Command | Result |
| --- | --- |
| `make check` | exit 0. Ruff, format and mypy (282 files) clean. pytest 5523 passed and 138 subtests. Frontend gate passed. Forge 70 passed. |
| `make security-check` | exit 0. pip-audit clean. Bandit found no medium or high issue. The secret gate passed. |
| `make contract-security-check` | exit 0. Slither found 0 results across 80 contracts. |
| `git diff --check` | exit 0 |

## Evidence still missing

- A real shadow capture (provider claims) under a plan frozen before its window, with sources
  and request allowance confirmed for that run.
- An independent time proof of the plan. The archive orders the plan only by its local hash
  chain and the host clock, so a consistent rewrite of the whole archive is not ruled out.
- Enough independent tokens per group, executable exits, fees, gas, slippage and depth, an
  untouched later window, and ablation of the signal source itself.
