# Bounded live canary: operational check and settlement boundaries

Run `make live-check` from the repository root. This resolves the configured primary
and secondary RPC endpoints, observes the dedicated Base account, and prints one
JSON report. Exit 0 means the implemented operational checks passed at that instant;
exit 3 means a refusal. It never constructs or contacts a signer, writes a loop
state, clears a halt, creates an evidence database, or submits a transaction.

The check covers the stop file, retained halt, legacy incomplete gas, pending
commitment, two-provider market/account observation, fresh head, closing block
identity, runtime pins, gas reserve, pool/oracle deviation, recorded position
liquidity, unexplained unit changes, existing loss checkpoints and account nonces.
Provider errors are redacted. Different endpoint strings are required but are
not proof of operational independence. Read access to the configured endpoints is
required; credentials are never printed.

`operational_checks_passed` is deliberately separate from `execution_eligible`,
which remains false on this observational report. The check does not verify a
signer's availability, reserve a nonce, establish complete economic accounting,
or grant a release. State can change immediately afterward. Its output is a
diagnostic snapshot, not retained raw-RPC evidence suitable for offline replay.

`make live-readiness` reports the current implementation blockers offline and
exits 3. `make live` now refuses before RPC resolution, state/lock creation or
signer construction. Supplying a socket or clearing a halt cannot waive the
release gate. The internal new-action method applies the same gate.

`make live-reconcile` performs one recovery pass on an existing pending transaction
without constructing a signer. It retains cost/position evidence, but clears
pending only when complete cash reconciliation succeeds (currently reverted
transactions and bounded approvals). Successful swap/mint/burn remain pending.
This command does not clear a halt or start another tick. Live `--drop-pending`
is refused: a provider returning no receipt and an unused nonce does not revoke
an already signed transaction or prove it can never land.

`make live-stop` places the STOP file; a stopped or halted loop does not
automatically unwind an existing LP. See [the remaining release work](LIVE_RELEASE.md).

## Signed intent and pending recovery

Before broadcast, `send_transaction` derives a public `SubmittedTransaction` from
the actual signed EIP-1559 bytes, including recovered account, transaction hash,
chain, nonce, target, native value, calldata hash and gas envelope. Its callback
must persist the pending item before the broadcast method is called. Failure to
persist aborts submission. No raw signed bytes, signature scalars or private key
are retained in this metadata.

The pending hash binds those fields, the exact configuration, local action terms,
the prior recorded position, and both pre-sign wallet proof references. The
current hash schema is `oasis.live-pending-plan.v2`. Changing any of those before pending resolution
refuses settlement. This detects accidental drift within the local state trust
boundary: it does not authenticate a maliciously rewritten state file or replace
independent signer policy. A legacy pending item without this commitment refuses;
do not synthesize metadata or discard pending to enable another action.
The older v1 hash cannot be automatically promoted to v2: it did not bind an
observed pre-sign wallet baseline, and recovery must not fabricate one.

Both provider chains are checked before transaction preparation. Settlement also
checks receipt gas against the signed envelope and retains the public transaction
metadata with the action history.

## Pre-sign wallet and reverted cash

Before calling the signing path, the loop retains two agreeing raw wallet
snapshots at the decision block hash: undelegated EOA code, nonce, native balance,
USDC balance/runtime/implementation and decimals, with opening/closing headers.
The balances must match the decision and the signed nonce must match the retained
nonce. Capture, replay or publication failures prevent submission. USDC and
Permit2 allowance decisions also require both providers to agree at that same
canonical block hash, including Permit2 expiration and nonce. Allowance reads
remain operational reads, not retained settlement evidence.

A failed transaction requires more than a gas receipt. Two raw interval proofs
must connect the pre-sign snapshot to the receipt block through every parent
header. The account nonce must increase by exactly one, native balance must
decrease by exactly the full retained transaction cost, and USDC must be unchanged.
Every block is checked for incoming/outgoing owner USDC or PositionManager NFT
Transfer events; any event, including offsetting token movements, refuses this
narrow reconciliation. Account code must be empty and USDC identity unchanged
at both endpoints. This is an EOA revert adapter, not general native-flow tracing.

The interval is limited to 128 blocks. Out-of-bound intervals, contradictory or
missing data, and persistence failure keep pending unresolved without counting
gas or resetting wallet units. Proofs can be retained before a later check fails;
their presence alone is not a state transition. Successful reconciliation retains
both `live_revert_cash` references and installs the proven receipt balances as
the next baseline. An unrelated later wallet change therefore cannot disappear
behind a fresh baseline. Loss checkpoints still apply the owner's existing policy.
These calls share the persistent receipt RPC budget; historical-state support,
provider latency and budget exhaustion can prevent automatic resolution.

## Successful approval settlement

A successful USDC/Permit2 approval now needs a `live_approval_cash` proof from each
provider. The signed target, zero native value and calldata hash must reconstruct
the exact bounded intent. USDC may approve only the configured Permit2; Permit2
may approve only the configured PositionManager or Universal Router. The amount
must not exceed the configured approval cap, and Permit2 expiry must be the
pre-sign decision block's timestamp plus the configured approval duration.

The proof reuses the same gas-only wallet and complete transfer-free interval
checks as a revert, but requires receipt status 1. It additionally reads the
closing allowance at the receipt block hash. USDC allowance must equal the
intended amount. Permit2 amount and expiration must equal the intent, its nonce
must equal the raw opening allowance nonce, and its runtime must match the
configured pin at both endpoints. An already expired allowance refuses.

Both proofs are replayed before counters change or pending clears. Their receipt
wallet balances become the next baseline; the preceding NAV observation is
invalidated until a new observation. Missing baseline, changed allowance, wrong
spender/calldata/value, unexplained balance movement or incomplete interval keeps
pending unresolved. This adapter accepts neither swaps nor liquidity operations.
The console displays the recorded proof-reference counts without claiming that
the server independently replayed them.

## LP position outcome

Successful mint/burn now needs more than a matching NFT Transfer log. Both providers
must prove the intended position state at the receipt's own block hash, using
fresh raw recording sessions and pinned PositionManager/StateView runtimes:

- Mint: exactly one minted NFT, its owner, complete PoolKey, pool prefix, range,
  absence of a subscriber, PositionManager liquidity and core position liquidity
  must match the intended result.
- Burn: the recorded NFT's position info must be cleared and liquidity in the
  **original** core position ID (manager, range and token ID salt) must be zero.
  The expected owner-to-zero NFT burn log is still required.

Opening and closing block headers must match, including timestamp and parent hash.
All position calls use EIP-1898 block hashes with `requireCanonical`. Both sources
must validate before either position proof is retained. The `live.receipts.db`
store now also contains `live_position_outcome` records with raw request/response
envelopes; `replay_position_outcome` re-derives them offline. Full cost and position
references are in each resolved LP action's state history. They share the bounded
receipt acquisition budget; exhausted budget leaves pending unresolved.

Position/cost proof failure keeps pending and the previous position, without
incrementing gas or action counters. Restart reacquires the missing proofs. A
cost proof can exist before a later position proof refuses; it is not a state
transition. Observed ownership/pool/range changes refuse new actions; disappeared
or changed liquidity halts even while the price remains in range.

## Remaining production work

- Whole owner cash reconciliation, including native-value refunds and swaps,
  must be integrated before treating action settlement as complete accounting.
  Successful swap/mint/burn now refuse settlement and preserve pending, counters,
  position and wallet baseline. Reverted actions and successful approvals preserve
  proven receipt balances when their full evidence checks pass.
- Digest isolation does not enforce transaction intent in the signer process.
  The separate policy sidecar/global account ledger is not this legacy canary loop.
- NAV is an ETH/USDC/principal mark using its declared oracle assumptions; it does
  not include full uncollected fee valuation or establish net strategy excess return.
- Three-block receipt depth is not Ethereum finality. A production reorg recovery
  service and a fully hash-linked live execution journal remain separate work.
- A public-provider observational check and synthetic tests prove no live profit.

Owner-approved limits and canary exception remain as recorded in
`docs/strategies/PHASE1_LP_EXPERIMENT_LOCK.md`; this work does not raise them.
