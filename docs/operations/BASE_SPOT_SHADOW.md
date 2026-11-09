# Base autonomous spot shadow

The Base spot agent consumes market data and records paper decisions. OKX OnchainOS supplies
discovery, security and quote data. GMGN can corroborate token data and reject material
disagreement when available. FOMO has an address-only nomination interface and still needs an
authorized data adapter. [OKX integration scopes](OKX_ONCHAINOS.md).

Entry uses a pullback in an observed uptrend, supported by liquidity and security checks
and paired entry and unwind quotes. Held positions are evaluated first against exit rules.
Missing or blocked quote data prevents the affected action. The paper book estimates gas,
fees and slippage and tracks exit value separately from forward price labels.

## Run and inspect

Use a configured local OKX CLI. Confirm the provider account and request allowance before online
reads. The shadow budget counts logical queries across restarts. Transport retries and provider
billing are separate, so this budget cannot establish a dollar cost ceiling.

```bash
.venv/bin/python -m scripts.run_spot_agent \
  --out data/spot/review-001 --ticks 3 --interval 120 --call-budget 500 --no-gmgn
make spot-report SPOT_RUN=data/spot/review-001
```

Remove `--no-gmgn` to enable a configured GMGN CLI, or provide its absolute path with `--gmgn`.
The dashboard reads the selected journal through `databases.spot_run`. A journal describes
recorded state. Process liveness requires a separate observation. Set STOP in the run directory
or signal the process to stop after the current deterministic paper tick.

`--live` refuses before wallet or RPC access. `make spot-live-preflight` is a separate
wallet identity and balance diagnostic. It uses read-only CLI queries and reports
`execution_eligible: false`. Keep the dedicated Base LP canary and its authority separate.
Historical spot journals remain readable and cannot be resumed.

## Replay and test a signal

Use an existing shadow directory and a new output file.

```bash
.venv/bin/python -m scripts.replay_spot_capture \
  --run data/spot/review-001 --out /tmp/oasis-spot-replay.json
```

The capture retains ordered provider responses, decision code identity, state and outcomes.
A session end record proves recorded completion. A partial capture can only replay its retained
prefix. To compare against the fixed no-smart-wallet-bonus baseline, run
[`scripts.run_spot_experiment`](../../scripts/run_spot_experiment.py) with a declared observation
window, sufficient label cutoff, and input provenance. Forward midpoint returns describe signal
groups. Promotion requires an untouched, cost-adjusted comparison using executable prices.

## Corroborate a historical fill

With a retained order and two frozen RPC input files, use
[`scripts.capture_spot_cost`](../../scripts/capture_spot_cost.py) to produce a new bundle with
`binding.json` and `evidence.db`. Inspect that bundle against the original journal.

```bash
.venv/bin/python -m scripts.report_spot_shadow \
  --run "$HISTORICAL_SPOT_RUN" \
  --cost-binding "$SPOT_PROOF_DIR/binding.json" \
  --evidence "$SPOT_PROOF_DIR/evidence.db" --require-receipt-match
```

The strict flag requires agreeing, transaction-scoped USDC and token Transfer nets that match
the journal. Both receipts must state the same canonical `transactionIndex`, and every log must
sit at that index. A receipt that omits it never matches. A missing or conflicting receipt proof
produces a nonzero exit. Historical native fees remain separate from strategy PnL. A matching
receipt does not establish complete wallet inventory, account NAV, or execution permission.

To check the fill against the wallet's balances across its whole block, add
`--settlement-binding SETTLEMENT.json` naming two retained `account_block_settlement` proofs
from the same provider pair, and `--require-block-settlement` to exit 2 unless the result is
`reconciled`. That status requires the native balance change to equal minus the receipt fee.
Each USDC and token balance and supply change must be fully explained by the block's Transfer
logs. The receipt rows must equal the block rows, and no other transaction may touch the wallet
in that block. Any shortfall is reported as `residual`, `unverified` or `conflict`, with raw
units. The check covers native, USDC and the order token only. It is still not account NAV,
finality, or execution permission.

`scripts.capture_spot_cost --with-settlement` produces those proofs from the same two frozen
files. Each file must also answer the pinned block settlement reads of the fill's wallet. The
bundle then gains `settlement.json`. It is published only when the settlement proofs read back
as the bundle's own. Any other outcome is published and printed, including `residual`,
`unverified` and receipts that contradict the block's own log list. Only the report's strict
flag refuses it.

To show one bundle in the dashboard, set `databases.spot_proof` to its directory alongside
`databases.spot_run`. The browser cannot name a path. `GET /api/v1/spot/fill-proof` runs the
same three checks as the report on the bounded observation reader, over one journal read and a
read-only evidence store. It returns `configured: false` when no bundle is set, and
`block_settlement: null` when the bundle has no `settlement.json`. A missing or incomplete
bundle returns 503 with code `missing`, an unreadable one `corrupt`, and a full reader `busy`.

The spot tab lists native fees, receipt asset matching and block reconciliation with their
providers, scope and raw base units. Its summary names the first gap: a mis-bound bundle, a
missing or stale record, a contradiction, a residual or another transaction in the block. Only
a clean pass reads as reconciled, and that covers this transaction, USDC and the order token.
Native is a whole-block net balance change compared with the receipt fee. Without a
per-transaction native trace, a matching net does not rule out other native movements in that
block. Nothing here enters strategy PnL or an execution gate. Proof failures clear only the
proof panel.

## Requirements for a future release

A proposal must name a reviewed token and route and bind the account, nonce, target bytecode,
calldata, input and minimum output, approvals, expiry, and gas. The exact envelope needs
independent same-block dual-source simulation and independent risk admission on fresh complete
account evidence. Submission and receipt acquisition must use the original scoped RPC budget.
An unresolved outcome blocks further orders until transaction, receipts, balances and native
costs reconcile. The signer must enforce these constraints and check the stop state before
submission. Use the [custody model](../security/CUSTODY_AND_AGENT_KEY_MODEL.md) and
[production control framework](../governance/PRODUCTION_CONTROL_FRAMEWORK.md).
