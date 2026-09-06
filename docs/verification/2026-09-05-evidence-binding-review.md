# Replay and Deployment Evidence Binding Review

**Date:** 2026-09-05  
**Scope:** shared dirty working tree; Python evidence and flow-replay boundaries  
**Status:** local research validation only; Phase 1 remains incomplete

**Follow-up:** [2026-09-06 position-change review](2026-09-06-position-change-review.md) records
the subsequent raw range/quorum evidence validation, ownership fixes and successful working-tree
`make check`. The counts and lint blocker below describe this earlier review, not the latest state.

## Defects reproduced and corrected

### P1: replay values could change while retaining the original evidence

`derive_replay_flow_observations` compared block/transaction/log identities but did not compare
amounts, effective fees, or complete replay states with the retained logs. Replacing `amount1=90`
with `900000` retained the original amount-evidence reference. Jointly changing the decoded event
and replay also passed while copying the original dataset hash.

`replay_pool_dataset` now re-decodes the retained dataset and compares all decoded inputs.
The flow adapter recomputes the deterministic replay and compares the complete result, including
liquidity transitions and final state. Regression tests first reproduced both bypasses and now
reject modified amounts, fees, final states, and jointly modified decoded/replay values.

This extra pass prioritizes correctness over speed for the current bounded research datasets.
Any later caching must bind the immutable dataset content, decoder version, and replay version.

### P1: economic enrichment could raise replay trust and overstate LP-fee evidence

A `SINGLE_PROVIDER_RESEARCH` observation could become a confirmed flow merely by supplying
classification and cost references. The range's Swap-event amount reference also counted as
evidence for a caller-supplied `lp_fee1`, although it did not establish position fee ownership,
protocol deductions, or conversion of token0 fees into currency1.

Confirmed construction now requires provider-quorum replay input and a separate amount reference
from receipt/trace evidence in the economic attribution. Single-provider observations remain usable
for unresolved/inferred research. Missing information is rejected rather than promoted silently.

These checks constrain admissible evidence, but do not themselves verify a receipt/trace's economic
interpretation. A content-verifying position settlement/valuation adapter remains required.

### P1: RPC method counts could attest unrelated runtime bytecode

`persist_v4_deployment_identity` accepted two purported block responses and four purported code
responses even when all request parameters were empty and every result was `0x`. The manifest
could then claim nonempty runtime hashes with unrelated retained envelopes.

Before insertion, the function now validates:

- unambiguous JSON, JSON-RPC version, integer request/response IDs, method labels, and unique IDs;
- a successful chain-ID read and matching block number/hash/parent/timestamp;
- each contract address and the exact EIP-1898 block-hash selector with `requireCanonical=true`;
- runtime byte length, Keccak-256, and SHA-256 recomputed from the actual code response;
- complete successful address coverage and a pinned closing header after all code reads.

Valid RPC error responses remain in the audit envelope but cannot satisfy coverage. Tests exercise
wrong chains/addresses, changed code, empty code, `latest`, another block, false/truthy canonical flags,
response-ID mismatch, duplicate JSON fields, repeated requests, reordered headers, and retries.

Envelope consistency does not prove RPC honesty, provider independence, verified source equivalence,
proxy implementation identity, pool existence, or LP access. Operator-assigned role names and the
caller-supplied PoolKey remain research inputs until independently attested.

## Verification

Observed on this working tree:

```text
Focused evidence/flow/dataset tests: 22 passed, 21 subtests passed
Full Python tests: 190 passed, 49 subtests passed
mypy: 61 source files passed
Changed-file Ruff lint/format: passed
make contract-check: passed
  18 Foundry tests/groups, zero failures/skips
  20,000 fuzz cases; 512 invariant runs, 65,536 handler calls, zero reverts
CI YAML: parsed, two jobs
git diff --check: passed
```

`make check` was attempted but the shared tree did not pass the global lint/format gate:
the concurrently changing `src/oasis_agent/models/finance.py` had I001, F401, FURB166 and a
formatting difference. Those edits were preserved. No all-green repository claim is made here.
Counts are observations at the verification time, not a promise about subsequent concurrent edits.
Foundry also emitted advisory notes and a local signature-cache permission warning.

## Ordered continuation

1. Finish the concurrent fee-controller change and rerun the complete repository gate. Preserve the
   observed Swap fee in accounting replay; keep proposed counterfactual fees in a separate result.
   Quote freshness must be bounded by feature observation age, not refreshed by republishing old data.
2. Apply payload-to-interpretation checks to the existing block-range persistence path. Its current
   method-presence checks still do not establish that captured headers/logs reproduce the dataset.
   `VerifiedBlockRange` references and provider labels alone do not close this gap.
3. Retain an actual bounded two-provider dataset and deployment dossier; prove provider independence,
   archive availability, contract roles and pool initialization. Add token, router/quoter, Permit2,
   proxy/admin, whitelist and ownership evidence before making a pool-access decision.
4. Implement per-position fee-growth/settlement reconciliation and timestamped token valuation.
   Derive LP fee allocation and strategy-borne costs from these records instead of caller-supplied
   economic values. Exclude affiliated volume from external revenue throughout the pipeline.
5. Execute the full pinned-fork NFT/LP lifecycle, then shadow reconciliation and economic comparisons
   with routing/volume elasticity. The existing transfer-only Permit2 mock does not close this gate.

No deployment, signing, broadcasting, capital movement, or commit was performed in this review.
