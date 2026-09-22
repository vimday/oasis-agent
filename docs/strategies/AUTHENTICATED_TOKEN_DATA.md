# Authenticated token research acquisition

Design recorded 2026-09-21 before implementation. This is an observation data plane,
not a release of autonomous trading or LP.

## Invariants and flow

Fixed, read-only GMGN CLI / OKX HTTPS routes -> bounded retained responses -> replayable
provider observations -> existing multi-token discovery priority. Candidate enrichment
is limited to a deterministic prefix of the discovered universe. Failed requests remain
failures, empty successful lists remain empty, and missing risk fields remain unknown.
No wallet keys, signatures for transactions, approvals or broadcast routes enter this flow.
OKX HMAC authenticates data requests only.

Source claims retain their original field names and units. GMGN bundle trading volume
ratios are not OKX bundle holding percentages. Provider risk labels are not a contract
audit, proof of common control, sellability, complete holder census or block-pinned evidence.
Collection time is not source event time; cross-provider calls are not atomic snapshots.
All outputs remain execution-ineligible. Existing scoring requires its independent
enrichment inputs; the collector must not fabricate those to obtain an entry score.

## Initial bounded scope

Base and Robinhood use the same explicit chain map. Each run obtains one five-minute
trending list per provider/chain, followed by GMGN info/security/pool/holders and OKX
advanced-info/holder/cluster-overview for a bounded candidate prefix. Retain selection,
unselected candidates, capture times, response hashes and per-request failures. No retries
or paid-plan upgrades by the wrapper; stop a provider for the run after an access/rate error.
GMGN CLI may internally retry; its version and output transformation boundary are recorded.
The wrapper spaces requests and never starts an unattended process implicitly.

## Sources and applicability

- [GMGN official skills](https://github.com/GMGNAI/gmgn-skills): market/token commands,
  chain-specific fields and query credentials. CLI output is retained as CLI output,
  not falsely labelled unmodified upstream HTTP bytes.
- [OKX authentication](https://web3.okx.com/onchainos/dev-docs/home/api-access-and-usage):
  UTC timestamp, exact path/query HMAC and secret handling.
- [OKX hot tokens](https://web3.okx.com/onchainos/dev-docs/market/market-token-hot-token):
  rankingTimeFrame=1 means five minutes; platform filters affect candidate coverage.
- [OKX advanced information](https://web3.okx.com/onchainos/dev-docs/market/market-token-advanced-info)
  and [clusters](https://web3.okx.com/onchainos/dev-docs/market/market-token-cluster-overview):
  field names and provider-defined risks. Percent fields remain provider percentages;
  ambiguous scale or methodology is not silently resolved from the value's magnitude.
- [OKX holders](https://web3.okx.com/zh-hans/onchainos/dev-docs/market/market-token-holder):
  current documentation describes up to 100 holders and default profit ordering. Do not
  calculate top-10 supply concentration from this list or presume the list is a census.
- [FOMO token analytics](https://fomo.family/answers/how-to-read-token-stats) confirms
  product analytics, but no public developer API was found in this review.
  [FOMO terms](https://fomo.family/terms) restrict unauthorized automated extraction.
  FOMO is `authorization_required`, not an empty feed or verified connector. Official
  support is support@fomo.family; an API/data licence inquiry is prepared below, not sent.

## FOMO inquiry draft

We are building Oasis Agent, a private on-chain research system. Do you offer authorized
read-only API access for token discovery, price/liquidity, holder concentration, bundle
labels and aggregate social signals on Base and Robinhood? Please provide documentation,
authentication, rate limits, pricing, timestamp semantics and permitted retention/use.
We do not request personal account data or permission to scrape private application APIs.

## Further acceptance gates

Sustained captures need persistent scheduling, crash recovery, quotas and freshness/gap
monitoring. Trading additionally requires contract/runtime verification, sell simulations,
executable depth, account reconciliation and global risk integration. LP requires pool
identity, tick/liquidity/flow evidence and out-of-sample costs/baselines; token popularity
alone cannot select a profitable range or hook. No economic edge is established here.

## Running the collector

GMGN uses `~/.config/gmgn/.env`; OKX selects only `OKX_API_KEY`, `OKX_SECRET_KEY`,
`OKX_PASSPHRASE` from the project `.env`. Do not pass secrets in command arguments.
Global GMGN CLI configuration is not OS-isolated; keep wallet keys out of it.

```bash
.venv/bin/python -m scripts.capture_token_research \
  --chains 8453 --limit 5 --enrich 1 --output reports/evidence/new-provider-capture
.venv/bin/python -m scripts.capture_token_research \
  --replay reports/evidence/new-provider-capture
```

Output directories must be new. `request.json` can be consumed by the existing
`scripts/discover_tokens.py --input` flow. The richer `result.json` is not a batch-score
API request. No frontend polling or scheduler is installed.

GMGN CLI 1.6.6 returns ranking envelopes but unwrapped token-detail objects; both are
handled explicitly. New captures use schema v2: normalized GMGN flags accept only explicit
booleans, integer 0/1 and yes/no strings. Conflicting aliases or unsupported values produce
unknown, with field provenance and conflict diagnostics. These remain provider claims.
Holder rows retain wallet identity, reported human-unit balance and explicit share units:
GMGN ratios (0..1), OKX percentages (0..100). Missing values differ from zero; duplicate
wallet rows are rejected. Neither list establishes a complete census or common control.

Replay supports v1 and v2, checks deterministic enrichment selection, request coverage,
provider halt order, field-unit metadata and raw hashes. A recorded normalization failure
now prevents successful replay; repair requires a separate versioned interpretation.
Hashes prove retained-byte consistency, not authenticity against a malicious archive author.
Retained raw responses support decoder changes without rewriting history.

See [actual validation and limitations](../../reports/provider-data-validation-20260921.md).

See [v2 validation](../../reports/provider-data-validation-20260922.md).
