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

The [meme scoring research workflow](docs/strategies/MEME_SCORE_RESEARCH.md) combines retained
GMGN/OKX ranks with supplied holder/contract/liquidity assessments through a shared CLI, API and
console view. It produces simulation proposals only; authenticated live acquisition and profitability
validation remain outstanding. See the [validation record](reports/meme-score-validation-20260919.md).
The [dynamic multi-token workflow](docs/strategies/MULTITOKEN_DISCOVERY_RESEARCH.md) adds bounded
discovery, batch enrichment checks and position exits independent of current popularity.


## One command

For the current live release blockers, without RPC or signing:

```bash
make live-readiness
```

New live actions are currently blocked by incomplete engineering controls.
See [the exact release gaps and closure criteria](docs/operations/LIVE_RELEASE.md).
`make live-reconcile` only attempts to reconcile an already pending transaction;
it never constructs a signer.

For a read-only operational check of the dedicated Base canary account:

```bash
make live-check
```

This checks both configured RPC providers without contacting a signer or changing
loop state. Exit 0 means the operational snapshot passed; it is not execution
permission or profitability evidence. See [the live check and settlement
boundaries](docs/operations/LIVE_START_CHECK.md).

For the retained-input research pipeline:

```bash
make run
```

On macOS you can also double-click `Oasis.command`; it opens the result page.

The offline workflow replays pinned inputs, joins complete account gas and position accounting,
assembles a cycle, journals its Shadow decision and prepares bounded simulation calldata.
It writes `reports/runs/current/report.html` and `report.json`, including each blocked step.
Re-running the same completed run verifies its seals and reuses its result. Changed code or inputs
require a new `RUN_OUTPUT`; partial failures remain retained and are not automatically retried.
The bundled configuration uses existing market evidence; account/position inputs are still missing,
so it returns exit 3. It never requests a key, acquires data, signs or broadcasts.

Use `make verify-run` to run the complete engineering checks first. See
[workflow inputs and operation](docs/operations/ONE_COMMAND_WORKFLOW.md) for the strict input contract.

## Start here

- [Current implementation and review](docs/CURRENT_IMPLEMENTATION.md): capability map,
  evidence boundaries and unresolved release blockers. Start here for current status.
- [Project charter](docs/PROJECT_CHARTER.md) and [Phase 1 blueprint](docs/strategies/ROBINHOOD_V4_AUTOLP_BLUEPRINT.md): objectives, experiments and promotion gates.
- [Custody model](docs/security/CUSTODY_AND_AGENT_KEY_MODEL.md): capital ownership and signing authority.
- [Contributing](CONTRIBUTING.md): locked development setup and mandatory verification.
- [Console read model](docs/architecture/CONSOLE_READ_MODEL.md) and [dashboard controls](docs/operations/LP_LAUNCH_CONTROLS.md): API ownership and observation campaigns.
- [Live canary console](docs/operations/LIVE_CONSOLE.md): recorded NAV/losses, pending transactions and reconciliation references, with explicit stale/missing states.
- [LP market pipeline](docs/architecture/LP_MARKET_FEATURE_PIPELINE.md), [RPC budgets and recovery](docs/operations/ROBINHOOD_RPC_USAGE.md), and [execution preparation](docs/architecture/LP_EXECUTION_PREPARATION.md): implemented interfaces.
- [Local signing transport](docs/security/LOCAL_SIGNER_TRANSPORT.md): implemented adapter and remaining service/isolation requirements.

Dated files in `docs/verification/` and `reports/` are historical validation snapshots, not current
release approval. Retained raw evidence under `reports/evidence/` must not be deleted merely because
its block or schema is old. Future architecture documents describe targets unless explicitly marked
implemented and tied to evidence.

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

Bounded authenticated GMGN/OKX token discovery and risk capture is described in
[Authenticated token data](docs/strategies/AUTHENTICATED_TOKEN_DATA.md). This supports
read-only research and offline replay, not unattended trading or LP execution.

Python 3.12 or newer, Node.js 22 or newer, and Foundry 1.8.1 are required.

```bash
make install
make contract-install
make check
```

All production-facing work must remain non-broadcasting by default. Do not deploy, sign, broadcast,
or spend funds without explicit authorization and the promotion gates defined in the project charter.
