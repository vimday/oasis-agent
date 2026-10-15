# Live canary observation in the console

Open **实盘与对账** in the observation console. The upper panel projects the
direct Base canary's local state; the lower execution journal and HALT recovery
sections remain separate data sources. Missing one does not invalidate or
populate the other.

`configs/server.local.toml` now declares:

```toml
[databases]
live_state = "data/live/live.state.json"
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
implementation report used by the execution gate and offline CLI. Its four
remaining engineering blockers are displayed even if the state file is absent.
New live actions remain blocked. This report preserves the owner-approved
USD 500 capital / USD 200 daily / USD 500 cumulative limits; it neither cancels
that approval nor upgrades the recorded paper shadow into live profit.

## Interpretation limits

This is a local declaration, not an authenticated execution journal or independent
replay. The response always has `execution_eligible: false`,
`evidence_replayed: false` and `profitability: unresolved`. A recent observation
does not establish process liveness, current chain state, finality or permission
to trade. NAV excludes complete uncollected-fee valuation and is not strategy
profit. The two loss quantities intentionally preserve the owner's existing,
different daily and cumulative accounting conventions.

The actual loop's signing policy and successful action cash reconciliation remain
separate unfinished work, as described in `LIVE_START_CHECK.md`. The console has
no action that can start a live trade or waive those requirements.
