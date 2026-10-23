# Free RPC and feed freshness recheck — 2026-10-23

The owner clarified that paid RPC and unavailable gateway credentials should not
be prerequisites to verification. Free endpoints are valid evidence sources when
their capabilities, agreement and reliability meet the same gates. Payment is
not a release condition. No paid endpoint, credential file, production key volume,
signer or broadcaster was accessed in this increment.

## Current two-provider feed diagnostic

One bounded capture used the existing fixed free endpoints:
`https://mainnet.base.org` and `https://base-rpc.publicnode.com`. Each made seven
requests without retry: chain ID, opening header, decimals and latest round for
each configured feed, then the closing header. Feed calls used EIP-1898 block hash
with `requireCanonical: true`. Both sources agreed on all decoded feed values and
the opening/closing block identity:

- Base chain 8453, block **52983689** at **2026-10-23 19:58:45 UTC+8**.
- Block hash: `0xbe3fe53c6563adcfd7cb04eb0c0bfd3e0a2b86cf7f0e9fa81b370c17b22e82a0`.
- ETH/USD address: `0x71041dddad3595f9ced3dccfbe3d1f4b0a16bb70`.
- USDC/USD address: `0x7e860098f58bbfc8648a4311b374b1d669a2bc6b`.
- Both reported eight decimals; positive answers, round IDs, answered-round
  ordering and update timestamps were validated.

| Feed | Answer, 10^-8 USD | Updated at, Unix seconds | Age at block |
| --- | ---: | ---: | ---: |
| ETH/USD | 240058000000 | 1792755787 | 938 seconds |
| USDC/USD | 99983572 | 1792673539 | 83186 seconds |

The head was recent at completion. USDC's approximately 23.1-hour age fails the
unchanged 1800-second maximum. Changing the RPC cannot refresh the same on-chain
round. Neither a dollar peg assumption nor an old accepted price was substituted.

This diagnostic reads only the two declared feed addresses. It does **not** check
their runtime hashes/descriptions, wallet holdings, LP fees, complete NAV, capital
history or current signing eligibility. It therefore reports `nav_verified: false`
and `execution_eligible: false`, even though its raw two-source values agree.

Raw report: `reports/evidence/free-feed-freshness-20261023.json`.
Evidence hash: `4187fb61237a1b4624e0936e8ba07d9e54b6caf536e8a59f8cd627ae0b36839b`.
The one-shot diagnostic source is retained locally as
`reports/review-20261023/free-price-check.py`; rerunning it refuses an existing
report. Offline review rehashed the report, decoded all 14 retained exchanges,
compared opening/closing headers and re-derived both feeds' decimals and rounds.
This review exited 0. It is raw diagnostic replay, not a production NAV acceptance.

## PublicNode receipt/trace diagnostics

The existing free discovery ledger had zero PublicNode requests. Two bounded
attempts used it: the historical LP transaction already under review, then one
recent transaction taken from the newly retained header. Each confirmed chain ID
but failed to obtain a receipt. Only the chain-ID response was retained for each;
neither reached a header lookup or `debug_traceTransaction`.

- Historical transaction:
  `0x22a7c0e715455f2ad99f57bcf7789edac2faf8c3506d1b911e8319bb7205c7f2`.
  Report: `reports/evidence/free-publicnode-trace-20261023.json`.
  Hash: `73e0592530da7e2b47660667f7f47816a1f9450a36303a3db85ed3aad360bc69`.
- Recent transaction:
  `0xca2ace27a8ce9ab7ec5a5b85dd4d4a9fd53ea1e54c48878b4423934d823f549b`.
  Report: `reports/evidence/free-publicnode-recent-trace-20261023.json`.
  Hash: `cbbedae594658546994d69961fce3a671e8815a951f4f193247860ecd25b7931`.

The diagnostic commands exited 0 because they successfully wrote refusal reports;
their receipt/trace checks are **false**, not passed. Hashes were verified offline.
No retained provider error establishes a cause, so this does not prove that
PublicNode lacks archive or trace support, or that all free providers are unsuitable.
There was no retry or automatic polling.

## Budget and remaining work

The original ledgers and policies were reused without reset, replacement or a
larger cap. Exactly **18 new free requests** were charged:

- Existing NAV ledger: `base_public` 24 -> 31 of 32; `publicnode` 23 -> 30 of 32.
- Existing capability ledger: `publicnode` 0 -> 4 of 8; `1rpc` remains 7 of 8.
- Paid acceptance remains **eight previous requests**, **zero new paid requests**.
  Its cumulative USD 0.10 ceiling remains unchanged.

The next source work is a free path that supplies sufficiently fresh authenticated
ETH/USDC prices and two usable receipt/trace sources. A free-tier or public source
must still pass the same raw replay, timestamp and identity checks. The previously
implemented RedStone candidate is one option, not a requirement to buy access.
Fresh-price integration, complete dedicated-account NAV, production simulation
and real dual-service receipt/cash recovery remain unresolved. No live release
gate, capital limit, feed-age policy or production implementation changed.
