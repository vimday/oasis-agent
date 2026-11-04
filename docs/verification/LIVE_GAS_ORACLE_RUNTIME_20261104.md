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
