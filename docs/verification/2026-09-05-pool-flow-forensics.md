# Pool Flow Forensic Reporting Foundation

**Date:** 2026-09-05  
**Scope:** fixture-only flow attribution and benchmark report integration  
**Decision:** `RESEARCH`; no live-chain profitability claim, deployment, signing, broadcast, or capital authorization

## 1. Objective and invariant

This slice prevents affiliated, self-generated, unknown, or weakly evidenced flow from being presented
as unrelated LP fee demand. The conservative accounting invariants are:

```text
confirmed external fee alpha
  = confirmed external LP fees
      - confirmed strategy-borne gas cost
      - confirmed external adverse-selection cost

net modeled LP PnL at the consolidated beneficial-owner boundary
  = confirmed external LP fees
      - all modeled strategy-borne gas cost
      - all modeled adverse-selection cost
      - modeled affiliated/self-trade external leakage
```

Gross LP fees remain a diagnostic total. Affiliated LP fees are internal transfers at the consolidated
beneficial-owner boundary, and unknown-flow fees are not proven external revenue. Neither is credited
to net modeled LP PnL. Signed losses are retained rather than clipped to zero.

## 2. Implemented capability

- `PoolFlow` binds every modeled flow to chain ID, pool ID, currency1 token address, decimals, block,
  transaction hash, and log index.
- Amounts remain nonnegative integers in one token's base units; mixed chain, pool, token, decimals, or
  block scope is rejected before arithmetic.
- Duplicate `(chain, pool, transaction, log index)` identities are rejected while multiple distinct
  logs in one transaction remain representable.
- Evidence is deeply normalized to an immutable tuple and bound to the flow's transaction, pool, log
  index, block, and explicit claim: `classification`, `amounts`, or `costs`.
- Classification, amount, and cost evidence kinds are allowlisted separately and require complete
  request, raw-response, and interpretation provenance hashes.
- A confirmed flow requires all applicable classification, amount, and cost claims. Unknown flow
  cannot be marked confirmed.
- Confirmed external fee alpha uses only confirmed external flows. Affiliated, unknown, and inferred
  external flow cannot contribute.
- Non-affiliated flow cannot carry self-trade cost. Affiliated or otherwise unresolved flow remains
  visible in diagnostic totals and the unresolved count.
- `PoolFlowForensicContext` binds the output to chain, pool, hook, currency, and exact opening/closing
  block identities.
- Because this fixture-only slice does not yet carry a verified parent-hash lineage, every included
  flow must match the closing block number and hash exactly. Multi-block reporting remains disabled
  until integration with a verified `BlockRangeDataset` supplies canonical ancestry.
- The report preserves evidence kind, claim, source, retrieval time, content hash, request hash,
  raw-response hash, interpretation hash, schema version, transaction identity, and block identity.
- Claims distinguish deterministic arithmetic, evidenced economic inputs, unresolved classifications,
  and ownership interpretations. No headline APR is emitted.

## 3. Fixture result and interpretation

The generated example contains three synthetic flows of `10,000 USDG base units` each. Each has
`100 USDG base units` of LP fees, `3` of gas cost, and `7` of adverse-selection cost. One flow is
classified affiliated with `95` of self-trading cost, one is confirmed external, and one is unknown.

```text
gross LP fees                     300 USDG base units
confirmed external fee alpha       90 USDG base units
affiliated/self-classified fees   100 USDG base units
unknown-flow fees                 100 USDG base units
self-trade cost                    95 USDG base units
gas cost                            9 USDG base units
adverse-selection cost             21 USDG base units
net modeled LP PnL                 -25 USDG base units
unresolved flow count                1
```

The negative result is intentional: affiliated and unknown fees are not credited as outside revenue.
The hashes, classifications, amounts, and costs are synthetic fixture inputs. They do not describe
benchmark-pool activity, prove wallet ownership or affiliation, constitute a historical replay, or
support a profitability conclusion.

## 4. External claim boundary

No raw RPC capture, provider response envelope, evidence database, or replayable JSON supporting the
previously circulated pool/wallet claims is stored in this repository. Those claims therefore remain
unverified research hypotheses in this deliverable. A block-explorer page or a single-provider manual
observation is not promoted to repository evidence without persisted request, response, interpretation,
block identity, and independent provider agreement.

The economic rule remains valid independently of those claims: fees paid by a strategy to liquidity
under the same beneficial owner are internal transfers, while gas, fees paid to other LPs or protocol
recipients, slippage, arbitrage leakage, and inventory mark-to-market can remain real losses. Only
subject-bound evidence for unrelated flow can support external fee alpha.

## 5. Files

Created or materially extended:

- `src/oasis_agent/farming/flow_attribution.py`
- `src/oasis_agent/farming/flow_forensics.py`
- `src/oasis_agent/farming/__init__.py`
- `src/oasis_agent/reporting/render.py`
- `tests/unit/test_flow_attribution.py`
- `tests/unit/test_config_report.py`
- `reports/benchmark-hook-analysis.md`
- `docs/verification/2026-09-05-pool-flow-forensics.md`

## 6. Review findings addressed

An independent read-only review found no Critical issues and six Important boundary issues. This slice
now addresses them as follows:

1. Asset and snapshot identity is carried by every flow, preventing cross-token and cross-scope sums.
2. Evidence is immutable, complete, claim-specific, and bound to transaction/pool/log/block subjects.
3. `retrieved_at` is preserved and rendered with the remaining provenance fields.
4. Deterministic arithmetic is separated from evidence-coverage claims; missing inputs remain unresolved.
5. Same-height alternate hashes and any earlier block without verified lineage are rejected.
6. Stable flow identities prevent duplicate accounting while allowing distinct logs in one transaction.

A further local review separated full classification coverage from amount/cost coverage: a report with
an unknown flow may confirm its modeled amount and cost provenance, but cannot claim that every flow
classification is confirmed.

## 7. Remaining blockers

This slice does not yet provide:

- replay-derived `PoolFlow` construction from real swaps, receipts, traces, transfers, or position
  ownership;
- verified multi-block ancestry for forensic flow aggregation;
- independent proof that any wallet is affiliated with another wallet or with the benchmark hook;
- protocol-fee, LP-share, inventory, markout/LVR, route-alternative, or price-impact reconstruction;
- two-provider-plus-archive agreement for a bounded chain range;
- full PositionManager lifecycle and settlement reconciliation;
- executable four-cell fee/range counterfactuals;
- pinned Robinhood fork verification or production Hook authorization.

Until those gates pass, benchmark access, external demand, fee alpha, and net profitability remain
unresolved. The maximum project state remains `RESEARCH`.

## 8. Verification

Commands:

```text
make report
make check
git diff --check
```

Result:

```text
benchmark report regenerated
compileall passed
Ruff lint passed
Ruff format check passed for 97 files
mypy passed for 59 source files
177 Python tests passed, 28 subtests passed
3 Foundry suites passed: 18 tests passed, 0 failed, 0 skipped
forge lint completed with non-blocking notes
git diff --check passed
```

This verification is deterministic and offline; it is not live-chain evidence, a profitability study,
a contract audit, or production approval.
