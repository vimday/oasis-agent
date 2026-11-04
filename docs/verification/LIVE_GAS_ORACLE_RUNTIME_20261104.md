# Fee-oracle code approval and bounded capture — 2026-11-04

New execution now requires explicit approval of the Base fee-oracle proxy,
implementation address and implementation code. The committed production
configuration still contains only its original five pins; it remains usable for
historical replay and read-only recovery, and is refused for new execution.
No production fingerprint was inferred from a fixture or an old observation.

## Confirmed implementation

`live/gas_oracle.py` defines three additional `expected_pins` entries:

| Entry | Meaning |
| --- | --- |
| `gas_oracle_proxy` | Keccak-256 of the existing `0x4200…000f` oracle role's proxy code |
| `gas_oracle_implementation` | The complete canonical 32-byte implementation storage word, containing a nonzero address |
| `gas_oracle_implementation_code` | Keccak-256 of code at that approved implementation address |

The validator expects the OP Proxy implementation layout defined by
[OP Proxy](https://raw.githubusercontent.com/ethereum-optimism/optimism/develop/packages/contracts-bedrock/src/universal/Proxy.sol)
and [OP Constants](https://raw.githubusercontent.com/ethereum-optimism/optimism/develop/packages/contracts-bedrock/src/libraries/Constants.sol).
The slot is `keccak256("eip1967.proxy.implementation") - 1`, independently
re-derived in a test. These source references establish the expected layout;
they do not establish Base's current deployed code. The attempted upstream
commit/file retrieval failed and is retained in `gas-oracle-runtime/upstream/`;
no upstream commit or deployed fingerprint is claimed verified by that attempt.

Configuration accepts exactly the original five entries or all eight. A partial
addition, a malformed/zero implementation word or a placeholder/empty-code hash
refuses. New strategy/authority execution CLIs check the full policy before RPC,
state creation or key access. One-command Docker start checks it before Docker,
environment access or output creation. Existing owner release approval remains
separate and unchanged.

The strategy checks the three observed gas-oracle roles beside its other runtime
pins. Its action boundary independently refuses an incomplete policy. The
authority checks before new RPC and again while constructing its simulation.
Simulation v3 adds two reads per provider to the existing twelve: the pinned
proxy implementation slot and code at the **approved** implementation address.
All are bound to the decision block hash with `requireCanonical: true`, followed
by the closing header. Agreement between providers cannot override the approved
slot or either runtime hash.

Both raw fourteen-read envelopes and the derived pin values are retained and
replayed before key use. Rehashed metadata cannot override their wire proof.
Old v1/v2 simulation summaries cannot approve a new signature. Existing receipt,
cash and risk recovery stays available under its original configuration epoch.
Capital, loss, gas and the two 1800-second feed ceilings were not widened.

## Verified development evidence

The five initial regressions failed against the previous implementation. The
focused suite passed 225 tests, including changed proxy/implementation code,
changed or malformed storage, both sources agreeing on unapproved values,
partial policies, forged retained summaries and early CLI refusals. Existing
fixtures for later boundaries were updated to provide explicit synthetic pins;
the original five-pin recovery fixtures remain.

The first full run retained four failures and 4196 passes: its CLI exit-status
fixtures still used the old execution policy. Those four fixtures were corrected
without weakening their exit/HALT assertions. The original failed report remains.
The final full `make live-quality` exited 0: **4200 tests, 138 subtests**, 244
mypy source files and eight frontend modules passed. Python/frontend, secret
scanning, Bandit and Compose each exited 0 without timeout or launch failure.
All log/report hashes and all 17 locked source hashes were checked after the run.
Four existing warnings remain recorded; no contract source changed.

```text
reports/review-20261104/gas-oracle-quality-v4/report.json
evidence_hash: 334229b770814e39a75f5660663f09afa961e833eced0c646b9b91c2f613fe38
reports/review-20261104/gas-oracle-runtime/summary.json
evidence_hash: b8ba027d49b694f670ba954f5bf78a7be35b8918dd9728b5dceadc4bbf65ea11
```

## One authorized real read-only attempt

The owner confirmed that the 197-request campaign was within the current free
quotas, and separately approved creating only the two missing STOP safety
markers after automatic approval review initially rejected that state mutation.
Both markers were created as UID 10001, mode 0600, under the existing service
locks. The ten original state/evidence/budget/lock files were byte-identical
before and after this offline preparation. STOP was never removed.

The exact original-volume plan permitted 99/98 requests across the two sources,
197 total, at most 120 seconds, no retries/signing/broadcasting and no budget
reset/copy/replacement. It ran with the previously verified immutable image
`80c47874655f…`, without key or socket mounts. The newly added gas-oracle reads
were outside that scope and were not included.

The attempt exited **3**, after about 5.43 seconds including Docker overhead.
The worker/supervisor refused after about 4.30 seconds: the secondary source's
first `eth_chainId` request returned **HTTP 403**. An already-in-flight primary
reply was then refused under cancellation. Actual consumption was **four**
requests: strategy primary 3, strategy secondary 1; authority 0/0. The original
budget counts changed from 900/743 to 903/744 for strategy and stayed 974/955 for
authority, with the original budget device/inode identities intact.

Only the primary head was observed: Base 8453, block 53504173,
`0x65d25aaef1f6cc3931aad77d9286c902f7d6a31e163abf0a8db748f35c8125ea`,
timestamp 1793797693, source `live_primary`. The retained request/response bytes
are in this attempt's `wire.jsonl`; this is not a two-source accepted block/NAV.
No accepted NAV, current receipt quorum, `references.json` or `fresh-nav.json`
was published. Risk journals, state projections and original evidence stores
were byte-identical after the attempt. Both production containers remain exited
on their original image, with STOP retained and no key use or transaction.

The declared incremental cost was zero under the owner's current free-quota
statement; provider billing was not independently queried. The earlier eight
paid requests and cumulative USD 0.10 ceiling were not reset or reinterpreted.
This one-shot campaign is terminal. The unused allowance is not retry permission.
HTTP 403 alone does not identify a quota, credential, gateway or client-header
cause; no extra probe was made to determine it.

```text
reports/review-20261104/authorized-nav-1223/terminal-result.json
evidence_hash: 2e45fcd0bb1a8bd7fa38ef362860be85d3fdc0bd887d5d79b61c7c9d1f412edd
```

## Unresolved production acceptance

Current two-source gas-oracle fingerprints still require authorized acquisition
and review. Adding them changes the configuration hash: the existing feed-only
migration does not establish an approved five-to-eight-pin epoch transition.
That history-preserving transition remains required; do not overwrite an old
configuration binding or initialize replacement state to activate it.

The secondary source's HTTP refusal, fresh complete strict USD/NAV, current
runtime/simulation, complete NFT unwind and operational restart acceptance remain
unresolved. The failed capture supplies no release or profitability evidence.

## Offline discovery and candidate review

`live/gas_oracle_evidence.py` supplies the retained-proof producer for the missing
three fingerprints. It accepts two complete raw captures at one explicit,
timestamped Base block. Each source must contain exactly these six reads, with
one attempt per read:

1. `eth_chainId`;
2. the opening header at the explicit block number;
3. proxy `eth_getCode` at the block hash;
4. proxy `eth_getStorageAt` at the EIP-1967 implementation slot and block hash;
5. `eth_getCode` at the address derived from that storage reply, at the same hash;
6. the closing header at the same number.

All three state reads require EIP-1898 `requireCanonical: true`. Opening/closing
block identities and parent hashes must agree across both sources, as must the proxy code, canonical
nonzero implementation word and implementation code fingerprints. Missing,
extra, retried, cross-block or substituted requests refuse before the first
accepted proof record is written. Reopening the store re-derives the exact
summary from each source's wire bytes; rehashed summaries cannot substitute for
raw proof.

The producer has **no network acquisition capability**. Its twelve-read template
excludes head discovery and does not authorize requests or extend the terminated
197-request campaign. A future collector still needs an explicitly authorized,
budgeted scope and valid provider access. Provider independence remains a
declaration to verify operationally; two different aliases alone do not prove it.

The offline command is:

```sh
python -m scripts.review_live_gas_oracle \
  --wire /review/gas-wire.json \
  --config configs/live.base-eth-usdc.json \
  --out /review/gas-discovery-review
```

Its wire JSON has exactly `block` (the complete `BlockRef`) and `providers`
(two objects with exactly `source`, `request`, `response`). Each request/response
is the existing `hex_exact_bytes` envelope containing the six exchanges. The
default declared pair is `live_primary` / `live_secondary`; `--sources` makes a
different pair explicit. Inputs are bounded regular files opened without
following a final symlink, and output requires a new private directory.

Only a successful replay and a block no more than 1800 seconds old produces
`candidate.json`, with original and proposed config hashes and both complete
evidence references. The source must keep both 1800-second feed ceilings and
the original five-pin shape. The proposal adds only the observed three entries;
every other config field stays identical. This discovery-block expiry is not
USD-feed freshness or complete NAV evidence. An exit of 0 means only
`candidate_requires_review`, with `runtime_approved`, `configuration_applied`,
`state_write_supported` and `execution_eligible` all false. Refusals exit 3,
publish no candidate and retain a fixed, redacted report. Existing output is
never overwritten.

No real capture has supplied these twelve exchanges. Production config,
journals, budgets, STOP files and services were not modified by this follow-up.
Source review/approval and a history-preserving five-to-eight-pin configuration
transition remain unresolved.

The follow-up adds 57 synthetic-only tests, covering raw request substitution,
chain/header/parent disagreements, malformed storage/code, retries, forged
summaries, source scope, expiry boundaries, exclusive output, redaction and CLI
exit status. The first full check retained 10 failures / 4247 passes because its
minimal PATH omitted Node.js (eight failures) and Foundry `cast` (two failures).
Only that test environment was corrected; no test or source was changed to
bypass those failures. The corrected `make live-quality` exited 0: **4257
tests, 138 subtests**, 245 mypy sources and eight frontend modules passed, with
all four quality subprocesses exiting 0. The four existing warnings remain.
All log/report hashes and the five locked source/config hashes were verified.

```text
reports/review-20261104/gas-discovery-quality-v1/report.json
failed evidence_hash: 7803d49932fe6d40d48afef8124ed500ff74f1e18c1427b538069e1f79406596
reports/review-20261104/gas-discovery-quality-v2/report.json
passed evidence_hash: 48be7ccc49f6db7521d53c22180a5d0513bab48c1ed3c6ab3622683b78556c31
reports/review-20261104/gas-discovery/manual-review.json
evidence_hash: fdb2bc5cb4452e1f2b493d8bc3a98ec7d2c6df77e8e3be4ce0719f7fa8bf1a96
```

## Committed immutable candidate

Implementation commit `85b39ec4556289bacc238600a677080881366772` built candidate
`sha256:0f7a2f70cb7e2a79bdb8548f92dc11789956134e9898c8b23b5c2013044e5c3e`
from its committed runtime-only archive, with the locked dependency layer cached.
Build and stopped-image source verification exited 0. All **349** application
files matched, manifest hash
`0d234d2f28e2d04874d0763a2d5f475a6bece829147deb2c59ed2e8c9df9175a`.

The unprivileged, network-disabled immutable image passed its Linux smoke with
exit 0: all three execution entrypoints refused the shipped unapproved policy,
and fourteen synthetic wire exchanges reconstructed the v3 simulation. No source,
production state, key or socket was mounted; only temporary tmpfs was writable.
Every exit and log/script/report hash was re-derived before recording:

```text
reports/review-20261104/runtime-85b39ec/summary.json
evidence_hash: d773718005f3fc5dd5d34c13fd4b4783bf8d49e197285abb1f2246a6a3cbddf2
```

This candidate was not deployed. Both original service containers were rechecked
as exited on image `0b4f8cb42deb`. The separate real capture's head response was
retrieved within the bounded host-observed interval from 2026-11-04T13:08:10.354000+00:00 to 2026-11-04T13:08:15.787400+00:00
(scope declaration before launch through recorded process exit). This is an
acquisition interval, not the block's timestamp or a two-source acceptance claim.

## Offline-discovery immutable candidate

Follow-up implementation commit `b4ff7484c953df22b753034b4ccef27367520b40`
built candidate
`sha256:391ae39ea89c5e6d2eb4a49c8d6cbba547d70c50eb11acf8d30b7a2166b29699`
from its committed runtime-only archive. Build, stopped-image source verification
and Linux smoke each exited 0. The dependency installation layer was cached.
All **351** application files matched, manifest hash
`137ce55d3caeb7f759dda34f90b57ca4077e81b01c680543e40a7e969dc00345`.

The immutable Linux image ran as UID 10001, with network disabled, a read-only
root, dropped capabilities and no production state, source, key or socket
mounts. Its synthetic twelve-exchange fixture produced only an unapproved
candidate; substituting `latest` into a hash-pinned request refused and produced
no candidate. The shipped production configuration remained byte-identical.
There were zero production RPC requests, signatures, broadcasts, migrations or
service starts. Both original containers were separately rechecked as exited on
image `0b4f8cb42deb`; the new candidate was not deployed.

```text
reports/review-20261104/runtime-b4ff748/summary.json
evidence_hash: 3bf9281d52aad7ff7305536229d51f1862b6cd633e50675a8ea9fc74ed0e6118
```

This is development and artifact evidence. Current dual-source fingerprints,
runtime approval, policy-epoch migration, strict USD/NAV and funded recovery
acceptance remain required; the secondary RPC's unresolved HTTP 403 and the
terminated one-shot authorization remain unchanged.

## Retained configuration replay follow-up

`live/config_epochs.py` now resolves original configuration hashes for historical
cash and NAV replay. Previously, an eight-pin target could recognize only its
own shape and that shape's old USDC age. Both existing five-pin settlement
history and stopped-service checkpoint planning then refused before a runtime
upgrade could be reviewed. Two synthetic regressions reproduced those failures
before the change.

The recognized replay recipes are exact:

| Current target | Permitted retained shapes |
| --- | --- |
| Five pins | The same five-pin policy; its historical 90000-second USDC variant |
| Eight pins | The same eight-pin policy; its historical USDC variant; the corresponding five-pin shapes with either USDC age |

These are hash-checked replay recipes, not evidence that each shape has been
used on chain. Apart from removing exactly the three new gas-oracle entries and
recognizing the already-supported historical USDC age, every field must remain
identical: account, original five runtime pins, pool, gas, capital, loss and
execution bounds. An old eight-pin policy with a different gas runtime is not a
known epoch. Missing, foreign or noncanonical digests have no fallback. The
90000-second objects remain invalid under current live-policy validation.

`rearm_history.replay_history` still uses each event's **first** journaled epoch
to replay its raw cash proof, even after a later bounded projection drops that
event. Relabelling an old proof with the target hash refuses. The recovery
collector's local checkpoint review likewise replays the original checkpoint
under its own policy; any fresh NAV must carry the target policy's own proof.
Its state, risk, holdings and complete gas checks remain in place. Planning
under another target produces another plan hash and creates no RPC permission.

No journal transition or authority was added. A normal execution journal bound
to the new hash still refuses the old journal, and feed-only migration still
refuses five-to-eight-pin expansion. The paired, history-preserving runtime
policy transition remains required before deployment. Production config,
journals, budgets, STOP files, release approval and both stopped services were
not changed; this follow-up made zero production RPC requests.

The two before-change regressions failed as expected. The related focused suite
passed 81 tests. The full `make live-quality` exited 0: **4286 tests, 138
subtests**, including 29 new cases, 246 mypy sources and eight frontend modules.
Python/frontend, secret scanning, Bandit and Compose each exited 0, and all log,
report and six locked source/config hashes were rechecked. Four existing
warnings remain. Early import-order and missing-type-annotation failures were
corrected, with their original logs retained.

```text
reports/review-20261104/config-epoch-quality-v1/report.json
evidence_hash: fb9e874c76ac986879759a4d4c1c1d43751d75f650c5df664e6190017bf72923
reports/review-20261104/config-epoch-replay/manual-review.json
evidence_hash: 24546c4600daaf7d0f59e3524af8b0d7a5c23f8bddd1c04505dba0bf68390b9c
```
