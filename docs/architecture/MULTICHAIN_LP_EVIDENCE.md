# Multi-chain LP and position settlement evidence

Updated: 2026-09-06. Scope: research and observation, not execution approval.

## Network boundary

Robinhood is the initial benchmark, not a restriction on the EVM accounting engine.
Base, Ethereum and other explicitly selected EVM deployments can use the same receipt,
trace, position and evidence interfaces. Network selection requires `chain_id` in TOML
or `OASIS_CHAIN_ID`; omitting both is now a configuration error. This intentionally
breaks the old implicit `4663` fallback. Existing explicit Robinhood configurations work.

Each chain must supply its own PoolManager, PositionManager, StateView, token/hook
identities and pinned runtime hashes. An address or PoolId on one chain does not
identify an asset/position on another chain. Configuration acceptance does not prove
v4 availability or production support. The same synthetic conformance tests run for
Ethereum, Base, Robinhood, BNB-chain IDs and testnet IDs without shared balances.
The user-provided abbreviation `bas` has not been resolved into a network identity.

Finality, RPC/trace capabilities, native gas currency, L1 data/operator/blob fees,
sequencer assumptions, price data, routing and capital limits are deployment-specific.
Robinhood Nitro assumptions do not apply automatically to Base or Ethereum. Unsupported
cost components remain unknown. There is no implicit bridge or cross-chain transfer.

The shared promotion gates apply independently to each chain and strategy deployment:
fixture, historical replay, pinned fork, shadow, canary, bounded production. A Base
Sepolia read cannot satisfy a Robinhood fork gate or establish mainnet profitability.

## Position receipt/trace adapter

`farming/position_settlement.py` implements a first core-delta evidence adapter:

1. Require an unresolved, timestamped ledger event with explicit chain, transaction,
   transaction index, log index, PoolKey, range, PositionManager sender and NFT salt.
2. Record chain ID, opening block header, full receipt, full Geth `callTracer`,
   hash-pinned PoolManager/PositionManager bytecode, then the same header again.
3. Reconstruct from raw JSON-RPC bytes. Check request identity, canonical quantities,
   header stability, receipt status and ordered log identities. Repeated target
   modifications in one transaction are currently rejected as ambiguous.
4. Bind a unique matching canonical `modifyLiquidity` calldata frame to the receipt
   event. Validate caller and call-tree ancestry, exact ABI encoding/return length,
   signed int128 token deltas and nonnegative accrued fees. Track execution context
   through CALL/STATICCALL, DELEGATECALL and CREATE/CREATE2; an implementation address
   must not become the sender of a proxy child call. PoolManager modifications must
   still be direct CALL frames. Reject other call types and reverted subtrees.
5. Store runtime code hashes and raw evidence in the existing content-addressed store.
   The hashes identify observed code; upstream equivalence and finality remain unresolved.
6. Attach the evidence to the matching event in `replay_captured_position_datasets` via
   its optional `settlements` argument. Reject another event/chain or duplicate capture.
   The replay payload schema is now `oasis.evidenced-position-replay.v2`.

The canonical PoolManager returns `callerDelta` and `feesAccrued`. Caller delta includes
principal, accrued fees and possibly hook deltas. It is owed to/from **PositionManager**;
it is not proof of the NFT owner's cash receipt. Neither principal nor owner payments
are inferred by subtracting fees or summing transaction-wide transfers.

`execution_gas_native_base_units` is `gasUsed * effectiveGasPrice` for the whole
transaction. It is not total chain cost or a per-NFT allocation. The retained receipt
allows a later chain cost adapter to interpret additional fields without inventing zeros.

`settlement_complete=false`, owner amounts and `net_pnl=null`, and
`execution_eligible=false` remain in all results. Missing trace expressed as a valid RPC
error retains the receipt/code evidence with `trace_unavailable`; null/malformed results,
transport gaps and inconsistent evidence fail validation. A raw-response reconstruction
is single-provider research, not independent security validation.

### Library use

Create a fresh `BoundedExchangeRecorder` and `JsonRpcTransport(recorder=recorder,
retries=0)`, then call `collect_position_settlement(transport, recorder, source=alias,
header=header, pool_manager=address, pool_key=key, entry=unresolved_entry)`.
Use `replay_position_settlement_evidence` to reload the retained raw bytes against an
explicit event scope; altered stored interpretations must fail reconstruction.
The transport must use that recorder. Collection has seven bounded requests and no
signer; retain failed sessions separately for investigation rather than fabricating data.
Pass successful captures to the replay pipeline, which validates them before persistence.

### Remaining settlement work

The next adapter must decode PositionManager actions and link `settle`/`take`, Permit2,
ERC-20 transfers, native transfers, refunds and recipient overrides to each NFT. It must
prove per-token balance conservation and ownership at the action time, handle multiple
NFTs/rebalances and reject ambiguous netting. Token semantics and runtime identity require
independent verification. Failed transaction costs, chain-specific total gas, pricing,
NFT ownership-transfer consideration, all-cost PnL, fork lifecycle and shadow remain open.

## Testnet probe

`PYTHONPATH=src python3 scripts/probe_v4_testnet.py --output <new-evidence-directory>`

This read-only script targets Base Sepolia only. It checks chain ID, resolves a block
32 blocks behind the observed head (a lag, not proof of finality), pins runtime reads,
and searches a bounded recent modification window (default 2,000 blocks; `--lookback`
permits at most 50,000, queried in chunks of at most 2,000). If a positive modification
exists, it captures the corresponding active NFT snapshot and receipt/trace evidence.
No matching event is reported as unresolved, not a successful settlement test. A mint
followed by a burn in the same block may cause the snapshot to reject; no fallback owner
is invented. Use a fresh output directory to preserve earlier runs.

Public endpoint and network identity: [Base documentation](https://docs.base.org/base-chain/api-reference/rpc-overview).
Addresses: [Uniswap deployment registry](https://developers.uniswap.org/docs/protocols/v4/deployments).
Delegate execution context: [EIP-7](https://eips.ethereum.org/EIPS/eip-7).
Trace format: [Geth callTracer](https://geth.ethereum.org/docs/developers/evm-tracing/built-in-tracers).
The probe uses system curl with HTTP/1.1 and TLS verification because the local Python
certificate/HTTP path and HTTP/2 transfer failed. It appends each RPC response immediately
with retrieval time and retains a validated snapshot even if later settlement capture fails.
It writes discovery wire data plus SQLite evidence and a JSON summary; it
never accepts a private key or submits a transaction.

Core semantics were checked against installed `@uniswap/v4-core` 1.0.2
`src/PoolManager.sol::modifyLiquidity`, with the reference commit
`46c6834698c48bc4a463a86d8420f4eb1d7f3b75` recorded separately in
`configs/v4-upstream.example.toml` (not independently re-fetched in this task).
The installation's package-lock integrity pins are distinct from deployed-code equivalence.
