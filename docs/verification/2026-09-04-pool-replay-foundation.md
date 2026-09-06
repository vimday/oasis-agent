# Pool Replay Foundation Verification

**Date:** 2026-09-04  
**Status:** offline deterministic foundation; not live-chain accounting evidence

## Implemented slice

This slice implements steps 1–4 of the Phase 1 replay sequence:

- timestamped block snapshots with number, hash, and parent hash;
- contiguous same-chain lineage and nondecreasing timestamp validation;
- PoolId-checked initialization envelopes with transaction/log position;
- block-pinned Swap envelopes with transaction/log position and effective fee;
- a pure pool-state reducer;
- deterministic replay across blocks with and without swaps;
- RPC preservation of block parent hash, log block hash, and transaction index;
- transition records for amount0, amount1, effective fee, `sqrtPriceX96`, exact Q192 price numerator,
  tick, and liquidity;
- cumulative signed token deltas and swap count.

## Enforced invariants

- The initialization PoolId must equal the PoolId derived from its PoolKey.
- Every replay block is contiguous and links to the preceding block hash.
- Events must reference the exact supplied block number/hash/timestamp.
- Events are supplied in strict `(blockNumber, transactionIndex, logIndex)` order.
- Duplicate positions, cross-chain events, cross-pool events, unpinned block hashes, and events before
  initialization fail closed.
- Swap deltas cannot both be zero and two nonzero deltas must have opposite signs.
- Price, tick, liquidity, and effective fee remain within their protocol/wire bounds.
- Blocks without swaps still advance the final state's block identity.
- Identical inputs produce an equal replay result.

## Explicit limitations

- Input is deterministic fixture data, not RPC-acquired evidence.
- The RPC client can acquire complete individual block snapshots and preserves replay-critical log
  fields, but it does not yet persist a validated contiguous range as an immutable replay dataset.
- Reorg replacement and finality state are not yet modeled.
- Only Initialize and Swap state are reduced. Modify-liquidity, donate, fee-growth, protocol-fee,
  token-transfer, gas, and PositionManager events remain absent.
- Liquidity is observed from Swap events; it is not yet independently reconstructed from liquidity
  modifications and tick state.
- The output is a pool transition ledger, not position-level accounting or profitability evidence.

## Verification

```text
python3 -m pytest -q tests/unit/test_replay.py
make check
git diff --check
```

Result:

```text
5 replay tests passed
78 total tests passed, 15 subtests passed
Ruff passed
Ruff format check passed
mypy passed for 40 source files
compileall passed
git diff --check passed
```

The next safe slice is a bounded range collector plus reorg-aware persistence and provider agreement.
PositionManager/NFT ownership and position ledgers should consume that reconciled dataset rather than
building on unpinned `latest` reads.
