# Efficiency refactor and Rust decision

Date: 2026-09-10. Status: first measured increment implemented; broader roadmap proposed.

## Outcome and invariants

The first increment improves journal polling and decision presentation without changing strategy,
risk thresholds, custody, signing, broadcasting, live configuration, or promotion gates. The
pre-existing untracked `configs/server.launch-ui.toml` is preserved.

Data flow: producer commits canonical records to SQLite; the observation server verifies records
and hash links; API endpoints expose the retained records; the dashboard presents decisions and
their reasons. This read path has no capital-transfer capability. Faster presentation does not
establish strategy quality, profitable execution, or deployment eligibility.

## Confirmed findings and changes

The previous cache key used row counts, maximum sequence, tail digests and policy hash. Altering
a payload while leaving its stored digest unchanged did not invalidate a warm cache. Six failing
regressions reproduced this across observations, decisions and execution events in DELETE and WAL
modes. Count probes also scanned journal records on every cache hit.

The server now probes `PRAGMA data_version` on its persistent query-only connections. External
SQLite commits invalidate the cache regardless of whether the tail changes. Explicit read
transactions bind version probes and full validation to the same per-database snapshot. Validation
failure discards cached state, translates SQLite errors into `ReadOnlyError`, and releases read
transactions. A WAL concurrency regression verifies that a commit during validation is excluded
from the current snapshot and detected on the next read. Warm reads execute no SELECT statements.

The dashboard shares concurrent GET requests for identical paths only while they are pending.
Settlement or rejection removes the pending request; it does not cache successful data across
refreshes. Overview polling no longer requests status twice. Decision stream notifications avoid
refreshing hidden pages and inactive decision tables; becoming visible refreshes non-polling tables
to recover missed notifications. Status failures clear optimistic badges;
missing health evidence displays unknown HALT status. Latest decisions expose retained reasons,
rebalance blockers and qualification before expandable, HTML-escaped canonical records.

## Reproducible measurement

Run `.venv/bin/python -m scripts.benchmark_server_reads --rows 20000 --samples 200`.
The benchmark uses temporary synthetic databases, including 20,000 observations, 20,000 decisions,
and one execution event. It measures direct journal reads, not HTTP throughput or live trading.
Environment: Python 3.12.4, SQLite 3.45.3 on the development machine.

| Measurement | Before | After |
| --- | ---: | ---: |
| First full read | 381.812 ms | 370.244 ms |
| Warm median, 200 reads | 4,860.792 µs | 14.125 µs |
| Warm p95, 200 reads | 6,521.375 µs | 15.792 µs |

The observed warm median improvement is approximately 344x for this operation only. These are
single-process local samples, not a statistically controlled production experiment. Cold validation
remains linear and synchronous; frequent journal commits still trigger full validation. The
benchmark prints environment and sample counts for repeatable comparison, and CI asserts removal
of scans rather than a hardware-dependent timing threshold.

## Rust decision

Do not introduce a Rust execution engine in this increment. The measured warm-read bottleneck was
an avoidable database scan, and a language rewrite would have preserved the flawed invalidation
rule unless its design also changed. No representative end-to-end profile currently establishes
that Python computation dominates the decision deadline.

For a future experiment, preserve Python as the research/reference and orchestration layer. Evaluate
Rust first for a bounded batch kernel such as replay, ABI decoding or route simulation only after
profiling identifies it as a dominant CPU cost. Do not move key custody or add broadcast authority
as part of performance work. An isolated signer or execution service requires its own threat model,
policy conformance tests, reconciliation, release review and authorization.

Proposed admission criteria, to be locked before a native prototype:

1. Retain a representative replay dataset and p50/p95/p99 profiles separating RPC wait, queueing,
   decode, strategy, risk, journal validation and serialization.
2. Define workload and latency/memory budgets. Consider Rust when one CPU kernel consumes at least
   30% of measured processing time and simpler batching/indexing changes fail the declared budget.
3. Require identical integer amounts, rounding, overflow behavior, rejection decisions and evidence
   bindings against the Python reference, including malformed and boundary inputs.
4. Require at least 2x kernel throughput and 20% end-to-end p95 improvement on the locked workload,
   including serialization/FFI cost, with no safety regression. These are proposed experiment
   thresholds, not established project SLOs or measured Rust results.
5. Ship an optional, reversible backend only after differential, fuzz, failure and restart tests;
   do not silently fall back after a disagreement.

SQLite documents that data_version comparisons are meaningful on the same connection and detect
commits by other connections: [SQLite PRAGMA documentation](https://www.sqlite.org/pragma.html#pragma_data_version).
Read transactions provide a stable database view: [SQLite isolation](https://www.sqlite.org/isolation.html).
Native boundaries have their own costs: [PyO3 performance guide](https://pyo3.rs/main/performance.html).

## Remaining work, ordered by dependency

| Workstream | Next concrete increment | Acceptance evidence |
| --- | --- | --- |
| Runtime | Profile representative replay, RPC, event-loop stalls and memory across journal sizes | Retained workload and p50/p95/p99 stage timings |
| Journal scaling | Separate source validation ownership; evaluate a dedicated reader worker and bounded queues | Corruption/reorg/slow-consumer tests; event-loop responsiveness under writes |
| Decision practice | Complete coherent live feature/reconciliation inputs and sustained shadow evaluation | Block-bound provenance, rejection reasons, net costs, untouched baseline comparison |
| Dashboard | Paginated decision drill-down, evidence age, actionable blockers and recovery guidance | Browser tests for empty, stale, corrupt, reconnect and narrow screens |
| Native acceleration | Prototype only the measured CPU kernel if admission criteria pass | Differential and end-to-end performance report |

Unresolved limitations: no atomic snapshot across separate database files; direct writes outside
SQLite and replacing open database files are unsupported (stop the server before replacement);
full-chain reads return internal dictionaries which callers must not mutate; cache memory grows
with journal history. This increment does not fix cold event-loop blocking, demonstrate live
profitability, complete the roadmap, or authorize production execution.

The subsequent [console/read-model refactor](CONSOLE_READ_MODEL.md) moves observation database
reads to a bounded worker and adds console capability coverage. The measurements below describe
this original increment; they are not an end-to-end benchmark of that later architecture.

## Validation

- Initial regressions: 7 failed as expected (6 cache-integrity cases and the no-scan assertion).
- Focused server/API/frontend suite: 56 passed; additional frontend explanation and unknown-state
  assertions passed after the final presentation changes.
- `make check`: passed; 1,004 Python tests, 108 subtests, 70 contract tests, no contract skips.
  Ruff, formatting, mypy (128 source files), dashboard syntax, Forge build/fmt/lint passed.
- `.venv/bin/python -m coverage run -m pytest`: 1,004 passed, 108 subtests; aggregate coverage 89%.
- Dependency audit: initial package-index access failed; retry passed with no known vulnerabilities.
- Bandit at repository `-ll` threshold: no medium/high findings; 44 low findings remain in the scan.
- Secret gate: passed. Slither: 80 contracts, 102 detectors, zero results.
- Browser smoke check against temporary synthetic data: overview rendered and canonical decision
  evidence expanded correctly. This is not a full responsive/accessibility certification.

Existing non-failing output includes one Python warning and Forge lint suggestions/signature-cache
write warnings. No contracts were modified, deployed or broadcast.
