# Keyless authority status recovery — 2026-10-19

This follows [the bounded provider acceptance](LIVE_ACCEPTANCE_20261019.md).
No production key or state volume was opened, no mainnet transaction was signed
or broadcast, and **zero RPC calls** were made. The cumulative acceptance paid
usage remains four calls; no budget was replaced or renewed. Release gates and
price-age/capital/loss limits are unchanged.

## Problem and change

After a signing reply is lost, strategy retains a signing reservation and needs
the authority's durable public transaction commitment. Previously the status
socket could only be served by starting the regular authority, which required
the production release gate, RPC setup and a key provider. Receipt-only recovery
could not supply this socket. Restarting signing capability is unnecessary for
reading the already recorded commitment.

`run_live_authority --status-only` now binds an existing configuration-matched
journal under the normal exclusive authority lock and serves only its public
account, chain, pending commitment and unresolved-signature flag. It refuses key
options, reconciliation mode and missing journals. It returns before RPC setup,
key loading or construction of `TransactionAuthority`.

The separate status server accepts only one exact canonical status request for
the configured account. It has no signing callback. Every accepted connection,
including refused requests, spends one slot; oversized replies are refused.
Its frame reader checks the absolute deadline between reads, so a client cannot
keep it alive by slowly delivering bytes. The deployment uses 12 connections and
60 seconds, then stops without automatically restarting.

Strategy now also requires `signing_unresolved` to be explicitly false before
adopting a public commitment. Missing or uncertain authority outcomes leave the
original strategy reservation untouched. Existing account/chain/nonce/action/
calldata/value/fee and opening-wallet-quorum checks still apply. No recovery path
re-signs or broadcasts.

## Deployment

The `authority-status` Compose service has no network and an empty environment,
mounts only authority state and the private socket, and never mounts the key
volume. `strategy-status-recovery` mounts its own state and that socket; it uses
the original strategy RPC budget for subsequent receipt verification. Both run
as UID/GID 10001 with read-only root, all capabilities dropped, no-new-privileges
and no automatic restart.

The regular services must be stopped first. Lost-reply adoption runs while the
authority still retains pending; authority receipt settlement follows afterward.
The exact commands are in [the deployment README](../../deploy/live/README.md).
If authority already cleared pending after settlement, the status endpoint does
not search historical journal entries and automatic adoption remains unresolved.
This is an explicit limitation, not permission to clear a reservation.

## Verification

The real-process lost-reply test now signs one synthetic transaction with a
fresh random fixture key, drops the socket reply, waits for the durable pending
commitment and SIGKILLs that process. The replacement process receives **no key
argument** and runs the actual status-only CLI. Key loading, RPC construction,
release checking and signing-authority construction are forbidden by the test.
It returns the same public transaction commitment, exits at its one-request
limit and leaves the authority journal byte-for-byte unchanged.

Protocol tests cover valid status, signing requests, a foreign account,
noncanonical frames, oversized replies, finite request/TTL bounds and a slow
partial frame reaching its absolute deadline. CLI tests reject key options and
missing journals. Adoption tests refuse uncertain and missing authority signing
status without mutating the strategy state. The existing two-process receipt
publication crash test remains separate and uses synthetic raw proofs.

Linux validation image:
`sha256:20f5cebf23a5234cd7d6ca6110a13c112b4198e77681f7c05489f2ac4e3960eb`.
All 42 focused Linux tests passed with no network, no production-key/state
mounts, read-only root, no Linux capabilities and UID/GID 10001.
Final host checks passed: 3069 tests plus 138 subtests, Ruff/format, mypy and
frontend syntax checks for eight modules. One existing websockets deprecation
warning remains. The first lint run caught a test-local import; it was moved
before this final full run, with no runtime behavior change.
The Compose configuration was expanded with placeholder endpoints and its mounts,
network, environment, user, capabilities and commands checked without starting
production services. Secret scanning and Bandit passed.

## Remaining production acceptance

This closes the keyless **status access** gap, not the real-canary acceptance.
Complex dual-provider LP trace/cash proof, fresh valid USD price evidence and
receipt recovery by deployed services against a real canary transaction remain
unaccepted. Earlier dRPC HTTP 408 and stale USDC/USD observations are retained;
no live probe was repeated. Synthetic tests are not live profitability evidence,
and no independent reviewer or external audit is claimed.
