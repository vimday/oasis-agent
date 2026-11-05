# Live canary observation in the console

Open **实盘与对账** in the observation console. The upper panel projects the
direct Base canary's local state; the lower execution journal and HALT recovery
sections remain separate data sources. Missing one does not invalidate or
populate the other.

`configs/server.local.toml` now declares:

```toml
[databases]
live_state = "data/live/live.state.json"
live_pair_inspection = "data/live/pair-inspection.json"
```

The path follows the server's existing working-directory convention. It may be
absent at startup: the panel reports missing state and never creates it. Only
the existing operations database is server-owned. There is no new execution,
signing, halt-clear, pending-drop, funding or RPC capability.

## Read contract

`GET /api/v1/live/status` returns `oasis.live-status.v1` with a redacted view of:

- the most recent recorded account observation, its chain/account/pool/config
  binding, block hash and timestamps;
- recorded NAV, daily checkpoint gross loss, cumulative net capital loss and
  the configured capital/loss limits;
- pending transaction identity, nonce and recorded signing time;
- the locally recorded position and complete-gas counter;
- at most 20 recent action entries, with counts of declared cost/cash/position
  references. Reference payloads and arbitrary history/error text are omitted.

The reader uses the existing bounded server read worker. It opens only a regular
file, rejects final-path symlinks and FIFOs, caps reads at 2 MB, refuses duplicate
JSON keys, malformed displayed fields, non-finite financial values and files
changed in place during the read. Atomic replacement is seen on the next request.
Errors return fixed reason codes, never a filesystem path or raw exception.
Large gas values remain decimal strings so the frontend cannot round wei through
a JavaScript Number. API metadata links the new route to the execution view.

`last_observation` is written by the loop only after a real observation reaches
its loss-check step. A failed read, stopped tick or pending wait does not refresh
that timestamp. Successful or reverted settlement invalidates the pre-action
snapshot, requiring another observation before displaying a new NAV. Old state
files load without the field and remain observation-missing; mtime is never a
substitute. The API considers both observation time and block time, refusing
future/inconsistent clocks and marking data older than 120 seconds stale.

The UI distinguishes unconfigured, missing, invalid, halted, pending, stale and
recently observed records. An API or response-validation failure clears previous
numbers and transaction details. The active execution view refreshes with the
existing visibility-aware 30-second poll and supports manual refresh. The user
can inspect evidence-reference counts without automatically expanding action
history on every refresh.

## Release status

The response also carries `release` (`oasis.live-release.v1`), from the same pure
implementation report used by the execution gate and offline CLI. The owner
opened the bounded canary release on 2026-10-31; the UI shows that recorded
authorization separately from current runtime admission. The earlier text
"Its four remaining engineering blockers ... New live actions remain blocked"
described an older release and is superseded by the shared `live_release()`
report and `docs/verification/LIVE_STARTUP_READINESS_20261105.md`.
This report preserves the owner-approved
USD 500 capital / USD 200 daily / USD 500 cumulative limits; it neither cancels
that approval nor upgrades the recorded paper shadow into live profit.

## Interpretation limits

This is a local declaration, not an authenticated execution journal or independent
replay. The response always has `execution_eligible: false`,
`evidence_replayed: false` and `profitability: unresolved`. A recent observation
does not establish process liveness, current chain state, finality or permission
to trade. NAV is not strategy profit. Historical principal-only observations remain labeled as such. New
`two_feed_principal_and_fees` observations include both USD feeds and uncollected
fees, require a recorded NAV deadline at most 120 seconds from the block, and
require both feed limits at most 1,800 seconds. The older statement "NAV excludes
complete uncollected-fee valuation" applies only to legacy observations.
The two loss quantities intentionally preserve the owner's existing,
different daily and cumulative accounting conventions.

The loop's signing policy and action cash reconciliation remain separate from
this observation contract; their current production acceptance is described in
the retained live verification records and `LIVE_START_CHECK.md`. The console has
no action that can start a live trade or waive those requirements.

## Paired journal inspection

The strategy JSON alone can lag a committed authoritative journal. The same
read-only status endpoint now includes `pair` (`oasis.live-pair-status.v1`),
read from an explicitly configured redacted inspection artifact. Missing state
and missing pair inspection remain separate conditions. The observation server
never opens either production journal, an evidence/budget database, or a key.

An operator can export the artifact from the original read-only state mounts:

```sh
python -m scripts.inspect_live_state \
  --strategy-state /strategy/live.state.json \
  --authority-state /authority/state.json \
  --config /review/runtime-target.json \
  --pair-status-out /review/pair-inspection.json
```

Use a new output path each time; it is created exclusively. The existing CLI
exit contract remains unchanged: HALT or another listed blocker produces exit
3 even if a valid inspection artifact was exported. A corrupt or advancing
history refuses export. The exporter fully validates both hash-linked journals
and supported configuration epochs, confirms each JSON belongs to its own
journal, and compares its canonical hash with the latest committed state.
Private error text, transaction data, financial values and raw references are
omitted. No HALT, STOP, budget, state, journal or configuration is changed.

The exporter refuses WAL format, recovery sidecars, foreign ownership, shared
inodes and permissive file modes. It uses immutable read-only SQLite handles to
avoid recovery/sidecar writes, with all four original file identities, sizes and
mtime/ctime checked across both complete replays. This diagnostic requires
stable original files; it is not a replacement for a locked migration, a
cross-service transaction or an execution lease. SQLite warns that immutable
connections skip locking and change detection; our identity checks refuse a
concurrent change rather than trusting that declaration alone.
See [SQLite URI parameters](https://www.sqlite.org/uri.html#uriimmutable).

The server validates the artifact's strict schema, duplicate-free JSON and
canonical content hash, then derives configuration/projection discrepancies.
It does not authenticate the artifact's author or re-run the journal verifier.
The UI displays the recorded runtime epoch, both configuration/tip hashes,
projection lag, HALT, pending/signing uncertainty and incomplete gas costs.
It compares the inspection target with the current local startup configuration.
An inspection older than 300 seconds is stale. This allowance applies only to
the offline inspection display; it cannot extend any USD feed or NAV deadline.
`aligned` means that the recorded configuration bindings and projections agree;
it does not mean the services are running or eligible to trade.
