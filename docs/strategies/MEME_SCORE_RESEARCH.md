# Meme candidate scoring and strategy preparation

Status: implemented research scoring/API/import foundation, not a live trading system.
Version: `oasis.meme-score.research.v1`. Defaults are hypotheses, not profitable calibrated settings.

## Data and authority flow

Authorized GMGN/OKX retained responses -> chain/address/time-scoped rank imports -> normalized
candidate + independent chain/risk inputs -> deterministic score -> simulate/watch/reject proposal.
The server and CLI invoke the same function. No scoring path imports signing or broadcasting.

`launch/trend_import.py` supports official GMGN `data.rank` and OKX hot-token `data` first-page
schemas. It validates chain/address, ranks, response status and duplicates, retains raw SHA-256 and
requires capture timestamps/window. It is an offline importer, not an authenticated API client.
The operator must retain the exact response bytes and query parameters, including first-page and
ranking sort/filter settings. API rank is relative to that provider's chosen universe; two sources
may share upstream data. Neither labels nor hashes alone attest authenticity or independent sources.
No source security flag is automatically promoted into a verified chain check.

GMGN query credentials are distinct from trading credentials. No private wallet key is needed by
this scoring module. No `GMGN_`/`OKX_` names were found in project `.env`, and gmgn-cli was absent
when checked. Other configuration locations were not searched. Live acquisition has not been tested.
Official docs: [GMGN Agent API](https://docs.gmgn.ai/index/gmgn-agent-api),
[GMGN ranking schema](https://github.com/GMGNAI/gmgn-skills/blob/main/skills/gmgn-market/SKILL.md),
[OKX hot-token API](https://web3.okx.com/onchainos/dev-docs/market/market-token-hot-token).
Provider documentation differs over supported chains; verify actual access per chain before use.

## Hard gates, then score

Chain/token mismatches, stale/future data, missing distinct providers or unequal ranking windows
block entry. Holder coverage, contract analysis, sell simulation, bounded/absent mint/freeze powers,
liquidity exit and wash-trading checks are `pass/fail/unknown`; only pass clears the research gate.
Concentration, common-control concentration, impact, round-trip costs, minimum holder/trader samples,
executable depth and FDV/liquidity caps also veto entry. Provider rankings never compensate for them.
The trader-count gate uses max(buyers, sellers), a conservative lower bound on the distinct union;
it never adds overlapping wallet counts to pass the minimum sample gate.
`mint_or_freeze_authority=pass` means authority was assessed as acceptably bounded/absent, not present.

`launch/holder_scoring.py` now replays two retained holder captures and overrides imported holder
count, top-ten concentration and supply coverage. It requires matching chain, token, runtime hash,
decimals, block number/hash/timestamp, freshness and retrieval before evaluation. Token runtime is
pinned, but proxy implementation/admin safety remains separate. Supply coverage is conditional on
standard truthful ERC-20 semantics; explicit candidate addresses are bounded to 512, so this is not
a complete holder indexer for arbitrary large tokens.

Raw balances cannot establish common beneficial ownership. `common_control_analysis` defaults to
`unknown`, which now blocks entry even if imported common-control concentration is low. The raw-holder
adapter deliberately resets this assessment to unknown; an independently evidenced control-analysis
stage remains required. Contract scanning and sellability are still supplied normalized assessments,
not newly implemented on-chain scanners. The base score remains `normalized_import_not_raw_attestation`;
the holder envelope separately identifies the narrower conditional two-source raw replay claim.

Component order and default weights (10000 integer basis-point scale):

| Component | Weight | Calculation / limitation |
|---|---:|---|
| Heat | 20% | Minimum of GMGN/OKX relative rank scores; not a return probability |
| Buyer share | 20% | Unique buyers / (buyers + sellers); wallets may overlap between sides |
| Executable depth | 25% | Size relative to 5x minimum accepted depth; not headline TVL |
| Holder dispersion | 20% | 1 minus larger top-ten/common-control concentration |
| Valuation proxy | 15% | Bounded liquidity/FDV ratio; not intrinsic value or proof of cheapness |

Money remains integer USD micros; token identity includes chain ID and canonical EVM address.
No binary float financial input is accepted. Total component weights must equal 10000. Output hashes
bind the complete candidate and policy. Safety checks are prior to deciding an entry proposal, even
though the diagnostic score remains visible for rejected candidates.

## Entry, exits and LP

Eligible research candidates above the threshold produce `simulate_entry`; otherwise watch/reject.
This is not a buy order. A downstream system would still need fresh executable quotes, full gas/tax/
MEV costs, atomic capital reservation, signer policy and reconciled fill/receipt handling.

Existing positions no longer require continuing GMGN/OKX popularity to evaluate exits.
Existing positions use **net executable exit value**, not last price, compared to original entry cost
including entry costs. Stop loss takes precedence, then high-water drawdown, take profit and time
stop. A stale position or any data/risk blocker gives `halt_review`, requiring a fresh exit simulation;
it does not silently hold, claim an exit happened or send a blind market sell. The high-water mark
and position values are supplied inputs; a persistent reconciled position service remains required.

LP is a separate hypothetical comparison. With no current position, no blockers and sufficient score,
a fresh LP forecast may propose `simulate_lp` only if routing is assessed pass and expected fees
strictly exceed all declared costs/inventory losses plus hold opportunity cost. This is an alternative
simulation candidate, never a directive to fund both spot and LP. The aggregate cost forecast is not
independently reconstructed by this module. A distinct Oasis hook pool needs distinct routing proof.

## Runnable interfaces

```sh
.venv/bin/python scripts/score_token_candidate.py \
  --input configs/meme-candidate.fixture.json \
  --policy configs/meme-score.research.json --output /tmp/new-score.json
```

The named fixture is synthetic. Outputs refuse overwrite. Offline import API:
`import_trends(raw, provider=..., chain_id=..., window_seconds=..., observed_at=..., retrieved_at=...)`.
Observation-server API: `POST /api/v1/research/token-score`, JSON `{candidate: ..., policy: ...}`;
returns exactly the same versioned `ScoreResult` as CLI/core. It does not persist or fetch data.
The dashboard “代币评分研究” view accepts candidate/policy JSON files and displays the same result,
including blockers and exit/LP proposals. It sends the original JSON without JavaScript integer
rounding and clears failed results. Streaming provider ingestion and execution integration remain
unimplemented.

## Experiment and latency requirements before promotion

Hypothesis: confluence of authentic breadth/depth and dispersed ownership selects better risk-adjusted
candidates than popularity alone. Rejection conditions include no net out-of-sample excess return,
excessive false positives, stale-data rate, concentration or inability to simulate liquidation.
Compare popularity-only, equal-weight and no-trade baselines with identical costs and universe.
All existing fixtures are development data; no final evaluation window has been opened. Before
real evaluation, freeze the chain universe, query filters, time splits, trial count, weights,
thresholds, missing-data policy, minimum samples, confidence and tail/drawdown acceptance criteria.
Record failed candidates and delisted/rugged tokens to avoid survivorship bias. Report unknown
expected returns until evaluation; do not annualize short-window fee income.

Fast discovery must deduplicate by chain/address and coalesce refreshes; cache only under explicit
TTL/runtime/block bindings. No latency target or ranking accuracy has yet been validated against
live APIs. Score computation is deterministic and bounded to two signals; network acquisition,
contract scanning, simulation and landing latency require separate benchmarks.

Global owner limits remain USD 500 capital / USD 200 daily loss / USD 500 cumulative loss. Scores
cannot widen these limits, and those shared ledgers are not connected to this module yet.

## Holder evidence integration (2026-09-20)

```sh
.venv/bin/python scripts/score_holder_candidate.py \
  --request /path/to/holder-score-request.json \
  --evidence /path/to/retained/evidence.db --output /tmp/new-holder-score.json
```

The strict request object contains `candidate`, `policy`, `identity` and exactly two `references`.
`identity` includes `chain_id`, `token`, `runtime_hash`, `token_decimals`; references are the two
`evidence` entries emitted by `capture_launch_holders.py`. Supply the candidate's actual matching
historical evaluation time; never relabel old evidence as fresh. This command uses retained raw
requests/responses, makes no RPC calls and refuses output overwrite. The current EvidenceStore
constructor can initialize/migrate its schema, so use a working copy for immutable archives.

The output retains the original candidate hash, a deterministic holder identity/reference binding
hash, the derived candidate, and the shared score. Non-holder provenance is preserved. Reversing
provider order produces the same result. Missing/corrupt/incompatible evidence raises an error with
no output; partial supply coverage produces an explicit holder failure. This enriched workflow is
currently CLI/library only; the existing web score endpoint still accepts normalized research inputs.
No automatic holder-address discovery, common-control attribution or live enrichment service is claimed.

Dynamic many-token discovery, source research and batch interfaces: [workflow](MULTITOKEN_DISCOVERY_RESEARCH.md).
