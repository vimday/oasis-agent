# Base fast USD prices and the live critical path

## Diagnosis

The retained Base capture at block 53983490 refused USDC/USD age 17269
seconds against the owner's 1800-second ceiling. Chainlink's official Base
directory declares an 86400-second heartbeat for the configured proxy
0x7e860098f58bbfc8648a4311b374b1d669a2bc6b, with a 0.3% deviation trigger.
A healthy feed can therefore fail our age policy between updates. A second
RPC reads the same round and does not solve that mismatch.
The fetched directory had SHA-256
9e12e1c3c5285471ee09a9ae831b901e28c813351dda88f1279a32e0401d76d3.
[Official directory](https://reference-data-directory.vercel.app/feeds-ethereum-mainnet-base-1.json)
[Directory selected by Chainlink documentation](https://github.com/smartcontractkit/documentation/blob/main/src/features/data/chains.ts)

The owner explicitly selected retaining 30 minutes and a faster price source.
The RedStone candidate was not a production NAV adapter and lacked a gateway
credential. Repeated startup hardening did not fix this critical path.
The capital and loss caps, current complete-account valuation, pinned chain
state, independent signing admission and uncertainty latches remain necessary.
Waiting for a slow supplier to satisfy a faster policy is an integration
error, not an additional assurance. Solana, broader OKX features and later
autonomous strategies are outside the Base canary acceptance path.

## New explicit price policy

The new usd_price_source value "kraken-bitstamp-usd-v1" opts a configuration
into Kraken and Bitstamp's public ETH/USD and USDC/USD trade histories. The old
configuration shape and hashes are unchanged; no fallback selects this policy
when Chainlink refuses. A policy change requires a separately reviewed,
configuration-bound runtime epoch.

Each observation uses at most four credential-free HTTPS GETs in 15 seconds,
with no redirect, proxy environment or retry. The verifier selects the latest
trade not later than the account's pinned block. It checks each exchange's
actual trade time against each feed's own 1800-second-or-stricter limit, requires
both suppliers, caps disagreement at the configured deviation (at most 40 bps),
and applies the existing 2% USDC band to each supplier. USD prices and valuation
use exact integers. An HTTP receive time cannot refresh an old trade.

These are explicitly trusted exchange HTTPS observations, not signed oracle
reports or proof of decentralized price consensus. No account or registration
was needed. Their canonical request URLs, exact response bodies, observed
clocks, selected trades and interpretation are retained. Source outages,
regional access restrictions, gaps in trade history and a divergence between
these venues and the pool can refuse deployment.

[Kraken recent trades](https://docs.kraken.com/api-reference/market-data/get-recent-trades)
[Bitstamp public transactions](https://www.bitstamp.net/api/)

## Production integration and bounded collection

The production NAV consumer, freshness calculation and acquisition now accept
this separate evidence kind only under the explicit policy. Wallet balances,
NFT inventory, LP principal and claimable fees still use two block-pinned RPC
providers. The signing authority obtains its own price observation rather than
accepting a strategy-supplied quote. Historical Chainlink evidence is replayed
under its original configuration; this new source cannot inherit the
historical 90000-second exception.

The joint recovery collector reuses one four-response price bundle for both
original services, with fresh NAV, gas fingerprints and five receipt histories
at one chain head. Its exact original-volume plan has 177 RPC requests
(strategy 48/47; authority 41/41), plus four public price GETs, and a 120-second
overall deadline. This replaces the 209-request pricing path. The plan is a
proposal, not approval to migrate, rearm, remove STOP or start trading.
The read-only keyless Docker plan completed with exit 0. Original request
counts were strategy 1007/845 and authority 974/955; no budget was copied,
replaced or reset.

## Actual source observation

The public USD collector completed four real public GETs with exit 0.
At host as-of 1794020660587 ms it derived ETH/USD 2245.16500000 and
USDC/USD 0.99981000. The selected exchange trades were all within 30 minutes.
This is a host-clock price observation, not a chain block, accepted NAV,
current trading authority or evidence of returns.

Raw bundle SHA-256:
69826aa369364d1e752f6183215aff0bf2a336c58cb743f533e9c1520eb2ad26.
Report evidence hash:
e3d3b068ac6a0b6b2162dc287fb11ffc1580db358e33a67bf3f4114da1e45d77.
Both are retained at
reports/review-20261107/public-usd-critical-path/live-prices-001/.

## Checks and limits

Focused recovery, authority, joint-capture and runtime-epoch checks passed
152 cases before the import cleanup. An initial integration failure was a
missing synthetic feed fixture, and a transport-test failure used an already
consumed mock response; their original logs are retained. The first full gate
failed the repository's nested-import rule and observed two source changes
during its run; it is not a passed gate. The dependency cycle was removed by
keeping production NAV dispatch in the valuation module and sharing only the
fixed refusal type with the price transport. Its subsequent code-standard and
new policy suite passed 23 cases.

No production configuration, STOP, HALT, basis, historical gas or trading state
was changed by implementation, source observation or read-only planning.
The next required evidence is a new bounded joint capture and a current
review of the exact candidate configuration before any application.

## First real complete-account capture and owner approval

The new policy completed an actual joint capture with exit 0 at Base block
54158088, hash
0x7b99ec0981eae3bd7129821d8fe71e40a9a7a20516549ee75dbc588cb4e8e67f.
Both original services replayed complete NAV of 501222084 USD micros and five
current receipt histories. The capture consumed 177 RPC reservations and four
public price GETs. Supervisor elapsed time was 118.229943 seconds; host setup
and export took 127.0406 seconds. The review had only four seconds of remaining
validity. That expired review was never applied and cannot authorize a later
start. Evidence is retained under
reports/review-20261107/public-usd-critical-path/joint-paid-001/;
terminal hash 3e30a3300b9f773c6bd33449f4a6bc0b2882754e427d6d4a8f5b3bf392f6bf06.
The conservative cumulative paid-request allowance after this run is 9381 USD
micros, including the original eight-request allowance; this is not an invoice.

The owner explicitly approved configuration
1f67a409e3b5b36de8cb97213b199238e6e28e6c0a74c862685036de1267fae3
and one bounded live start after new fresh evidence and all gates pass. The
production declaration now contains exactly that configuration. This approval
covers migration and recovery of the original services, with no new funds or
transfers, at most one strategy tick and the same cumulative USD 0.10 RPC cap.
It does not establish profitability or authorize an unattended campaign.

## Removing the remaining acquisition bottleneck

The explicit public-source collector now captures the same account once and
copies only its bounded new evidence graph into the other original evidence
store. Both stores independently replay the exact original RPC envelopes and
reconcile their own historical checkpoint; no budget or database is copied.
A focused test verifies the authority budget remains byte-for-byte unchanged.
The new original-volume plan passed offline with exit 0 and requires 95 RPC
requests (strategy 48/47, authority 0/0) plus four public GETs. Its hash is
9024c6c1a2b7e50751b5f7a6df78865206185d504ff81e7b84e10ddedde53fe1.
This leaves time for the existing current-time migration and paired recovery
checks instead of weakening freshness.

The one-shot launcher adds one strategy tick, a 180-second authority TTL and
foreground Compose shutdown when either service exits. It retains an uncertain
startup guard after timeout and never retries. The loop now exits immediately
after its final tick instead of sleeping for another polling interval. Legacy
retained-proof tests explicitly retain their original Chainlink configuration;
changing the production declaration must not silently switch synthetic tests
to an unmocked public transport. Initial local test failures and their corrected
reruns remain in public-usd-critical-path/once-focused*.log.

## Owner-approved one-shot startup outcome

Commit bf3b340 was pushed after the frozen development gate passed 4794 tests,
138 subtests, frontend checks, type checks, secret scan, Bandit and Compose.
One new authorized capture then completed with worker and supervisor exit 0
in 82.244330 seconds, using exactly 95 original-ledger RPC reservations and
four price GETs. At Base block 54167102, hash
0xcaf9dc5e0bdec53623720d3832e8ddfd068f1e2a593c48791f22060265fd5623,
its complete account NAV was 500754569 USD micros. The exact approved gas
pins were observed. Both original journals migrated and rearmed atomically;
each independently replayed five settled receipts. The guarded Compose start
used the verified image and the original volumes, including the original
canary key volume mounted only in authority.

**Runtime acceptance is still incomplete.** The strategy attempted one live
tick and logged `skip: budgeted live RPC read refused; details redacted`. It
never reached a fresh runtime observation or an action decision. The 8/7 new
strategy reservations and zero authority reservations are consistent with a
refusal during the runtime settlement recheck, plausibly the primary read of
the fourth historical block. The image did not retain a safe reason for the
individual request, so the HTTP, network or protocol cause is unresolved.
The strategy process and Compose reported exit 0 despite the skipped tick;
the enclosing acceptance operation correctly returned 3. The authority exited
137 on shutdown and was not OOM-killed. Both containers are stopped, both STOP
markers are restored, and there are no new signatures, broadcasts, unresolved
signing attempts or pending transactions. The original budget identities and
five settled receipts are preserved. The cumulative conservative RPC cost
allowance is 10881 USD micros, below the 100000-micro cap; no provider invoice
was verified. This one-start authorization is consumed and was not retried.

Raw evidence and the outcome are retained under
reports/review-20261107/public-usd-critical-path/joint-approved-once-001/.
Terminal evidence hash:
03f7503fe8c31edfc5168a7291066eae0055e2de29d30d5795ce76d4f0523c1f.
Full log readback hash:
a31a73ae3c67b19b93e04c8a9fd3886b9b0b2069de25fdfeacfe760ab04839c7.

The follow-up code treats a skipped bounded live tick as an unsuccessful run
while preserving its state and report. Direct budgeted runtime refusals now
expose only the approved provider alias, an allowlisted method and a safe
category or HTTP status. No endpoint, error body, automatic retry, extra
request or weaker settlement gate is introduced by this fix.


## Authorized settlement diagnosis

The operator approved one separate read-only diagnosis. The retained plan
`c8575ab1e3df0dd0f17227c3bc6b4bf6e4dc73ef149b196600ff78816a8a2b8f`
finished with exit 0 in 21.298 seconds. Both providers reported Base chain ID
8453 and finalized heights above all five original settlement blocks. Both
agreed on all five block and receipt interpretation hashes. The previous
runtime refusal did not recur. Its original cause remains unresolved.

The strategy budget counts advanced from 1111/946 to 1123/958, twelve requests
per provider. The authority budget was unchanged and both STOP markers were
preserved. The campaign used no retries, service starts, signatures or
broadcasts. The cumulative conservative RPC allowance is 11205 USD micros.
The evidence is in
`reports/review-20261107/public-usd-critical-path/joint-approved-once-001/diagnosis-authorized-001/`
and its evidence hash is
`46b1e57fd6a41aef8a3ac898e1abb458cdac8fcae538886e65cbc2fd96169ad3`.

The normal operational precheck now preserves the same sanitized diagnostic
from budgeted reads. A refusal still blocks the observation. The successful
diagnosis verifies historical settlement reads at its own observation time.
A subsequent bounded production start requires fresh prices and NAV, a current
policy check and separate owner authorization.
