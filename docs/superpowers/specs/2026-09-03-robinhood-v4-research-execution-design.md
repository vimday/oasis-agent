# Oasis Agent: Robinhood Chain Uni v4 Research and Execution Design

**Date:** 2026-09-02
**Status:** Approved and implemented in milestones
**Scope:** Robinhood Chain / Uniswap v4 pool and hook analysis, LP farming research, arbitrage simulation, and guarded execution

## 1. Purpose and success criteria

Oasis Agent is a reproducible research and execution system for Robinhood Chain liquidity and arbitrage strategies. The first benchmark is Pool ID `0x374531f25cfb1b92f6d03b946598d53dd934fba6e138bcece153a52113aa73a5` and hook `0x1191Ac2561817686EFE4296B37BA5a5419Eaa880`.

The primary optimization target is risk-adjusted LP net yield, not gross fee rate:

```text
LP net yield = gross LP fees
              - impermanent loss
              - adverse-selection loss
              - gas
              - rebalancing cost
              - execution failure cost
              - inventory and liquidity risk premium
```

The implementation must compare strategies using the same chain data, capital, time window, price path, and cost assumptions. Results must distinguish confirmed chain facts from bytecode inferences and unresolved questions.

A strategy is successful only when it improves out-of-sample LP net yield while satisfying explicit fee, exposure, and execution risk limits. No strategy may depend on undisclosed fees, trader identity discrimination, arbitrary asset extraction, or bypassing protocol controls.

## 2. Scope and non-goals

### In scope

- Robinhood Chain RPC data acquisition and immutable evidence storage.
- Uniswap v4 PoolKey, PoolId, PoolManager event, fee, hook, and balance analysis.
- Unverified EVM bytecode metadata, selector, permission-bit, storage, and call-pattern analysis.
- Historical LP fee, inventory, price, and impermanent-loss accounting.
- Transparent dynamic-fee strategy modeling and backtesting.
- Arbitrage opportunity detection, quote construction, simulation, risk checks, and reporting.
- A guarded execution state machine whose default mode is observation/simulation.
- Local fork and synthetic-trader adversarial fixtures for testing accounting and disclosure risks.

### Out of scope

- Exploiting a third-party pool or hook on mainnet.
- Deliberately undisclosed or trader-specific charges.
- Sandwiching, malicious transaction ordering, denial of service, or unauthorized asset extraction.
- Storing private keys or silently broadcasting transactions.
- Treating a selector-name database match or decompiler guess as verified source semantics.
- Claiming profitability from in-sample or unreconciled data.

## 3. Architecture

The system is organized as independently testable layers:

```text
CLI / configuration
        |
Evidence store and RPC client
        |
EVM analysis + Uniswap v4 decoding
        |
Market data and accounting models
        |
Farming analytics + arbitrage research
        |
Simulation and risk gate
        |
Explicit approval -> external signer -> broadcast -> receipt verification
        |
Structured reports and audit logs
```

Each result carries `chain_id`, `block_number`, `block_hash`, retrieval time, source endpoint, and the bytecode or dataset hash used to produce it. Historical observations and latest-state reads are never silently mixed.

The initial implementation uses Python 3.12 with a `pyproject.toml`, `uv`, `pytest`, `ruff`, `mypy`, `pydantic`, `httpx`, `web3.py`, `eth-abi`, and `eth-utils`. Numeric token and fee calculations use integer arithmetic or explicit `Decimal` contexts; binary floating point is not used for settlement decisions.

Proposed package boundaries:

```text
src/oasis_agent/
  cli.py
  config.py
  errors.py
  models/
  rpc/
  evidence/
  evm/
  uniswap_v4/
  market_data/
  farming/
  arbitrage/
  risk/
  execution/
  reporting/
```

## 4. Evidence and chain-data layer

The RPC layer provides:

- endpoint health checks and chain-ID validation;
- bounded retries with exponential backoff and rate-limit handling;
- `eth_getCode`, `eth_call`, `eth_getLogs`, block, transaction, receipt, and trace retrieval where supported;
- block-pinned reads for all historical analysis;
- raw response persistence in a local evidence store;
- deterministic request and response hashes;
- schema validation and explicit missing-data errors.

The evidence store initially uses SQLite with content-addressed raw payloads. A report references evidence IDs rather than copying unverifiable values. If an endpoint disagrees with another endpoint at the same block, the result is marked inconsistent and cannot enter an execution decision.

Configuration is environment- or file-based and must reject missing chain ID, PoolManager allowlist, or RPC URL. Secrets are never accepted in checked-in configuration files.

## 5. EVM and hook analysis

The static analyzer operates on a bytecode hash and reports:

- deployed bytecode length and hash;
- Solidity CBOR metadata when present;
- compiler metadata and source-verification status;
- dispatcher selectors and bytecode destinations;
- hard-coded addresses and event/error topics;
- `SLOAD`, `SSTORE`, `CALL`, `STATICCALL`, `DELEGATECALL`, `CREATE`, and `SELFDESTRUCT` features;
- hook permission bits derived from the address;
- dynamic-fee and override-flag indicators;
- likely PoolManager caller checks;
- confidence-tagged control-flow observations.

Findings use exactly three evidence levels:

- `confirmed`: directly observed from chain state, event data, or unambiguous bytecode behavior;
- `inferred`: supported by control flow or conventions but not source-verified;
- `unresolved`: requires source, fork execution, or additional evidence.

The analyzer must not generate an exploit payload. It may generate safe read-only calldata and local simulation inputs.

For the benchmark hook, the first report records the observed `beforeInitialize`, `beforeAddLiquidity`, and `beforeSwap` permissions; absence of observed swap-delta permissions; PoolManager address checks; owner/registrar/whitelist interfaces; and dynamic fee override indicators. The report explicitly states that unverified bytecode does not prove the exact fee-selection algorithm.

## 6. Uniswap v4 analysis and accounting

The v4 layer provides:

- canonical PoolKey encoding and PoolId reconstruction;
- PoolManager event topic definitions and decoding;
- initialization, swap, liquidity, fee, and protocol-fee accounting;
- signed `BalanceDelta` decoding with overflow and range checks;
- exact-input and exact-output simulations as separate code paths;
- token transfer and PoolManager accounting reconciliation;
- hook fee and dynamic-fee comparison against UI/quoter observations when available.

For every analyzed swap, the reconciliation model tracks:

```text
trader net input
trader net output
PoolManager delta
pool delta
protocol fee
LP fee
hook delta
ERC-20 transfer delta
```

A report cannot call a fee hidden or undisclosed based only on a static PoolKey fee. It must show a difference between the effective settlement fee and the fee a reasonable caller could observe before execution, with evidence of the relevant UI, quote, calldata, or hook return behavior.

## 7. Farming and LP research

The farming subsystem calculates position and strategy performance from block-pinned snapshots. It supports concentrated-liquidity ranges, active liquidity, fee growth, inventory exposure, price movement, and rebalancing.

Every result includes:

- capital and units;
- start/end block and timestamps;
- gross fees;
- protocol and hook-adjusted fees where measurable;
- impermanent loss methodology;
- gas and rebalancing assumptions;
- adverse-selection and inventory-risk assumptions;
- confidence interval or scenario range;
- dataset and code version hashes.

The baseline comparison includes the observed target-hook strategy, a static-fee strategy, and the proposed transparent dynamic-fee strategy. Calibration and evaluation periods are separated. Stress tests include volatility spikes, one-sided flow, fee changes, RPC gaps, and failed rebalances.

## 8. Transparent dynamic-fee strategy

The reference strategy optimizes LP net yield using observable pool conditions:

```text
fee = clamp(
  base_fee
  + volatility_premium
  + inventory_imbalance_premium
  + adverse_selection_premium
  - rebalancing_incentive,
  min_fee,
  max_fee
)
```

The production-safe hook design requires:

- immutable or timelocked parameters;
- on-chain minimum and maximum fee bounds;
- bounded per-block fee changes;
- a public view of the effective fee for a PoolKey and direction;
- deterministic inputs that a quote provider can reproduce;
- events for configuration and effective-fee changes;
- no wallet-identity or balance-based discriminatory pricing;
- no private hook delta fee collection;
- standard PoolManager fee accounting.

The backtester must also include a deliberately adversarial **local-only** undisclosed-fee fixture to measure trader-cost transfer and detect when quote/UI assumptions fail. The fixture uses synthetic traders, has no signer, and is unreachable from the broadcast path.

## 9. Arbitrage research and execution state machine

Arbitrage opportunities are immutable research records containing:

```text
path
required capital
expected output
expected profit
worst-case profit
fee bound
slippage bound
price impact
gas estimate
latency budget
confidence
expiry block
```

The state machine is:

```text
observe -> quote -> simulate -> risk_check -> approve -> sign -> broadcast -> verify
```

Default mode is `observe`. `simulate` may construct calldata but cannot sign or broadcast. `approve` emits a complete transaction summary for an explicit operator decision. `execute` requires explicit configuration, an external signer, allowlisted contracts/tokens, successful block-pinned simulation, bounded fees/slippage/gas/capital, a non-expired opportunity, and post-receipt accounting verification.

The execution module refuses:

- unknown chain IDs or PoolManagers;
- bytecode changes after analysis;
- unresolved dynamic fees or unbounded hook deltas;
- failed or stale simulations;
- non-allowlisted targets;
- unlimited approvals;
- non-closed token/accounting reconciliation;
- opportunities below the configured net-profit safety margin;
- transactions whose post-state cannot be verified.

The system never logs private keys or raw signing material. Signer integration is an interface; the initial implementation provides a dry-run signer and a test signer only.

## 10. Risk model and safety gates

Risk levels are `LOW`, `MEDIUM`, `HIGH`, `CRITICAL`, and `BLOCKED`. `HIGH`, `CRITICAL`, and `BLOCKED` records cannot reach broadcasting without a separately reviewed policy override, and the default policy contains no override for unresolved accounting or undisclosed fee behavior.

Mandatory limits include:

- maximum capital per opportunity and session;
- maximum effective fee and fee change per block;
- maximum price impact and slippage;
- maximum gas and minimum net profit;
- maximum inventory and time in position;
- opportunity expiry and nonce consistency;
- contract, token, and chain allowlists;
- bytecode-hash pinning;
- post-trade balance and event reconciliation.

All policy decisions are structured, deterministic, and persisted with the evidence used.

## 11. Testing and quality gates

Unit tests cover PoolId, fee encoding, permission bits, signed deltas, event topics, metadata extraction, selector extraction, integer amount math, and risk-policy decisions.

Property tests cover fee bounds, accounting closure, exact-input/output invariants, expiry, allowlist enforcement, no-broadcast-on-failed-simulation, and numeric overflow boundaries.

Integration tests use a local EVM fork or deterministic test fixtures for:

- target-pool initialization and event decoding;
- both swap directions;
- dynamic fee changes;
- empty and non-empty hook data;
- fee override return data;
- hook reverts;
- gas estimation;
- receipt verification;
- token and PoolManager reconciliation.

The repository gate is:

```text
ruff check
ruff format --check
mypy
pytest
coverage threshold
```

No commit or completion claim is made without the project test command and its output.

## 12. Initial deliverables and sequence

1. Project metadata, configuration schema, typed models, errors, logging, and test gate.
2. RPC client, evidence store, block-pinned reads, and health checks.
3. EVM bytecode/metadata/selector/permission analyzer.
4. v4 PoolKey, PoolManager events, dynamic-fee, and reconciliation modules.
5. Reproducible report for the benchmark pool and hook.
6. Farming and LP net-yield accounting with backtests.
7. Transparent dynamic-fee strategy simulator and local-only adversarial fixture.
8. Arbitrage scanner, route model, simulator, and risk gate.
9. Dry-run execution state machine and receipt verifier.
10. Local-fork integration suite; production execution remains opt-in and disabled by default.

## 13. Acceptance criteria

The first milestone is accepted when:

- a clean environment can install and run the project from the repository;
- the benchmark report is regenerated deterministically from pinned evidence;
- all benchmark conclusions have evidence levels and bytecode/data hashes;
- dynamic-fee and accounting behavior are tested in both swap directions;
- LP net yield is reported net of stated costs and validated out of sample;
- the transparent strategy beats the chosen baseline under the documented dataset and risk constraints, or the report explicitly records that it does not;
- no test, example, or default command can broadcast a mainnet transaction;
- lint, type checks, tests, and coverage pass through the project build command.
