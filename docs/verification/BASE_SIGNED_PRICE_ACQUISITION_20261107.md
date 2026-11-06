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

## Wallet acquisition preflight

Both Base wallet providers now verify the chain ID and the full pinned block
header before either requests account code, nonce, balances or USDC data.
A foreign-chain block supplied by the caller refuses before creating provider
sessions. Provider chain, block hash or parent hash disagreement now stops after
two requests per provider, rather than consuming all ten wallet reads per
provider. Successful captures retain the existing request sequence and evidence
format, including the closing header check.

The four regression cases reproduced the excess requests before the fix. The
corrected focused run passed 135 tests across wallet, signed NAV, production
valuation and recovery. An initial invocation named a nonexistent test path
and ran no tests. These checks used local fixtures.

Final `make live-quality` exited 0 with 4,752 tests and 138 subtests.
All 685 frozen inputs matched. Python/frontend, secret scan, Bandit and
offline Compose each exited 0. The quality evidence hash is
`86840be6e6dacb23fee8324d97edf3869c54ddb5ebc3d00b72c8153309f99307`.

## Position outcome preflight

Position capture now checks both providers' opening chain and block headers,
and verifies the pinned PositionManager and StateView code, before either
provider reads NFT ownership, position data or liquidity. A foreign-chain block
refuses locally. A disagreeing header or code stops after four reads per provider
instead of all nine for a held position. A successful capture keeps its prior
request sequence, closing header check and evidence format.

Four focused regressions reproduced the extra reads before the fix. The
corrected focused run passed 148 tests covering position outcomes, valuation,
signed NAV and recovery. Offline readback of the five retained Base receipts
and the retained burn simulation reproduced their original evidence hashes
`d6924a16a7700315c45d26b30ac3d00ba83e8d78a41c41228f368a5197296720` and
`15d4e1b74c81e91e5ec44d474a73046e1b406850b45b18ef1526cc9b5579bffd`.
The observations remain tied to their original Base blocks 53984727 and
53985629. No new RPC request was made.

Final `make live-quality` exited 0 with 4,756 tests and 138 subtests.
All 685 frozen inputs matched. Python/frontend, secret scan, Bandit and
offline Compose each exited 0. The quality evidence hash is
`ac83d15c656bef1fcb3d5b577fee7fca6632df219f2e19933e5448d7a9c972b5`.
