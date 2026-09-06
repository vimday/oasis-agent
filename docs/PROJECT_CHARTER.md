# Oasis Agent Project Charter

**Status:** Active project constitution  
**Updated:** 2026-09-04  
**Phase:** 1 — Multi-chain EVM automated LP with Uniswap v4 hooks; Robinhood initial benchmark

## 1. Vision

Oasis Agent is intended to become a safe, autonomous on-chain capital-allocation system. It should
continuously discover, validate, execute, and improve profitable strategies while surviving hostile
contracts, adversarial order flow, bad data, reorgs, infrastructure failures, and changing market
regimes.

Long-term strategy families are:

1. automated concentrated-liquidity provision;
2. atomic on-chain and inventory-backed spot/perpetual arbitrage;
3. perp-perp and funding/basis arbitrage across decentralized venues;
4. equity/equity-future versus on-chain equity-perp arbitrage;
5. tightly risk-bounded short-horizon trading in meme assets;
6. cross-strategy capital allocation based on marginal risk-adjusted return.

The aspiration is an "on-chain automatic money machine": systematic, always measuring, and highly
automated. There is no guaranteed-profit assumption. The system earns the right to deploy capital
only when a measured edge survives costs, adversarial testing, and explicit loss limits.

## 2. North-star objective

Maximize long-run compounded net asset value subject to a hard survival constraint.

For any strategy `s` over evaluation window `T`:

```text
net_pnl(s, T) = realized trading and LP revenue
              + mark-to-market inventory change
              - protocol and hook fees
              - impermanent loss / LVR
              - adverse-selection loss
              - gas and priority fees
              - rebalancing and hedging costs
              - failed-transaction costs
              - financing and opportunity costs
```

```text
excess_return(s, T) = net_return(s, T) - net_return(baseline, T)
```

Optimization is constrained by maximum drawdown, tail loss, capital at risk, inventory exposure,
contract risk, data confidence, execution reliability, and strategy capacity. Gross fees, nominal
APR, win rate, and simulated profit are diagnostic metrics—not objectives.

## 3. Phase 1 objective

Build and validate an automated LP system for an explicitly selected EVM Uniswap v4 pool.
Robinhood Chain remains the initial benchmark; Base, Ethereum and other EVM chains are eligible
research targets, with independent deployment, cost, finality, routing and promotion evidence. The strategy uses
a transparent hook to adapt LP fees and, later, range/inventory management from reproducible public
state. It must improve net LP performance relative to comparable passive and static-fee positions.

Initial benchmark context:

- Chain ID: `4663`
- Pool ID: `0x374531f25cfb1b92f6d03b946598d53dd934fba6e138bcece153a52113aa73a5`
- PoolManager: `0x8366a39cc670b4001a1121b8f6a443a643e40951`
- Observed hook: `0x1191Ac2561817686EFE4296B37BA5a5419Eaa880`

The complete Phase 1 execution, experiment, routing, range, keeper, and release sequence is specified
in `docs/strategies/ROBINHOOD_V4_AUTOLP_BLUEPRINT.md`. The observed pool is a benchmark, not the
presumptive production pool: its hook source, liquidity-access policy, token risks, and routing must
pass the blueprint's evidence gates.

These identifiers are research targets, not automatic authorization to transact. Addresses,
bytecode hashes, permission bits, PoolKey, ownership, and upgrade state must be revalidated against
block-pinned chain evidence.

### Strategy thesis

A static fee forces LPs to accept the same price for liquidity in quiet, toxic, balanced, and
one-sided regimes. A deterministic dynamic fee may create excess return by charging more when
expected LVR/adverse selection is high and less when attracting benign flow is valuable.

A candidate fee policy is conceptually:

```text
fee = clamp(
    base_fee
    + volatility_premium
    + direction_aware_inventory_premium
    + adverse_selection_premium
    - benign_flow_incentive,
    min_fee,
    max_fee,
)
```

This is a hypothesis, not proof of alpha. Fee increases may reduce volume or route flow elsewhere.
The experiment must therefore jointly model fee income, flow elasticity, inventory, LVR, gas, and
competitor pools.

### Required baselines

Every evaluation uses the same capital, token prices, block window, range, flow, and cost model for:

- passive buy-and-hold inventory;
- passive concentrated LP with a static fee;
- identical range/rebalance policy without the dynamic-fee component;
- the candidate hook strategy;
- when measurable, the observed benchmark hook strategy.

The incremental contribution of fee policy and range management must be reported separately.

## 4. Phase 1 workstreams

### A. Evidence and market reconstruction

- Acquire blocks, logs, calls, receipts, traces where available, token metadata, and bytecode from
  at least two independent RPC sources for decision-critical observations.
- Pin historical reads to explicit blocks and model reorg/finality behavior.
- Store canonical raw evidence content-addressably without collapsing records from different
  chains, blocks, kinds, or sources.
- Reconstruct PoolKey/PoolId, liquidity, ticks, swaps, fee changes, LP positions, and token flows.
- Maintain separate token0/token1 ledgers and value them only with explicit prices and timestamps.

### B. Accounting and attribution

- Reconcile every swap and position event across PoolManager deltas, pool state, hook deltas,
  protocol fees, LP fees, ERC-20 transfers, and transaction gas.
- Define signs and units in types and tests.
- Attribute PnL into fee income, inventory change, IL/LVR, adverse selection, rebalancing, hedging,
  gas, execution failures, and unexplained residual.
- Any unexplained residual blocks profitability claims and execution promotion.

### C. Strategy research

- Use walk-forward or otherwise leakage-resistant calibration/evaluation splits.
- Model both swap directions and fee-volume elasticity.
- Stress volatility jumps, price gaps, one-sided flow, low liquidity, fee competition, RPC gaps,
  delayed keepers, reverted swaps, and parameter changes.
- Report distributional results: confidence interval, drawdown, tail loss, turnover, gas burden,
  capacity, regime dependence, and sensitivity to every important parameter.
- Pre-register falsification thresholds before treating an experiment as validation.

### D. Hook and keeper implementation

- Build a minimal, deterministic Solidity hook against the canonical v4 interfaces.
- Authenticate PoolManager callbacks and validate PoolKey, hookData, fee bounds, direction, and
  returned delta semantics.
- Publish an effective-fee quote path that routers can reproduce.
- Use immutable or timelocked bounded parameters with observable events and an emergency pause.
- No wallet discrimination, hidden fee, private hook delta, arbitrary token extraction, or unsafe
  upgrade authority.
- Keepers must be idempotent, bounded, replay-safe, observable, and unable to bypass risk policy.

### E. Verification and operations

- Unit/property tests for decoding, math, accounting, strategy, and risk policy.
- Solidity fuzz/invariant tests and static analysis.
- Pinned local-fork tests covering initialization, liquidity changes, both swap directions, exact
  input/output, fee override, callbacks, reverts, malicious tokens, and post-state reconciliation.
- Shadow decisions against live data without signing.
- Canary deployment with minimal capital, hard daily loss, exposure, gas, and drawdown limits.
- Automatic halt on stale/inconsistent data, changed bytecode, accounting residuals, invariant
  failures, unexpected ownership changes, or breached risk limits.

## 5. Promotion gates

```text
fixture
  -> historical replay
  -> pinned local fork
  -> live shadow mode
  -> minimal-capital canary
  -> bounded production
  -> gradual capital scaling
```

Each arrow requires a written evidence report. A later stage cannot substitute for a failed earlier
gate, and strong in-sample profit cannot waive security or reconciliation requirements.

### Minimum quantitative acceptance criteria

Exact thresholds belong in a versioned strategy configuration and must be chosen before the final
evaluation. At minimum, production promotion requires:

- positive out-of-sample net excess return over every declared comparable baseline;
- confidence bounds and sample size sufficient to distinguish edge from noise;
- drawdown and stress losses below the approved risk budget;
- zero unexplained accounting residual above explicit base-unit tolerances;
- no critical/high unresolved security findings;
- reliable quote/simulation/receipt reconciliation across both swap directions;
- demonstrated ability to halt and safely unwind.

If the candidate fails, the correct output is a rejection report—not modified metrics or relaxed
tests after observing the result.

## 6. Capital and execution safety

Production execution must enforce all of the following independently of strategy recommendations:

- chain, PoolManager, hook, router, token, quoter, and bytecode-hash allowlists;
- per-transaction, per-position, per-token, per-strategy, and per-day capital limits;
- minimum worst-case net return and maximum fee/slippage/price-impact/gas limits;
- inventory, range, concentration, drawdown, loss-rate, and time-in-position limits;
- fresh block/simulation requirements, expiry block, nonce consistency, and reorg policy;
- exact or tightly bounded approvals; no blanket approval by default;
- external signing policy with keys unavailable to research processes;
- receipt, event, balance, and position reconciliation after execution;
- a kill switch independent of the strategy process.

No single model output may directly cause an unrestricted transaction. Policy enforcement must be
deterministic, reviewable, and fail closed.

Phase 1 custody follows `docs/security/CUSTODY_AND_AGENT_KEY_MODEL.md`: begin with a user-controlled,
externally held whitelisted LP position and no agent signing key. Mature autonomous canaries use a
different, minimally funded, revocable agent account with no Treasury pull authority. Hook governance,
Treasury, manual LP ownership, agent operation, emergency control, and revenue custody remain separate.

## 7. Security and market threat model

Assume exposure to:

- incorrect ABI/event/fee/delta decoding and numeric overflow or sign errors;
- malicious, fee-on-transfer, rebasing, callback-capable, or non-conforming tokens;
- hook reentrancy, spoofed callbacks, unsafe hookData, bad permissions, and upgrade/admin capture;
- stale/manipulated prices, flash-loan state distortion, low-liquidity oracle windows;
- sandwiching, backrunning, JIT liquidity, toxic flow, private-order-flow selection, and censorship;
- RPC lies/disagreement, missing logs, rate limits, stale heads, reorgs, and sequencer outages;
- simulation/landing divergence, nonce races, replacement transactions, fee spikes, and partial
  execution;
- model overfitting, look-ahead leakage, survivorship bias, regime shift, and underestimated costs;
- operational compromise, leaked secrets, dependency attacks, and unsafe configuration changes.

Every production feature must state which threats it addresses and which remain.

## 8. Roadmap beyond Phase 1

The shared target architecture is defined in
`docs/architecture/AUTONOMOUS_TRADING_SYSTEM.md`. Strategy-specific economic and operational rules
live under `docs/strategies/`. The intentionally coarse cross-strategy ordering and dependency plan
is in `docs/strategies/LONG_TERM_PROFIT_ROADMAP.md`; Phase 1 remains the only implementation priority.

### Phase 2 — On-chain and crypto-venue arbitrage

Add atomic-first opportunity discovery and execution with complete route simulation, fee/gas/MEV
costs, expiry, revert protection, allowlisted targets, and post-trade reconciliation. Then add
shadow-only spot-perp, perp-perp, and funding/basis research before building non-atomic hedge
coordination. Never execute an opportunity whose stressed worst-case net result is below the safety
margin.

### Phase 3 — Equity–crypto cross-market arbitrage

Add regulated-broker and direct-market-data adapters as separately permissioned capabilities. Model
market sessions, halts, borrow/locate, dividends/corporate actions, contract multipliers, FX,
settlement, margin, funding, liquidation, and capital-transfer time. Promotion requires independent
position reconciliation and a hedge-group state machine that fails closed on stale or unknown state.
See `docs/strategies/PERP_CROSS_MARKET_ARBITRAGE.md`.

### Phase 4 — Meme short-horizon trading

Treat this as the highest-risk strategy family. Before live use, require token-contract scanning,
sellability simulation, holder/liquidity concentration analysis, mint/blacklist/tax/proxy authority
checks, liquidity-lock evidence, strict position sizing, bounded approvals, and automatic exits.
Never manipulate markets, manufacture volume, mislead traders, or coordinate pump-and-dump activity.

### Phase 5 — Portfolio allocator

Allocate capital across validated strategies using marginal risk-adjusted return, correlation,
liquidity, capacity, and tail exposure. A strategy can be profitable alone yet receive zero capital
when it worsens portfolio survival.

## 9. Current implementation map

| Area | Current package | Required destination |
|---|---|---|
| RPC | `src/oasis_agent/rpc` | Redundant, block-pinned, reorg-aware acquisition |
| Evidence | `src/oasis_agent/evidence` | Context-preserving immutable provenance |
| EVM analysis | `src/oasis_agent/evm` | Verified bytecode/permissions/control-plane analysis |
| v4 primitives | `src/oasis_agent/uniswap_v4` | Canonical ABI, fee, PoolId, delta, state reconstruction |
| LP research | `src/oasis_agent/farming` | Full replay, attribution, baselines, walk-forward tests |
| Arbitrage | `src/oasis_agent/arbitrage` | Later-phase route simulator and atomic executor |
| Risk | `src/oasis_agent/risk` | Deterministic layered capital and execution policy |
| Execution | `src/oasis_agent/execution` | Simulate/approve/sign/broadcast/verify with fail-closed gates |
| Reporting | `src/oasis_agent/reporting` | Evidence-linked performance and security reports |

This table is directional. It does not assert that the destination capabilities already exist.

## 10. Definition of done

A task is done only when:

- behavior and safety invariants are explicit;
- implementation, tests, documentation, and configuration agree;
- focused and full quality gates pass without skipped tooling;
- chain-facing claims cite pinned evidence;
- financial claims specify units, costs, benchmark, window, uncertainty, and limitations;
- the diff contains no secrets or unauthorized state changes;
- remaining risks and assumptions are documented.

The project is successful not when it trades often, but when it can identify real edge, deploy only
when safe, account for every unit of value, stop quickly when wrong, and improve without hiding
losses or uncertainty.

## 11. Practice basis

This charter is operationalized by `AGENTS.md`, path-scoped `.claude/rules/`, `REVIEW.md`, and the
strategy experiment template. Their external practice basis and project-specific adaptations are
listed in `docs/references/ENGINEERING_SOURCES.md`. Canonical protocol source outranks secondary
articles for v4 semantics; exact checked-out dependency commits outrank moving `main` branches.

The control ownership, retained evidence, release decisions, and present code maturity map are
normatively defined in `docs/governance/PRODUCTION_CONTROL_FRAMEWORK.md`. Phase 1 deployment evidence
must satisfy `docs/security/V4_HOOK_PRODUCTION_STANDARD.md`; a checklist item is not complete merely
because it is mentioned in an agent instruction.

Agent instructions are advisory context, not a security boundary. Production controls must be
enforced independently through typed validation, executable invariants, CI, least-privilege signing,
deployment manifests, monitoring, and the incident response runbook.
