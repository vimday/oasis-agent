# Free NAV command budget continuity — 2026-10-23

Continues [the free-provider diagnostic](FREE_RPC_RECHECK_20261023.md).
This increment makes no RPC request, reads no credential or wallet key, and does
not change the production release conditions or oracle policy.

## Failure and correction

`scripts.verify_free_live_nav` previously constructed its budget path as
`<output>/budget.db`. Although an existing output directory refused, choosing a
different output directory created a new 32-request allowance per source. That
violated the persistent campaign budget boundary. A failure also reopened the
budget without requiring it to exist, allowing a missing ledger to be recreated.
The CLI returned exit 0 for a successfully written refusal report.

The command now binds to the original repository-relative
`reports/evidence/live-nav-acceptance-20261018/budget.db`, independently of the
working directory or output path. It exposes no budget-path override or quota
initializer. Local preflight, every network reservation and final accounting use
`require_existing=True`; symlinked, missing or damaged ledgers refuse without a
replacement. The existing policies remain 32 requests per source with one-second
spacing. The two fixed no-key public endpoints and zero-retry policy are unchanged.

Before constructing a session, the command derives the complete wallet-only
request count from the wallet/NAV request builders: 24 primary and 23 secondary,
including the primary head lookup. Insufficient remaining quota refuses before
network use or evidence-database creation. Concurrent consumers can still use
quota after preflight; each request's durable reservation remains authoritative.
Changing output directories cannot renew that allowance.

The v2 diagnostic report carries the required request counts, the opening counts
when available, and final counts or `null` when accounting cannot be verified.
A missing/damaged final budget forces `nav_verified: false`. Refusal exits 3;
writing a refusal report is not an acceptance pass. `execution_eligible` remains
false on success too. Raw partial evidence may remain after an interrupted run.

The preceding real diagnostic left only 1 primary and 2 secondary requests in
this NAV campaign. That is insufficient for another full capture. This change
does not replenish them or run such a capture. The paid campaign remains at eight
previous requests under the same cumulative USD 0.10 ceiling.

## Validation

Five new regression cases failed against the old implementation: missing, corrupt,
symlinked and insufficient budgets under renamed outputs, plus ledger deletion
during acquisition. The original failure log is retained locally in
`reports/review-20261023/free-nav-budget-before.log`. It is not reclassified as a
pass. Two initial lint line-length findings were corrected by formatting; the
subsequent explicit Ruff check exited 0.

The focused suite passes 14 tests. Its successful path runs the real capture,
retention and replay code over synthetic raw RPC fixtures and an isolated SQLite
budget, spends exactly 24/23 requests, then refuses a renamed second output before
another HTTP-fixture call. The test substitutes a synthetic feed runtime pin and
zero pacing delay; it is not a live feed or provider acceptance result. Other
cases verify redaction, current-head refusal, budget deletion, and CLI exit 3
from another working directory. No test uses the real campaign ledger.

The complete `make live-quality` run is retained at
`reports/review-20261023/free-nav-budget`. All four commands exited 0, with no
timeout or launch failure; every command log digest was independently checked:

- 3418 tests and 138 subtests passed; one existing websockets deprecation warning.
- Compile, Ruff/format, mypy and syntax checks for eight frontend modules passed.
- Secret gate passed without credential verification calls.
- Bandit reported zero medium/high findings; 85 low findings remain.
- Offline Compose validation passed seven services; no container was started.

Quality report evidence hash:
`95efd1d8c69a6876a62ccfd8db423bf437b59a2ba0d1b6d8956c5981c41269da`.
This is development validation only. The latest real USDC feed observation still
fails the 1800-second limit, and production dual-source simulation and dedicated
account receipt/cash recovery remain outstanding.
