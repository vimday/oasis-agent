# Live authority simulation replay — 2026-10-18

## Change and invariant

The authority previously retained raw simulation responses but checked only that
the stored summary matched the bytes just written. Its durable signing reservation
kept two content hashes. Equal provider results produce equal content hashes, so
that reservation alone did not identify both independent sources and raw envelopes.

`live/authority_simulation.py` now uses one validator for acquisition and offline
replay. The v2 payload explicitly binds the configuration, timestamped block,
transaction, nonce, gas limit and maximum gas price. Replay reconstructs the exact
request sequence and validates the original wire responses, chain, opening and
closing headers, all nonce observations, call return, gas estimate, observed Base
fee oracle code, L1 upper bound and operator fee. It compares the derived payload
byte for byte and refuses an interpretation claiming execution eligibility.

Both complete `EvidenceRef` values are now retained in the signing reservation
before key use, including source, request/response hashes and retrieval time.
Identical result hashes do not collapse source identity. The existing hash-linked
journal preserves the reservation snapshot after successful signing as well as
across unknown outcomes. A quorum must name the caller's declared source pair.

Acquisition reopens and replays its retained records before returning to the
authority. If retention or replay fails, the key provider is never called.
Replay performs no RPC requests and grants no signing/broadcast capability. It
does not establish current freshness or provide an execution permit. The v1
summary remains historical evidence but is not accepted as a v2 admission proof;
old journals are neither deleted nor reset.

## Validation scope

Final `make check` passes: 2990 Python tests, 138 subtests, 70 Solidity tests,
eight frontend modules, lint, formatting and mypy. Bandit reports no medium/high
findings and the secret gate passes. An initial test expected the key-provider's
internal error text; it was corrected to expect the actual redacted authority
error, and the full suite was rerun after that correction.

Adversarial tests reopen the evidence database, replay without sessions or keys,
reject foreign scope and repeated sources, and reject rehashed forged summaries,
call results, excessive fees, closing nonces, changed block selectors, truncated
captures, legacy schemas and eligibility claims. Two individually valid but
disagreeing provider fee observations also fail quorum. Tests verify that failed
replay never reaches the key and an unknown key-provider result retains both full
references in durable state.

The Linux container tests use a read-only root, no network and ephemeral test
keys. All 44 selected tests pass, covering the replay checks, full-transaction
socket boundary and a real process SIGKILL/restart. These are synthetic mechanism
proofs, not production provider or economic acceptance. The existing four release
blockers remain.

Validation image:
`sha256:1bb081b98cd01e1d012e80687b3894c614cf79f75da25dc42497fcf2512a310a`.
Runtime image:
`sha256:5fbe308a28588892f3326e6670c6cfbc285e387213a895cfcbe225c62fd1d771`.
The runtime's default offline readiness command still exits 3 with all four
blockers and `new_actions_allowed: false`.

## Price-source research, without paid requests

The current [Pyth API documentation](https://docs.pyth.network/price-feeds/core/api-reference)
states that Hermes requires an API key after the August 26, 2026 upgrade.
Its [EVM update documentation](https://api-reference.pyth.network/price-feeds/evm/updatePriceFeeds)
requires a fee obtained with `getUpdateFee`. Therefore it cannot be assumed to
be a free drop-in replacement for the stale USDC/USD feed. No API key was
requested, no plan purchased, and no update transaction sent. A new source would
also need identity/signature or on-chain verification, freshness and conservative
valuation acceptance. The frozen 1800-second limit remains unchanged.

This follow-up performs no live RPC probing, production signing or broadcasting.

An offline hostname-family check identified `BASE_RPC_PRIMARY_ALT` as Alchemy,
without exposing the URL or credential. The account's plan remains unknown.
[Alchemy's published plans](https://www.alchemy.com/docs/reference/pricing-plans)
exclude Debug/Trace APIs from Free Tier. Earlier successful trace diagnostics
therefore must not be treated as evidence of free service. No request was sent
to that endpoint during this follow-up; a verified tariff and spending ceiling
are still needed before sustained use.
