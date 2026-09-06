# Oasis Agent

Oasis Agent is an evidence-driven on-chain research and execution system for liquidity provision,
arbitrage, and later tightly bounded short-horizon strategies.

The current Phase 1 objective is to develop a transparent Uniswap v4 hook and automated LP system
on explicitly selected EVM chains that produces measurable risk-adjusted excess return over passive
and static-fee baselines. Robinhood is the initial benchmark; Base and Ethereum are also research
targets. Configurations must explicitly set `chain_id` (or `OASIS_CHAIN_ID`); no network is implied.

This repository is currently a research and safety foundation with Python replay/accounting
primitives and a minimal Solidity hook prototype. It is **not** a production hook, autonomous
executor, or demonstrated profitable system. Fixture reports, unit tests, and local canonical-core
integration tests must not be interpreted as Robinhood fork validation or live profitability.

## Start here

- [Multi-chain LP and settlement evidence](docs/architecture/MULTICHAIN_LP_EVIDENCE.md): network
  boundaries, receipt/trace adapter, testnet probe and remaining owner-settlement work.

- [Latest multi-chain and testnet verification](docs/verification/2026-09-06-multichain-settlement-adapter.md):
  real Base Sepolia core-delta evidence, offline regression and explicit remaining settlement/security gaps.

- [Prior local acceptance](docs/verification/2026-09-06-phase1-local-acceptance.md): raw snapshot
  reconstruction joined to retained event evidence, repeatable checks, and explicit live-funds no-go.

- [Project charter](docs/PROJECT_CHARTER.md): vision, objective function, Phase 1 plan, threat model,
  promotion gates, and roadmap.
- [Robinhood v4 Auto-LP blueprint](docs/strategies/ROBINHOOD_V4_AUTOLP_BLUEPRINT.md): the detailed
  Phase 1 pool, hook, fee, routing, range, keeper, accounting, risk, and promotion design.
- [Long-term profit roadmap](docs/strategies/LONG_TERM_PROFIT_ROADMAP.md): ordered LP, atomic
  arbitrage, perp, market-making, equity–crypto, meme, and portfolio-allocation program.
- [Autonomous trading architecture](docs/architecture/AUTONOMOUS_TRADING_SYSTEM.md): shared data,
  risk, execution, reconciliation, signer, and portfolio boundaries.
- [Active LP playbook](docs/strategies/ACTIVE_LP_PLAYBOOK.md): concentrated-liquidity math, active
  range policy, rebalancing, and v4 dynamic-fee research.
- [Perp and cross-market arbitrage](docs/strategies/PERP_CROSS_MARKET_ARBITRAGE.md): perp basis,
  equity-perp hedging, two-leg state machine, stale-data controls, and promotion criteria.
- [Production control framework](docs/governance/PRODUCTION_CONTROL_FRAMEWORK.md): enforceable control
  ownership, evidence, capital scaling, and current code maturity map.
- [v4 hook production standard](docs/security/V4_HOOK_PRODUCTION_STANDARD.md): Phase 1 identity,
  permission, fee, accounting, security, economic, and deployment gates.
- [Custody and agent key model](docs/security/CUSTODY_AND_AGENT_KEY_MODEL.md): external capital,
  LP-owner whitelist, small agent account, signer policy, revocation, and recovery stages.
- [Engineering constitution](AGENTS.md): mandatory standards for AI and human contributors.
- [Review policy](REVIEW.md): capital-aware severity and adversarial review lenses.
- [Security policy](SECURITY.md): private disclosure and response expectations.
- [Engineering sources](docs/references/ENGINEERING_SOURCES.md): authoritative practices adopted by
  the repository.
- [Strategy experiment template](docs/templates/STRATEGY_EXPERIMENT.md): precommitted research design
  and promotion record.
- [Incident response runbook](docs/runbooks/INCIDENT_RESPONSE.md): containment, evidence, recovery,
  and post-incident procedure.
- [v4 research design](docs/superpowers/specs/2026-09-03-robinhood-v4-research-execution-design.md):
  original subsystem design.
- [Initial verification](docs/verification/2026-09-02-initial-verification.md): scope and limitations
  of the offline milestone.
- [Current Phase 1 audit](docs/verification/2026-09-04-project-audit.md): document authority, reviewed
  code-change map, defects closed, open blockers, and ordered next-work queue.
- [Pool replay foundation](docs/verification/2026-09-04-pool-replay-foundation.md): block-lineage,
  event-ordering, reducer invariants, verification, and remaining replay limitations.
- [Position accounting foundation](docs/verification/2026-09-04-position-accounting-foundation.md):
  block-hash-pinned PositionManager reads, liquidity/token ledger, exact range math, accounting output,
  counterfactual comparison, and remaining live/fork blockers.
- [Hook contract foundation](docs/verification/2026-09-05-hook-contract-foundation.md): minimal
  one-pool hook, token-aware fee bands, official PositionManager lifecycle, invariant campaign, CI,
  and the remaining production blockers.
- [Replay and deployment evidence review](docs/verification/2026-09-05-evidence-binding-review.md):
  replay-value binding, provenance checks, confirmation restrictions, current verification results,
  and the next evidence/accounting tasks.
- [Latest position-change review](docs/verification/2026-09-06-position-change-review.md): review of
  the position-evidence closure commit, ownership/accounting fixes, full working-tree verification,
  remaining evidence gaps, and the next implementation acceptance criteria.

## Repository layout

```text
src/oasis_agent/
  rpc/            JSON-RPC access
  evidence/       evidence persistence
  evm/            bytecode and hook analysis
  uniswap_v4/     v4 primitives and event decoding
  farming/        LP accounting and strategy research
  arbitrage/      opportunity research
  risk/           deterministic risk decisions
  execution/      guarded state transitions
  reporting/      evidence-linked reports
```

Future target packages described by the architecture include `market_data`, `venues`, `strategies`,
`portfolio`, and an expanded `execution` layer. They do not exist yet.

## Development

Python 3.12 or newer, Node.js 22 or newer, and Foundry 1.8.1 are required.

```bash
python3 -m pip install -e '.[dev]'
make contract-install
make check
```

All production-facing work must remain non-broadcasting by default. Do not deploy, sign, broadcast,
or spend funds without explicit authorization and the promotion gates defined in the project charter.
