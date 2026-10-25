# PositionManager inventory coverage — 2026-10-25

## Finding and implementation

Previously the direct-loop NAV proof verified ownership and liquidity of a declared
position but did not compare that declaration with the account's complete NFT count
at the configured PositionManager. An account declaring no position could own an
unreported NFT, or a declared position could coexist with another unvalued NFT.
The calculated NAV would omit that exposure.

New captures pin the manager runtime and `balanceOf(account)` to the same EIP-1898
block hash as the wallet, position and USD observations, independently through both
providers. Zero declared positions requires zero NFTs. One declared position requires
exactly one NFT, in addition to its existing owner/pool/range/liquidity proof. Missing,
extra, malformed or disagreeing observations refuse NAV. The signing authority
reconstructs this evidence before reserving a nonce or calling its key provider.

This establishes coverage of the configured manager only. It does not discover
assets in other protocols, arbitrary tokens or the account's funded historical basis.
No release blocker is removed and no production acceptance is claimed.

Both production NAV and signed-price candidate schemas advance to version 2. Version 1
records remain byte-verifiable for historical checkpoint replay but are rejected by
current admission/freshness verification. A mixed-version quorum also refuses. No
existing evidence is rewritten. Signed-price candidates remain excluded from the
production signing path.

Acquisition adds two reads per provider. The signed-price CLI now budgets 17 reads per
provider without a position or 28 with one, plus one primary head lookup. The free
Chainlink NAV diagnostic derives its larger 26/25 request requirement from the shared
request plan. Existing frozen budgets and request limits are unchanged; an allowance
that cannot fit the capture still refuses before network access.

## Development verification and outstanding checks

The 14 targeted inventory/admission/legacy-replay/request-accounting regression cases
passed (exit 0). Ruff, formatting, mypy, frontend checks, secret scanning and Bandit
each exited 0; their raw log hashes were independently rechecked against
`reports/review-20261025/nav-inventory-development/static-checks.json`.

Earlier development runs exposed stale synthetic NFT declarations and request-count
assertions after adding four total reads. Fixtures and expectations were corrected.
One authority regression initially expected `ValueError` instead of the observation
boundary's existing `ChainError`; it was corrected without changing production error
semantics. Original failed logs are retained in the same review directory. A sandboxed
socket test also failed to bind; this is not a passing socket validation.

The first complete `make live-quality` attempt was **not executed**: automatic approval
review failed because its usage quota was exhausted. The service explicitly described
this as a review failure, not a determination that the action was unsafe. No bypass
was attempted. After the review service recovered, the complete check was started;
its final result is recorded below. A narrower offline test selection is recorded
separately and cannot substitute for the full release-development check. The broader
offline selection exited 0: **193 passed, 1 deselected** in 35.51 seconds, with the
Unix-socket integration test explicitly excluded. Its original log is
`reports/review-20261025/nav-inventory-development/offline-focused-final.log`.

The subsequent offline review also exercised version 1 signed-price candidates:
their historical bytes still reconstruct, but both an all-legacy pair and a mixed
version pair refuse current inventory verification, with and without a declared
position. The inventory module's 14 cases passed (exit 0, 4.12 seconds) in
`reports/review-20261025/nav-inventory-development/signed-legacy-followup.log`;
Ruff and formatting passed for the added cases. This alone does not complete the
full quality run or alter any production gate.

## Runtime image source verification

The complete development quality run subsequently passed with all four command groups
exiting 0 and no timeout/launch failure: **3488 tests and 138 subtests**, Ruff, format,
compile checks, mypy (231 source files), frontend syntax (8 modules), secret scanning,
Bandit (88 low, zero medium/high findings), and expanded Compose policy verification.
The one test warning is the existing websockets deprecation. Each raw log hash and
the canonical report hash were checked independently:

`reports/review-20261025/nav-inventory-quality/report.json`, evidence hash
`8a87de1afbfff456e584d56edea40240f3bcf725133237ac6b57b9ca2663cd49`.
This supersedes the earlier pending-quality status, retaining its original failure
records and offline-only results. Tests use synthetic inputs; no production evidence
gate is passed by this result.

The first build used `--network=none`, missed the existing dependency layer cache and
failed to install the locked packages without network access (exit 1). The original
failure remains in `nav-inventory-development/image-build.log`. A second build used
the original build network mode and reused the existing dependency layer (`CACHED`),
exiting 0 without a new dependency installation; its log is `image-build-cached.log`.
Neither build invoked a chain RPC or started production services.

The resulting `oasis-live:local` / `oasis-live:nav-inventory-20261025` image is
`sha256:bb1ac3127f74ac8e95e008ebe877107936779f0a3f2cec7564e6147ca9a90ec4`.
The stopped-container source verifier exited 0 and matched all **328 tracked runtime
files**. No container process was started and no key/state volume was mounted. Its
report is `reports/review-20261025/nav-inventory-development/image-sources.json`:

- Application manifest: `853d19cc17d61089162fd683474d9d95ef71839ef3f6d3b8e14bfdd17397d1c0`.
- Evidence hash: `dfdf69b6187d1e44c2f97e4f284555f0160af6779db5195eb18c4ad846b23161`.

This proves local application-byte identity, not dependency provenance, live provider
acceptance, a new Linux test-suite run, or production readiness.

## Duplicate file cleanup

Deleted only these two untracked, generated compiler working copies after verifying
exact byte equality with retained original downloads:

| Removed duplicate | Retained original under `reports/evidence/token-reproduction-compilers-20261002/` |
| --- | --- |
| `reports/review-20261002/token-source-reproduction/compiler-0.js` | `attempt-01.raw` |
| `reports/review-20261002/token-source-reproduction/compiler-1.js` | `attempt-03.raw` |

Freed **32487613 bytes** (32.5 MB). The per-file byte counts and SHA-256 mappings are
in `reports/review-20261025/duplicate-compiler-cleanup.json`. Both retained originals
were hashed again after deletion. Old logs and checkpoint manifests remain unchanged;
their former working paths are now documented aliases of those retained original
bytes, not currently present files. Reproducing an old compiler invocation requires
materializing the original bytes at its former path after checking the recorded hash.

No keys, request budgets, real state, raw chain evidence, or failed acceptance records
were deleted. This increment made zero RPC requests and performed no signing,
broadcast, funding or production service startup. The previously authorized one-shot
free current-state run remains terminal after its stale USDC/USD refusal. Fresh USD
valuation, production dual-source validation and dedicated-account dual-service receipt
recovery remain unaccepted.

After testing, removed 180 regenerable Python/tool cache files (18736677 bytes),
excluding tracked files and refusing symlinks. Their path/size inventory is
`reports/review-20261025/generated-cache-cleanup-nav.json`. Together with the duplicate
compiler copies, this turn removed **51224290 bytes** (51.2 MB), excluding the small
cleanup manifests. Earlier cleanup amounts are not included again.
