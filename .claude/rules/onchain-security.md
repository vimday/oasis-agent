---
paths:
  - "contracts/**/*.sol"
  - "test/**/*.sol"
  - "script/**/*.sol"
  - "src/oasis_agent/{evm,uniswap_v4,risk,execution}/**/*.py"
  - "tests/**/*{evm,v4,execution,risk}*.py"
---

# On-chain and Uniswap v4 security rules

For Phase 1 hook work, read `docs/security/V4_HOOK_PRODUCTION_STANDARD.md` and use its deployment
packet as the release definition of done.

Use the checked-out, commit-pinned canonical `v4-core` and `v4-periphery` source as the normative
interface and semantic reference. Never implement protocol behavior from memory, a blog post, or a
selector database when source is available. Record the upstream commit in evidence and reports.

## Required v4 semantics

- Validate hook permissions against the low 14 address bits and ensure declared permissions exactly
  match deployed-address flags. Test every enabled callback and prove disabled callbacks are absent.
- Authenticate every callback with exact PoolManager identity. Return the exact expected selector
  and ABI shape. Reject malformed `hookData`; do not decode before checking length/domain.
- A dynamic pool uses the exact dynamic-fee sentinel. A per-swap override is accepted only where v4
  permits it, has the override bit removed with the canonical mask, and validates to
  `MAX_LP_FEE = 1_000_000` pips. Zero is protocol-valid unless strategy policy rejects it.
- Decode the packed protocol fee by swap direction. Compute total swap fee with canonical v4 math:
  protocol fee is applied to input first and LP fee to the remainder. Do not add percentages.
- Treat PoolManager unlock accounting as a per-currency ledger. Every nonzero currency delta must be
  settled before unlock returns. Never net token0 against token1 or mix assets in one scalar.
- Respect exact-input/exact-output sign conventions, int128 boundaries, native currency behavior,
  fee-on-input semantics, callback order, and the 100% fee restriction for exact-output swaps.
- Verify event signature in `topics[0]`, exact indexed-topic count/order, ABI word count, canonical
  padding/sign extension, and numeric bounds before constructing domain objects.

## Contract design constraints

- Prefer immutable, non-upgradeable, minimal hooks. Any mutability needs explicit bounded roles,
  timelock, emitted events, read-only preview, emergency response, and a documented exit path.
- Minimize storage writes and external calls in swap callbacks. No arbitrary calls, unbounded loops,
  ambient approvals, `tx.origin`, unsafe delegatecall, or silent fallback behavior.
- Fee inputs must be reproducible from manipulation-resistant state available at the decision block.
  Model flash-loan/state manipulation and block-builder transaction ordering explicitly.
- Emergency pause may stop new risk but must not permanently trap user funds or block safe unwind.

## Mandatory verification

- Unit tests for each callback and boundary.
- Stateful fuzz/invariant tests for access control, conservation by currency, fee bounds, callback
  responses, and inability to leave unsettled deltas.
- Differential tests against pinned v4-core test vectors or direct canonical-library calls.
- Slither on every contract change; Echidna or Foundry invariant campaigns on financial invariants.
- Pinned local-fork tests for both directions and exact-input/exact-output, including reverts,
  reentrancy attempts, malicious/non-standard tokens, admin transitions, and changed bytecode.
- Record tool versions, seeds, fork chain/block/hash, RPC source, compiler version/settings, optimizer,
  and upstream dependency commits. A clean unit suite alone is insufficient for deployment.
