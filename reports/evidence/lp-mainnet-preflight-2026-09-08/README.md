Oasis Agent LP mainnet preflight evidence package.
Date: 2026-09-08 (Asia/Shanghai)

Verified code gates:
  - python-check: 782 passed, 108 subtests passed; Ruff, formatting and mypy clean across 105 source files.
  - contract-check: 68 passed / 8 suites; 10,000-case fuzz tests and 512 invariant runs with 65,536 calls; format/build/lint clean.
  - security-check: pip-audit found no known vulnerabilities; Bandit found 0 medium/high issues; secret gate passed.
  - contract-security-check: Slither analyzed 80 contracts with 102 detectors and found 0 results.

Release-path additions covered by the gates:
  - 14-check, dual-provider, block-pinned deployment identity and control-state verifier.
  - External-signer deployment and pool-configuration scripts; no raw private-key input.
  - Bounded ERC-20/ERC-20 first-canary mint with exact short-lived Permit2 allowances,
    immediate allowance cleanup, and one-NFT approval to the bounded executor.
  - Independent emergency revocation of ERC-20, Permit2 and NFT authority.
  - Corrected live-shadow exact-input/exact-output fee matching and hash-linked evidence.
  - Observation-server schema, strict RPC quantity parsing, route discovery and ETag fixes.
  - Strict V4 Quoter ABI codec and block-pinned, two-provider, bidirectional exact-input/output
    route probe with runtime/binding checks and structured revert evidence.

Real-chain evidence retained separately:
  - reports/evidence/live-shadow-robinhood-testnet-reverify-2026-09-08
    Robinhood testnet: 341 blocks, 4 real swaps, deployed-hook 500-pip quote matched all 4.
  - reports/evidence/lp-e2e-robinhood-testnet-2026-09-07
    Robinhood testnet: 35 signed transactions covering mint, four swap modes, collect,
    recenter, remove and approval cleanup with receipt/token/gas reconciliation.
  - reports/evidence/deploy-check-robinhood-benchmark-57426748-v7-2026-09-08
    Robinhood mainnet read-only identity sample at block 57,426,748. One public provider
    matched the configured core hashes and token metadata/runtime hashes, but the dual-provider
    gate is unavailable. PONS/USDG slot0 was tick 887271 near the maximum price boundary.
  - reports/evidence/route-probe-robinhood-benchmark-57453812-v4-2026-09-08
    Robinhood mainnet read-only route sample at block 57,453,812. Expected PoolManager/Quoter/
    Universal Router hashes and the Quoter binding matched. All 12 PONS/USDG quotes failed:
    six NotEnoughLiquidity and six PriceLimitAlreadyExceeded at the maximum price boundary.

Release outcome:
  identities_verified: false
  execution_eligible: false
  production_broadcast: none

Remaining inputs are a second independent production RPC, an economically qualified target
pair/pool with routing and exit-depth evidence, separated deployer/governor/guardian/LP-owner
addresses with an external signer, a precommitted canary risk budget, and sustained shadow/canary
evidence. Third-party commercial audit is not listed as a blocker for the project-owned minimal
canary; traceable independent cross-review evidence remains required by the release assessment.

Verify this package from its directory with:
  shasum -a 256 -c MANIFEST.sha256
