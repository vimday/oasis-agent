# Engineering Practice Sources

These sources are normative inputs to the repository rules. Pin exact dependency commits in build and
evidence manifests; links below identify upstream authorities, not dependency versions.

## Uniswap v4

- [v4-core Hooks library](https://github.com/Uniswap/v4-core/blob/46c6834698c48bc4a463a86d8420f4eb1d7f3b75/src/libraries/Hooks.sol): hook
  address flags, callback invocation, response shape, return deltas, and fee override handling.
- [v4-core LPFeeLibrary](https://github.com/Uniswap/v4-core/blob/46c6834698c48bc4a463a86d8420f4eb1d7f3b75/src/libraries/LPFeeLibrary.sol):
  dynamic and override flags, canonical mask, and `MAX_LP_FEE`.
- [v4-core ProtocolFeeLibrary](https://github.com/Uniswap/v4-core/blob/46c6834698c48bc4a463a86d8420f4eb1d7f3b75/src/libraries/ProtocolFeeLibrary.sol):
  directional fee packing and combined swap-fee formula.
- [v4-core PoolManager](https://github.com/Uniswap/v4-core/blob/46c6834698c48bc4a463a86d8420f4eb1d7f3b75/src/PoolManager.sol): singleton,
  unlock/settlement, hook order, dynamic fee update, events, and accounting behavior.
- [v4-core repository](https://github.com/Uniswap/v4-core): canonical contracts, tests, architecture,
  license, and integration examples.
- [v4-periphery IPositionManager](https://github.com/Uniswap/v4-periphery/blob/dce236d4e2057422d0791d9a973a58765eb46f65/src/interfaces/IPositionManager.sol),
  [StateView](https://github.com/Uniswap/v4-periphery/blob/dce236d4e2057422d0791d9a973a58765eb46f65/src/lens/StateView.sol), and
  [PositionInfoLibrary](https://github.com/Uniswap/v4-periphery/blob/dce236d4e2057422d0791d9a973a58765eb46f65/src/libraries/PositionInfoLibrary.sol):
  NFT/core-position identity, packed range metadata, state reads, and fee-growth checkpoints.
- [v4-core TickMath](https://github.com/Uniswap/v4-core/blob/46c6834698c48bc4a463a86d8420f4eb1d7f3b75/src/libraries/TickMath.sol) and
  [SqrtPriceMath](https://github.com/Uniswap/v4-core/blob/46c6834698c48bc4a463a86d8420f4eb1d7f3b75/src/libraries/SqrtPriceMath.sol):
  exact tick/price conversion, liquidity amount formulas, bounds, and rounding.
- [Uniswap v4 hook concepts](https://developers.uniswap.org/docs/protocols/v4/concepts/hooks): pool-hook
  binding, lifecycle callbacks, address-encoded permissions, CREATE2 deployment, and return-delta surface.
- [Official v4 hook repository](https://github.com/Uniswap/v4-hooks-public): maintained reference hook
  implementations and published audit material; patterns still require commit pinning and local review.
- [Uniswap dynamic-fee documentation](https://developers.uniswap.org/docs/protocols/v4/concepts/dynamic-fees):
  pool-creation dynamic flag, periodic `updateDynamicLPFee`, per-swap overrides, and manipulation/gas
  considerations.
- [Uniswap ALF interface](https://github.com/Uniswap/v4-hooks-public/blob/main/src/alf/interfaces/IALFHook.sol):
  bounded-gas indicative quote, liveness, hook metadata, and swap-to-price routing surface.
- [Uniswap ALF fee library](https://github.com/Uniswap/v4-hooks-public/blob/main/src/alf/libraries/FeeLib.sol):
  one shared effective-fee implementation for quote and execution, including directional protocol fee.
- [Uniswap permissioned-pool hook](https://github.com/Uniswap/v4-hooks-public/blob/main/src/permissioned-pools/PermissionedHooks.sol):
  official reference surface for gating pool operations; its existence does not remove the need to
  disclose restrictions, minimize callbacks, preserve removal, and test sender semantics.

Adopted practice: protocol-compatible behavior is derived and differential-tested against a pinned
canonical commit, not re-created from memory. Quote and settlement share exact fee code and rounding;
new hook pools must prove router reach rather than assuming protocol compatibility creates volume.

## Robinhood Chain

- [Connecting to Robinhood Chain](https://docs.robinhood.com/chain/connecting/): chain IDs, ETH gas,
  production providers, archive access, public RPC limitations, sequencer feed, and sequencer endpoint.
- [Differences from Ethereum](https://docs.robinhood.com/chain/differences-from-ethereum/): Arbitrum
  `block.number`, ArbSys L2 height, constant `prevrandao`, L1-data gas, address aliasing, staged
  finality, compliance filtering, and first-come-first-served transaction ordering.
- [Transaction finality](https://docs.robinhood.com/chain/transaction-finality/): sequencer soft
  confirmation, L1 posting, Ethereum finality, reorganization assumptions, and withdrawal-delay
  distinction.
- [Gas and fees](https://docs.robinhood.com/chain/gas-and-fees/): L2 execution plus L1 calldata fee and
  calldata/batching implications.
- [Data Streams](https://docs.robinhood.com/chain/data-streams/): signed pull-based high-frequency data
  availability; individual asset/feed support still requires explicit verification.
- [Governance](https://docs.robinhood.com/chain/governance/): Security Council thresholds/timelock and
  current BoLD validator model.
- [Bridging](https://docs.robinhood.com/chain/bridging/): canonical and third-party routes, with the
  canonical L2-to-L1 challenge-period implication for liquidity planning.
- [UniswapX Robinhood chain playbook](https://github.com/Uniswap/UniswapX/blob/main/playbook/chains/robinhood.md):
  official integration notes for PoolManager, v4 quoting/routing, Arbitrum block-number semantics,
  Permit2, gas, and on-demand block-production caveats.

Adopted practice: use timestamp-based elapsed-time controls, ArbSys only for actual L2 height, two
production RPC providers plus archive evidence, tiered finality, isolated ETH reserves, and explicit
router validation. Public RPC, a soft receipt, or generic Data Streams availability is never promoted
into a stronger production assumption.

## Smart-contract security

- [Trail of Bits — Building Secure Contracts](https://github.com/crytic/building-secure-contracts):
  secure workflow, maturity criteria, token integration, incident response, fuzzing, and analysis tools.
- [Trail of Bits secure development workflow](https://github.com/crytic/building-secure-contracts/blob/master/development-guidelines/workflow.md):
  Slither per change, documented properties, fuzz/symbolic evaluation, manual review of MEV and DeFi risk.
- [Trail of Bits token integration checklist](https://github.com/crytic/building-secure-contracts/blob/master/development-guidelines/token_integration.md):
  non-standard tokens, privileged behavior, callbacks, fee-on-transfer, and concentration risks.
- [OpenZeppelin access-control guidance](https://docs.openzeppelin.com/contracts/5.x/access-control):
  least privilege and delayed administration with an explicit warning that timelock configuration has
  its own availability and governance tradeoffs.
- [Safe smart-account modules](https://docs.safe.global/advanced/smart-account-modules): modules can
  add narrowly scoped automation but are security-critical because enabled modules may execute Safe
  transactions; every module requires explicit authority review and transaction-policy tests.
- [NIST SSDF 1.1](https://csrc.nist.gov/pubs/sp/800/218/final): prepare, protect, produce secure software,
  and respond to vulnerabilities across the development lifecycle.

Adopted practice: define security properties before tools, test them statefully, minimize authority,
review token assumptions, secure the build chain, and plan incident response before launch.

## Quantitative LP research

- [Automated Market Making and Loss-Versus-Rebalancing](https://arxiv.org/abs/2208.06046): LVR as a
  central adverse-selection cost of AMM liquidity provision.
- [Optimal Fees for Liquidity Provision in Automated Market Makers](https://arxiv.org/abs/2508.08152):
  fee/volume/adverse-selection tradeoff under cross-venue routing and evidence for robust
  threshold-style fee schedules.
- [Optimal Dynamic Fees in Automated Market Makers](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5278863):
  dynamic fee regimes and inventory/external-price-based approximations; this is a research input,
  not a directly deployable controller.
- [Automated Market Making and Arbitrage Profits in the Presence of Fees](https://arxiv.org/abs/2305.14604):
  how fees and discrete block arrival affect arbitrage profit and LP adverse selection.
- [Mitigating Adverse Selection in Concentrated Liquidity AMMs with Dynamic Fees](https://arxiv.org/abs/2606.23070):
  agent-based evidence that dynamic fees may compensate stale-price risk while not necessarily
  reducing LVR itself.
- [Bailey et al. — Effects of Backtest Overfitting](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2308659):
  why selecting among repeated backtests can manufacture apparent performance that fails out of sample.

Adopted practice: evaluate LP returns net of LVR and execution costs; explicitly model routing demand
and competition; begin with bounded threshold/inventory/impact candidates; never claim that more fee
income automatically reduced LVR; isolate untouched test periods, record all trials, and make
promotion thresholds immutable before final evaluation.

## Perpetuals and cross-market execution

- [Hyperliquid WebSocket documentation](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket):
  disconnect recovery, resubscription, and snapshot behavior.
- [Hyperliquid subscriptions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions):
  book, BBO, trade, order, fill, funding, liquidation, and account-state stream semantics.
- [Hyperliquid HIP-3](https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-3-builder-deployed-perpetuals):
  deployer responsibility for market definitions, oracle operation, leverage, settlement, and
  manipulation resilience.
- [Hyperliquid funding](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding),
  [margining](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margining), and
  [liquidations](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations): venue-exact
  funding, mark/oracle, maintenance margin, and liquidation behavior.
- [Hyperliquid nonces and API wallets](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets)
  and [rate limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits):
  signer isolation, nonce/replay domains, batching, cancel capacity, and traffic limits.
- [IBKR Web API documentation](https://ibkrcampus.com/campus/ibkr-api-page/webapi-doc/): authenticated
  sessions, market-data permissions/subscriptions, timestamps, and venue constraints.
- [IBKR order submission](https://interactivebrokers.github.io/tws-api/order_submission.html): order
  status may be duplicated and intermediate callbacks are not guaranteed; executions must also be
  monitored.
- [Databento Live API](https://databento.com/docs/api-reference-live): heartbeat, slow-reader detection,
  reconnect, replay/snapshot recovery, timestamps, and explicit data-gap handling.
- [SEC Regulation SHO guidance](https://www.sec.gov/rules-regulations/staff-guidance/trading-markets-frequently-asked-questions-8):
  marking, locate, price-test, and close-out constraints relevant to equity short hedges.
- [FINRA trading-halt rule](https://www.finra.org/rules-guidance/rulebooks/finra-rules/6120) and
  [CME price-limit guidance](https://www.cmegroup.com/education/articles-and-reports/understanding-price-limits-and-circuit-breakers):
  cases where the hedge leg may be unavailable or price constrained.

Adopted practice: normalize economic contracts and venue-specific clocks/rules; treat every
non-atomic arbitrage as a partially failing distributed transaction; reconcile executions and
positions independently; stop increasing exposure whenever hedge state is stale or uncertain.

## On-chain execution and MEV

- [Flashbots documentation](https://docs.flashbots.net/): private transaction and bundle submission,
  simulation, inclusion, and MEV-aware execution interfaces.
- [Chainlink documentation](https://docs.chain.link/): oracle/data-feed, high-frequency data-stream,
  sequencer-uptime, and automation integration references.
- [Chainlink feed selection and risk mitigation](https://docs.chain.link/data-feeds/selecting-data-feeds):
  source/methodology assessment, market hours, deprecation, circuit breakers, freshness, independent
  references, soak testing, and explicit safeguards for single-source feeds.
- [Chainlink L2 sequencer feeds](https://docs.chain.link/data-feeds/l2-sequencer-feeds): sequencer-outage
  detection and recovery/grace-period considerations for L2 applications.
- [dYdX integration documentation](https://docs.dydx.xyz/): a second official perpetual-venue
  reference for market/account streams and automated order management.

Adopted practice: private submission is a risk-control option, not an inclusion or privacy guarantee;
oracle/mark/index/executable prices remain distinct; venue adapters must encode venue-specific
semantics behind a common conformance contract.

## Field reports supplied by the project owner

- [CBB — equity-perp arbitrage field report](https://x.com/Cbb0fe/status/2095250548744405297):
  reported maker/hedge parameters, capital-aware entry thresholds, direct-feed latency work, temporary
  edge compression, and a severe stale-position-data/repeated-hedge incident.
- [SUOHA.AI — active LP thread collection](https://x.com/SUOHA_AI/status/2095001169793814891):
  practitioner hypotheses about finite fee windows, directional/single-sided LP, range exits,
  rebalancing cost, pool selection, and MEV precautions.

These posts are unverified practitioner reports, not normative protocol or profitability evidence.
The project adopts their testable failure modes and hypotheses, rejects production experimentation
and universal APR/loss shortcuts, and validates every claim independently before use.

## Reliability, AI instructions, and supply chain

- [Google SRE production practices](https://sre.google/sre-book/service-best-practices/): SLO/error-budget
  release policy, actionable alerts, and blameless postmortems.
- [Google Canary Analysis Service](https://research.google/pubs/canary-analysis-service/): partial,
  time-bounded production changes evaluated against meaningful metrics before roll-forward or rollback.
- [Google blast-radius partitioning](https://research.google/pubs/avoid-global-outages-by-partitioning-cloud-applications-to-reduce-blast-radius/):
  isolation of failure domains alongside canary deployment.
- [Claude Code project memory](https://code.claude.com/docs/en/memory): concise root instructions,
  `AGENTS.md` import, and path-scoped `.claude/rules/`.
- [Claude Code review policy](https://code.claude.com/docs/en/code-review): repository-specific
  `REVIEW.md` severity and review instructions.
- [GitHub Actions secure use](https://docs.github.com/en/actions/reference/security/secure-use): least
  privilege, secret protection, and immutable full-SHA action pinning.

Adopted practice: concise always-on context, domain rules loaded by path, evidence-based review,
measured reliability budgets, actionable operations, and immutable CI dependencies.

## Translation rule

Sources are not collected for prestige. Each adopted practice must map to a project control, code or
operational enforcement point, retained evidence, and a failure test. The normative mapping lives in
`docs/governance/PRODUCTION_CONTROL_FRAMEWORK.md`; Phase 1 protocol-specific application lives in
`docs/security/V4_HOOK_PRODUCTION_STANDARD.md`. If a source cannot be translated into a falsifiable or
auditable requirement, it remains background reading rather than a production claim.
