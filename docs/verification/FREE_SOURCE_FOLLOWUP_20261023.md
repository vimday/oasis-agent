# Free source follow-up and probe budget continuity — 2026-10-23

Continues [the free-provider check](FREE_RPC_RECHECK_20261023.md) and
[the NAV budget correction](FREE_NAV_BUDGET_20261023.md). Payment remains neither
an evidence requirement nor a release gate. No RPC request, wallet key access,
signing, broadcasting or production service start occurred in this increment.

## One public signed-price gateway observation

The [re.al integration documentation](https://docs.re.al/services/oracles/redstone-on-re.al)
still lists `oracle-gateway-1.a.redstone.finance` for RedStone PrimaryProd.
The current [RedStone SDK request interface](https://github.com/redstone-finance/redstone-oracles-monorepo/blob/main/packages/sdk/src/request-data-packages.ts)
requires authenticated gateways; its
[gateway configuration](https://github.com/redstone-finance/redstone-oracles-monorepo/blob/main/packages/sdk/src/data-services-urls.ts)
uses `oracle-gateway.a.redstone.finance`. These are source-access observations,
not a conclusion that an API key necessarily costs money or that every public
price distributor is unavailable.

A single unauthenticated GET to the legacy endpoint
`https://oracle-gateway-1.a.redstone.finance/data-packages/latest/redstone-primary-prod`
returned **HTTP 403**. The diagnostic had a 15-second total deadline, ten-second
HTTP timeout, one-million-byte ceiling, no redirects, retries or credential/environment
loading. It retained the complete 449-byte response. No signed package was obtained,
so no fresh price quorum or NAV acceptance is claimed. The result does not establish
why access was refused or rule out another public source.

Local source: `reports/review-20261023/free-redstone-check.py`.
Raw evidence directory: `reports/evidence/free-redstone-gateway-20261023`.
Retrieval started at Unix milliseconds `1792766139270` and completed at
`1792766140683`. Offline inspection verified the raw response length and SHA-256,
then rehashed the report excluding its own digest:

- Response SHA-256: `a74af6aac9f32ef8852d50cd1a22b3b3d3d31d20f3f067ed28080194cf3c6dd3`.
- Report hash: `a9adfe578412c591e780d45d5649f33291708f5c493cb96e2b58a5f8d1b45676`.
- Diagnostic exit code: **3**, a refusal rather than a successful price check.

## Capability-probe correction

The free trace diagnostic still allowed a `--budget` override for its first two
providers and automatically initialized missing ledgers. Changing the working
directory also moved its relative default ledger. These paths could silently
renew the same campaign's allowance. The discovery providers already rejected a
second attempt, but still initialized a missing ledger. A written refusal report
also incorrectly resulted in CLI exit 0.

`scripts.probe_free_base_rpc` now selects only the original repository-relative
ledger for each provider group, exposes no budget argument in its CLI or probe
function, and requires the existing policy/hash-linked usage history at preflight,
every request reservation and final accounting. Missing, damaged or symlinked
ledgers refuse; at least four requests must remain before constructing a network
poster. Discovery still refuses any previously charged attempt. Concurrent consumers
remain constrained by per-request durable reservations; preflight does not reserve
all four requests atomically. No ledger is recreated on failure.

The v2 report separates `capability_verified` from the individual observations and
always keeps `execution_eligible: false`. It records opening/final request counts
and a fixed failure stage without logging exception text. CLI exit 3 means refusal;
exit 0 means only that this limited capability diagnostic completed. A call-trace
shape check is not raw execution replay, cash reconciliation, source independence,
provider reliability or production release approval. The original v1 reports and
their misleading process-exit evidence remain intact and are not relabeled as passes.

Real ledgers were inspected read-only after the change and remain unchanged:

| Original ledger | Counts |
| --- | --- |
| Free capability | 1rpc 7/8; publicnode 4/8 |
| Free discovery | quicknode-demo 4/4; onfinality 2/4 |
| Free NAV | base_public 31/32; publicnode 30/32 |

Paid acceptance remains at the previously recorded eight requests under the same
cumulative USD 0.10 ceiling. This turn's one price-gateway GET is not an RPC request.

## Validation and remaining work

The focused suite passes 20 tests using temporary budgets and fake HTTP responses.
It covers persistent spending across renamed outputs and working-directory changes,
all-provider override rejection, missing/corrupt/symlinked/insufficient ledgers,
ledger loss during acquisition, retained rate-limit refusal without retry, and a
dangling output symlink. These tests never open the real campaign ledgers.

The full `make live-quality` run is retained at
`reports/review-20261023/free-probe-budget`. All four commands exited 0 without
timeout or launch failure, and each raw log hash was independently verified:
3432 tests and 138 subtests passed, with one existing websockets deprecation
warning; compile, Ruff/format, mypy and eight frontend module syntax checks passed;
the secret gate passed without credential verification calls; Bandit reported
zero medium/high findings and 85 low findings; offline Compose validation passed
seven services without starting a container. The quality evidence hash is
`7c43ce1dfca323fd6002226b41ce5c740c3b27a76f602f0d9d8c215e33c953ac`.
This is development validation, not production acceptance.

Fresh authenticated ETH/USDC pricing, complete dedicated-account NAV, current
production two-source simulation/runtime evidence and dedicated-account dual-service
receipt/cash recovery remain unresolved. This change preserves their release gates.
