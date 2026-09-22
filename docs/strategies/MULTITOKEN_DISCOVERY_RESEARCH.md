# Dynamic token discovery and research scoring

Updated 2026-09-20. No fixed LAPTOP target in this workflow. Implemented discovery/batch research,
not a live autonomous trader. High scores are neither return probabilities nor trade authorization.

## Official practices reviewed and project decisions

| Primary source | Relevant capability/practice | Project adaptation and limits |
|---|---|---|
| [GMGN market reference](https://github.com/GMGNAI/gmgn-skills/blob/main/skills/gmgn-market/SKILL.md) | Trending time windows, launchpad filters, creation time and market fields | Retained rank imports feed many chain/address candidates; compare the same window. Do not reuse market-cap, FDV or volume fields interchangeably. Authenticated CLI/API capture remains unconnected. |
| [GMGN quick start](https://docs.gmgn.ai/index/quick-start) | Security/transaction data comes from third parties | Two app labels do not prove independent economic or chain evidence. |
| [OKX hot tokens](https://web3.okx.com/onchainos/dev-docs/market/market-token-hot-token) | Up to 100 ranked results, time windows and filters | Preserve query scope and ranking type. No extrapolation from page rank to calibrated expected return. |
| [OKX holders](https://web3.okx.com/onchainos/dev-docs/market/market-token-holder) | Bounded holder list, tags and funding-source fields | Useful candidate addresses, not a complete balance census. Pagination within the bounded result does not establish all holders. |
| [OKX cluster overview](https://web3.okx.com/onchainos/dev-docs/market/market-token-cluster-overview) | Shared-funding, holder-age and concentration diagnostics | Require provenance, unit verification and chain support before mapping to common-control risk. Provider cluster scores are not forensic certainty. |
| [OKX Trenches](https://web3.okx.com/onchainos/dev-docs/market/market-scan-chain-api-reference) | New-token, developer and bundle analysis APIs | Future authenticated acquisition should cover launch/developer history separately from popularity. Not implemented by the rank importer. |
| [FOMO exits](https://fomo.family/answers/when-to-sell-a-memecoin) | Preplanned targets, trailing/time exits, staged realization and declining activity | Keep exits independent from entry popularity. Existing net-value stop/trailing/profit/time proposals remain simulation only. Staged fills require a reconciled fill ledger before implementation. |
| [FOMO trader assessment](https://fomo.family/answers/how-to-find-profitable-crypto-traders) | Multi-window history, sample size and sizing consistency | Social trades are leads; require complete realized history and executable follower costs. Do not promote screenshot/leaderboard PnL into our net PnL. |
| [DEX Screener API](https://docs.dexscreener.com/api/reference) | Public latest profile feed, documented rate limits | Supplemental discovery only: profile update is not token birth, an organic popularity rank or executable liquidity. |

User confirmed FOMO means `fomo.family`. Official home/FAQ pages were reviewed; no verified developer
API contract was found there. A typed `fomo` discovery signal is an integration boundary for
explicitly retained normalized input, **not a working FOMO connector**. Unofficial APIs, session-token
extraction and anonymous marketing claims were not used as engineering authority.

## Implemented data flow

`retained GMGN/OKX rank responses / normalized discovery signals / public profile capture`
-> strict `DiscoveryRequest` -> bounded deduplicated candidate queue -> independent evidence
acquisition -> `BatchRequest` -> shared `score_candidate` results and independent held-position results.

- Identity is chain plus canonical EVM address, never symbol. Configurable chain scope, no fixed token.
- At most 2000 input signals / 500 candidate slots. Excluded stale/future/off-chain and capacity-deferred
  counts are explicit; the complete request hash retains their influence on the audit input.
- A provider contributes at most one latest rank per token. Same-time disagreements require review;
  choosing the most favorable duplicate is forbidden. GMGN/OKX ranks must share the configured window.
- Attention priority uses the weaker of two market ranks. A single rank has half weight; social and
  profile-only records do not boost it. Attention is an enrichment queue priority, not a trade score.
- Explicit creation times classify immature/new/established; missing/conflicting times block entry
  research promotion in batch scoring. Creation semantics must refer to the same launch event; these
  imported timestamps do not establish a raw on-chain birth proof.
- Missing enrichment produces `score=null` plus status, never a numerical substitute. Enrichment
  outside the selected universe, duplicate candidates and mismatched evaluation times are rejected.
- Rank evidence hashes/times/windows must match the latest discovered evidence before scoring entries.
- Existing positions are separately evaluated even after leaving discovery or losing all trending
  signals. Popularity filters no longer suppress stop-loss proposals. Fresh chain, risk and net exit
  valuation checks remain enforced. Holdings absent from the request are unknown, not presumed closed.
- No broadcast, signer, approvals, capital allocation, paid queries or live configuration mutation.

## Runnable commands

```sh
# Exactly one public request, no retry. New directory required.
.venv/bin/python scripts/discover_tokens.py --public-profiles \
  --chains 8453 4663 1 56 --output /tmp/new-discovery-capture

# Replay DiscoveryRequest JSON. New file required.
.venv/bin/python scripts/discover_tokens.py \
  --input /path/to/request.json --output /tmp/new-discovery-result.json

# BatchRequest = {discovery: DiscoveryRequest, policy: ScorePolicy, candidates: Candidate[]}
.venv/bin/python scripts/score_token_universe.py \
  --input /path/to/batch.json --output /tmp/new-batch-result.json
```

Library rank bridge: `import_ranking_discovery`, preserving provider/time/window/raw hash. FOMO signals
must remain labelled supplied inputs until an authorized documented capture adapter is validated.
The console supports individual candidates and BatchRequest uploads through the shared
`POST /api/v1/research/batch-score` API. It separates attention from research score, shows missing
enrichment as unscored, and lists held-position exits independently. It also displays imported
`failure.json` HTTP errors without treating them as empty markets. It does not poll live feeds.
No persistent scheduler, backfill, provider authentication client or streaming recovery is claimed.

## Actual network outcome in this change

The local public profile command returned HTTP 403. The first attempt produced no result, and the
instrumented retry retained sanitized `failure.json`. A separate single no-credential request to the
official OKX Base hot-token endpoint also returned HTTP 403. Do not reinterpret these as empty lists,
rate-limit passes or tested live connectors. No credentials or paid RPCs were used. A web retrieval
could view public profile data, but that does not establish successful local runtime acquisition.
Artifacts: `reports/evidence/live-discovery-20260920-diagnostic/` and
`reports/evidence/okx-hot-access-20260920/`.

## Research scoring and exit progression

Current scoring: hard vetoes first, then heat/buyer share/depth/holder dispersion/FDV-liquidity proxy.
Costs use integer USD micros; executable exit value includes selling costs. No edge is established.
Follow-up research should separately measure organic acceleration, independent buyer breadth, funded
clusters/dev selling, quote stability, social/trader activity and follower slippage. A new feature
requires a frozen trial ledger, split and acceptance criteria before untouched evaluation.

An intended entry still needs pinned contract/proxy/authority/token behavior, holder coverage/control,
bidirectional quote and sell simulation, full cost model, global cash/NAV/loss reconciliation, shared
capital reservation and isolated signer policy. The owner envelope remains USD 500 total, USD 200
daily loss, USD 500 cumulative loss; no chain or strategy gets a second allowance.

Future staged exits need reconciled partial fills and idempotent tranche IDs; never infer a sale from
an intent or count proceeds twice. Liquidity/contract failure calls for fresh executable exit analysis,
not unbounded slippage. LP is a distinct cost/routing/counterfactual experiment. These are requirements,
not implemented production behavior. Defaults and attention weights remain unlocked hypotheses under
`reports/experiments/meme-confluence-v1-design.md`; no promotion or profitability claim.


## Batch console verification (2026-09-20)

The batch response carries the original evaluation timestamp plus stale/future, off-chain and
capacity-deferred counts. Unknown enrichment stays unscored; an empty imported holding list is never
labelled an empty account. Semantic evidence-binding failures return a sanitized HTTP 422 response.
The browser sends original JSON text so integer financial amounts are not rounded. Failed requests
clear both candidate and exit tables. Imported provider failures display locally and are not uploads
of credentials. The endpoint performs no RPC, writes no trading state and never signs/broadcasts.

An isolated localhost server accepted 50 synthetic candidates over real HTTP with exact core parity;
this is a transport/UI integration check, not real-market accuracy. See
`reports/batch-console-validation-20260920.md` for gates and remaining limits.
