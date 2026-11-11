# Base LP research checkpoints

These historical checkpoints cover distinct inputs to the Phase 1 experiment. The
[experiment lock](../strategies/PHASE1_LP_EXPERIMENT_LOCK.md) defines economic acceptance.
For current canary status, see [current implementation](../CURRENT_IMPLEMENTATION.md).
Original working notes and raw artifacts are retained locally.

## Target screening and coverage

On September 27, 2026, two providers agreed on all 12 direct quote checks for each of the
hookless Base ETH/USDC and USDC/cbBTC fee-500 pools at block 51826740. The ETH/USDC pool also
passed four pinned-fork tests covering mint, swap, collect, removal, rollback on allowance
failure, and standard-router settlement across both directions and three sizes. Fork balances
and order flow were synthetic. Organic routing and two-sided admission depth remain separate gates.

The two-provider observer captured a two-block window ending at 51827557. An earlier ten-block
capture failed freshness and maturity. A historical replay with different maturity settings
produced two mature swaps. Both samples fell short of the locked evaluation window.
The first historical price acquisition was rate-limited. A later market attempt spent its
allowance before a retention timestamp error, and a separate attempt stopped on a provider
transport failure. On September 28, a two-provider hash-bound account-flow capture failed at
the second provider's block header; no inventory quorum or continuous flow proof resulted.

## Price inputs

Two providers agreed on the declared ETH/USDC inventory and price inputs at Base block
51818700. The price ratio differed from pool slot0 by approximately 493 ppm. The historical
account frame was rejected for current ledger admission. USDC/USD was stale at a later
checkpoint, so the same policy rejected that paired capture.

The October 2 campaign verified USDC metadata at 51949838 and 51950272, opening ETH/USD and
USDC/USD prices, and closing ETH/USD. Its declared Chainlink feed and proxy runtime pins came
from the September 27 reference request. Closing USDC/USD was refused after one complete
source response: the round was 3,914 seconds old under the historical 3,600-second bound.
The campaign stopped at the first refusal. The original proof and diagnostic preserve that
result, and no complete two-provider closing price or USD PnL was produced.

## Owned LP cash

On October 8, synthetic raw replay verified the single-block ERC-20 cash bridge for
manifest-declared positions with unchanged ownership. Exact boundary, payment, receipt, gas and
Transfer-log proofs reconcile internal transfers with zero external-flow credit. Native
currencies, shared position transactions, and incomplete cash proofs were refused. This
checkpoint has no real-account settlement proof.
