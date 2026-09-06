# Hook Contract Foundation Verification

**Date:** 2026-09-05  
**Status:** local research foundation; `RESEARCH` only  
**Authorization:** no deploy, signing, broadcast, live configuration, or capital movement

## Implemented boundary

`contracts/` now contains a minimal Phase 1 Hook and CREATE2 factory with these enforced properties:

- the Hook address carries only `beforeInitialize`, `beforeAddLiquidity`, and `beforeSwap` flags;
- the first valid configuration permanently binds the Hook to one PoolId;
- initialization is restricted to one immutable initializer and a preconfigured dynamic-fee PoolKey;
- swaps are public and fee selection is independent of trader identity;
- LP additions must originate through the immutable PositionManager and are authorized by the current
  owner of the exact NFT identified by the canonical tokenId-as-salt convention;
- NFT pool and tick range are checked against the callback before an addition is accepted;
- removal callbacks are disabled, so pause, whitelist revocation, or governance loss cannot block the
  canonical removal path;
- the guardian can pause and revoke but cannot unpause, configure fees, or add an LP;
- fee versions only increase and all configured fees are capped at 100,000 pips (10%);
- `previewFee` and `beforeSwap` share the same deterministic implementation, state hash, version, and
  five-second quote validity;
- CREATE2 deployment rejects any predicted address whose permission bits differ from the declared
  minimal surface.

## Defects found and closed

The first Hook draft compared `abs(amountSpecified)` with one pair of raw thresholds. In Uniswap v4,
the specified amount is denominated in the input token for exact-input swaps and the output token for
exact-output swaps. One threshold therefore silently compared token0 and token1 base units, which is
invalid for assets with different decimals and could select the wrong fee band.

The configuration now carries separate medium/high thresholds for token0 and token1, and the Hook
uses the canonical `(amountSpecified < 0) == zeroForOne` identity to select the specified token. Tests
cover all four direction/mode combinations. The unused `minimumFee` field was removed rather than
retained as a misleading control.

The draft also allowed several pools while using one global LP whitelist. It is now permanently bound
to the first configured PoolId, preventing a whitelisted account from gaining implicit authority over
future pools.

## Canonical integration evidence

The locked Solidity graph includes:

- `@uniswap/v4-core` 1.0.2;
- `@uniswap/v4-periphery` 1.0.3, npm publication commit
  `60cd93803ac2b7fa65fd6cd351fd5fd4cc8c9db5`;
- `@openzeppelin/contracts` 5.4.0.

`package-lock.json` retains the registry URLs and integrity hashes. The npm package versions and
publication commit are not, by themselves, proof that a Robinhood deployment has identical runtime
bytecode; that remains a separate fork/release-manifest gate.

The local integration suite now executes:

1. an actual canonical `PoolManager` initialization and no-fill swap through the Hook, then checks
   the emitted fee override and state;
2. an official v4-periphery `PositionManager` first mint after the NFT and `PositionInfo` are created;
3. exact rejection of a non-whitelisted NFT owner through canonical ERC-7751 `WrappedError` bubbling;
4. whitelist revocation followed by a successful full liquidity decrease by the existing NFT owner.

The PositionManager lifecycle replaces Permit2 with a minimal transfer-only mock to isolate Hook/NFT
semantics. It does not validate Permit2 allowance, signature, nonce, expiration, or deployment
identity.

## Verification gate

Commands:

```text
make check
git diff --check
```

Result:

```text
169 Python tests passed, 28 subtests passed
18 Solidity tests/invariants passed
20,000 fuzz cases passed
512 stateful invariant runs, 65,536 handler calls, zero reverts
compileall, Ruff lint/format, and mypy strict passed
Foundry format, strict build, tests, and warning-level lint gate passed
git diff --check passed
```

Foundry emitted note-level style/performance suggestions and a harmless local signature-cache warning;
there were no compiler or linter warnings under the configured warning-deny gate.

## Remaining release blockers

- pin and reproduce the Robinhood PoolManager, PositionManager, Permit2, router, quoter, token, and
  Hook runtime identities at one block hash;
- run the full first-mint/increase/transfer/revoke/collect/decrease/burn lifecycle on a pinned
  Robinhood fork, including both tokens and native currency where applicable;
- test real Permit2, malicious/nonstandard tokens, callbacks, reentry, revert-data grief, price limits,
  exact input/output fills, tick crossing, and emergency paths;
- add Slither, dependency/secret scanning, coverage, artifact provenance, bytecode-size/gas budgets,
  symbolic/manual review, and an independent audit;
- feed real replay/accounting evidence into one versioned fee experiment and prove fee/volume response,
  routing, LVR, gas, inventory risk, and out-of-sample excess return;
- complete shadow reconciliation, halt/unwind drills, and a separately authorized minimal canary.

Until these gates pass, the Hook is a verified local research artifact, not a production contract or
evidence of profitable LP operation.
