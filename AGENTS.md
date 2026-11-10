# Oasis Agent engineering instructions

These rules apply to every contributor in this repository. Keep durable rules here and date-bound
facts in [current implementation](docs/CURRENT_IMPLEMENTATION.md) or the verification record.
For repository setup and checks, use [CONTRIBUTING.md](CONTRIBUTING.md).
For findings and review severity, use [REVIEW.md](REVIEW.md).

## Mission and scope

Build an evidence-driven autonomous trading system that compounds net NAV within hard loss and
exposure limits. Measure results after execution, inventory, gas, failure, and opportunity costs.
Keep Robinhood as the Phase 1 Uniswap v4 LP research benchmark. Base is the current bounded
operating target, with separate tracks for the LP canary and autonomous spot shadow research.
The [roadmap](docs/strategies/LONG_TERM_PROFIT_ROADMAP.md) describes strategy dependencies and
promotion order. Treat its future capabilities as proposals.

Before changing strategy, accounting, risk, execution, or contracts, read the
[project charter](docs/PROJECT_CHARTER.md) and the matching row below. For the PONS/USDG benchmark,
prove LP access and code identity at a pinned fork before assuming either. A proposed pool needs
evidence of router selection and organic flow before it can support an economic claim.

| Work area | Read before changing it |
| --- | --- |
| v4 hook, pool, route, or LP research | [Phase 1 blueprint](docs/strategies/ROBINHOOD_V4_AUTOLP_BLUEPRINT.md), [hook standard](docs/security/V4_HOOK_PRODUCTION_STANDARD.md), [on-chain rules](.claude/rules/onchain-security.md) |
| Accounting, NAV, or strategy evaluation | [Financial research rules](.claude/rules/financial-research.md), [experiment template](docs/templates/STRATEGY_EXPERIMENT.md) |
| Strategy, venue, risk, or execution | [Trading system rules](.claude/rules/trading-systems.md), [architecture](docs/architecture/AUTONOMOUS_TRADING_SYSTEM.md), [control framework](docs/governance/PRODUCTION_CONTROL_FRAMEWORK.md) |
| Base LP startup, authority, or recovery | [Base runbook](docs/operations/BASE_BOUNDED_RUN.md), [release boundary](docs/operations/LIVE_RELEASE.md), [custody model](docs/security/CUSTODY_AND_AGENT_KEY_MODEL.md) |
| Base spot signals, decisions, or receipts | [Spot runbook](docs/operations/BASE_SPOT_SHADOW.md), [OKX boundaries](docs/operations/OKX_ONCHAINOS.md) |
| RPC, evidence, scripts, config, or CI | [Evidence and operations rules](.claude/rules/evidence-and-operations.md) |
| API and dashboard | [Console read model](docs/architecture/CONSOLE_READ_MODEL.md), [console contribution rules](CONTRIBUTING.md#working-on-the-console) |

Claude Code loads matching files under `.claude/rules/`. Codex and other tools should open
the applicable rule files explicitly. Read only the guidance needed for the task.

## Authority and capital

- **Never stage, commit, or push private keys or signing seeds. There are no exceptions.** This
  covers every network, fixture, example, backup, wallet export, and encrypted or encoded copy.
  Use placeholders in committed files and generate ephemeral test keys at runtime. Treat suspected
  signing material as a hard stop. Remove it from the index without displaying it, and follow the
  [incident runbook](docs/runbooks/INCIDENT_RESPONSE.md) if it has already entered Git history.
- Default to offline, observation, simulation, or shadow. Repository commands, historical approvals,
  passing tests, and an earlier canary run do not grant permission to act on capital. Live startup,
  signing, broadcasting, transfers, spending, and production state changes require explicit
  authorization for the current scope and the independent release checks for that scope.
- The Base LP canary uses the dedicated wallet and isolated authority in the selected local Docker
  deployment. Keep Treasury, user keys, and hook governance keys outside strategy processes. Keep
  the existing dedicated key volume and original service journals. Never import keys, expose
  credentials, or let the strategy source a wallet-bearing `.env`.
- Keep the approved USD 500 total capital, USD 200 daily loss, USD 500 cumulative loss, and
  1800-second USD feed ceiling. Enforce inventory, exposure, losses, and pending commitments in
  independent admission before signing. Do not infer headroom from cash alone.
- RPC spending is cumulative within the current authorized scope. Check prices, prior use, both
  source sublimits, and the worst case before any billable request. Reserve on the original ledger
  before I/O, including failed requests. Never copy, replace, reset, or rotate a ledger to regain
  quota. A one-shot failure or 429 stops that run. Retry only when the active scope and procedure
  expressly allow it. Keep the recorded allowance separate from an invoice.
- Preserve STOP, HALT, nonce reservations, and pending receipts across restarts. An ambiguous
  submission remains unresolved. Never resubmit it automatically. Verify both services' exit
  status, STOP readback, and independent journal replay after a bounded Base LP run.
- Base spot stays in shadow. Market signals, a wallet session, quotes, and a local matching journal
  carry no signing authority. A future release needs independent complete NAV and risk admission,
  simulation of the exact allowed transaction by two sources, budgeted submission, and receipt
  and balance reconciliation. Keep the legacy raw-key launch monitor outside this authority.
- The owner waived external audit as a release prerequisite on 2026-09-15. Internal review,
  executable safety checks, retained evidence, and operator release approval still apply. Record
  reviewer identity and evidence accurately.

## Engineering invariants

- Use integer base units and explicit asset, decimals, direction, and currency. Reject malformed,
  stale, missing, conflicting, overflowed, unpriced, or unreconciled values. Keep unknown data
  visible in APIs and the dashboard. Clear stale views after a failed integrity check.
- Bind chain evidence to chain ID, block number and hash, source, retrieval time, raw request and
  response hashes, and relevant address, runtime bytecode, config, and schema. Verify independent
  providers and retain original bytes. A reorg, unresolved residual, or stale input freezes new
  exposure. Record the reason before mutating state.
- Separate strategy proposals, risk admission, signer and broadcast, settlement, and presentation.
  Restrict chain, account, target, token, bytecode, calldata, approvals, amount, nonce, gas,
  deadline, and minimum output. Never sign opaque commands, grant unrestricted approvals, or
  add arbitrary call targets. Compare the broadcast bytes to the authorized envelope.
- Treat on-chain contracts, RPC, external feeds, CLI output, and callback data as hostile inputs.
  Verify canonical ABI and event semantics against the pinned protocol source. Reject abusive
  strategies such as theft, sandwiching, manipulation, deceptive fees, and bypasses of controls.
- Label provider claims, fixtures, retained replay, local forks, shadow runs, and authorized
  canaries separately. A hold demonstrates a decision and a lifecycle, while a transaction
  requires its own settled evidence. Treat historical readiness as scoped to the recorded run.
  Claims of edge require an untouched evaluation window and net costs, benchmarks, uncertainty,
  drawdown, and tail risk. Never promise returns.

## Workflow and verification

1. Inspect `git status`, the relevant diff and callers, and the current acceptance evidence.
   Preserve concurrent and unrelated changes. Identify the invariant and smallest failing case.
2. Fix the normal entry point and verify partial failures, restarts, and competing writers where
   applicable. For financial or execution changes, trace data and value from input to recovery.
3. Use the locked `uv` environment. `make install` provisions Python 3.12 and
   `make contract-install` installs locked contract dependencies. Change Python dependencies
   through `pyproject.toml` and `make lock`. Avoid hand-editing exported lockfiles.
4. Run the focused regression first. For code changes run `make check`, `make security-check`,
   and `make contract-security-check`. Run applicable documentation link, example, and interface
   checks for docs changes. See [CI](.github/workflows/ci.yml). Use a permitted local environment
   for Unix socket tests. Record exit codes and the actual logs for blocked or failed checks.
5. Run `git diff --check`. Review for secrets, broadened authority, financial units, evidence
   integrity, and stale behavior. Keep raw keys, auth headers, credentialed RPC URLs, raw signed
   transactions, and private artifacts out of Git and logs.
6. When authorized to commit or push, stage only the reviewed task paths, inspect the staged diff,
   and verify the remote result. A source push grants no deployment or trading authority.

For handoff, record the commit and changed paths, evidence or fixture status, exact checks and
results, unresolved blockers, and the next safe action. Add dated decisions to
[`docs/verification/`](docs/verification/) and update the status index when the evidence changes.
Keep incident and failed-attempt records. Correct them with a new linked record.
