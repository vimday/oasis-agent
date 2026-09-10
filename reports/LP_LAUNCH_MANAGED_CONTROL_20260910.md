# LP / Launch managed control delivery — 2026-09-10

## Confirmed implementation

The dashboard now saves, applies, rolls back, starts and stops independent LP shadow / Launch
observe campaigns through the same validated backend models. Users can configure RPC profiles,
token policy, public wallet, discovery/events, retained LP input selection, cycles and timeout.
Policy documents remain exact strings across the browser boundary. The desired, applied and
running revisions are distinct; edits cannot silently mutate a running strategy.

The controller has immutable hash-linked revisions, optimistic concurrency, idempotent starts,
bounded concurrency, per-run retained input/evidence copies, fixed child commands, a minimal child
environment and process-group shutdown. A lifetime lock inherited by workers blocks replacement
controllers until orphans exit. Restart recovery marks interrupted runs failed without auto-resume.
Tests cover stop before spawn, stop during input preparation, concurrent/duplicate starts, stale
revisions, rollback, restart, corrupt state, and persistence failure before and after run creation.

RPC URLs supplied in the dashboard are memory-only. Validation errors, worker tails and audit
records redact sensitive material. Campaign requests cannot pass arbitrary commands or file paths.
The control process does not expose signing or broadcasting, and managed Launch workers always use
dry-run mode with an empty environment-file source.

Launch v2 supports configured token decimals/reference supply; on-chain decimals must match.
FDV comparisons use exact integer fractions, including equality at caps. Legacy v1 serialized
policies remain compatible. Existing venue/discovery/scheduling/settlement and journal efficiency
work in the starting worktree is preserved and included in the repository checks.

Efficiency evidence includes 20 identical concurrent observation RPC requests resulting in one
upstream request, fixed cache/lock bounds, bounded stream buffers, streaming retained-file hashing,
and off-event-loop evidence copies. These are correctness/resource tests, not live trading latency
or profitability measurements. Earlier benchmark artifacts retain their own measured scope.

## Verification

- `make check RUNNER='.venv/bin/python -m coverage run'`: exit 0; 994 Python tests and 108
  subtests passed; 70 Foundry tests passed with zero failed/skipped. Ruff lint/format, mypy
  (128 source files), dashboard syntax validation and contract checks passed.
- Coverage for server modules, Launch config/monitor and LP shadow service: 85% combined.
  Launch monitor coverage is 56%; this is not full live-path coverage.
- `make security-check`: exit 0; dependency audit found no known vulnerabilities; Bandit reported
  no medium/high severity issues (43 low severity findings); secret gate passed.
- `make contract-security-check`: exit 0; 80 contracts, 102 detectors, zero Slither results.
- Supplemental secret scan of new reports and `.env.example`: one candidate was the public
  `diagnostic_input_sha256` evidence digest; no credential values were printed or verified remotely.
- Actual local browser check exercised save, apply, start and stop using a synthetic token, dummy
  endpoints and a harmless test worker. Actual LP subprocess integration exercised retained evidence
  and generated a shadow recenter decision. Neither check broadcast transactions.

Known non-failing tool output includes one deprecated WebSocket dependency warning, Foundry Solar
preprocessor diagnostics followed by successful Solc compilation, restricted signature-cache writes,
and Forge style lint notices. No tests were weakened or skipped to obtain acceptance.

## Unresolved boundary

This is a complete managed observation workflow, **not completion of autonomous live LP/Launch
trading**. The isolated production signer, target-specific release/security evidence, sustained
shadow acceptance and untouched economic validation remain outstanding. The standalone live Launch
CLI is not controlled by these dashboard stop buttons; observation stop is not a position unwind.
No deployment, live configuration change, signing, broadcasting or funds movement was performed.

Operator instructions: [LP / Launch dashboard controls](../docs/operations/LP_LAUNCH_CONTROLS.md).
The local `configs/server.launch-ui.toml` remains outside the commit.
