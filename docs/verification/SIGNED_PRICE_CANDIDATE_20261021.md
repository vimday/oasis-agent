# Signed price candidate verification — 2026-10-21

Continues [historical commitment recovery](LIVE_HISTORICAL_RECOVERY_20261020.md).
This increment authenticates retained ETH and USDC oracle price packages offline.
It does **not** replace the production Chainlink reader or grant live admission.
No RPC, price gateway, wallet-key or signing-service request was made in this
increment. Source research used public upstream source files only. Cumulative
paid RPC acceptance usage remains eight calls; no budget was reset or increased.

## Confirmed behavior

`live/signed_prices.py` pins RedStone's PrimaryProd service, five signer addresses
and minimum three unique signers to upstream commit
`ee55521d0949a9f944d9568bd34671ab14c56118`. Its trust policy comes from the
[canonical consumer](https://github.com/redstone-finance/redstone-oracles-monorepo/blob/ee55521d0949a9f944d9568bd34671ab14c56118/packages/evm-connector/contracts/data-services/PrimaryProdDataServiceConsumerBase.sol).
It supports exactly the ETH and USDC single-point numeric packages, eight price
decimals and 32-byte unsigned values. This is an intentionally narrow adapter.

The signed message is feed ID (32 bytes), value (32), timestamp milliseconds (6),
value byte size (4), point count (3), all unsigned big-endian. The verifier hashes
these bytes with Keccak-256 and recovers the ECDSA signer. It rejects high-s or
noncanonical signatures, unknown signers, duplicate signers, different feed IDs,
bad widths, unknown package fields and inconsistent claimed signer addresses.
Gateway `isSignatureValid` and point metadata never establish authenticity.
Service/package labels are unsigned; the pinned recovered keys and signed feed
bytes establish the accepted identity, not those labels.

Every package across both feeds must carry the same signed timestamp, no later
than the observation clock and no more than 1,800 seconds old. A feed's maximum
minus minimum must be at most 40 basis points of its minimum. These are candidate
screening bounds, not new production release thresholds. Callers may tighten
them, never widen them. The median uses integer floor for an even signer count.
JSON duplicate keys, non-finite numbers, booleans as integers, excessive input
size and incomplete feed sets refuse. Returned evidence includes raw input hash,
signer-policy hash, source commit, observation clock, every authenticated witness,
the screening policy and an evidence hash; `execution_eligible` is always false.

Numeric JSON is parsed as Decimal, rounded half-up to eight decimals, then its
resulting bytes must authenticate. Protocol string values mean canonical base64
raw bytes; a decimal string is not a price assertion. The upstream numeric SDK
uses JavaScript `Number.toFixed`, so binary floating-point half-tie cases may
differ and be refused by this adapter. Exact base64 values avoid this compatibility
limit. This verifier does not claim to reproduce every JavaScript numeric encoding.

`python -m scripts.verify_signed_prices` accepts a bounded regular input file,
refuses symlinks/FIFOs, uses the host clock by default and writes an exclusive
report. `--at-ms` explicitly labels a historical replay clock. Failures emit a
fixed refusal without echoing input. The command does not load environment
credentials, make network requests, or touch production state. Its input file
must be retained alongside the report for replay; a hash alone is not raw evidence.

## Canonical vector provenance

The committed fixture `tests/fixtures/redstone-primary-prod-historical.json`
contains all five ETH and all five USDC packages from the upstream
[historical production fixture](https://github.com/redstone-finance/redstone-oracles-monorepo/blob/ee55521d0949a9f944d9568bd34671ab14c56118/packages/protocol/test/primary-prod-data-packages.json).
Extraction retains values, signatures, timestamps and signer labels; unrelated
feeds and unsigned metadata are removed. The upstream file is 1,513,077 bytes.

| Artifact | SHA-256 |
| --- | --- |
| Upstream full fixture | `9e580e6f742c18f74187ab343df1bb16c7600016d6d14919d7d8ca16b11b1157` |
| Committed subset | `0dad2a8aa0552d6a1b1f4eb6a271d55cf9746aef7b360129fd5ecc64555fd0c8` |

At its signed timestamp `1719856900000`, all ten production signatures verify
without mocked trust. ETH median is `348695203485`, USDC median `99988697`, both
in units of 10^-8 USD. These are historical prices, not current prices. The same
file fails under the current host clock. Local retained reports are under
`reports/review-20261021/signed-prices/`:

- `historical.json`: successful historical interpretation, evidence hash
  `452a04cec6798d9e712f81fa87bae79c5ed545163c6c7709d113fe37430cf6b3`.
- `current-refusal.json`: refused at host timestamp `1792548043537`, evidence hash
  `4824338c45e83726c9b14d398a02b3a2b9ba9b5236794e92af3b15f85e0d3a22`.

Neither report is chain-derived evidence or block-bound NAV. Tests cover valid
three/four/five-signer sets, boundary freshness, tampering, signer duplication,
spread, cross-feed timestamp mismatch, signature malleability, raw-value encoding,
CLI redaction and output preservation. Only the cross-feed timestamp negative
test uses ephemeral test signers, with production trust replaced inside that test.
The canonical compatibility test uses the actual pinned production signer set.

Validation: `make python-check frontend-check` passed Ruff, formatting, mypy,
3,147 tests with 138 subtests, and syntax checking for eight frontend modules.
The shared release-blocker text shown by the dashboard now distinguishes offline
historical verification from pending fresh-price/NAV integration; its 45 focused
release/dashboard tests passed after that wording change. Secret scanning passed
after reviewing only the public upstream commit and ten public oracle signatures
as nonsecrets. Bandit reported no medium/high severity findings. Logs are retained
in the same local report directory. No contract code or dependency was changed.

## Unresolved production conditions

The pinned [official SDK request path](https://github.com/redstone-finance/redstone-oracles-monorepo/blob/ee55521d0949a9f944d9568bd34671ab14c56118/packages/sdk/src/fetch-data-packages.ts)
uses authenticated gateways and `x-api-key`. No usable production credential or
fresh signed package has been supplied; no price gateway access was attempted.
The source snapshot establishes that SDK path, not a claim that every possible
data distributor requires payment.

Production adoption still requires a provisioned source, bounded acquisition and
retention, current signer-policy review, explicit asset/USD mapping, block/time
binding to independently captured account state, consistent replay in NAV and
the signer boundary, and sustained freshness/failure acceptance. Multiple oracle
signers do not constitute two independent RPC providers or independent economic
price methodologies. A signed price is not proof of executable liquidity or profit.

The existing USD 500 capital, USD 200 daily and USD 500 cumulative loss limits,
1,800-second production price-age limit and all release gates remain unchanged.
Fresh full-account NAV, current simulation/runtime evidence and real-canary
receipt/cash recovery acceptance remain required. No real trade was submitted.
