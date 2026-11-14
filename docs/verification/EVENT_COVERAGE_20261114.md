# Event range coverage and Base observation attempt

Date: 2026-11-14. Baseline: `b81bcea`. Reviewer: Codex implementation self-review; no
independent reviewer or audit is claimed. This increment is observation and offline replay only.

## Invariant and implementation

A chain-wide tip does not prove that every filter observed every block. The retained synthetic
probe has filter A at blocks 100–102 and B at 100 and 103: tip 103 must leave B missing 101–102.
This was a missing coordinator capability, not evidence that an existing live strategy made that
incorrect completeness claim.

`CanonicalChainStore` now records coverage for each chain/stream/block, including observed empty
blocks. `coverage(stream, chain, start, end)` returns missing blocks and the contiguous frontier
relative to the explicit start. Existing databases begin with unknown coverage until ranges are
replayed. An unchanged block hash with different filter log contents is refused rather than silently
replacing events. Distinct source/capture provenance is retained independently of deduplicated logs.
Reorgs require a retained common ancestor, record their reason, and invalidate all affected streams'
coverage and logs atomically. Original capture references remain available as historical evidence.

Tip validation now runs inside `BEGIN IMMEDIATE`. A multi-filter batch can use the same transaction;
exceptions and process exit cannot commit a partial batch. Competing writers serialize before
reading the current tip. This SQLite projection is local storage, not a tamper-proof consensus proof.

`oasis-public-log-range-plan-v1` freezes chain/source, parent hash, 1–16 blocks, address, an exact
1–4-topic prefix, and a declared lock time. It costs N+3 requests: chain ID, N headers, one filtered
log query and a closing header. The existing original ledger, no-retry, STOP, byte and time limits
apply. Header ancestry, post-lock timestamps, filter and block membership, removed/duplicate/ordered
logs, numeric positions and closing-header consistency are checked during capture and offline replay.
Empty arrays are observations; null is missing data and stops the campaign.

`scripts.import_public_range` rechecks original bytes, imports through the existing verified evidence
boundary, and records per-filter coverage. `scripts.replay_cycle_shadow --capture FILTER=DIR
--chain-store DB` feeds those captures into the normal shadow entry point. It validates the full
cohort before committing coverage. A missing current input remains missing even when historical
coverage exists; no quote wakeup is emitted for an incomplete quote-filter cohort. The report is an
immutable historical assessment, not a persisted dispatch queue or a continuously refreshed view.
No scheduler, signing authority, transaction or profitability result has been added.

## Real Base attempt: stopped, no event window acquired

The predeclared experiment is retained under
`reports/research/shadow-coverage-20261114/observation-design.json` and its original
`base-observation/original-budget.sqlite3`. It selected a candidate WETH/USDC pool before requests,
then specified the first three blocks after the first post-identity head, with Swap/Mint/Burn filters.
Worst case was 32 anonymous free reads: 2 identity head + 10 identity + 2 range anchor + 18 range reads.
This was a separate event-acquisition experiment; the prior Metric deployment campaign (10/32 on
Base), other stopped campaigns, and their original ledgers were not modified or retried.

PublicNode's [Base page](https://base.publicnode.com/) advertised the anonymous free endpoint.
The factory address was checked against [Uniswap's Base deployment documentation](https://developers.uniswap.org/docs/protocols/v3/deployments/v3-base-deployments).
The event signatures match the [v3-core v1.0.0 interface](https://github.com/Uniswap/v3-core/blob/v1.0.0/contracts/interfaces/pool/IUniswapV3PoolEvents.sol).
An explorer lookup returned HTTP 403; it is not used as identity evidence.

At Base block **57195653**, hash
`0x250e85901aefdde394aa9513a506b6d9afbd838bc817d548e71eff8a12475d04`, a single provider returned:

- Factory `0x33128a8fc17869897dce68ed026d694621f6fdfd` `getPool(WETH,USDC,500)`:
  `0xd0b53d9277642d899df5c87a3966a349a798f224`.
- Pool `factory()`, `token0()`, `token1()`, and `fee()` agreed: WETH
  `0x4200000000000000000000000000000000000006`, USDC
  `0x833589fcd6edb6e08f4c7c32d4f71b54bda02913`, fee 500 pips.
- Observed factory runtime keccak:
  `0x95707a4ac71f20181a63ef7d180e3c625be5d20fc8f6f980befa966bad568132`.
- Observed pool runtime keccak:
  `0xcd06f61c6db6a1d8317548aaaa0aa83254624aec741534c51815810e977587ae`.

These are consistent registry/getter observations, not independent source recompilation, token
safety, provider independence or evidence of executable liquidity.

The next head response declared block **57195673**, hash
`0x156bb2048c048a2587eba7e3a1c7c725bd9e26cdc4c86771e4208f9ef135edbc`, timestamp
`1794629510`. The local response clock was `1794629509970` ms: the declared block time was **30 ms
in the future** relative to that clock. Context decoding otherwise succeeded. The collector refused
it under the existing time rule; local clock accuracy versus provider timestamp accuracy is unknown.
No tolerance was introduced and no new campaign was used to retry the failed scope.

The attempt exited **3**, persisted `STOP`, and spent **14/32** requests. No `eth_getLogs` was sent,
no real event coverage was imported, and no equal-budget polling comparison or profit can be reported.
The failed raw response SHA-256 is
`0beede23d4798f3d0c9cab893104efda4e0c1e81c280e8aae616538a76fb6ad9`.
Its offline replay verifies the retained failure and exports only the successful chain-ID read.
The remaining allowance is not permission to resume a stopped campaign. Dollar allowance is not an invoice.

## Verification and remaining work

Focused tests cover missing intermediate filter blocks, empty coverage, restart, common-ancestor
reorg invalidation, conflicting same-hash logs, legacy migration, provenance, competing writers,
exception and process-exit rollback, strict range capture, first-failure STOP, raw-byte tampering,
and the public-capture → evidence-store → cycle-shadow CLI path. Restarting the latter with one
missing filter clears quote wakeups even though the earlier coverage remains in the database.
An initial integration test used the intentionally synthetic chain ID with a real source alias;
it failed correctly and was corrected to a matching synthetic fixture on the source's chain ID.
Its original failure log remains `focused-initial.log`.

All local logs and raw responses remain ignored under `reports/research/shadow-coverage-20261114/`.
The final check counts and exit codes are recorded below after completion.

Still required: actual prospective event windows, independent time/source evidence, a durable
outbox with proposal invalidation and restart delivery semantics, maker/oracle triggers, a frozen
same-budget polling comparison, exact transaction simulation and untouched net-NAV evaluation.
Per-filter retained coverage is an engineering prerequisite, not completion of the event strategy.

Final checks on this increment:

- Focused regression: 130 tests passed (exit 0), `focused-final-2.log`.
- `make check`: exit 0; **5,920 Python tests, 138 subtests, 9 frontend modules,
  70 Foundry tests**. `make-check.log` includes lint, format and mypy results.
- `make security-check`: exit 0; dependency audit, Bandit and secret gate passed,
  `security-check.log`.
- Documentation: 4 changed documents, 34 local links, 3 shell examples, schema example and
  both new/extended CLI help interfaces validated; exit 0. `docs-check.log` retains the
  first 33-link pass; the added execution-plan link was checked separately.
- Original Base observations and failure replayed offline with matching hashes; no new RPC.
- Local tools: Python 3.12.4, Node v23.11.0, Foundry 1.8.4 Homebrew. CI specifies Node 22.14.0
  and Foundry 1.8.1; this record reports the local versions actually used and does not claim a CI run.
- `make contract-security-check`: exit 0; Slither analyzed 80 contracts with 102 detectors,
  **0 findings**, `contract-security-check.log`.
- `git diff --check`: exit 0. Reviewed task paths contain no raw captures, credentials, keys,
  new call targets for signing, financial arithmetic or production-state changes.
