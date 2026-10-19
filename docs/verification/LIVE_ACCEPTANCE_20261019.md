# Live prerequisites continuation — 2026-10-19

This continues the owner's local Docker deployment and USD 0.10 acceptance-only
paid RPC authorization. No production key was read or reprovisioned, no mainnet transaction
was signed or broadcast, and no release gate was opened. Evidence paths keep
their original acquisition dates. The production key provisioning result remains
in [the previous acceptance](LIVE_ACCEPTANCE_20261018.md).

## Confirmed implementation

The historical provider acceptance tool now collects and independently replays
transaction identity, receipt, OP full fees, runtime bytes, closing block and a
nested callTracer tree. Both sources must agree exactly before accepted evidence
is written. Transaction lookup previously failed the budget method allowlist;
`eth_getTransactionByHash` now accepts exactly one canonical transaction hash.
Malformed requests refuse before budget reservation or network use.

The CLI binds its persistent budget to the repository, even when invoked from
another working directory or with another output directory. Its lifetime ceiling
is 12 calls per source, three-second spacing, one ordinary transport-read retry
and **zero trace retries**. Each attempt is charged before I/O. Failed first-source
validation prevents paid-peer calls. Failures retain fixed categories and request
hashes without endpoint URLs or exception text. Existing output directories and
insufficient budgets refuse; none of the earlier closed campaigns was reset.

`run_live_authority --reconcile-only` now permits old pending receipt recovery
while new signing remains blocked. It owns the same exclusive state lock and
configuration-bound journal, requires the existing two-source cost/cash proofs,
and returns before loading a key or creating any signing socket. It refuses key
and socket flags, uncertain signing attempts and inconsistent proofs. Repeated
successful recovery counts one action and one fee only. The normal authority
entry point still refuses before key/network/state access when release is closed.

The two Compose recovery services mount only their respective existing state
volumes; neither mounts a key or signing socket. They share each service's
original request ledger, not a fresh recovery budget. Their operator commands
and unresolved-signature limitations are documented in the deployment README.

## Confirmed live observation and offline replay

Initial validation of the new tool charged eight free dRPC requests and zero
Alchemy requests. Seven raw responses were retained. The eighth request,
`debug_traceTransaction`, returned **HTTP 408** and was not retried. No complete
provider quorum was retained; the attempt is refused. The remaining four free
requests cannot fund another complete nine-request capture, so a renamed output
cannot restart this acceptance.

- Chain: Base 8453, block 51949838, timestamp 1790689023.
- Block hash: `0xec2779b91c51550bd764f1cb1c5a5f597a128fd9efd0c54895a824a1b6d32fb1`.
- Transaction: `0x22a7c0e715455f2ad99f57bcf7789edac2faf8c3506d1b911e8319bb7205c7f2`.
- Payer: `0xaf2bfb6b69dfe6efd257fe8cd694175156a23812`, a third party.
- Source: documented public `https://base.drpc.org/`; raw retrieval recorded as
  `2026-10-18T11:35:06.195895+00:00`.
- Raw report: `reports/evidence/live-provider-tool-validation-20261018/report.json`.
- Report hash: `6e9fe71cc8cd4cc218ac3e6a516b6ea2d4dc8d8b1c02761cb14f77bf0527b758`.

Replaying the first six retained exchanges **offline** reconstructs execution
8,959,320,000,000 wei plus L1 441,778,148,158 wei, operator/blob zero: total
**9,401,098,148,158 wei**. This agrees with the prior retained two-source cost
proof. The seventh response binds transaction body, payer and block. This is a
historical third-party fee result, not canary cash conservation or trace success.
The source report's hash was verified before replay.

Offline report: `reports/review-20261019/free-provider-cost-replay.json`; hash
`7bb8ae518e72c52a100b888ce77384b6687f717c5ffc04cc81f67e2f1427808a`.

**New paid calls: zero. Cumulative acceptance paid calls: four.** The existing
conservative public-rate allowance remains USD 0.0021, not a verified invoice.
The new tool predeclared a maximum combined allowance of USD 0.0084 for its 12
possible paid calls plus the prior four, within USD 0.10. Its paid allowance was
unused. No exhausted or closed budget was deleted or replaced.

## Validation

- Final host checks: 3050 tests plus 138 subtests passed, with one existing
  websockets deprecation warning; frontend syntax checks passed for eight modules.
  The additional HTTP-408/no-trace-retry regression added after full-suite
  collection passed in the nine-test provider CLI run. The first sandboxed suite
  had eight local-socket permission failures; the authorized full rerun passed.
- Linux validation image:
  `sha256:5bcb1710e4022b08011207c9458ed675f19853290ebee5040fbda41b181d3267`.
- 53 focused Linux tests passed in a network-disabled, read-only-root, capability-
  free container running UID/GID 10001, with no key or host-state mounts. These
  include real process SIGKILL/restart and socket lost-reply recovery using only
  synthetic receipts and an ephemeral fixture key.
- Docker Compose configuration was expanded using placeholder RPC environments;
  both recovery services have exactly one own-state mount, no key/socket mount,
  UID 10001, read-only root, all capabilities dropped and no automatic restart.
- Ruff/format and mypy passed. Secret scanning passed without credential
  verification requests; Bandit found no issue in the new/changed entry points.

These are synthetic mechanism tests, not deployed reconciliation of a real
canary receipt. No independent reviewer or external audit is claimed.

## Unresolved production conditions

1. **Complex dual-source LP trace/cash:** simple historical trace access and fee
   agreement do not establish a complete nested LP cash proof. The free provider
   timed out; the acceptance budget is retained and cannot be silently renewed.
2. **Fresh USD valuation:** no new valid price capture was obtained. The prior
   USDC/USD round failed the 1800-second rule. The reviewed Pyth one-hour and
   RedStone one-day heartbeat policies do not establish continuous compliance.
   No peg substitution, stale round acceptance or freshness relaxation was made.
3. **Real dual-service receipt recovery:** the isolated key is provisioned and
   recovery code is executable, but no real dedicated-account receipt has been
   reconciled by deployed services. Missing trace/price evidence still blocks new
   transactions needed for that acceptance. Synthetic tests cannot close it.

The fixed production release blockers therefore remain closed. This work adds
recoverability and more precise evidence without claiming profitability or full
production readiness.
