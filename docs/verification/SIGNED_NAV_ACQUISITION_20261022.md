# Bounded signed NAV candidate acquisition — 2026-10-22

The retained-price verifier and signed NAV library previously had no operator
capture command. `scripts.capture_signed_nav` now joins local authenticated price
verification, one bounded two-provider account capture, raw-evidence replay and
current-clock checking. Production valuation and release policy remain unchanged.

## Operator entry point

The default invocation is an offline plan. Supply a fresh retained RedStone
PrimaryProd ETH/USDC package file, the reviewed live configuration, an explicit
position JSON file, the **existing** campaign budget, and its unchanged RPC profile:

```sh
.venv/bin/python -m scripts.capture_signed_nav \
  --config /absolute/path/to/live-config.json \
  --prices /absolute/path/to/signed-prices.json \
  --position /absolute/path/to/position.json \
  --rpc-profile /absolute/path/to/existing-rpc-profile.json \
  --budget /absolute/path/to/existing-budget.db \
  --out /absolute/path/to/new-plan-directory
```

The position file must contain JSON `null` for a declared wallet-only observation,
or exactly `token_id`, `lower`, `upper`, `liquidity`, `opened_at` integer fields.
This declaration is not discovery of all assets or proof that no other LP exists.
Malformed declarations, unsigned/expired prices and incompatible or insufficient
budgets refuse before constructing a network session. Inputs are bounded regular
files; duplicate fields and final-component symlinks refuse. No historical clock
flag exists. An authentic historical price fixture refuses under the current clock.

After reviewing the plan and establishing the RPC monetary cost, an operator may
invoke the same command with `--capture` and a **different new output directory**.
Use the **same budget path and policy**. Capture reads the primary URL from the
`BASE_RPC_PRIMARY_ALT` environment variable (or `--primary-rpc-env`) and the
secondary URL from the environment variable named by the configuration. It never
loads `.env` or any credential file. Both URLs must use HTTPS and distinct hosts;
this does not certify infrastructure independence. URLs are not printed or retained
in the report. Do not export wallet keys into this process.

The frozen budget aliases must be `live_primary` and `live_secondary`. An unrelated
ledger or alias mismatch is a blocker, not a reason to create, copy or reset a
budget. The command cannot prove that an operator selected the original ledger;
protect its path and lifecycle through deployment controls. Its quotas count
requests, not dollars. The owner's cumulative USD 0.10 acceptance ceiling and
existing eight paid requests remain separate constraints. **Do not run paid
capture while total costs cannot be established.** No capture was run against a
real provider during this increment.

## Acquisition and evidence boundaries

- Wallet only: at most 16 primary / 15 secondary requests (31 total).
- One declared LP: at most 27 primary / 26 secondary requests (53 total).
- The primary count includes one latest-header lookup. All account/state reads
  then bind to its block hash, with both providers checking chain and headers.
- No retry or polling is configured. Each provider uses the existing durable
  reserve-before-send budget, including transport failures, with no refund.
  The command also bounds logical requests per source across all fresh sessions.
- Both providers run concurrently while calls within a provider remain sequential.
  Local price/clock and request bounds are checked before each logical request.
  A pacing wait or in-flight HTTP call is not forcibly interrupted by that clock
  check; later checks refuse an expired result. Existing HTTP timeout is 15 seconds.
- Original signed prices, agreeing raw wallet proofs, optional NFT/core position
  proofs, uncollected fees and NAV candidate proofs are retained in `evidence.db`.
  The completed pair is replayed and checked against the current host clock before
  publication. Account age remains at most 120 seconds and price age at most the
  configured 1,800-second ceiling. Clock integrity is a deployment assumption.
- Success writes `check-input.json`, consumable by `scripts.verify_signed_nav`,
  plus `report.json` with configuration/price hashes, request bounds, cumulative
  counts and evidence references. Reports always say `execution_eligible: false`.
  Plan success is explicitly `candidate_verified: false`.
- Refusals have a fixed message and stage, never exception text. Partial successful
  evidence may remain after refusal; it is not an accepted pair. Failed HTTP bodies
  and the initial lookup are not published as diagnostic raw-wire dumps. Missing
  or damaged budgets are never replaced. Output directories are exclusive.

A candidate is scoped to declared ETH/USDC and at most one declared position. It
still does not prove complete asset discovery, capital history, realized PnL,
current production runtime/simulation acceptance or real-canary receipt recovery.
No signer or broadcast consumer accepts this candidate evidence kind. Fresh price
access and review of that oracle policy remain unresolved; this command does not
fetch prices or waive that review. USD 500 capital, USD 200 daily loss and USD 500
cumulative loss bounds are unchanged.

## Validation

Tests use the already retained authentic historical price signatures, controlled
clocks, synthetic chain wire and temporary real quota/evidence databases. They
cover wallet/LP capture and independent replay, cumulative quota across output
directories, no network in default planning, refusal before spending, provider
failure/disagreement, clock expiry/regression, and budget deletion mid-capture.
No production ledger, Docker key volume, signing or broadcasting is involved.

The first focused run had two failures: the synthetic fixture only accepted hex
block numbers and rejected the command's initial `latest` lookup. The test HTTP
adapter now supplies that header explicitly; assertions check refusal stages so
later-path tests cannot pass because of that unrelated fixture limitation.
Final validation results are recorded below after checking all command exit codes.

The final focused capture/quorum/library suite passed 74 tests. The complete
`make live-quality QUALITY_OUTPUT=reports/review-20261022/signed-nav-acquisition`
run passed all four commands (each exit 0, no timeout or launch failure):

- Python compile, Ruff/format, mypy, 3,383 tests and 138 subtests; one existing
  websockets deprecation warning remains. Eight frontend modules passed syntax.
- Secret gate found no unreviewed candidates and made no credential verification calls.
- Bandit found zero medium/high severity findings; 85 low severity findings remain.
- Offline Compose validation passed all seven services with absent/synthetic RPC
  cases. No Docker service or real chain capture was started.

Each retained command log digest was checked against the report. Quality evidence
hash: `7fc2c9a68a74fca8c91bdb69565917640c5ce16d1583e302d2a0cbe2c1e308a7`.
This is development evidence, not current production NAV or live acceptance.
