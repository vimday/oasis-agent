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

## Owner-confirmed strict feed policy — 2026-10-27

The owner explicitly selected “ETH 和 USDC 都保持 1,800 秒” in reply to
`call_d9bf21bd768f44cb8a868c42d3ec1112`. Both production configuration fields
now have a hard maximum of 1800 seconds. The superseded 90000-second USDC setting
is rejected during configuration validation, and raw NAV regression tests cover
both feeds at 1800 (accepted boundary) and 1801, 5261 and 90000 seconds (refused).
Before-key-use deadlines still include both individual feed timestamps. The
existing USDC answer band remains an additional check, never a freshness waiver.
The previous amendment is preserved and marked superseded in the experiment lock.

The first focused test command exited 1 because the sandbox refused the Unix
socket bind in `test_full_transaction_authority_over_private_socket`
(`PermissionError: [Errno 1] Operation not permitted`); it is not reported as a
passing run. The subsequent full local quality run permitted test sockets and
exited 0: **3515 tests and 138 subtests passed**, with one existing websockets
deprecation warning. Compile, Ruff/format, mypy, eight frontend modules, the
secret gate, Bandit (89 low findings, zero medium/high) and offline seven-service
Compose validation passed. Each of the four check groups exited 0 with no timeout
or launch failure, and all retained raw log hashes were verified.

Evidence: `reports/review-20261027/strict-feed-quality/report.json`; canonical hash
`d8addb0d7e6cc2f853c673df96aef9881c56b9c0a4a58f7c1fe9815f616e760f`.
No RPC or credential verification requests, production service start, key-volume
access, signing or broadcasting were performed. Original refusal evidence and
budget ledgers were not rewritten. Fresh USD/NAV, current production two-source
validation and dedicated-account receipt/cash recovery remain unaccepted; local
tests do not replace those requirements. Existing runtime images were not rebuilt
or promoted by this change and must satisfy the launcher's source matching gate.

## Candidate public oracle route — 2026-10-27 (not captured or accepted)

Pyth's [official sponsored feed documentation](https://docs.pyth.network/pro/sponsored-feeds)
lists ETH/USD and USDC/USD Solana shard-0 accounts with one-minute heartbeat
parameters. This is an announced update policy, not evidence that either price is
currently fresh; the documentation warns updates may be delayed. The official
[Solana integration guide](https://docs.pyth.network/pro/use-prices-on-blockchain/solana)
distinguishes fixed price-feed accounts from ephemeral updates and requires feed
identity and timestamp validation. This candidate would read existing public
accounts, without requesting authenticated Hermes prices or posting updates.

The complete addresses extracted from the official document are:

- ETH/USD: `42amVS4KgzR9rA28tkVYqVXjq9Qa8dcZQMbH5EYFX6XC`.
- USDC/USD: `Dpw1EAVrSB1ibxiDQyTAW6Zip3J4Btk2x4SgApQCeFbX`.

A bounded plan is retained in `reports/review-20261027/pyth-source-docs/plan.json`:
Solana's public mainnet endpoint and PublicNode, at most three read requests each
(`getGenesisHash`, `getMultipleAccounts` at finalized commitment, `getBlock` for
the returned context slot), six total, 60 seconds overall, stop on error, no retry,
redirect, paid call, signature or broadcast. Its authorization is **pending**;
no RPC request was made and no prior quota was reused. Plan hash:
`fcfde2c1a1e0c8b10456c0d5d6cd239b9c23a0bb65f1bde996f8377c45b4d69f`.
The full public HTML is retained with SHA-256
`4ff34bd5618e911fd666ed7944e187442b097bead7d819d9edda4beaf27418ce`.
The initial system-Python document fetch failed CA verification; using the normal
certifi CA bundle succeeded without disabling TLS verification.

Even successful acquisition would establish only raw source availability. Account
owner/program and format pins, feed IDs, verification level, confidence bounds,
freshness, two-source agreement and Base decision/key-use clock bindings still
need implementation and validation. Solana's `minContextSlot` is not an exact
historical-account snapshot selector. Neither a recent response nor this source
plan is complete Base NAV, a production oracle adapter, independent price consensus
or permission to clear a release blocker. Dedicated-account real receipt/cash
acceptance remains separate and has no transaction records supplied by the owner.

## Authorized Pyth source discovery: fresh single-source fields, peer refused

The owner replied “授权本次 6 次免费只读探测” to
`call_274aeff5d8c94615bb65bf39b5b8d466`, authorizing exactly the preceding plan.
The exclusive campaign `reports/evidence/authorized-free-pyth-274aeff5/` retains
the authorization, a request-reservation journal, raw HTTP/RPC responses and the
terminal report. No old budget supplies this quota. The runner was checked offline
with injected six-call success and first-error failure responses before execution;
those checks made zero network calls.

The actual command exited **3** after **5629 ms**: Solana's public mainnet endpoint
completed its three reads, then PublicNode's first `getGenesisHash` returned
**HTTP 403**, with a retained 17-byte response. The campaign stopped immediately.
Usage was **Solana public 3/3, PublicNode 1/3, total 4/6, paid 0**. The remaining
two requests do not authorize a retry or a replacement endpoint. Hashes of all
11 pre-existing evidence budget files matched before and after. No key, signing,
broadcast or production service was involved.

The public endpoint returned account context slot **462172138**, whose separately
read block hash was `8CJ8VnttRstf8BwFuvMZgWDKShTfXsTV2FgmPq1s2QFp` and block time
`1793125718`. The account response was received at `1793125718941` Unix ms.
Offline field interpretation of its two 134-byte accounts found:

| Field | ETH/USD | USDC/USD |
|---|---:|---:|
| Price integer (exponent -8) | 234517299888 | 99981780 |
| Decimal USD price | 2345.17299888 | 0.99981780 |
| Confidence integer | 187137750 | 14904 |
| Publish time (Unix seconds) | 1793125672 | 1793125700 |
| Age at capture (whole seconds) | 46 | 18 |
| Posted slot | 462171939 | 462172062 |

The decoder checks the Anchor discriminator, feed IDs, the `Full` enum byte, field
lengths, positive price/confidence, timestamps and posted slots against the retained
context. Its layout was compared with the upstream
[PriceUpdateV2 definition](https://github.com/pyth-network/pyth-crosschain/blob/main/target_chains/solana/pyth_solana_receiver_sdk/src/price_update.rs)
and [PriceFeedMessage definition](https://github.com/pyth-network/pyth-crosschain/blob/main/pythnet/pythnet_sdk/src/messages.rs).
These are moving source references, not a pinned deployed-program validation.
`Full` is an observed account field, not independently verified signature evidence.
Both accounts report owner `rec5EKMGg6MxZYaMdyBfgwp4d5rB9T1VQH5pJv5LtFJ`; the
program's deployed code, upgrade authority and PDA derivation were not validated.
There is no second-provider agreement or proof of account state against a block
root. Freshness above describes the capture instant, not perpetual eligibility.

Canonical terminal report hash:
`0554e975a972a9169a196bb66ee491fad1edcdaa823801a3ce64a73ecf8f32ff`.
Canonical offline interpretation hash:
`393d85acad735b1d3745aef75d598d3b4f6c2752bf7e7ed0d336e4214d8fc42d`.
The report hashes and each retained response hash were verified. Runner, decoder,
process logs and prior-budget hashes remain under
`reports/review-20261027/pyth-source-docs/`.

This establishes a promising raw public price source, not production oracle,
Base NAV or receipt acceptance. All `execution_eligible`, `oracle_validated` and
`nav_verified` flags remain false. Next work is a pinned offline decoder/program
review and an explicitly bounded replacement-peer acquisition, followed by Base
clock binding and production integration. No release blocker is cleared here.

## Retained Pyth decoder — 2026-10-28 (offline development only)

`evidence/pyth_price.py` now provides a pure, typed decoder of the previously
captured account bytes. It accepts only the two documented Core shard-0 account /
feed pairs, the reported Core receiver owner and non-executable accounts, the
observed 134-byte `PriceUpdateV2` layout, `Full` enum field, exponent -8 and zero
padding. Price must be positive and greater than confidence; this is a structural
screen, not an approved production confidence/spread policy. Partial verification,
foreign feed/owner, unsupported lengths, non-integer context, future/stale times
and posted slots beyond the reported context are refused. Both feeds retain the
owner's 1,800-second ceiling, checked in milliseconds without rounding a stale
observation down. EMA and write-authority fields are retained as uninterpreted
diagnostics. The result always reports `oracle_validated = false` and
`execution_eligible = false`; no production consumer imports this decoder.

The format sources are now pinned to upstream commit
`c2b6cf416412b3282831e67f62b43fea496823d8`, rather than a moving branch:

- [PriceUpdateV2 and verification enum](https://github.com/pyth-network/pyth-crosschain/blob/c2b6cf416412b3282831e67f62b43fea496823d8/target_chains/solana/pyth_solana_receiver_sdk/src/price_update.rs),
  SHA-256 `12d0ce8bc3907ae2949043397eaf3d5bd25deed98450c6969d957be402c807ae`.
- [PriceFeedMessage](https://github.com/pyth-network/pyth-crosschain/blob/c2b6cf416412b3282831e67f62b43fea496823d8/pythnet/pythnet_sdk/src/messages.rs),
  SHA-256 `859f315c9474e694dc5ddb04d1900c5bae8b115a347c4e59746123245cbabc2a`.
- [Receiver program identifiers](https://github.com/pyth-network/pyth-crosschain/blob/c2b6cf416412b3282831e67f62b43fea496823d8/target_chains/solana/pyth_solana_receiver_sdk/src/lib.rs),
  SHA-256 `738f6e4fa9f5807cf3b1baca3407a4caa110c913fcafa3e3952f88b3332f3ce2`.

The SDK distinguishes Core `rec5…` from pro-compatible `rec2…`; this decoder
rejects the latter. That source distinction does **not** attest to deployed code,
upgrade authority, write authority or current consensus verification. The pinned
source's Wormhole comments alone cannot establish the current deployed trust
model. PDA derivation, program/code/upgrade validation, raw RPC envelope and chain
binding, independent sources and a Base decision/key-use clock remain required.
An account response plus a separately fetched block header is not an account-state
proof. A decoded historical price remains historical, even when a caller supplies
its original capture clock.

The original two public RPC responses were retained as the explicitly historical
fixture `tests/fixtures/pyth-solana-single-source-historical.json`, SHA-256
`0196c1166b820604c1ff9039401440ab857d2cd280fc4ef1e60031c9e36476b3`.
Tests verify their body hashes and known field values, then mutate identity,
layout, signed values, confidence, time and slots; synthetic boundary cases cover
exactly 1,800 seconds and 1,800 seconds plus one millisecond for each feed. They
are decoder tests, not signature/quorum/production acceptance.

The official source API lookup failed (sandbox DNS, then public API HTTP 403 rate
limit). Anonymous read-only Git ref lookup succeeded, and the three raw sources
were fetched by the resulting immutable commit with normal TLS verification.
Raw sources and their hash manifest are in
`reports/review-20261028/pyth-layout-source/`. This made no RPC requests and used
no credentials. The terminated six-request campaign was not resumed.

Validation: the 44 focused decoder tests passed. Their first combined command
exited 1 because the format check required docstring indentation; this was fixed.
The first full development run then passed Python/frontend but failed the secret
gate on 14 unreviewed public identifiers, hashes and retained response encodings.
Each was inspected and added individually to the existing non-secret fingerprint
baseline, with its reason; no scanner rule or scan scope was weakened. That failed
report remains at `reports/review-20261028/pyth-decoder-quality/report.json`, hash
`decf836ad14998a67d599549e5bb060d34af9bd9932566da62ca8a5fab485ea6`.

The subsequent full `make live-quality` exited 0: **3559 tests and 138 subtests**,
compile, Ruff/format, mypy, eight frontend modules, secret scan, Bandit (89 low,
zero medium/high) and seven-service offline Compose validation passed. All four
check exit codes, report hash and raw log hashes were verified. Report:
`reports/review-20261028/pyth-decoder-quality-reviewed/report.json`, hash
`55ca200652659fb979b7e8fe46993150b9702dac2ff08d009cb7f920030c804a`.
The existing websockets deprecation warning remains. `make contract-check` also
exited 0: 70 tests passed, none failed/skipped, plus format/build/lint. Its only
operational warning was the sandbox refusing a Foundry signature-cache flush;
raw log hash `3bfc9b7d39054c75df3f789c88d114d0001739fc93da7e8730dba5c6a646641f`
at `reports/review-20261028/pyth-contract-check.log`.

All 11 pre-existing budget hashes were rechecked unchanged. No RPC, key-volume
read, signing, broadcast or production start occurred. The previously verified
Docker image has not been rebuilt with this new source file and must pass a fresh
source-match verification before any later launch. Fresh complete NAV, production
dual-source validation and dedicated-account real receipt/cash recovery remain
unaccepted; this change clears no production release gate.

## Sponsored-account write authority and PDA review — 2026-10-28

Follow-up review found a missing diagnostic invariant: the decoder accepted a
changed `write_authority` even though the supported sponsored accounts must name
themselves. Four negative tests (zero and other-feed authorities for each feed)
failed before the fix. The decoder now checks the 32-byte authority against the
allowlisted account's public bytes; independent Base58 encoding tests bind those
byte pins back to the existing address pins. All 50 focused tests then passed.
The earlier decoder's write-authority field is therefore no longer merely
uninterpreted. This is a candidate-parser hardening change; no live consumer or
release authority was added.

The pinned Pyth commit remains
`c2b6cf416412b3282831e67f62b43fea496823d8`. Its
[push-oracle SDK](https://github.com/pyth-network/pyth-crosschain/blob/c2b6cf416412b3282831e67f62b43fea496823d8/target_chains/solana/programs/pyth-push-oracle/src/sdk.rs)
derives the account with a little-endian u16 shard and the 32-byte feed ID. Its
[update path](https://github.com/pyth-network/pyth-crosschain/blob/c2b6cf416412b3282831e67f62b43fea496823d8/target_chains/solana/programs/pyth-push-oracle/src/lib.rs)
uses that PDA itself as the signed CPI write authority, checks the feed after an
update and ignores older/equal timestamps. The
[receiver](https://github.com/pyth-network/pyth-crosschain/blob/c2b6cf416412b3282831e67f62b43fea496823d8/target_chains/solana/programs/pyth-solana-receiver/src/lib.rs)
requires the existing writer (or an uninitialized account), checks the configured
VAA owner, verified VAA and allowed emitter, verifies the Merkle update and stores
the writer and price fields. Governance can change the allowed emitters and
Wormhole address; source-level checks do not establish their current deployed
values or upgrade authority. `Full` remains an observed field, not locally
verified signatures.

For both ETH and USDC, the offline calculation matched the documented account
exactly at shard **0**, bump **255**, under Core push program
`pythWSnswVUd12oZpeFP8e9CVaEqJg25g1Vtc2biRsT`. It follows the
[Solana derivation](https://solana.com/docs/core/pda/pda-derivation): SHA-256 of
the seeds, bump, program bytes and domain separator. For each resulting compressed
Edwards y-coordinate, `(y² - 1)/(d*y² + 1)` is a quadratic nonresidue modulo
`2^255 - 19`, so no x-coordinate exists under
[RFC 8032 section 5.1.3](https://www.rfc-editor.org/rfc/rfc8032#section-5.1.3).
Both use the first possible bump, establishing canonical addressing for these
two exact seed tuples. Basepoint/identity positive controls were also checked.
This small offline calculation is retained for review, not introduced as a
general-purpose cryptographic implementation in the live application. Both
historical account bodies name the matching PDA as their writer.

Evidence is in `reports/review-20261028/pyth-pda-review/`: fixed upstream sources
and `sources.json`, `check_pda.py`, its raw log and `report.json`, plus the
before/after regression logs. Report hash:
`08e10870b0ee17c911b6ba0495199cdd9236a08a3777b68aad5308d70697c0c6`.
Source SHA-256 values:

- `sdk-pda.rs`: `9aff2e5d7d69e4905e3ec62b543995d6947d3b7478f8ff5af6c422d3ad4fc435`.
- `push-sdk.rs`: `a1727d5798f26672ca4d13f6acbd77546e29e24480ac02829a0a27335eb14eae`.
- `push-lib.rs`: `449975b46f8c0df749bb6671abcf55a280db8c631bfa1bda3c03aa3187e188a1`.
- `receiver-lib.rs`: `554462c221f075e8d7b85f685ea64f3c6ee4d8224a8c475af33c8234d9bc6f8f`.

Remaining acquisition requirements now include both deployed program/data accounts
and their upgrade authorities, the receiver configuration and configured VAA trust
source, in addition to fresh price accounts and independent provider agreement.
None of those reads is covered by an unused part of the terminated discovery
campaign. No new RPC requests were made. Address derivation does not authenticate
RPC state, deployed bytecode, configuration or Base-clock freshness, and it cannot
satisfy NAV or dedicated-account receipt acceptance.

Final validation: `make live-quality` exited 0 with **3565 tests and 138 subtests**,
compile/Ruff/format/mypy, eight frontend modules, the secret gate, Bandit and
offline Compose all passing. Every check exit code and raw log hash was verified.
The only new non-secret baseline entry is the standard Base58 alphabet used in
the address-pin test. Quality report:
`reports/review-20261028/pyth-authority-quality/report.json`, hash
`60e0d7b4faf8c254abcda35572fcc16c220bd09b27c6db10cb4df9872ca6e505`.
`make contract-check` exited 0 with 70 tests, none failed/skipped, plus
format/build/lint; its retained log hash is
`0e53c0496617cfe78a4f2213631f707821a137fda4c74a3aa7ae04bc56680cb2`.
The existing websockets deprecation and sandbox Foundry signature-cache warning
remain. All 11 old budget hashes were verified unchanged. No production image
rebuild, key access, signing, broadcast or release-gate change occurred.
