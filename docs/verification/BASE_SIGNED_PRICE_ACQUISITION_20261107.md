# Base signed-price acquisition

The current acceptance scope is Base. At the retained Base block 53983490,
the configured USDC/USD round was 17,269 seconds old. Both provider proofs
reproduced the freshness refusal. The gas-oracle candidate fingerprints, five
dual-source receipts and unsigned burn rehearsal are retained separately.
[Base acquisition record](LIVE_PAID_EVIDENCE_20261107.md)

`python -m scripts.capture_signed_prices --out <new-private-directory>` makes
one HTTPS GET for ETH and USDC using `REDSTONE_API_KEY` from the process
environment. It uses the current RedStone SDK's per-feed route, with a
15-second total fetch deadline, a bounded response and a fixed destination.
The verifier checks the pinned PrimaryProd signer quorum, the signed
timestamps, spread and the existing USDC valuation band. A successful run
retains the exact `prices.json` and a hashed `report.json`. The price file
is the input to `scripts.capture_signed_nav --prices`. That command defaults
to planning and uses the original RPC budget when capture is separately
enabled. [RedStone gateway SDK](https://github.com/redstone-finance/redstone-oracles-monorepo/blob/main/packages/sdk/src/fetch-data-packages.ts)

The process had no gateway credential. The real entry point refused at
credential preflight with exit 3 and zero requests. The HTTP success path was
tested with simulated transport and authentic historical signatures. The
gateway's current payload and live signer set still require verification. The
collector accepts the existing single-feed package format. It refuses a
different package format, a stale or inconsistent quorum, or a response that
reflects the credential. The focused suite passed 89 tests.

The offline Base startup check also exited 3 with
`gas_oracle_runtime_pins_missing`. An authenticated price candidate can support
a Base account capture and policy review. Production use requires a reviewed
price-source policy and current same-block full NAV, approved gas-oracle pins
and dual-service receipt and cash recovery. Execution eligibility remains false.

RedStone's published PrimaryProd consumer declares three required signers and
the same five addresses as the pinned local verifier. Current gateway access
and live response compatibility remain the next acquisition check.
[PrimaryProd consumer](https://github.com/redstone-finance/redstone-oracles-monorepo/blob/main/packages/evm-connector/contracts/data-services/PrimaryProdDataServiceConsumerBase.sol)

Final `make live-quality` exited 0. The 685 frozen inputs matched, 4,748
tests and 138 subtests passed, and each of the Python/frontend, secret scan,
Bandit and offline Compose checks exited 0. The quality evidence hash is
`dd7eabc0d2ad2a058cf1fb5c1c5368672bb56b4f89475e3a2f1ba0d6283eb5ef`.
Offline `make contract-check` exited 0 with 70 tests, zero failures and
zero skips. The contract log SHA-256 is
`882e307cd7ea06a3d55f5a4e0976f5ee49071d5707450a09589ac860980011d2`.
