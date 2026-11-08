# Oasis Agent

Oasis Agent researches and operates bounded on-chain strategies with replayable evidence,
independent risk checks, and explicit signing authority. The long-term objective is risk-adjusted
net NAV growth after all costs. The first research program is transparent Uniswap v4 LP, with
Robinhood as the benchmark and Base as the current bounded operating target.

## What you can use today

| Track | Implemented boundary |
| --- | --- |
| Offline LP research | Replay pinned inputs, attribute token and gas movements, evaluate a shadow decision, and prepare bounded calldata for simulation. |
| Base ETH/USDC v4 LP | The operator can run a release-gated Docker canary for one to three ticks. Historical evidence includes a two-tick hold and verified shutdown. Every new run needs current evidence and authorization. |
| Base autonomous spot | Shadow discovery, entry and exit decisions, a paper book, deterministic replay, and historical receipt review. GMGN is an optional check on OKX data. FOMO is an integration slot. The spot execution entry refuses live trading. |
| Dashboard | Read-only financial evidence and status, plus separately authenticated observation and shadow campaigns. |
| Later strategies | Atomic arbitrage, hedging, market making, and portfolio allocation are research and architecture tracks. |

See the [current capability and evidence index](docs/CURRENT_IMPLEMENTATION.md) for exact
acceptance results and open gates. No strategy has demonstrated sustained net profitability.

## Start locally

Use Python 3.12, `uv`, Node.js 22, and Foundry 1.8.1 as pinned by
[CI](.github/workflows/ci.yml). Install the locked dependencies and run the offline demo.

```bash
make install
make report
```

Open `reports/benchmark-hook-analysis.md` for the offline fixture report. Install locked
contract dependencies with `make contract-install` before running Solidity tests.
The full `make run` workflow needs its retained evidence and hash manifests. Follow the
[offline workflow guide](docs/operations/ONE_COMMAND_WORKFLOW.md) to supply those inputs
and interpret its exit status.

Start the local dashboard with `make run-server` and open
[localhost:8666](http://127.0.0.1:8666). The default loopback configuration reads retained
journals. Configure the operations token and approved sources before running a campaign.

## Choose an entry point

| Task | Entry point |
| --- | --- |
| Inspect static Base LP release and Docker readiness | `make live-docker-readiness`. Runs offline. A local check pass leaves runtime admission unresolved. |
| Inspect the dedicated Base LP account | `make live-check`. Uses the configured RPC pair. Run only with a current request allowance. |
| Recover a pending Base LP transaction | Follow the [release and recovery guide](docs/operations/LIVE_RELEASE.md). Recovery can read chain data and update existing accounting. |
| Run an authorized bounded Base LP canary | Follow the [Base runbook](docs/operations/BASE_BOUNDED_RUN.md) for preflight, original ledgers, STOP recovery, and post-run verification. |
| Run or replay Base spot shadow | Follow the [spot runbook](docs/operations/BASE_SPOT_SHADOW.md) for provider scope, replay, and receipt verification. |

## Project and contribution

- [Charter](docs/PROJECT_CHARTER.md), [Phase 1 LP blueprint](docs/strategies/ROBINHOOD_V4_AUTOLP_BLUEPRINT.md),
  and [long-term roadmap](docs/strategies/LONG_TERM_PROFIT_ROADMAP.md) define objectives and gates.
- [Control framework](docs/governance/PRODUCTION_CONTROL_FRAMEWORK.md),
  [custody model](docs/security/CUSTODY_AND_AGENT_KEY_MODEL.md), and [incident response](docs/runbooks/INCIDENT_RESPONSE.md)
  govern execution and recovery.
- [Contributing](CONTRIBUTING.md), [AGENTS.md](AGENTS.md), and [review policy](REVIEW.md) define
  development checks and review. [SECURITY.md](SECURITY.md) covers private vulnerability reports.

`src/oasis_agent/` holds acquisition, evidence, strategies, execution, risk, and the dashboard.
`contracts/` contains the Solidity hook and executor, and `scripts/` exposes operator and
research entry points. Dated verification reports retain what ran, against which inputs, with
which outcome. Match their scope to the claim you are checking.
