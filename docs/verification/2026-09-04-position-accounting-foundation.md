# Position Accounting Foundation Verification

**Status:** offline foundation verified; live deployment identity, traces, fork behavior, and profit
evidence remain unresolved  
**Date:** 2026-09-04

## Implemented boundary

The position path now includes:

- EIP-1898 `blockHash` plus `requireCanonical` calls, preventing multiple position reads from silently
  mixing different blocks at a reused height;
- official PositionManager `tokenId`-as-salt position-key derivation and packed `PositionInfo` decoding;
- block-pinned `ownerOf`, `getPoolAndPositionInfo`, `getPositionLiquidity`, StateView position state,
  and fee-growth-inside reads with exact ABI return-length checks;
- canonical `ModifyLiquidity` decoding, including the legitimate zero-liquidity fee-poke case;
- replayed position liquidity with PoolId, PositionManager, range, salt, NFT owner, block, event order,
  and closing-snapshot checks;
- a settlement ledger that refuses to emit token totals when even one action lacks independently
  retained, block-pinned settlement attribution;
- exact integer TickMath vectors and concentrated-liquidity principal amounts for below, inside, and
  above-range states, with contract-compatible mint/removal rounding choices;
- structured accounting output with opening and closing principal, uncollected fees, valued cash
  flows, external costs, and net PnL rather than one unsupported yield number;
- a four-cell static/dynamic-fee and fixed/active-range comparison object that rejects mixed dataset,
  block window, capital, replay engine, or flow-response assumptions.

## Canonical references

The implemented semantics were checked against these exact upstream revisions:

- `v4-core` commit `46c6834698c48bc4a463a86d8420f4eb1d7f3b75`, including `IPoolManager`,
  `Position`, `TickMath`, and `SqrtPriceMath`;
- `v4-periphery` commit `dce236d4e2057422d0791d9a973a58765eb46f65`, including
  `IPositionManager`, `PositionManager`, `PositionInfoLibrary`, `StateView`, and `LiquidityAmounts`.

The official deployment registry currently maps Robinhood Chain to PoolManager
`0x8366…0951`, PositionManager `0x58da…4fa7`, and StateView `0xf333…673b`. The commit pins define the
differential reference, but the registry does **not** by itself prove exact source commit, compiler,
constructor immutable, and runtime-bytecode identity. That identity remains an explicit promotion
blocker in `configs/v4-upstream.example.toml`.

A single-public-RPC reconnaissance at block `53,907,118`, hash
`0x6ac41f945c1a6ccf4595a37a4cb077ea9a31ad50f683a8a2197ab20b129baad9`, confirmed EIP-1898
`requireCanonical` support, nonempty runtime code at all three addresses, and that both periphery
contracts' `poolManager()` immutables point to `0x8366…0951`. Runtime sizes and keccak hashes are
recorded in the manifest. This useful check is still not the required two-provider retained evidence.

## Important accounting semantics

`ModifyLiquidity` does not emit token settlement amounts. It emits position identity and liquidity
change, and a zero change is legal for a fee poke. Therefore neither pool event deltas nor ERC-20
transfers may be casually assigned to one NFT in a batched transaction. A ledger entry is settlement
complete only when a position-scoped trace or a proven transaction-exclusive attribution is retained
as evidence. Partial attribution produces `UNRESOLVED_SETTLEMENT`, never a partial PnL.

Pool square-root price values the raw currency base units. Conversion to display units, USD, or any
other numeraire requires token-decimal metadata and separate price evidence; it is not silently mixed
into settlement arithmetic.

## Verification result

At this checkpoint:

```text
make check
115 passed, 19 subtests passed
ruff lint/format: pass
mypy strict: pass
git diff --check: pass
```

## Still required before shadow or capital

- resolve and verify Robinhood PositionManager/StateView addresses and deployed bytecode identity;
- acquire a two-provider plus archive block range and persist raw request/response envelopes;
- ingest transaction inputs, receipts and traces, and prove position-scoped settlement attribution;
- reconcile event replay to authoritative slot0, tick, liquidity, fee-growth, balances, NFT owner,
  and position snapshots at checkpoints;
- run the four counterfactual cells through one executable simulator with an explicit fee/route-flow
  elasticity model, gas and L1 calldata costs, LVR/adverse-selection attribution, and held-out data;
- differential-test the Python math against the pinned Solidity sources and add fuzz/property runs;
- implement and compile the minimal hook/executor, then pass unit, fuzz, invariant, fork, shadow,
  halt, and unwind gates.
