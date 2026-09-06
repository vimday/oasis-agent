# Robinhood Chain Uni v4 Benchmark Hook Analysis

> **Fixture-only research report.** This is not live-chain evidence, a backtest, or a production-readiness decision.

## Provenance

- Status: `fixture_only`
- Data: deterministic fixture; not live-chain evidence or a backtest

## Identity

- Chain ID: `4663`
- Pool ID: `0x374531f25cfb1b92f6d03b946598d53dd934fba6e138bcece153a52113aa73a5`
- PoolManager: `0x8366a39cc670b4001a1121b8f6a443a643e40951`
- Hook: `0x1191Ac2561817686EFE4296B37BA5a5419Eaa880`
- Pool fee field: `8388608` (`0x800000`, dynamic-fee sentinel)
- Tick spacing: `60`

## Hook permissions

- `before_initialize`: enabled
- `before_add_liquidity`: enabled
- `before_swap`: enabled
- `after_swap`: disabled
- `before_swap_returns_delta`: disabled
- `after_swap_returns_delta`: disabled

## Default fee configuration

```text
[10000, 20000, 30000, 200, 3400, 3300, 3300, 16777216]
```

## Findings

- **confirmed** — `beforeSwap` is enabled by the hook address permissions.
- **confirmed** — No swap-return-delta permission is enabled by the address mask.
- **inferred** — Bytecode contains a fee override path and likely dynamic fee configuration.
- **unresolved** — The exact fee-selection algorithm requires pinned fork execution or verified source.

## Fixture flow-attribution example

> **Fixture-only research report.** It is not live-chain evidence, a backtest, or a production decision.

## Snapshot identity

- Status: `fixture_only`
- Chain ID: `4663`
- Pool ID: `0x374531f25cfb1b92f6d03b946598d53dd934fba6e138bcece153a52113aa73a5`
- Hook: `0x1191ac2561817686efe4296b37ba5a5419eaa880`
- Currency1 token: `0x0000000000000000000000000000000000000001`
- Currency1 decimals: `6`
- Opening block (exclusive): `53576330`
- Opening block hash: `0xb439ac5045cdaf5f86b0d55a8d80769f24377e4fc2e4e7bfa6b8dbb5cbc2d74b`
- Closing block (inclusive): `53576331`
- Closing block hash: `0xa439ac5045cdaf5f86b0d55a8d80769f24377e4fc2e4e7bfa6b8dbb5cbc2d74b`

## Conservative economic decomposition

- Flow count: `3`
- Gross volume: `30000 USDG base units`
- Gross LP fees: `300 USDG base units`
- Confirmed external fee alpha: `90 USDG base units`
- Affiliated/self-classified LP fees: `100 USDG base units`
- Unknown-flow LP fees: `100 USDG base units`
- Self-trade cost: `95 USDG base units`
- Gas cost: `9 USDG base units`
- Adverse-selection cost: `21 USDG base units`
- Net modeled LP PnL: `-25 USDG base units`
- Unresolved flow count: `1`
- Confirmed flow count: `2`
- All flows economically evidenced: `false`

## Evidence-qualified claims

- **confirmed** — The displayed totals are deterministic integer arithmetic over the supplied fixture inputs.
- **inferred** — Affiliated labels are evidence-backed inputs, not independent proof of common beneficial ownership.
- **unresolved** — Unknown or non-confirmed external flow does not establish unrelated market demand or fee alpha.
- **confirmed** — Every modeled flow amount and cost is bound to block-pinned evidence; unknown classifications remain unresolved.

## Flow evidence

- Transaction: `0x1111111111111111111111111111111111111111111111111111111111111111`
  - Pool ID: `0x374531f25cfb1b92f6d03b946598d53dd934fba6e138bcece153a52113aa73a5`
  - Log index: `0`
  - Claim: `amounts`
  - Level: `confirmed`
  - Kind: `synthetic_flow_fixture`
  - Source: `synthetic-fixture`
  - Retrieved at: `2026-09-04T00:00:00+00:00`
  - Block: `53576331`
  - Block hash: `0xa439ac5045cdaf5f86b0d55a8d80769f24377e4fc2e4e7bfa6b8dbb5cbc2d74b`
  - Content hash: `1111111111111111111111111111111111111111111111111111111111111111`
  - Request hash: `4444444444444444444444444444444444444444444444444444444444444444`
  - Raw response hash: `5555555555555555555555555555555555555555555555555555555555555555`
  - Interpretation hash: `6666666666666666666666666666666666666666666666666666666666666666`
  - Schema version: `2`
- Transaction: `0x1111111111111111111111111111111111111111111111111111111111111111`
  - Pool ID: `0x374531f25cfb1b92f6d03b946598d53dd934fba6e138bcece153a52113aa73a5`
  - Log index: `0`
  - Claim: `costs`
  - Level: `confirmed`
  - Kind: `synthetic_flow_fixture`
  - Source: `synthetic-fixture`
  - Retrieved at: `2026-09-04T00:00:00+00:00`
  - Block: `53576331`
  - Block hash: `0xa439ac5045cdaf5f86b0d55a8d80769f24377e4fc2e4e7bfa6b8dbb5cbc2d74b`
  - Content hash: `1111111111111111111111111111111111111111111111111111111111111111`
  - Request hash: `4444444444444444444444444444444444444444444444444444444444444444`
  - Raw response hash: `5555555555555555555555555555555555555555555555555555555555555555`
  - Interpretation hash: `6666666666666666666666666666666666666666666666666666666666666666`
  - Schema version: `2`
- Transaction: `0x1111111111111111111111111111111111111111111111111111111111111111`
  - Pool ID: `0x374531f25cfb1b92f6d03b946598d53dd934fba6e138bcece153a52113aa73a5`
  - Log index: `0`
  - Claim: `classification`
  - Level: `confirmed`
  - Kind: `synthetic_flow_fixture`
  - Source: `synthetic-fixture`
  - Retrieved at: `2026-09-04T00:00:00+00:00`
  - Block: `53576331`
  - Block hash: `0xa439ac5045cdaf5f86b0d55a8d80769f24377e4fc2e4e7bfa6b8dbb5cbc2d74b`
  - Content hash: `1111111111111111111111111111111111111111111111111111111111111111`
  - Request hash: `4444444444444444444444444444444444444444444444444444444444444444`
  - Raw response hash: `5555555555555555555555555555555555555555555555555555555555555555`
  - Interpretation hash: `6666666666666666666666666666666666666666666666666666666666666666`
  - Schema version: `2`
- Transaction: `0x2222222222222222222222222222222222222222222222222222222222222222`
  - Pool ID: `0x374531f25cfb1b92f6d03b946598d53dd934fba6e138bcece153a52113aa73a5`
  - Log index: `0`
  - Claim: `amounts`
  - Level: `confirmed`
  - Kind: `synthetic_flow_fixture`
  - Source: `synthetic-fixture`
  - Retrieved at: `2026-09-04T00:00:00+00:00`
  - Block: `53576331`
  - Block hash: `0xa439ac5045cdaf5f86b0d55a8d80769f24377e4fc2e4e7bfa6b8dbb5cbc2d74b`
  - Content hash: `2222222222222222222222222222222222222222222222222222222222222222`
  - Request hash: `4444444444444444444444444444444444444444444444444444444444444444`
  - Raw response hash: `5555555555555555555555555555555555555555555555555555555555555555`
  - Interpretation hash: `6666666666666666666666666666666666666666666666666666666666666666`
  - Schema version: `2`
- Transaction: `0x2222222222222222222222222222222222222222222222222222222222222222`
  - Pool ID: `0x374531f25cfb1b92f6d03b946598d53dd934fba6e138bcece153a52113aa73a5`
  - Log index: `0`
  - Claim: `costs`
  - Level: `confirmed`
  - Kind: `synthetic_flow_fixture`
  - Source: `synthetic-fixture`
  - Retrieved at: `2026-09-04T00:00:00+00:00`
  - Block: `53576331`
  - Block hash: `0xa439ac5045cdaf5f86b0d55a8d80769f24377e4fc2e4e7bfa6b8dbb5cbc2d74b`
  - Content hash: `2222222222222222222222222222222222222222222222222222222222222222`
  - Request hash: `4444444444444444444444444444444444444444444444444444444444444444`
  - Raw response hash: `5555555555555555555555555555555555555555555555555555555555555555`
  - Interpretation hash: `6666666666666666666666666666666666666666666666666666666666666666`
  - Schema version: `2`
- Transaction: `0x2222222222222222222222222222222222222222222222222222222222222222`
  - Pool ID: `0x374531f25cfb1b92f6d03b946598d53dd934fba6e138bcece153a52113aa73a5`
  - Log index: `0`
  - Claim: `classification`
  - Level: `confirmed`
  - Kind: `synthetic_flow_fixture`
  - Source: `synthetic-fixture`
  - Retrieved at: `2026-09-04T00:00:00+00:00`
  - Block: `53576331`
  - Block hash: `0xa439ac5045cdaf5f86b0d55a8d80769f24377e4fc2e4e7bfa6b8dbb5cbc2d74b`
  - Content hash: `2222222222222222222222222222222222222222222222222222222222222222`
  - Request hash: `4444444444444444444444444444444444444444444444444444444444444444`
  - Raw response hash: `5555555555555555555555555555555555555555555555555555555555555555`
  - Interpretation hash: `6666666666666666666666666666666666666666666666666666666666666666`
  - Schema version: `2`
- Transaction: `0x3333333333333333333333333333333333333333333333333333333333333333`
  - Pool ID: `0x374531f25cfb1b92f6d03b946598d53dd934fba6e138bcece153a52113aa73a5`
  - Log index: `0`
  - Claim: `amounts`
  - Level: `unresolved`
  - Kind: `synthetic_flow_fixture`
  - Source: `synthetic-fixture`
  - Retrieved at: `2026-09-04T00:00:00+00:00`
  - Block: `53576331`
  - Block hash: `0xa439ac5045cdaf5f86b0d55a8d80769f24377e4fc2e4e7bfa6b8dbb5cbc2d74b`
  - Content hash: `3333333333333333333333333333333333333333333333333333333333333333`
  - Request hash: `4444444444444444444444444444444444444444444444444444444444444444`
  - Raw response hash: `5555555555555555555555555555555555555555555555555555555555555555`
  - Interpretation hash: `6666666666666666666666666666666666666666666666666666666666666666`
  - Schema version: `2`
- Transaction: `0x3333333333333333333333333333333333333333333333333333333333333333`
  - Pool ID: `0x374531f25cfb1b92f6d03b946598d53dd934fba6e138bcece153a52113aa73a5`
  - Log index: `0`
  - Claim: `costs`
  - Level: `unresolved`
  - Kind: `synthetic_flow_fixture`
  - Source: `synthetic-fixture`
  - Retrieved at: `2026-09-04T00:00:00+00:00`
  - Block: `53576331`
  - Block hash: `0xa439ac5045cdaf5f86b0d55a8d80769f24377e4fc2e4e7bfa6b8dbb5cbc2d74b`
  - Content hash: `3333333333333333333333333333333333333333333333333333333333333333`
  - Request hash: `4444444444444444444444444444444444444444444444444444444444444444`
  - Raw response hash: `5555555555555555555555555555555555555555555555555555555555555555`
  - Interpretation hash: `6666666666666666666666666666666666666666666666666666666666666666`
  - Schema version: `2`

## Safety interpretation

- Gross fees are not treated as proof of unrelated external demand.
- Self-classified flow remains economically charged for modeled trading, gas, and adverse-selection costs.
- Unknown flow remains unresolved and cannot support a profitability or deployment claim.


## Limitations and safety

- Source code is not verified.
- This report is generated from deterministic local fixture data.
- No mainnet transaction is sent by the report command.
- Dynamic fee claims require block-pinned settlement reconciliation.
