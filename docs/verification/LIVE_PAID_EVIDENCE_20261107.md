# Paid read-only Base evidence — 2026-11-07

The owner authorized paid RPC for NAV, simulation and receipt evidence. Three
separate single-attempt, 120-second scopes used the existing Alchemy and dRPC
endpoints and original request ledgers. No retry, signature, broadcast, service
restart, configuration apply, budget replacement or change to STOP/HALT occurred.
Evidence execution used a keyless container and a source manifest recorded
against `73ee12b` plus the three changed acquisition modules.

| Scope | Base head | Requests | Worker / supervisor / host | Outcome |
| --- | ---: | ---: | --- | --- |
| Same-block Gas and NAV | 53983490 | 37 | 3 / 3 / 3 | Gas fingerprints agree; USDC freshness refused |
| Existing settlement receipts | 53984727 | 27 | 0 / 0 / 0 | Five current receipts match both original histories |
| Unsigned burn rehearsal | 53985629 | 55 | 0 / 0 / 0 | Full-envelope simulation agrees and replays |

## NAV and Gas

At Base block 53983490 (`0x3e9472d0119e13060ad14add90be4a04a53d12c2e5759be5b2971d02a97df236`,
timestamp 1793987073), both providers returned ETH/USD age **404 seconds** and
USDC/USD age **17269 seconds**. USDC retained the same round timestamp
1793969804 as the previous refused capture. The production 1800-second ceiling
refused before wallet or position acquisition. All 37 wire responses matched
their reserved requests; offline replay reproduced the refusal. The three Gas
oracle fingerprints were re-observed and replayed. They produce the same
candidate configuration hash `357cb773b1f05ea42f531be0e95fedd0d91a76456e184b4beb6ac2c429941202`.
No complete NAV was accepted and no Gas transition was applied.

## Receipts and simulation

At Base block 53984727 (`0x806d387b8df662c3604e61ceb3492c09cd3491fa7e19301147f21437393eae33`,
timestamp 1793987321), both RPCs returned the same five receipts from the
original canonical settlement anchors. Each matched the raw receipts reproduced
from **both** service journals' cash and Gas proofs. The read-only history and
wire replay preserved original receipt digest
`ca219b0937d5a4334d646fee01bb5c28cf341bc0e5fc9753d3b3c6dff19394ab`.
The receipt observation was independent of NAV and is not a same-head rearm
artifact. No new settlement was written to either journal.

At Base block 53985629 (`0x2206be672cc3bc21aed27d583e2a86f8bd1688d86dc59c435a121416e1a3fa6a`,
timestamp 1793987501), two RPCs verified the existing NFT and core liquidity,
reproduced the pinned quote and nonce, and simulated the same unsigned bounded
burn envelope. The position was in range; this was an exit rehearsal, not an
autonomous recenter signal. Both returned `0x`, estimated **165988 gas**, and
agreed on the rechecked Gas runtime, L1 upper bound **11398005374 wei** and
operator fee **0 wei**. Offline replay regenerated calldata and fee terms and
verified both raw provider proofs. No burn, swap, mint, withdrawal or capital
transfer occurred. This does not establish a full recenter, actual unwind,
fresh risk admission or restart acceptance.

## Cost and provenance

119 new requests were reserved (61 Alchemy, 58 dRPC), all on the original
strategy ledger. Final strategy counts are **1007/845** and authority counts
remain **974/955**. The public-rate allowance is **USD 0.001629** for these
requests and **USD 0.006984** including the recorded historical allowance and
conservative repricing of the prior 85 requests. Earlier free scopes remain
owner declarations. This is not invoice verification. The cumulative
**USD 0.10** cap remains unchanged. The conservative bound uses 40 CU per
allowlisted read at USD 0.525/M CU for Alchemy and 20 CU at USD 0.30/M CU for
dRPC. Published rates and units are in
[Alchemy pricing](https://www.alchemy.com/docs/reference/pricing-plans),
[Alchemy method costs](https://www.alchemy.com/docs/reference/compute-unit-costs)
and [dRPC method costs](https://drpc.org/docs/pricing/compute-units).

Terminal and offline replay hashes, respectively:

| Local directory under `reports/review-20261107/` | Terminal | Offline replay |
| --- | --- | --- |
| `paid-joint-001/` | `358fd3e6c34ce942996376068d426614eea47010a412d892a27697c265e05e20` | `ab401a9092d732923b1a07593b56dde5b45db0921e4697fe5a0232bd63c2b26f` |
| `paid-receipts-001/` | `8c5905cc11b785ac56db7f20ddb5944b70012c9dc50f68cb70833a6a6cda75af` | `d6924a16a7700315c45d26b30ac3d00ba83e8d78a41c41228f368a5197296720` |
| `paid-simulation-001/` | `e30faec005ac15101fcf0ffc6014f3b2153a9d77c1d7048940356eb06a0d41e7` | `15d4e1b74c81e91e5ec44d474a73046e1b406850b45b18ef1526cc9b5579bffd` |

The receipt and simulation evidence workers and their exact wire outputs remain
in those local directories. These read-only review helpers are not service
entry points. The first local NAV wrapper failed before creating an attempt or
making a request due to a report field assumption; the first offline history
helper failed on an uninitialized journal handle. Corrected runs and their
results are separate from those failures. None of the three wire scopes retried.

## Development checks

The joint collector now binds the acquisition order to the plan hash. Both
providers must pass the original feed identity, raw agreement, freshness and
depeg rules before inventory calls. Request counts on success and historical
NAV evidence formats are unchanged. The paid variant binds the plan and
endpoint fingerprints, restricts priced wire methods, and checks the cumulative
allowance before opening the transport. The existing zero-cost path remains
available.

An initial focused run exposed that a fresh-session API rejected a populated
recorder. The corrected implementation validates freshness once and pauses the
same two sessions at the price barrier, preserving unique RPC IDs for replay.
Focused acquisition and valuation tests then passed, including five paid-scope
cases. Full `make live-quality` exited 0: **4715 tests**, **138 subtests**,
11 warnings, eight frontend modules, no medium/high Bandit findings, and zero
Compose network calls. All 680 frozen inputs were unchanged. Quality report
self-hash: `d49d31907b7f700a7800b6f1b2ca02219f5e77626bce8ef3efbb5e078b3ca65c`; outer exit record:
`07ac9e5c019033ce279df511188dbb5268be086f364d8582ae467eff478480a1`.

The remaining release blockers include a fresh full NAV under the 1800-second
policy, applied and approved runtime transition, same-head recovery
readiness, and demonstrated unwind and restart. Existing USD 500 capital,
USD 200 daily loss and USD 500 cumulative loss limits were retained.
