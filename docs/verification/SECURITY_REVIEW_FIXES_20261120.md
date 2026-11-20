# 2026-11-20 profit-engine security review fixes

## Scope

Reviewed the Wave 1-22 profit-engine change `5b66a13..208e3e8` (129 files, +74,729 lines) and
fixed the confirmed issues below. All changes remain research/offline or shadow scoped:
`execution_eligible = false`, no signing, broadcast, transfer, or deployment authority is granted.

## Fixed

1. **Intent vault arbitrary calldata drain.** `OasisIntentVault` no longer forwards `SwapStep.data`
   to an allowlisted router. It calls the typed `IOasisSwapAdapter.swapExactInput` interface with
   the vault as recipient, requires `minOut > 0`, and verifies both the adapter return value and
   the vault balance delta. Regression: `test_executeReactorFillIgnoresArbitraryRouterCalldata`,
   `test_swapStepRejectsZeroMinOut`.
2. **Unbounded ERC-1271 deadline.** Quotes are rejected when `deadline > block.timestamp + 300`.
   Regression added to `test_erc1271DomainSeparatedSignatureLifecycle`.
3. **Deployment role collapse.** `DeployOasisProfitEngine` and `DeployOasisContracts` now require
   all cold roles to be distinct and `quoteSigner != operator` by default. The documented
   solo-operator profile requires the explicit `OASIS_ALLOW_SOLO_ROLE_COLLAPSE=true` opt-in.
4. **Rehearsal bypass.** Rehearsal is now a server-wide mode, not a per-proposal client flag.
   `AtomicExecutionPolicy.rehearsal_only` is mutually exclusive with `release_gate_open`, and
   `run_profit_authority --allow-rehearsal-loopback-anvil` refuses a non-rehearsal policy.
5. **Nine-gate readiness was advisory only.** Live `_release_is_open()` now verifies an emitted
   `oasis.profit-engine-readiness.v1` report (9/9 gates, self-hash, `execution_eligible=False`).
   `run_profit_strategy --mode live` requires `--readiness-report`.
6. **Pending signature did not block a new signature.** The authority derives
   `has_unresolved_pending` from its own unsettled `signed_decisions`, not from the client flag.
7. **Silent settlement after state loss.** The authority exposes `settled_tx_hashes` from its
   reconciled settlement journal; the strategy clears a local pending transaction only when that
   hash is present. Absence from the pending list no longer implies settlement.
8. **Key-bearing authority had network egress.** `profit-authority` runs with
   `network_mode: none` and no RPC environment; `verify_profit_compose` enforces it.
9. **CI did not build the profit-engine deployment.** Added a `profit-deployment` job that runs
   `make profit-deployment-check` and builds `deploy/profit-engine/Dockerfile`.
10. **Duplicate pool snapshots were silently overwritten.** `PoolGraph::upsert_pool` rejects a
    same-block conflicting snapshot and an older-block stale snapshot.
11. **Small-sample LCB visibility.** OOS summaries now expose `oos_lcb_sample_floor=5` and
    `oos_lcb_min_sample_met`, so callers can see whether the normal-approximation LCB has at least
    five settled trades. The readiness gate keeps its separate `min_oos_trades=5` default; the BBO
    full-size fill model remains a documented upper-bound assumption.

## Residual release blockers

- Settlement receipts and dual-provider simulations are still supplied by the strategy client.
  The signing authority is intentionally offline and cannot independently fetch or verify them.
  A separate network-capable verifier is required before any live release.
- Owner scope signoff remains a free-text attestation. A cryptographic owner signature and expiry
  bound are still required for Gate 9.
- `sweepProfit` lets the hot operator move principal to the immutable `REVENUE_SINK` (denial of
  service, not theft). This is retained as the documented profit-collection path.
- The production runner still only performs status sync; `submit_dual_simulated_intent` has no
  non-test caller. The live signal-to-sign-to-broadcast path is not wired.

## Verification

- `make lint format typecheck`: pass (ruff, 879 files formatted, mypy 359 sources).
- `uv run python -m pytest`: 6,514 tests collected, full suite exit 0.
- `OASIS_REQUIRE_FOUNDRY_ARTIFACTS=1` ABI/authority/readiness suite: 37 tests passed.
- `make security-check`: pass (pip-audit 0 vulnerabilities, bandit 0 High/0 Medium, secret gate).
- `make contract-check`: 17 suites, 125 tests passed; `forge fmt --check` and `forge lint` pass.
- `make contract-security-check`: exit 0; informational findings only.
- `make rust-check`: fmt, clippy, and 18 tests passed.
- `make frontend-check`: pass (9 local modules); `make profit-deployment-check`: pass.
- `git diff --check`: clean.
- Docker image build was not executed locally because the Docker daemon is unavailable; the new CI
  `profit-deployment` job builds `deploy/profit-engine/Dockerfile` on every push.

## Authority boundary

No command in this record grants live trading authority. A future live release still requires the
owner-scoped capital approval, 9/9 readiness report, `policy.release_gate_open`, and the separate
settlement/simulation verifier described above.
