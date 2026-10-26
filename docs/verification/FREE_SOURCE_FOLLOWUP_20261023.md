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

## Follow-up: separate legacy price API — 2026-10-25

The official [legacy HTTP API documentation](https://github.com/redstone-finance/redstone-api/blob/main/docs/HTTP_API.md)
also documents a separate unauthenticated `api.redstone.finance/prices/` interface.
Its older price-object format is not evidence that it serves the current
PrimaryProd three-signer packages required by this repository. The current SDK
request interface and gateway configuration linked above still require an API
key; neither observation establishes whether obtaining a key costs money.

One direct GET to
`https://api.redstone.finance/prices/?symbols=ETH,USDC&provider=redstone`
returned **HTTP 500**, with a complete 299-byte HTML error body. The bounded
diagnostic exited **3**; it used no credential, redirect, retry or RPC request.
The earlier web-fetch tool could not access the URL and supplied no HTTP response;
only this directly retained response is used for the HTTP-status conclusion.
The diagnostic used a 15-second overall deadline, ten-second HTTP timeout and
one-million-byte response bound. No fresh signed price or NAV was produced.
This failure does not establish that all public sources are unavailable.

- Diagnostic source: `reports/review-20261025/public-price-source/observe.py`.
- Raw evidence: `reports/evidence/free-redstone-price-api-20261025/`.
- Retrieval interval: `1792937994839`–`1792937996385` Unix milliseconds.
- Raw response SHA-256:
  `b32ccc77423c80566f051749de1f5c583be7423848d77a753f9124e420aa65cc`.
- Canonical report hash, excluding its own digest:
  `47b2df1618224cf14df90ddf036a1d06e57f7ff8e0ed44e4c3000936e1cb7f5b`.

This follow-up changes documentation only. It does not consume or renew the
terminal one-shot free current-state grant, touch the prior budget ledgers, or
use paid RPC. Offline signature/NAV candidate validation already exists, but
production `replay_nav` still accepts only the existing Chainlink-backed proof
kind. A valid fresh signed-price input would therefore enable candidate capture
and testing, not automatically authorize production signing. Current production
two-source runtime/simulation and dedicated-account receipt/cash recovery remain
separate outstanding requirements. The 1800-second limit and closed release
gate remain unchanged.

## Newly authorized current-state attempt — 2026-10-26

The owner explicitly authorized a second, separate free read-only attempt in
reply to `call_dfcbe62df1674b6fa42c9d4761636204`: Blockmachine and QuickNode,
at most 64 requests each, 120 seconds overall, stop on failure without retry,
no paid requests, signing or broadcasting. The owner also confirmed **there are
no real dedicated-account transaction records**. This is not authorization to
create a transaction merely to obtain a receipt.

The actual command exited **3** after **15478 ms**. At Base block **53117555**,
hash `0x3b840242aa22e9e87f91fe7ffd0e73d802dfc7f42d14d8c85d16a729715a2187`,
the first provider's ETH/USD round was 98 seconds old, but its USDC/USD round was
**5261 seconds old**, above the unchanged 1800-second ceiling. The USDC round's
update time was `1793019211`; observation time was `1793024472` Unix seconds.
Feed code/description/decimals were checked, but the failure preceded the closing
header and second-provider confirmation. This is a refused first-source feed
observation, not a fresh USD quorum, NAV or production acceptance.

Actual usage: **Blockmachine 11/64, QuickNode 0/64, paid requests 0**. The attempt
stopped at `feed_preflight`; NAV, runtime and simulation stages were not entered,
and no NAV evidence database was created. Unspent request allowances do not
authorize a retry. Original campaign authorization, budget and report hashes were
checked before and after and remained unchanged.

Raw report/captures and the separate authorization/budget are retained under
`reports/evidence/authorized-free-current-20261026/`. Canonical report hash:
`08d617df172893e63eb0326353300718726115bde0601b671e1c42ae929b2b0f`.
The report, authorization and script hashes were verified offline. The script,
actual process log, prior-file hash inventory and offline request-count plan are
under `reports/review-20261026/`. The plan requires 56/55 requests if all checks
pass, and covers only declared ETH/USDC with zero declared v4 NFTs plus read-only
approval simulation; it does not establish all-token inventory, full account
history, upstream provider independence or real receipt recovery.

Local Docker metadata showed only `oasis-canary-keys`, with no `oasis-live` project
containers or expected authority/strategy state volumes. No volume contents or
key material were read. Fresh eligible USD pricing remains required before NAV
acceptance; dedicated-account receipt/cash acceptance also needs a real transaction
and its original execution/recovery records. None is fabricated or replaced by
historical third-party or synthetic evidence, and all release gates remain closed.
