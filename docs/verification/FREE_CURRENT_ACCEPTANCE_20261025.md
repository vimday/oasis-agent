# Free current-state attempt and report cleanup — 2026-10-25

## One explicit owner-authorized attempt

The owner approved one new current-state read-only campaign using only the public
Blockmachine and QuickNode documentation endpoints: at most 64 requests per source,
a 120-second total deadline, no retries, stop on failure, no paid requests, signing
or broadcasting. This is a separately authorized phase, not a reset or replacement
of any earlier discovery, NAV or paid acceptance ledger. The approval is retained
in `reports/evidence/authorized-free-current-20261025/authorization.json`.

The fixed-output runner refuses an existing campaign directory before creating any
budget or transport. It does not read `.env`, load a key, mount a production volume,
or expose a broadcast method. It tests feed freshness before wallet acquisition,
runtime checks and a simulation-only approval envelope. A successful diagnostic
would still not establish all-token inventory, funded historical basis, upstream
provider independence or real receipt recovery.

The actual run exited **3** after **11.695 seconds**, at `feed_preflight`:

- Base 8453, block **53056495**, timestamp **1792902337**.
- Block hash `0xd6179b40ca2bc353b731613eda9ac66609eb78ed6a910e750a200dbb7fffab7b`.
- Blockmachine ETH/USD answer **236050622043** with eight decimals, updated at
  **1792901837**, age **506 seconds** at its check.
- Blockmachine USDC/USD answer **99984118** with eight decimals, updated at
  **1792846375**, age **55972 seconds** at its check, exceeding **1800 seconds**.
- Both feed proxy runtime hashes, descriptions and decimals were checked. Reads
  used the observed block hash. The refusal happened before a closing header and
  before the second provider; this is not an accepted two-source price proof.
- Requests spent: **Blockmachine 11, QuickNode 0**. Paid requests: **0**.
- NAV, runtime and simulation checks were not reached. No dedicated-account
  receipt acceptance occurred. All release gates and capital/loss limits remain.

Changing RPC transport cannot refresh an old on-chain round. The unused request
allowance does not permit an automatic retry of this one-attempt authorization.
A suitable fresh verifiable price source remains necessary before proceeding.

Raw request/response report:
`reports/evidence/authorized-free-current-20261025/report.json`; hash:
`8df64ee36b9f42ff036d280e700990353a418fa86f9177247c9f60485e2f916c`.
Authorization hash:
`a4f55ecc516e1c5c51d1f5eafef1b51505de8ad10c48ad2f6f1fffc22a21db26`.
Runner: `reports/review-20261025/authorized_free_current.py`; hash:
`c9a20998fde5c5cc67baeeb291346576caf1fc084a11ada1c2504b543b4f61ef`.
Execution log: `reports/review-20261025/authorized-free-current.log`.

Offline replay verified all three hashes, all 11 wire exchanges, and both decoded
rounds against the report (exit 0, `replay_current.py` / `replay-current.log` in the
same review directory). An initial ad-hoc replay mistakenly treated
`decode_exchange`'s returned parameter list as a request dictionary and failed with
`TypeError`; that original failure remains in tool output. The corrected replay
uses the exchange's method and the returned parameters. No network retry occurred.
Four synthetic feed-preflight smoke cases passed before acquisition: fresh, exact
1800-second boundary, expiration during capture, and already expired.

## New free historical trace capability (2026-10-24)

A separate first discovery of Blockmachine used four no-key reads and exited 0.
Its [official Base documentation](https://blockmachine.io/docs/base-rpc) describes
a keyless free-tier endpoint and debug methods, but also says its Base suppliers
proxy upstream node pools without Base validator sampling. Thus agreement does
not establish independence from another provider's upstream infrastructure.

The complete raw trace and receipt agree with the retained QuickNode sample for
Base block **51949838**, hash
`0xec2779b91c51550bd764f1cb1c5a5f597a128fd9efd0c54895a824a1b6d32fb1`,
transaction `0x22a7c0e715455f2ad99f57bcf7789edac2faf8c3506d1b911e8319bb7205c7f2`.
Offline replay against the retained dRPC transaction body counted **169 frames**:

- Trace hash `53825d86a9f4c1598f5e2a6f7f528c1a6840ce03dfcf145c5d9f249bd7b25c86`.
- Receipt hash `fc4f6857f3ef8533392700420ee731431b7029f2c67152d17350a088c90619f4`.
- Acquisition: `reports/evidence/free-blockmachine-20261024/report.json`, hash
  `b7f38155dfd84175dc222477b77f87cd8eeefe79d737f9df1813a1e86486ee84`.
- Offline comparison: `reports/review-20261024/free-blockmachine-replay.json`, hash
  `6318b6152fc6ff323df1de9d9fc5b0e82b2ad8c51159b61dfb49a56610cf2b9d`.

This is a historical third-party transaction, not the canary's own cash proof,
current runtime/simulation acceptance or sustained provider reliability.
No new full fee-oracle capture is attributed to Blockmachine.

Read-only checks confirmed original ledger usage is unchanged: capability 1rpc 7 /
publicnode 4; discovery quicknode-demo 4 / onfinality 2; NAV base_public 31 /
publicnode 30; provider acceptance drpc 8 / alchemy 4. Total paid acceptance remains
at the previously recorded eight requests, under the same USD 0.10 cumulative
ceiling with invoice pricing unresolved. The separate Blockmachine discovery
ledger remains at four. None was reset, copied, replaced or deleted.

## Old generated report cleanup

Removed **810 loose synthetic pytest files** and **169 pytest links** from twelve
`synthetic-state*` directories under the October 2 position-hold-return,
position-return-replay, position-cost-valuation and historical-quote-depth reviews.
These are retained test temporary directories, not real chain captures. Tracked
files and names indicating keys, secrets, budgets, nonces or journals were excluded.

Because historical checkpoint manifests reference some of these fixtures, exact
bytes were first compressed and every archived file's hash verified, then checked
again on disk before deleting the loose directories. The original historical
manifests and failed/passed test logs remain intact; their old fixture paths now
refer to archive members rather than live files. Do not interpret absent loose
fixtures as missing real production evidence or rewrite those old manifests.

- Original bytes: **117815734**; archive bytes: **8004470**.
- Net reduction before the small manifest: **109811264 bytes** (about 109.8 MB).
- Archive: `reports/archives/synthetic-tests-20261002.tar.gz`.
- SHA-256: `1f26742e0856d502d24face747e8414c741decc158edf42e48cdf37bdd63d13f`.
- Path/hash inventory: `reports/archives/synthetic-tests-20261002.manifest.json`.

Original chain evidence, including the large market archive, all real request
budgets, runtime state, keys and source files are preserved. Age alone does not
make an evidence database disposable. An archive is not a production state restore;
only synthetic test artifacts are included.

## Remaining production boundary

A read-only Docker volume-name check found no volumes for the configured
`oasis-live` Compose project. No service was started and no key volume was mounted.
Consequently no configured-project strategy/authority journal was available for
real receipt recovery inspection. This does not rule out custom deployments.

Next dependencies remain a fresh verified USD price path, full account inventory
and funded basis, current two-provider runtime/simulation proof with source
independence, then both deployed services' real dedicated-account receipt/cash
recovery. No gate is marked passed by a fixture, archive cleanup or historical trace.
Production code did not change in this increment; the prior 3473-test quality run
is historical development evidence, not a newly run suite or production acceptance.
