# Historical feed-policy migration

This is the one supported configuration transition: USDC/USD maximum age from
90000 to 1800 seconds, with ETH/USD remaining at 1800 and all other configuration
fields unchanged. The transition appends to each original journal, retains its
old rows, and leaves both services HALTed. It cannot acquire RPC data, load a key,
sign, broadcast, remove STOP or start services.

## Required retained inputs

- Both original, distinct service journals and their original evidence databases.
- Both services stopped, their existing owned service locks, and a regular `STOP`
  file beside each state file. Application holds both locks simultaneously.
- Two fresh NAV references per service, already retained in its evidence database,
  from the configured two providers. Both services must agree on position, block
  and complete NAV. Raw observations must satisfy both 1800-second feed ceilings
  and the stricter 120-second decision freshness window at application time.
- Historical checkpoint/cash proofs, reconciled inventory, no pending transaction,
  unresolved signing attempt or unacknowledged flow, and unexhausted loss limits.

The fresh reference JSON has exactly `strategy` and `authority` keys, each holding
two serialized `EvidenceRef` objects. It references raw records; it cannot replace
them with prices or a declared NAV. The operator must acquire those records under
a separately authorized, cost-verified request budget. An expired or exhausted
grant is not renewed by migration.

## Review and explicit application

The command defaults to read-only review:

```sh
.venv/bin/python -m scripts.migrate_live_feed_transition \
  --strategy-state /original/strategy/live.state.json \
  --strategy-evidence /original/strategy/live.receipts.db \
  --authority-state /original/authority/state.json \
  --authority-evidence /original/authority/evidence.db \
  --fresh-nav /operator/retained-fresh-nav-references.json
```

After reviewing the actual inputs, explicit application adds `--apply --out` with
a new attempt directory. No wrapper should retry this command automatically.
The command fsyncs an unresolved attempt record before mutation, locks both
services, verifies their original journal tips, replays the evidence, and checks
freshness again immediately before each journal commit. It preserves funded
basis, external flows, accumulated loss accounting, gas, positions and history;
the action counter only rolls over when the observed risk day changes.

The two SQLite transactions are **not atomic together**. A process failure can
leave one service migrated and the other on its prior policy. Both remain closed.
`unresolved_inspect_both_journals`, an incomplete attempt, or a missing final
report requires read-only inspection of both original journals. Do not erase an
epoch, recreate a journal, rebind its configuration manually, or reset a budget.
An old-config writer cannot append after the transition. A stale JSON projection
can be reconstructed from the already committed journal without undoing it.

Exit 0 means the requested review passed or both journal migrations completed
HALTed; it does not mean live execution is permitted. Exit 3 is a refusal or an
unresolved attempt. Preserve the attempt and original evidence in either case.
Neither a migrated state nor a dashboard status substitutes for current release,
signer, receipt, risk, custody and operational gates.
