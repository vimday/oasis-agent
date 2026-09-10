# LP / Launch / console refinement — 2026-09-10

Historical first-stage record. Superseded by [managed control delivery](LP_LAUNCH_MANAGED_CONTROL_20260910.md).
Statements below describe the first stage before managed control and the final commit.

Status: verified incremental improvements; the user's complete cross-stack configuration and
live-control objective is **not complete**. No production readiness or profitability claim.
No commit, deployment, live configuration change, transaction signing or broadcast was performed.
Existing uncommitted Launch work was preserved; the initial worktree already contained extensive
monitor, RPC, venue, journal and UI changes. Those changes are not attributed to this refinement.

## Invariants and implemented changes

1. Observation stop is an actual process lifecycle operation. The server owns the process group,
   records `stopping`, reaps it, then records `stopped`. Stop before spawn prevents the command from
   starting. Repeated stop is idempotent. Unknown or foreign-owner active runs fail closed. Server
   shutdown awaits cleanup before closing the operations journal. This is **not** an on-chain
   position unwind or a stop control for separately running live Launch processes.
2. Child output cannot grow process memory with total log volume. Streams are drained in 4 KiB
   chunks, oversized lines above 8 KiB are omitted, and retained output tails are bounded to
   2,000 characters. URLs, bearer-style credentials and long hexadecimal material are redacted.
   The regression emits 2 MB plus a synthetic credential-bearing URL and verifies bounded,
   redacted output. The process-group implementation targets the repository's POSIX deployment.
3. Concurrent identical read-only RPC calls share the first successful response through bounded
   lock striping and the existing observation TTL. A regression sends 20 concurrent identical
   calls and observes exactly **one** upstream call. This is an upstream-request count result,
   not a measurement of trading latency. Cache capacity is 256 entries and lock count is 64.
   Failed HTTP responses are rejected before result caching; provider error text is not exposed.
   Journal integrity verification remains mandatory on reads; no TTL bypass was introduced there.
4. Frontend configuration documents travel as strings to preserve integers beyond 2^53. The
   backend validates Launch through `ExecutionConfig.from_json` and LP through `parse_policy`.
   The result includes an exact document string, SHA-256 digest, filename, `applied=false` and
   `execution_eligible=false`. RPC exports contain environment-variable references, not URLs.
   Validation is authenticated and audited using digests, without storing the configuration body.
5. Launch parsing rejects duplicate JSON keys, empty schedules/fee lists, duplicate or out-of-range
   fees, nonpositive gas limits, and overrides of routing constants excluded from policy hashing.
   LP parsing rejects unknown/missing envelope fields and invalid chain/age bounds before opening
   a journal. Existing valid serialized execution policy formats are preserved.
6. Launch FDV display uses the retained supply/cap instead of unrelated constants and does not
   truncate fractional USD-micro spend before calculating FDV. Strict comparison rejects equality
   at the cap. Missing policy valuation inputs produce no fabricated quote. The execution monitor
   still assumes 18 target decimals; this change does not generalize its token support. Unknown
   target contracts are no longer automatically labelled LAPTOP. Candidate gates require a real
   boolean `true`, not a truthy string.
7. The console shows backend capabilities, provides observation stop buttons, suppresses duplicate
   start clicks, invalidates downloads after edits and keeps tokens in memory only. Tokens are no
   longer attached to read-only fetches. The new configuration editor matches the existing dark
   theme. A Node syntax check is part of `make check` and the Python CI job.
8. The server capability-import test now parses Python AST imports at every nesting level rather
   than scanning top-level text. Identifier matching distinguishes stdlib `signal` from signing
   imports. It continues to reject the named transaction/signing capabilities.

## Using the configuration workbench

Open **运行与操作 → 配置工作台**. Select Launch, LP or RPC, load a clearly labelled example or
paste the intended JSON, and use the operations token entered on the overview tab to validate.
Download is enabled only for the current validated contents. Loading an example is not loading the
active campaign and does not qualify its economic parameters.

- Launch export is consumed by `scripts/run_launch_monitor.py --config <file>`; default observation
  mode remains in force. The supplied strategy still has Base / 18-decimal / fixed-reference-supply
  assumptions. A new token address does not establish token safety, identity, or permission to trade.
- LP export is consumed by `scripts/run_lp_shadow.py --policy <file>` with independently retained
  inputs/evidence and a correctly bound journal. Pool and token identities come from evidence.
- RPC export is a `[[chains]]` TOML fragment for the server configuration. It is not a replacement
  for the complete server TOML. Its environment variable must exist on the server. The template's
  zero PoolManager is an unverified placeholder, not a deployment address or an execution target.
- Start runs the existing server-configured observation command; downloading a configuration does
  **not** change that command. Configure the desired command before starting the server. Stop only
  targets runs owned by that server instance. A restart cannot establish ownership of old processes.

## Validation and exact results

Baseline: `make python-check` passed with 953 tests and 108 subtests; existing formatting and type
checks passed before edits. Regression-first tests reproduced missing stop/cleanup/output controls,
RPC duplication/error handling, and invalid configuration acceptance before their fixes.

Final validation:

| Command / check | Result |
| --- | --- |
| `make check RUNNER='.venv/bin/python -m coverage run'` | Exit 0; compile, Ruff lint/format, mypy (126 source files), 973 Python tests, 108 subtests, frontend syntax, Forge format/build/test/lint passed |
| Python tests inside that gate | 973 passed, 1 warning, 108 subtests passed in 27.27s |
| Foundry CI profile inside that gate | 70 passed, 0 failed, 0 skipped across 9 suites in 14.86s |
| `make frontend-check` after the final CSS adjustment | Exit 0; 1 inline script parsed successfully |
| `make security-check` | Exit 0; dependency audit found no known vulnerabilities; Bandit threshold and secret scan passed |
| `make contract-security-check` | Exit 0; Slither analyzed 80 contracts with 102 detectors, 0 findings |
| Bandit and secret scan after adding the frontend-check script | Exit 0; 0 medium/high, 43 low-severity issues inventoried; no unreviewed secret candidates |
| `coverage report --include='src/oasis_agent/server/*,src/oasis_agent/sniping/config.py,src/oasis_agent/shadow/service.py'` | 1,863 statements, 136 missed, 93% statement coverage; not branch coverage or whole-repository coverage |
| `git diff --check` | Passed |

Browser validation used a separate loopback server, a temporary `/tmp` operations DB and a sleeping
Python test command. It did not connect to RPCs, use active strategy journals or execute trades.
Verified all three configuration templates, a non-JavaScript-safe integer (`100000000000000001`),
start → running → stopped, download invalidation after editing, and disabled start after reload
clears the in-memory token. The editor was also visually checked. These interactive checks do not
replace an automated browser regression suite.

Non-fatal existing output: one `websockets.legacy` deprecation warning, Forge lint advisories and a
sandbox denial when Foundry attempted to flush its global signature cache. No test was skipped or
weakened to hide a failing gate. The dependency audit initially could not access the package index
in the sandbox; the authorized network-enabled rerun succeeded.

## Outstanding scope / release blockers

- No unified managed LP + Launch campaign service exists yet. The server still has one configured
  observation runner, not independent per-strategy control ownership or cross-process recovery.
- Applying versioned configurations from the UI to future runs, credential-management integration,
  atomic revision conflicts, rollback and desired/observed configuration reconciliation remain.
  Current UI support is validation/export, not hot application or persistent strategy management.
- Independent external signing/custody, reconciled position-aware stop semantics (stop entry vs
  continue exits vs verified unwind), explicit live execution authorization, and target-specific
  release gates remain necessary before offering live start/stop. Existing Launch code still has
  an in-process signing path; this work does not qualify it as production custody.
- Arbitrary token decimals/supply, protocol identity changes, production routing/economics, sustained
  strategy-shadow acceptance and pinned adversarial campaigns remain unverified. No new fork,
  mainnet, routing, economic or profitability evidence was acquired here.
- API responses and retained journal validation still need production-size load testing. The RPC
  client cache is bounded by entry count, not aggregate response bytes. No broad performance claim
  about LP execution, swap latency or opening-race competitiveness is supported by these tests.

The concrete implementation above is reviewable and tested. The full request to refine every LP,
Launch and frontend/backend path and make all runtime controls configurable remains larger than
these verified changes; this report deliberately does not claim that objective is finished.
