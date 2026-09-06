# Position evidence closure: independent follow-up review

**Date:** 2026-09-06 (Asia/Shanghai)  
**Reviewed commit:** `ef4c3bfc5464535a893f91bac0afee06b7d7891b`  
**Execution context:** shared working tree, including existing uncommitted dependencies  
**Status:** local research validation; no production or profitability approval

## Outcome

The reviewed change strengthens range provenance at the position replay boundary, requires complete
canonical block coverage at the ledger builder, and pins settlement references to the exact entry
block. These improvements are retained. The initial working-tree Python gate passed with **239 tests
and 58 subtests**. Passing that gate did not expose the ownership/accounting defects below.

This review covers the new position evidence/ledger commit and its ownership/accounting consumers;
it is not an independent audit of every dirty file or of another contributor's separate worktrees.
The commit imports modules still untracked in this workspace. Its standalone clean-checkout build
has not been demonstrated; the successful gate below applies to the complete working tree only.

## Reproduced findings and fixes

### P1 — NFT ownership events bypassed the ledger's canonical lineage check

Location: `src/oasis_agent/farming/position_ledger.py`, `build_position_token_ledger`.

Liquidity entries were checked against the canonical chain, but a supplied ownership replay was
only compared with the opening/closing snapshots. A transfer with the right height and a different
hash or timestamp passed. Replacing its transfer sequence with an empty tuple, or changing its
reported final owner, also passed. This could reconcile liquidity on one history and ownership on
another.

The builder now validates every ownership transfer's complete `BlockRef` against the same canonical
lineage, recomputes the ownership replay, and compares the supplied result. Four adversarial regression
cases failed before the fix and now reject these inputs. The dataset adapter already provides one
coherent lineage; this closes the independently callable ledger-builder boundary as well.

### P1 — equal endpoint owners were mistaken for continuous beneficial ownership

Location: `src/oasis_agent/farming/accounting_replay.py`, `replay_position_accounting`.

An NFT could move A → B → A during the interval and the report still return `COMPLETE` without
beneficial-owner continuity evidence. The same happened with no ownership replay at all. This is
not sufficient to attribute the interval's cash flows and inventory change to A.

Accounting now considers every actual owner transition. Missing ownership replay, or an unaccounted
transition (including a round trip), yields `UNRESOLVED_OWNERSHIP` with no net PnL. A generic continuity
reference cannot replace missing transfer history. An empty, explicitly supplied replay remains a
research input; proving absence of transfers requires the complete retained NFT event dataset.

### P1 — a valid self-transfer stopped complete NFT dataset replay

Location: `src/oasis_agent/farming/position_ownership.py`, `PositionTransferEvent`.

The model rejected every `from_owner == to_owner`. The locked canonical ERC-721 implementation
permits nonzero self-transfers, clears the token approval, and emits Transfer; PositionManager calls
that implementation. The full-contract decoder would therefore stop on a self-transfer even for
another NFT, before it filtered token ID.

The model now retains nonzero self-transfers without inventing an ownership change. Zero-to-zero
remains invalid. Tests cover direct decoding, owner replay, accounting, and raw-envelope-to-dataset
replay for both the target NFT and another NFT. Approval state itself is not replayed by this model.

Canonical basis: [ERC-721 specification](https://eips.ethereum.org/EIPS/eip-721), plus locked local
`contracts/node_modules/@uniswap/v4-core/lib/solmate/src/tokens/ERC721.sol` (`transferFrom`) and
`contracts/node_modules/@uniswap/v4-periphery/src/PositionManager.sol` (`transferFrom`). The Solidity
package provenance is documented separately in `contracts/README.md`; it is not the Python research
vector commit in `configs/v4-upstream.example.toml`.

### P2 — standalone Transfer decoding accepted removed logs and noncanonical token topics

The decoder accepted `removed=true` and a one-byte or 33-byte zero-padded token ID topic. The range
collector rejected these already, but direct decoder consumers did not. The decoder now rejects
removed logs and requires the indexed token ID to be exactly one ABI word. Three red-to-green
regression cases cover this boundary.

## Evidence foundation now exercised in the shared tree

The following earlier working-tree changes are included in this verification, not attributed solely
to the latest reviewed commit:

- Range evidence schema v2 binds the exact address/topic filter, all numbered headers, decoded logs,
  and a closing-header reread to actual retained request/response envelopes. Method counts alone are
  insufficient. Errors are retained but cannot supply successful coverage.
- Quorum persistence requires raw captures from every declared provider, not only provider names.
  Agreement is recomputed before interpreted evidence is persisted.
- Canonical stream scope is persistent. Reusing a stream with a narrower filter cannot delete the
  broader stream's logs. Conflicting retained headers are rejected transactionally.
- Core ModifyLiquidity and PositionManager Transfer datasets feed the same position ledger without
  requiring a noncanonical ModifyPosition mirror. Token settlements remain unresolved.

Schema migration: v2 content hashes intentionally differ from v1 and include query scope. A nonempty
legacy canonical stream without stored scope must be rebuilt under a new stream ID. No destructive
migration was performed. Provider agreement does not prove provider independence, honest answers,
finality, or deployment/source equivalence.

## Verification

```text
make check: exit 0
  compileall: passed
  Ruff lint/format: passed, 105 files formatted
  mypy: passed, 63 source files
  pytest: 247 passed, 65 subtests passed
  forge fmt/build/test/lint: passed under the current configured gate

FOUNDRY_PROFILE=ci forge test (separate confirmation): exit 0
  18 tests/groups passed, 0 failures, 0 skips
  20,000 fuzz cases
  two grouped invariants: 512 runs, 65,536 handler calls, 0 reverts

git diff --check: passed
```

Foundry emitted advisory lint notes and a signature-cache permission warning. Solidity was unchanged
in this follow-up. Slither is not installed on the current PATH; coverage, independent security
analysis, dependency/secret scanning and pinned Robinhood fork validation were not completed here.
The existing `make check` does not enforce all of the constitution's production gates. Its successful
exit must not be relabeled as completion of that security campaign.

## Next implementation order and acceptance criteria

1. **Snapshot evidence adapter.** Bind PositionManager ownership/position info and core fee-growth
   reads to retained EIP-1898 `eth_call` responses, runtime identity, exact block and NFT. Reject a
   changed return value, foreign token ID, mixed block, missing response or wrong implementation.
2. **Position settlement adapter.** Derive per-action token0/token1 base-unit flows from receipt/trace
   records pinned to transaction hash, action index, NFT, pool and canonical block. An arbitrary
   amount plus a reference to another transaction in the same block must fail. Reject ambiguous
   multi-position/netted transactions until trace attribution is available; ERC-20 transfers alone
   do not establish a particular position's LP fee allocation.
3. **Accounting attestation.** Reconcile principal, accrued/collected fees, payer/recipient cash flows,
   transfer consideration, valuation and actual execution costs. Bind beneficial-owner attestations
   to the whole NFT interval and transfer sequence. `COMPLETE` currently means a calculation over
   supplied research inputs, not independently verified economic truth; generic evidence references
   and default zero external cost must never authorize capital deployment.
4. **Read-only live replay dossier.** Acquire a bounded two-provider range and deployment identity,
   retain all raw evidence and produce accounting residuals, including honest unresolved outputs.
   Do not infer access or profit from the observed PONS pool's displayed APR.
5. **Fee parity and economic experiment.** The root tree has controller state/config models, a
   Decimal research policy and an amount-band Solidity hook, not one verified shared controller.
   Join them only with fixed-point differential vectors, lag/freshness checks and identical public
   input definitions. Preserve observed fees in historical accounting; proposed fees belong in a
   separate counterfactual with routing/volume elasticity and precommitted out-of-sample tests.
6. **Fork → shadow → canary.** Finish real Permit2, full NFT/LP lifecycle, malicious-token/re-entry,
   unwind and security campaigns before any deployment proposal. Current local integration uses a
   transfer-only Permit2 mock. External user capital and keys remain outside the agent process.

No commit, deployment, signing, broadcasting, live configuration change, or capital movement was
performed in this follow-up review.
