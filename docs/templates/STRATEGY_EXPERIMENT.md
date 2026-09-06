# Strategy Experiment Record

Copy this file for every material hypothesis. Complete the design and acceptance fields before viewing
the final test window.

## Identity

- Experiment ID:
- Owner/reviewer:
- Code commit and dirty-tree hash:
- Dataset/evidence manifest hash:
- Chain, pools, tokens, block/time range:
- Venues/accounts/instruments and exact contract mapping:
- Created/locked timestamp:
- Related threat model/decision record:

## Hypothesis

- Economic mechanism:
- Why the edge should exist:
- Who pays for the edge and why it persists:
- Conditions where it disappears:
- Primary falsification test:

## Frozen design

- Strategy/range/fee policy:
- Observable inputs and lookback windows:
- Parameter search space and total trials:
- Capital and sizing rule:
- Execution/MEV model:
- Gas/priority/revert assumptions:
- Price source and valuation currency:
- Event/receive/process clocks and freshness limits:
- Market sessions, halts, maintenance, and reconnect policy:
- Token behavior assumptions:
- Funding/borrow/dividend/FX/corporate-action assumptions:
- Partial-fill, order-status, and hedge-state model:

## Data split

- Discovery window:
- Calibration window:
- Validation window:
- Untouched final test window:
- Walk-forward schedule:
- Purge/embargo rule:
- Missing-data/reorg policy:

## Baselines

- Hold:
- Passive LP:
- Static-fee LP:
- Same range policy without candidate feature:
- Competing pools/routes:

## Precommitted acceptance and kill criteria

- Minimum net excess return/confidence:
- Maximum drawdown/expected shortfall:
- Maximum inventory/concentration/time at risk:
- Minimum sample/volume/regime coverage:
- Maximum unexplained accounting residual:
- Capacity and gas thresholds:
- Maximum gross/net delta and unhedged-notional-time:
- Margin/liquidation and capital-transfer thresholds:
- Security/reliability gates:
- Immediate kill conditions:

## Results — fill only after design lock

- Net PnL and excess return by baseline:
- Attribution: fees / inventory / IL / LVR / gas / rebalance / failures / residual:
- Confidence, multiple-testing adjustment, and trial ledger:
- Drawdown/tail/regime/sensitivity/capacity:
- Deviations from frozen design:
- Decision: reject / revise as new experiment / promote one stage:
- Independent reviewer sign-off:
