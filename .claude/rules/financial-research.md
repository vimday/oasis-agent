---
paths:
  - "src/oasis_agent/{farming,arbitrage}/**/*.py"
  - "src/oasis_agent/models/finance.py"
  - "tests/**/*{farming,arbitrage,finance,backtest,strategy}*.py"
  - "reports/**/*.md"
---

# Quantitative research and accounting rules

Every strategy begins with an economic mechanism and falsifiable hypothesis, not a parameter search.
Create an experiment record from `docs/templates/STRATEGY_EXPERIMENT.md` before final evaluation.

## Data and backtest integrity

- Use event-time ordering and data available at the simulated decision timestamp. Prevent look-ahead,
  survivorship, selection, and cross-validation leakage.
- Separate discovery, calibration, validation, and untouched final test windows. Use walk-forward
  evaluation; purge/embargo overlapping label or lookback windows where applicable.
- Maintain a strategy/parameter trial ledger, including failed variants. Correct conclusions for
  multiple testing; never report only the best run.
- Reconstruct executable state, not candle approximations, when pool ticks/liquidity/order flow are
  available. State assumptions when mempool or ordering data is unavailable.
- Include failed/reverted/censored transactions and RPC gaps. Never drop adverse observations merely
  because parsing, pricing, or simulation failed.

## PnL and risk

- Maintain ledgers by `(chain, account, token, token_decimals)` in integer base units. Conversion to
  a reporting currency requires timestamped price evidence and an explicit rounding policy.
- Attribute LP PnL into fees, inventory mark-to-market, IL, LVR/adverse selection, gas, priority fee,
  rebalance/hedge costs, failed execution, financing, and unexplained residual.
- Compare identical capital, time window, range policy, price path, and execution assumptions against
  hold, passive LP, static-fee LP, and relevant competing-pool baselines.
- Model fee-volume elasticity and routing loss. Higher fee income per trade is not alpha if flow leaves.
- Report sample size, turnover, exposure time, capacity, max drawdown, tail loss/expected shortfall,
  sensitivity, regime breakdown, confidence intervals, and uncertainty from unavailable data.
- Strategy acceptance must use precommitted thresholds and an untouched out-of-sample result. A failed
  threshold produces a rejection record; do not tune the threshold after seeing the result.

## Tests

Property-test conservation, unit consistency, monotonic fee bounds, exact signs, rounding direction,
zero/max values, and symmetry/asymmetry expectations for both directions. Add metamorphic tests for
token decimal changes and reporting-currency changes. Golden fixture values require source provenance.

