# Account reconciliation and signer risk connection

Design: 2026-09-22, before implementation. This is a guarded integration boundary,
not a claim of deployed full-account indexing or live execution.

A trusted, independently operated account reader must enumerate the configured accounts
and their entire asset/position/debt inventory, replay opening/closing balances and all
movements (including failed-transaction gas), and value every line with pinned evidence.
Missing inventory, unknown pending operations, residuals, stale prices or missing chain
coverage must reject. A strategy cannot supply an approval boolean to this reader.

The global journal uses a fixed account/asset policy and owner USD limits. It reconstructs
NAV from integer asset units and rational USD-micro prices; assets round down and debts
round up. Cash-flow-adjusted negative NAV changes consume loss budget; gains never refill
it. Sum losses across accounts before testing limits. UTC+08 day boundaries require an
explicit boundary snapshot, rather than silently charging prior-day loss to the next day.

All account frames advance together. Their block identities, time skew, continuity and
source evidence are retained. The initial frame establishes the loss-budget inception;
it cannot prove pre-inception losses. Deposits/withdrawals and bridge classifications must
be independently replayed by the reader. Unresolved bridges block admission.

Snapshot ingestion, loss-latch evaluation and worst-case USD reservation share one SQLite
BEGIN IMMEDIATE transaction. Pending commitments survive restart and provider failure, and
nothing here is time-released: there is no timeout or automatic settlement. A caller may
explicitly release one commitment by request hash when it knows no transaction was produced;
a release only withdraws reserved capital and never weakens recorded losses or a recorded halt.
A subsequent snapshot may conservatively double-count pending capital already invested; that
blocks availability until a verified settlement adapter is implemented.

The signer calls an independently configured risk guard after static checks and before
requesting any signature. Missing guard, reader failure or risk rejection denies. The guard
binds the full EIP-1559 request hash, independently priced maximum incremental commitment
including gas, account, chain, current block and global policy. Its policy digest is pinned
in the signer journal. An attempt that wins the nonce reservation but produces no transaction
releases its own reservation; a successful signature keeps it until an independent
reconciliation releases it. The default CLI loads no reader or guard and remains unable to sign.

Trust boundary: typed reconciliation inputs and hashes do not themselves prove raw-RPC
truth or inventory completeness. A production source must replay those proofs and run with
independent credentials/permissions. No caller-supplied JSON approval adapter is provided.
This change does not implement the missing all-asset chain indexer, independent LP prices,
bridge settlement service, OS isolation or production release approval.
