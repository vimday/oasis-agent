# Historical trace quorum acceptance — 2026-10-20

Continues [keyless recovery](LIVE_KEYLESS_STATUS_20261019.md). Real historical
transaction evidence now demonstrates two-provider agreement on a complex LP
call tree and its complete receipt. This is not a dedicated-account cash proof,
production service acceptance, or profitability result. No production key was
read, no transaction was signed/broadcast, and no release gate or risk limit changed.

## Confirmed evidence

- Base chain 8453, block 51949838, timestamp 1790689023.
- Block hash: `0xec2779b91c51550bd764f1cb1c5a5f597a128fd9efd0c54895a824a1b6d32fb1`.
- Transaction: `0x22a7c0e715455f2ad99f57bcf7789edac2faf8c3506d1b911e8319bb7205c7f2`.
- Payer: third party `0xaf2bfb6b69dfe6efd257fe8cd694175156a23812`.
- QuickNode and Alchemy independently returned identical **169-frame** nested
  callTracer trees, with `onlyTopCall=false`, and identical complete receipts.
- Trace SHA-256: `53825d86a9f4c1598f5e2a6f7f528c1a6840ce03dfcf145c5d9f249bd7b25c86`.
- Receipt SHA-256: `fc4f6857f3ef8533392700420ee731431b7029f2c67152d17350a088c90619f4`.
- Raw comparison report: `reports/evidence/retained-trace-quorum-20261019/report.json`;
  digest `5d15f9a982d58ba5202a7dc32f5fba31c486c4c65c9df7b25484fb6347da5b0b`.
- QuickNode raw report: `reports/review-20261019/quicknode-demo-probe.json`;
  digest `44ad4ce8ac0c569e7d111bd1dc08d7d96d8d2ea693f4f3edc5c656a6ec2afcef`.

QuickNode acquisition is `2026-10-19T13:37:59.935256+00:00`; Alchemy completion
started at `2026-10-19T13:42:21.734745+00:00`. Each raw report retains its original
acquisition timestamp. Evidence paths retain
their acquisition-session names; this document's date is the continuation date.
The independently retained dRPC transaction body binds root sender, destination,
input, value and block. Both trace captures replay chain, receipt, header and full
trace requests with exact parameters. Frame count is not nesting depth.

The separate two-source full-cost proof was re-derived offline from a temporary
copy of its original evidence database; the source database hash did not change.
Execution gas 8,959,320,000,000 wei plus L1 441,778,148,158 wei, operator/blob zero,
totals **9,401,098,148,158 wei**. Both cost sources and trace sources bind the same
transaction/block. This does not manufacture QuickNode fee-oracle reads from
another provider's captures. Combined offline recheck:
`reports/review-20261020/trace-cost-recheck.json`, digest
`35a4577821b72e8b8371a9d4878b005f871bb32e262714b9fede3dc147306d9f`.
The fee proof retains its GasPriceOracle runtime bytes; this trace capability
check does not establish current target-contract bytecode pins.

## Provider selection and budget

[QuickNode's Base callTracer documentation](https://www.quicknode.com/docs/base/debug_traceTransaction)
publishes the no-key documentation endpoint used here. Its one successful sample
does not establish production availability or a service-level guarantee.
[OnFinality's Base page](https://onfinality.io/en/networks/base) publishes the other
new free endpoint. It returned chain ID, then the receipt transport failed;
there was no trace request and no retry. This is an unavailable sample, not proof
that its trace method is unsupported.

The two new free providers have a separate fixed repository ledger, four requests
each for their one discovery attempt. Counts are QuickNode 4, OnFinality 2.
Earlier 1RPC/PublicNode/dRPC attempts were not reset or repeated.

The paid completion used only the existing acceptance ledger's unused Alchemy
allowance: four requests, no retries. That ledger now records dRPC 8 / Alchemy 4.
Together with the earlier four paid requests, cumulative acceptance usage is
**eight paid requests**. At the previously declared conservative allowance of
1000 CU/request and USD 0.525/million CU, the estimate is **USD 0.0042**, within
the owner's USD 0.10 acceptance ceiling; this is not a verified invoice.

The completion CLI refuses missing existing budgets, any previously charged paid
attempt, and changed retained inputs. A permanent exclusive claim now also
prevents two processes from passing the initial zero-count check simultaneously.
The claim remains after failure. Existing evidence was acquired before this
additional concurrency guard; its four charged calls already prevent another run.

Offline replay opens no credentials, network transport or budget:

```sh
.venv/bin/python -m scripts.complete_retained_trace_quorum \
  --peer reports/review-20261019/quicknode-demo-probe.json \
  --body reports/evidence/live-provider-tool-validation-20261018/report.json \
  --out reports/evidence/retained-trace-quorum-20261019 --replay
```

The shared CLI/dashboard release description now distinguishes this historical
success from the remaining dedicated-account and provider-reliability acceptance.
All four release blockers remain closed.

## Validation

Host checks passed: **3080 tests and 138 subtests**, Ruff/format, mypy for 229
source files, secret scanning, Bandit at the configured medium/high threshold,
and frontend syntax checks for eight modules. The existing websockets legacy
deprecation warning remains. The readiness CLI exits 3 with all four blockers,
unchanged USD 500/200/500 capital/daily/cumulative limits and no allowed new actions.

All **27 focused Linux tests** passed in existing validation image
`sha256:20f5cebf23a5234cd7d6ca6110a13c112b4198e77681f7c05489f2ac4e3960eb`,
with the five changed/new provider source/test files mounted read-only. This is
an existing image plus source overlays, not a newly built production image.
Network was disabled, root was read-only, all capabilities were dropped,
no-new-privileges was enabled, UID/GID was 10001 and no keys/state were mounted.
The first attempt could not start because Docker was stopped; after starting
the already authorized local Docker application, the test run passed.

Regression coverage includes refusal of changed reports, missing budgets,
already charged attempts, a competing invocation before the first charge,
provider disagreement, block/root-input/value mismatch, reverted/single-frame
traces, and altered parsed results even when the report digest is recomputed.
Offline CLI replay is tested with credential, budget and transport construction
forbidden. Logs, overlay hashes and readiness output are retained locally under
`reports/review-20261020/`.

## Remaining conditions

1. Fresh valid ETH/USD and USDC/USD evidence under the existing 1800-second limit.
   The previous stale USDC observation remains refused. API3's documented
   [24-hour heartbeat](https://docs.api3.org/dapps/integration/) does not establish
   continuous compliance. Pyth's pull API is a candidate, but its
   [current Hermes access requires an API key](https://docs.pyth.network/price-feeds/core/upgrade/preparing).
   No configured Pyth key was found; no subscription was purchased. A key alone
   would not finish integration: signed payload verification, feed/runtime pins,
   block/time binding and raw replay still require implementation and validation.
2. Dedicated-account cash conservation, complete NAV and historical baseline,
   plus fresh two-provider simulation with production runtime pins.
3. Real deployed strategy/authority recovery against a dedicated-account receipt.
   The existing SIGKILL/restart and lost-reply tests remain synthetic mechanism
   evidence. No real canary trade has been made to manufacture a passing result.

No independent reviewer or external audit is claimed.
