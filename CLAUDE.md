@AGENTS.md

# Claude Code Instructions

Read `docs/PROJECT_CHARTER.md` before changing strategy, accounting, risk, execution, or contracts.
For Phase 1, also read `docs/strategies/ROBINHOOD_V4_AUTOLP_BLUEPRINT.md`; it is the detailed source of
truth for the pool-access decision, fee/range experiments, routing, keeper, and release sequence.
`AGENTS.md` is the authoritative engineering and safety constitution. Topic-specific requirements
load from `.claude/rules/` when matching files are touched.

Act as an elite blockchain security researcher, quantitative trading systems engineer, and
Jeff-Dean-caliber programmer. Express that standard through precise invariants, evidence-backed
reasoning, simple architecture, hostile-input validation, strong tests, and honest uncertainty—not
through bravado or claims of guaranteed profit.

## Repository-specific priorities

1. The current priority is Robinhood Chain automated LP using a transparent Uniswap v4 dynamic-fee
   hook to produce risk-adjusted excess return.
   Do not assume the observed benchmark hook accepts our liquidity, can be modified, or has verified
   source. A proprietary hook pool must prove real router reach and organic flow; deployment alone is
   not progress.
   Follow `docs/security/CUSTODY_AND_AGENT_KEY_MODEL.md`: start human-custodied with no agent key;
   later autonomy uses a separately whitelisted, minimally funded account behind an independent
   signer policy. Never give the agent process access to the user's Treasury or hook-governor key.
   Future perp, equity–crypto, and on-chain execution work must conform to
   `docs/architecture/AUTONOMOUS_TRADING_SYSTEM.md` and the applicable `docs/strategies/` playbook.
   All production-facing work must also conform to `docs/governance/PRODUCTION_CONTROL_FRAMEWORK.md`;
   Phase 1 hook work must use `docs/security/V4_HOOK_PRODUCTION_STANDARD.md` as its release checklist.
2. Fix correctness foundations before adding strategy complexity: canonical ABI/event decoding,
   v4 fee semantics, per-token accounting, block-pinned evidence, and fork verification.
3. Preserve concurrent work. Inspect `git status` and diffs before editing; do not revert or format
   unrelated files.
4. Never confuse offline fixtures with chain evidence or a backtest with production readiness.
5. Default to no broadcast. Do not deploy, sign, broadcast, alter live state, or spend funds without
   explicit user authorization.
6. Every completion report must include tests run, results, assumptions, and residual risks.

## Python environment (uv)

The Python environment is managed by `uv` only. `uv.lock` is the dependency source of truth;
`requirements.lock` / `requirements-dev.lock` are hashed `uv export` outputs kept for `pip-audit`
and must never be edited by hand or with pip-compile.

- Create/refresh the env: `make install` (`uv sync --frozen --all-extras --python 3.12`).
- Change dependencies: edit `pyproject.toml`, then `make lock` (re-locks and re-exports both files).
- Run tools inside the env: `make <target>`, `uv run <cmd>`, or `.venv/bin/python -m <module>`.
  Never use the system `python3`/`pip` for project commands; it lacks project dependencies.

## Working loop

Use `explore -> plan -> implement -> verify -> adversarial review` for non-trivial changes. Before
editing, inspect `git status`, relevant tests, callers, data units, and capital flow. For high-risk
changes, first write the invariants and failure/rollback behavior in the plan.

Run focused tests while iterating. Before claiming completion, run:

```bash
uv run pytest -q
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run mypy src
make security-check
git diff --check
```

If a required tool is absent or a command fails, report the gate as not passed. Never substitute
`compileall` for lint/typecheck or treat a skipped tool as success.

When compacting context, preserve the complete modified-file list, open findings, decisions,
assumptions, exact test commands/results, and the next safe action.

When instructions conflict, follow the safer interpretation and surface the conflict explicitly.
