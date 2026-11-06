# Signed NAV policy alignment — 2026-11-07

## Confirmed implementation

The signed-price NAV candidate accepted mutually agreeing USDC prices outside the
production NAV stable band. A signer spread limit checks agreement, not proximity
to USD 1. This was a candidate-path policy gap: its separate evidence kind already
prevented production NAV admission. It was not an observed production bypass.

Both NAV paths now call the same fixed integer check. At eight price decimals,
98,000,000 and 102,000,000 are admitted boundaries; one unit beyond either is
refused. Production arithmetic and its refusal text are unchanged. The signed
path applies the check to the authenticated median actually used for valuation,
after signature and per-feed age verification, before constructing an RPC session.
Both historical v1 and current v2 evidence bindings reapply the same check during
raw replay. Prices are neither clipped nor replaced with a synthetic peg.

The general signed-price verifier remains able to authenticate depeg observations
for research. Authentication alone does not establish NAV eligibility. Candidate
kind separation and `execution_eligible: false` remain unchanged.

The signed NAV CLI also opens its initial and final budget-count inspections with
`read_only=True`. These are SQLite read snapshots, without an immediate writer
lock. Actual capture still reserves every request against the supplied existing ledger
via `BudgetedPoster`. Proving that it is the original approved ledger remains a
deployment/scope requirement. No ledger policy, count or reservation path was relaxed.

## Regression evidence

The stable-band suite uses ephemeral in-memory test keys and synthetic signed
bytes; it is not production price evidence. Before the change it produced
12 failures and 2 passes. After the fix all 14 cases passed, covering inclusive
boundaries, one-unit violations, zero-spread shared depegs, both replay versions,
pre-RPC refusal and continued rejection by the production NAV consumer. The final
related suite passed 144 tests. Scoped Ruff, formatting, mypy and diff checks each
exited 0. Original formatting failures remain in the local record.

The budget regression uses real SQLite on disposable test ledgers. With another
connection holding `BEGIN IMMEDIATE`, the original offline plan refused. The
before suite recorded 1 failure and 3 passes. The repaired path reads both snapshots
without a writer transaction, preserves inode, bytes and committed counts, and
makes zero requests. Missing ledgers, mismatched policies and absent policy rows
still refuse without repair. The final related suite passed 80 tests; Ruff,
formatting and diff checks exited 0. These overlapping suites are not additive.

Local original logs are retained in
`reports/review-20261107/signed-nav-stable-band/` and
`reports/review-20261107/signed-nav-budget-readonly/`.
An independent engineering agent (`capture_review`) reviewed both changes and their
raw check results without finding a blocking issue; this is not an external audit.

## Unresolved production boundary

This continuation issued zero RPC or price-acquisition requests, accessed no
production Docker state or key material, and changed no production configuration,
STOP/HALT marker or budget. It did not start services, sign or broadcast.
The preceding 209-request authorization ended after the separately documented
85-request stale-USDC refusal; unused allowance is not reusable authorization.
The prior eight paid requests and cumulative USD 0.10 cap remain unchanged.

Fresh signed ETH/USDC packages have not been acquired. Historical fixtures,
previous gateway failures and synthetic regressions cannot establish freshness.
The signed candidate CLI also still needs a hard acquisition deadline and an
approved scope bound to its plan, endpoints, original budget and actual request
count before unattended acquisition. These fixes do not supply that scope or
promote candidate prices into production authority.

Gas-oracle runtime transition, fresh complete NAV, full-envelope dual-source
simulation, current five-receipt recovery and full unwind/restart acceptance
remain unresolved as recorded in
[LIVE_REAL_CAPTURE_AND_AUTONOMY_20261107.md](LIVE_REAL_CAPTURE_AND_AUTONOMY_20261107.md).
USD 500 total capital, USD 200 daily loss, USD 500 cumulative loss and each feed's
maximum 1800-second age are unchanged. No live readiness or profitability is claimed.

## Full development gate

The complete `make live-quality` gate exited **0**. Python/frontend, secrets,
Bandit and offline Compose each exited 0 without timeout or launch failure.
The raw command log hashes and report self-hashes were recomputed and matched.
It passed **4710 tests and 138 subtests**, type checking across 251 source files,
and syntax checks for eight frontend modules. The 11 unsuppressed pytest warnings
comprise one websockets deprecation and ten warnings about fork in threaded tests.
Bandit reported zero medium/high severity issues and 98 low severity issues;
its existing comment/suppression warnings remain in the raw log. The secret gate
made no credential-verification requests. Compose was configuration-only, with
zero network calls and no container starts.

All **680** frozen source, test, configuration and build/dependency files remained
unchanged during validation. The four unrelated untracked verification documents
were preserved and excluded from this change.

The quality report is
`reports/review-20261107/signed-nav-policy-quality/report.json`, self-hash
`5fe288ac9f147e4535af55c42ce05ab36707a95631fcf2a68fd9b1b96934d6d0`.
The outer exit record is
`reports/review-20261107/signed-nav-policy/quality-exit.json`, self-hash
`413f8f9a7fec9a15f048676929abb941f3dfba2eeeff680d8aa96dd6a6bef0da`.
The independently reviewed regression artifact manifest is
`reports/review-20261107/signed-nav-policy/regression-review.json`, self-hash
`b4e935ad1beea8e9e4ee904e8510b7cfbde3e1eeb0133686631dcc5780576c8b`.
Development validation does not replace any unresolved production acceptance.
