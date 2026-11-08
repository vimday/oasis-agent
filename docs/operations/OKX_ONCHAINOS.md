# OKX OnchainOS boundary

The Base LP runtime supports one optional external price-ratio check before swap or mint.
It is separate from block-pinned NAV, receipts, independent signing policy and release approval.
Exits do not consult it. The price API details below were reviewed on 2026-11-04.

The [Base spot shadow agent](BASE_SPOT_SHADOW.md) separately uses read-only OnchainOS market,
security and quote commands. Its optional Agentic Wallet diagnostic reads identity and balances.
Wallet credentials and CLI output carry no production authority. The autonomous spot entry
refuses live mode. Neither integration issues OKX payment, swap execution or broadcast commands.

The [price API](https://web3.okx.com/onchainos/dev-docs/market/market-price)
uses `POST /api/v6/dex/market/price`, lowercase EVM contract addresses and
millisecond price timestamps. Query authentication covers the exact request body
with HMAC-SHA256, following
[OKX authentication](https://web3.okx.com/onchainos/dev-docs/home/api-access-and-usage).
This authenticates a data request and is not an on-chain signature.

The [current fee schedule](https://web3.okx.com/onchainos/dev-docs/market/market-api-fee)
classifies price requests as Basic: a monthly free allowance precedes paid access.
It documents x402 and subscription options and 402 responses when payment is
needed. Therefore possessing a query key is not evidence of free remaining quota.
Oasis sends no payment header, implements no payment response flow and does not
purchase or renew subscriptions. Historical approval of a different RPC request
budget does not authorize OKX requests.

## Runtime admission

An enabled guard now requires all three query credentials in explicit environment
variables (`OKX_API_KEY`, `OKX_SECRET_KEY`, `OKX_PASSPHRASE`), a retained free-only
grant, and its original pre-provisioned SQLite request ledger. The strategy never
reads a credential file. No credentials, grant or OKX ledger were acquired or
created during this implementation; provider account/billing acceptance remains
unresolved.

`OkxPriceReadGrant` accepts exactly these JSON fields:

| Fields | Required meaning |
| --- | --- |
| `schema` | `oasis.okx-price-free-read-grant.v1` |
| `endpoint` | `https://web3.okx.com/api/v6/dex/market/price` |
| `chain_id`, `currencies` | One EVM chain and exactly two distinct lowercase pool currencies; native currency remains the zero address in the grant |
| `api_key_hash` | SHA-256 of the query API key; never include the key itself |
| `valid_from_ms`, `valid_until_ms` | Integer UTC epoch milliseconds; at most 24 hours apart |
| `max_requests`, `min_interval_ms` | Integer cap 1–4096 and minimum interval 100–60000 ms |
| `billing_mode` | Exactly `free_quota_only`; unknown and paid pricing refuse |
| `free_requests_remaining` | Documented remaining allowance, at least the requested cap |
| `pricing_evidence_hash`, `authorization_evidence_hash` | Non-placeholder SHA-256 digests of retained operator-reviewed pricing and owner authorization records |

These records are explicit operator declarations, not independent verification of
the provider's billing state. Other users of the same API key can consume its
allowance. An unverified allowance must remain unresolved; do not fabricate a
grant to enable the feature.

Only after that scope has been approved, provision a **new** ledger once using
`RpcBudget(path, grant.policies())`, rejecting an already-existing destination.
The full grant digest determines the budget source identity. Changing identity,
scope, price evidence, expiry or quota then fails against that same ledger.
Do not delete, copy, replace or regenerate the ledger to obtain additional quota.
Normal execution always opens it with `require_existing=True`.

Add these options to an otherwise authorized and passing live/shadow invocation:

```text
--external-price-guard-bps 50
--okx-price-grant /operator/retained-okx-grant.json
--okx-price-budget /operator/original-okx-budget.db
```

This does not authorize production startup or funds movement. Missing, expired,
foreign, unknown-priced or exhausted admission refuses before RPC resolution,
state creation or signer construction. Guard options are rejected for reporting,
precheck and recovery commands rather than silently ignored.

## Response and failure handling

One admitted POST requests both currencies. Both rows must match the exact
chain/token set and remain fresh at the same response-receive time; default age
is 300 seconds and the maximum is 1800. Response size, decimal representation,
timestamps, duplicate/missing/foreign rows and clock regression are bounded or
refused. A very large decimal exponent cannot trigger an unbounded integer
conversion. Authentication redirects are refused.

Each request reserves quota before network I/O, with no refund for failure and
no waiting or retry. A failed source makes no further calls in that process;
an operator restart still sees the consumed original quota and grant expiry.
Grant validity is rechecked after admission and after transport. No payment
negotiation, on-chain signature or broadcasting path exists in this client.

Tests use synthetic credentials, raw responses and temporary ledgers. They do
not establish current OKX service availability, free entitlement, live price
agreement, complete NAV or permission to trade. Original development logs are
under `reports/review-20261104/okx-alignment/`.
