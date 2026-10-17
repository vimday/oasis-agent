# Direct Base canary release boundary — 2026-10-17

**New live transactions remain blocked.** The four missing controls now have
implementations and executable tests. Production acceptance is incomplete;
`make live-readiness`, the console, the action path and the authority CLI enforce
the same closed release gate. A commit or a local fork result cannot open it.

The owner's 2026-10-08 trial-1 exception remains unchanged: dedicated Base account,
USD 500 committed capital, USD 200 daily checkpoint gross loss (UTC+8), USD 500
cumulative funded-basis net loss. The waived long OOS window and external audit
are not reintroduced. No sustained live profitability is claimed.

## Implemented controls and their evidence

| Control | Implemented behavior | Validation boundary |
| --- | --- | --- |
| Successful owner cash | `live/action_cash.py` replays complete bounded native call trees, refunds, ERC-20/NFT transfers, wallet snapshots and whole receipt costs. Both sources must agree; any residual preserves pending. | Unit cases and seven actual contract actions on a pinned **local** Base fork pass. The fork uses one Anvil node, artificial balances and explicitly synthetic zero OP fee metadata. It does not validate independent production trace providers or production OP costs. |
| Full transaction authority | `live/authority.py` independently captures NAV, derives the bounded action, re-encodes exact calldata, checks nonces/code/fees/capital/loss, retains two-source simulation and reserves the attempt before key use. The socket accepts full envelopes and public status requests; no digest endpoint or fallback exists. | Malformed, foreign, stale and over-budget proposals are refused before the ephemeral test key. Private Unix-socket integration and Docker confinement pass with synthetic markers. Production key provisioning, two independent production RPC services and live capture latency remain unvalidated. |
| Exact account NAV | `live/valuation.py` includes wallet balances, LP principal and accrued core fees, using the pool currencies' separate pinned USD feed rounds. Each leg floors to USD micros. `exact_risk.py` uses integer loss checkpoints; floats are display projections. | Unit coverage includes USDC depeg, modular fee growth, one-micro boundaries and conservative legacy migration. The live read-only check refused the USDC/USD feed under its completeness/freshness checks (1800-second maximum age); no replacement peg or relaxed age limit is used. |
| Durable recovery | `live/journal.py` commits configuration-bound hash-linked SQLite snapshots before replacing the JSON projection, rejects stale writers and detects inconsistent history. Signing reservations persist across crashes. Settled blocks and receipts are checked again before another action. | Crash/projection interruption, reservation, conflicting writer, missing/truncated journal and reorg tests pass. Lost replies can adopt matching public authority metadata and independently reconcile receipts; recovery never signs or broadcasts. A real authority process lost-reply/SIGKILL/restart drill passes with an ephemeral test key, including in Linux Docker. Deployed two-service recovery with real receipts remains outstanding. |

The canonical action encoder is shared for byte equality; the authority owns its
configuration, risk history and raw acquisition. Sharing implementation does not
make the two processes independent software implementations.

## Local fork reproduction

```sh
.venv/bin/python -m scripts.verify_live_action_fork \
  --fork-url "$BASE_ARCHIVE_RPC_URL" \
  --block 52598014 \
  --block-hash 0x37e02a6d9d38ca39ebdbf475a9ed154212e55cb8f525faeb96bace6426cdf0fe \
  --out reports/review-20261017/local-action-fork.json
```

The harness verifies the upstream block, starts loopback Anvil and verifies its
client identity before creating an ephemeral account. Funding and broadcasting
are restricted to that local node. The completed run covered USDC approval, both
Permit2 approvals, ETH→USDC, USDC→ETH, mint with refund and whole-position burn.
Original Anvil RPC responses are retained separately from the explicitly marked
zero-OP-fee fixture. Report, raw evidence and budgets are local artifacts under
`reports/review-20261017/`, not a production acceptance certificate.

## Operator recovery and deployment boundary

`make live-check` performs read-only two-provider observation without loading a
key or writing trading state. Its temporary NAV evidence is deleted afterwards;
it is an operational diagnostic, not retained release evidence.

`make live-reconcile` reconciles existing pending transactions only. For a lost
reply after the authority committed a signed transaction, use:

```sh
.venv/bin/python scripts/run_live_lp.py --mode live --reconcile-only \
  --signer-socket /absolute/private/authority.sock
```

This optional connection requests **public status only**. Account, transaction
identity, calldata hash, original intent, fee limits and original wallet-proof
block must match the strategy's durable reservation. An unknown key-provider
outcome without a committed transaction hash stays blocked. A transaction that
was signed but never broadcast is also retained as unresolved; this command does
not retransmit it or release its nonce. Unrelated halt reasons are preserved.

The operator authority entry point is `python -m scripts.run_live_authority`.
It requires its own private state/evidence directory, STOP file, separate key
source, bounded lifetime and signature count. Its release check precedes key
loading. The supplied socket permissions require the same UID; they provide
process separation, **not** protection from another unrestricted process with
that UID. Separate filesystem namespaces/container mounts must prevent strategy
and server access to the authority key and journal before deployment. A local Docker confinement drill with synthetic markers now passes; see
`deploy/live/README.md`. Production key provisioning remains separate.

The existing acquisition budget is 4096 requests per provider with a 3000 ms
interval, persisted across restarts. Independent providers now acquire concurrently, and signing preparation can reuse
its retained decision-block wallet proof. Direct head/nonce/receipt reads now share
the retained-acquisition quota and have no automatic retries. An explicit RPC pacing profile is
available; the existing default budget and any existing policy lock remain unchanged.
Capture plus simulation still must finish inside the 120-second freshness bound. Production RPC capacity,
trace support and capture latency must be measured and addressed before release;
restarting must not reset the budget or make stale evidence acceptable.

Base admission includes observed L1 upper-bound and operator fees alongside the
EIP-1559 gas ceiling. The L1 estimate follows the
[Optimism Fjord predeploy specification](https://specs.optimism.io/protocol/fjord/predeploys.html).
It is a bound observed at the pinned block, not a cap on future L1 fee movement.
Receipt accounting still requires actual complete chain-specific costs.

Legacy monetary baselines migrate conservatively by ceiling to micros and carry
a migration marker. This preserves declared losses, but does not turn historical
float declarations into independently verified on-chain accounting. Real retained
account history and complete position valuation still require operational acceptance.

## 2026-10-17 deployment follow-up

Local Docker was selected by the owner. Container confinement and a real authority
process SIGKILL/restart test passed with synthetic inputs; no real key was used.
Free-provider discovery is bounded to four requests per probe and eight total per
provider, persisted across runs. The owner explicitly requires conserving paid
RPC spend; no polling of paid providers or automatic paid fallback is enabled.
See `deploy/live/README.md` for the implemented package and outstanding validation.
