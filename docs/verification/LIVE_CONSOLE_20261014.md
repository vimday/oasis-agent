# Live console and business-state observation — 2026-10-14

## Confirmed changes

The direct canary now retains a timestamped, configuration-bound account
observation at its loss check and invalidates it after settlement. The observation
API has a bounded, redacted read-only projection of that state. The console's
**实盘与对账** panel consumes it independently of the generic execution journal,
showing risk metrics, pending, recorded position and evidence-reference counts.
Read failures clear old data and missing state does not become zero balance.

The read model keeps the observation server free of signing/broadcast imports.
It neither loads wallet material nor opens the live state for writing. Limits
are displayed from the recorded configuration; no trading or risk policy was
raised. All response outcomes retain closed execution eligibility and unresolved
profitability.

## Validation

Tests exercise the actual API and frontend module, including missing/invalid
files, duplicate keys, symlink/FIFO refusal, file-size bounds, future/stale clocks,
unknown/legacy observations, budget exhaustion, non-finite/negative/boolean
financial data, sensitive-field omission, large gas integers, atomic file
replacement, failed-poll clearing and route-to-console metadata. A loop regression
checks that failed observation does not refresh its timestamp.

The browser check used an isolated temporary observation server on loopback with
no RPC chains, no operation token and a separate temporary operations journal.
The configured actual live-state file was absent. The visible page correctly
reported **尚无实盘记录**, with unknown NAV, losses, pending and position. Manual
refresh preserved this result; no state file was created. The screenshot is
`reports/review-20261014/live-console/dashboard.jpg`.

Full check logs and source/log hashes are retained in
`reports/review-20261014/live-console/validation.json`. The first Python gate
stopped on one overlong line in the new frontend test; the correction and final
full run are recorded separately. No Solidity source changed. No live trading,
funded run, profitability result or security audit is claimed.

Usage and the API's declaration-versus-evidence boundary are documented in
`docs/operations/LIVE_CONSOLE.md`.
