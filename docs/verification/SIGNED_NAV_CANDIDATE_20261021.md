# Signed-price NAV candidate — 2026-10-21

Continues [offline signed-price verification](SIGNED_PRICE_CANDIDATE_20261021.md).
This increment connects authenticated ETH/USDC prices to retained two-provider
wallet and declared LP inventory. It does not adopt the oracle in production or
open the live release gate. No real RPC, oracle gateway, signer or key-volume
access was used. Cumulative paid acceptance usage remains eight requests; its
USD 0.10 budget and ledger have not been changed.

## Confirmed implementation

`live/signed_nav.py` retains two distinct candidate evidence kinds: a price binding
and each provider's NAV proof. The binding contains the exact two wallet proof
references, full block identity, configuration hash, native ETH/Base USDC mapping,
USD quote unit, authenticated witnesses and raw signed packages. Replay derives
the interpretation again from the original packages and wallet envelopes. A
rehash of a false interpreted price cannot make it valid.

The signed timestamp must not postdate the account block. Its age at the explicit
observation clock must be at most the configured limit, never above 1,800 seconds.
The wallet block must be no more than 120 seconds behind that clock and cannot
be in its future. Replay records that the clock is an explicit input; replay at a
historical clock does not establish current freshness. Signatures, price age and
block-clock compatibility are checked before invoking the RPC session factory.

Both wallet proofs must describe the configured account/assets and agree on the
block, balances, nonce and runtime state. Each NAV proof must use one of those
exact wallet references at that exact block. Pool state, declared NFT ownership,
manager/core liquidity and fee-growth inputs retain the existing hash-pinned
capture and replay checks. Both providers' raw core results must agree even when
rounding would hide their difference in final fees or USD values.

The valuation covers native ETH and USDC in the wallet plus principal and
uncollected fees of the explicitly declared LP position. Each of the four asset
legs is floored independently to USD micros, using the authenticated eight-decimal
median prices. This is a scoped inventory valuation, not discovery of all assets,
a transaction history reconciliation, proof of liquidation value or a PnL result.

Validated wallet/price records can remain if later position/core acquisition
fails; no NAV candidate is emitted on that failure. These records cannot authorize
execution. The original `live_nav` replay still demands Chainlink feed envelopes;
it rejects both the candidate kind and a candidate relabelled with its production
kind/schema. Stale Chainlink continues to refuse with a valid candidate beside it.
The strategy, signer and production `capture_nav` have no candidate fallback.

## Evidence scope and validation

Tests use the ten real historical RedStone signatures already pinned in the
previous increment, at signed timestamp `1719856900000`, with **synthetic** RPC
wallet, pool and LP responses. No test result is a real account valuation or a
claim about the chain at that historical timestamp. No production signer trust
is mocked in this test suite.

Coverage includes wallet-only and LP valuation, fee/principal integer accounting,
1800/1801-second price ages, the 120-second observation boundary, future quotes,
tampered or missing feeds before acquisition, provider disagreement, foreign
accounts, cross-wallet and cross-block price references, altered price payloads,
tighter replay policy, candidate relabelling and the unchanged Chainlink refusal.

The shared release status consumed by the CLI and dashboard now reports this
candidate connection while keeping all four production blockers closed.

Validation: the 47 focused signed-NAV and existing Chainlink valuation tests
passed. `make live-quality QUALITY_OUTPUT=reports/review-20261021/signed-nav-candidate`
then completed all four commands with exit code 0, no timeout or launch failure:

- Python/frontend: Ruff, formatting, mypy, 3,202 tests and 138 subtests passed;
  eight frontend modules passed syntax checking. One dependency deprecation
  warning remains.
- Secret scan: no unreviewed candidates, no credential verification requests.
- Bandit: no medium/high severity findings; low severity findings and scanner
  comment warnings remain in the retained log.
- Compose: seven services passed absent-RPC and synthetic-RPC configuration cases;
  no container or network call was used by this check.

Raw logs and the per-command result report are retained in that output directory.
Report evidence hash:
`03cbe0e111a791f02a2c5936c686ad04fc8fafeb184bfe26dd26752beae61797`.
The report explicitly retains `execution_eligible: false`.

## Unresolved

Fresh authenticated packages and a usable provisioned production source are still
missing. The candidate must undergo production source-policy review, bounded
acquisition, trusted live-clock admission, signer-side replay and sustained
freshness/failure acceptance before adoption. Two RPC providers and multiple
oracle signers establish different trust boundaries; neither implies independent
economic price methodologies.

Full-account/history reconciliation, fresh production simulation/runtime evidence
and real-canary dual-service receipt/cash recovery remain unaccepted. No new funds,
transfer or transaction authority was added. Capital/loss limits remain USD
500/200 daily/500 cumulative. The existing Docker image was not rebuilt for this
candidate library; there is no current-image production deployment claim.
