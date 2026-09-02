# Robinhood Chain Uni v4 Benchmark Hook Analysis

## Identity

- Chain ID: `4663`
- Pool ID: `0x374531f25cfb1b92f6d03b946598d53dd934fba6e138bcece153a52113aa73a5`
- PoolManager: `0x8366a39cc670b4001a1121b8f6a443a643e40951`
- Hook: `0x1191Ac2561817686EFE4296B37BA5a5419Eaa880`
- Pool initialization fee: `60` (`0.006000%`)

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

## Limitations and safety

- Source code is not verified.
- This report is generated from deterministic local fixture data.
- No mainnet transaction is sent by the report command.
- Dynamic fee claims require block-pinned settlement reconciliation.
