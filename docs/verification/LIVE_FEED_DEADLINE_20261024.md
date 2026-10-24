# Current-clock feed deadline and generated-file cleanup — 2026-10-24

## Bug and correction

A Chainlink round could be 1800 seconds old at the observed block and still be
accepted after collection or simulation advanced the host clock. The prior
consumer checks bounded the block age to 120 seconds but did not account for
each feed's remaining lifetime. A recent block does not refresh an older round.

`nav_fresh_until()` replays the declared two-source NAV quorum, then derives the
minimum of the head's 120-second deadline and ETH/USD and USDC/USD `updatedAt`
plus the configured feed-age limit. It takes these timestamps from canonical raw
round responses, not a caller's valuation or a fresh retrieval timestamp.
The observation path and free NAV diagnostic compare the host clock after raw
replay. The independent transaction authority computes its own deadline and
checks again immediately before its only key-provider call, after simulation
and durable nonce reservation. The boundary remains inclusive in whole seconds:
age 1800 passes, 1801 refuses. A refusal after reservation preserves the existing
uncertain reservation; it does not erase history or automatically retry signing.

Historical checkpoint replay stays independent of current freshness: it must
continue to verify the original accounting baseline after restart. Current
admission still needs fresh evidence. This change does not introduce an alternate
price source or admit signed-price candidates into production.

## Regression evidence

The new synthetic tests independently expire ETH and USDC during capture and
between durable reservation and key use. Against the old code, four cases failed
because no refusal occurred; the two exact-boundary cases passed. Original log:
`reports/review-20261024/feed-current-clock-before.log` (exit 1).
The first broader sandbox run passed 73 cases and failed one private-socket test
because the sandbox prohibited `bind`, not because of a policy assertion. That
unmodified failure is retained in `feed-current-clock-sandbox.log`.
The free diagnostic also tests a successful raw capture whose price expires
before the final verdict, retaining its spent request counts while refusing.
All test prices, RPC responses and signing keys are synthetic.

## Cleanup boundary

Removed 945 generated cache/Finder-metadata files totaling 38,616,132 bytes:
root pytest/mypy/Ruff caches and Python bytecode/Finder metadata under source,
scripts, tests and scratch. Each candidate was checked against the tracked-file
inventory; symlinks and tracked files were refused. Local deletion inventory:
`reports/review-20261024/generated-cache-cleanup.json`.
Tests may regenerate caches. No original evidence, budget ledger, key, runtime
state, source script or installation metadata was deleted. In particular, older
raw evidence is still necessary for replay and is not disposable by age alone.

## Production boundary

No real RPC, signature, broadcast, funding or production service startup occurred.
USD 500 capital, USD 200 daily loss, USD 500 cumulative loss and 1800-second feed
limits are unchanged. Fresh USD/NAV, current production two-provider simulation
and runtime evidence, and dedicated-account dual-service receipt/cash acceptance
remain unresolved. Local regression and image checks cannot replace them.

## Completed development and runtime checks

`make live-quality QUALITY_OUTPUT=reports/review-20261024/feed-current-clock`
completed with all four command exit codes 0, no timeout or launch failure.
**3473 tests and 138 subtests** passed, plus compile, Ruff/format, mypy, eight
frontend modules, secret scanning, Bandit and offline Compose policy validation.
The existing websockets deprecation warning remains; Bandit reports 88 low and
zero medium/high findings. All raw log hashes and the quality report hash were
recalculated after completion. Quality evidence hash:
`bb9ccfadcdbf4a9d0553a2df029e78a6a07df3d2d18770a999d540af543dc709`.

The runtime build reused its cached dependency installation layer and succeeded.
Candidate image
`sha256:d6a825fe3d2593d410fa6ad0dbe7049bd2ae701f4dffc44f48fdd24dc56ff9e0`
passed the stopped-container comparison of 328 tracked application files.
Report: `reports/review-20261024/feed-clock-image-sources.json`; evidence hash:
`b0a6118bd34dbd7836859f23a03a9255b0ccfab71d6317267a1ab9faa11977b1`.
Manifest hash: `9822c76216ce9235986b666598df36b62c15128e179ebe906b2bf77941bc00c4`.
The build log is retained as `reports/review-20261024/feed-clock-build.log`.

`oasis-live:local` and `oasis-live:feed-clock-20261024` identify this candidate.
The prior verified runtime remains under `oasis-live:source-check-20261024`.
Changing the local image tag did not restart or deploy production services.
Application identity is checked; dependency bytes and the Docker daemon remain
trusted. No new confinement or funded production acceptance is claimed here.
