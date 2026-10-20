# Historical commitment recovery — 2026-10-20

Continues [historical trace acceptance](LIVE_TRACE_QUORUM_20261020.md).
This change closes the recovery-order limitation documented in
[keyless status recovery](LIVE_KEYLESS_STATUS_20261019.md): when authority settles
and clears its pending transaction before strategy recovers a lost signing reply,
strategy can now retrieve the original public commitment by its reserved nonce.

No production key/state volume was opened and no mainnet transaction was signed
or broadcast. **Zero RPC requests** were made in this continuation. Cumulative
paid acceptance usage remains eight requests; no budget was reset or increased.
All release gates and the USD 500/200/500 capital/daily/cumulative limits remain
unchanged. This is mechanism validation, not real-canary acceptance.

## Behavior and trust boundary

The canonical account-bound status request optionally carries one integer nonce
in `[0, 2**64)`. Boolean, float, negative, oversized and string values refuse
before the client opens a socket. Missing nonce preserves the existing current-
pending behavior. The keyless server exposes only its existing status callback
and an optional historical reader; neither callback signs or broadcasts.
Connection count, TTL, frame-size and partial-frame deadline limits still apply.

Strategy recovery sends the nonce from its own durable signing reservation.
The authority reads the full configuration-bound hash-linked journal in a SQLite
read transaction, validates all entries including those after the match, and
returns a snapshot only if every occurrence of that nonce has the same pending
commitment. Conflicting transaction/intent/provenance/timestamp metadata refuses.
It does not rely on the 200-entry display history or stop verification at the
first matching snapshot. A lookup does not advance the caller's journal writer
revision, so a stale writer still fails compare-and-swap after a historical read.

The selected snapshot is decoded through the existing strict state/transaction
types. Missing commitments and snapshots with unresolved signing outcomes refuse
adoption. The response also retains the **current** authority signing uncertainty
flag, so an old known transaction cannot hide a new unknown signing attempt.
The production authority and keyless CLI share this status implementation.

History supplies public metadata only. It does not restore authority pending,
change either ledger's counters or certify settlement. Strategy still checks
account, chain, nonce, action/intent, destination, value, calldata, fee/gas bounds
and its original opening-wallet proof pair. It then independently reconstructs
the transaction receipt, complete gas and cash through its existing two-source
reader. Failure leaves the adopted pending available for reconciliation and
never requests another signature or retries broadcast.

## Verification

The retained-raw-proof integration test first settles a synthetic approval in
authority: one action, 218 wei full cost, no pending. Strategy recovers the old
commitment from history but initially retains zero actions and zero gas; only
its own receipt/cash verification advances those counters to one and 218.
Repeated adoption is refused. The authority journal remains byte-identical
during lookup, and missing nonce lookup returns no commitment.

Additional regressions reject conflicting commitments, corruption after the
matching entry, foreign configuration, invalid nonce encodings and unknown
current signing status. The stale-writer test proves that historical lookup
cannot refresh compare-and-swap authority.

The existing lost-reply process test now exercises both current and historical
queries. It signs only with a random fixture key, drops the socket reply,
SIGKILLs the signer, then starts the real keyless status CLI in a new process
without passing a seed. In the historical variant, a separate synthetic state
publication clears pending before restart. This publication models the ordering
boundary; the raw receipt settlement itself is covered by the integration test
above. Key loading, signing-authority construction, release promotion and RPC
construction are forbidden in the restarted process. Its journal is unchanged.

- Host checks: **3093 tests and 138 subtests** passed, Ruff/format, mypy for 229
  source files, secret scan, Bandit at the configured medium/high threshold and
  frontend syntax for eight modules. One existing websockets deprecation warning.
- Linux checks: **90 focused tests** passed with network disabled, read-only
  root, dropped capabilities, no-new-privileges and UID/GID 10001. No production
  keys/state were mounted.
- Linux base image:
  `sha256:20f5cebf23a5234cd7d6ca6110a13c112b4198e77681f7c05489f2ac4e3960eb`,
  with ten explicit source/test overlays mounted read-only. This was not a new
  production image. An initial network-disabled build could not install missing
  cached dependencies, so verification used the already installed image instead.
- Local logs, overlay hashes and readiness output:
  `reports/review-20261020/historical-recovery/`.

The shared CLI/frontend readiness description and deployment recovery instructions
now reflect historical lookup. All four release blockers remain. Fresh valid
USD valuation, dedicated-account complete NAV/cash, production two-provider
simulation and deployed dual-service recovery against a real canary receipt are
still unaccepted. No independent reviewer or external audit is claimed.
