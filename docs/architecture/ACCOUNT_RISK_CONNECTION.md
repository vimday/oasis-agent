# Account reconciliation and signer risk connection

Design: 2026-09-22, before implementation. This is a guarded integration boundary,
not a claim of deployed full-account indexing or live execution.

A trusted, independently operated account reader must enumerate the configured accounts
and their entire asset/position/debt inventory, replay opening/closing balances and all
movements (including failed-transaction gas), and value every line with pinned evidence.
Missing inventory, unknown pending operations, residuals, stale prices or missing chain
coverage must reject. A strategy cannot supply an approval boolean to this reader.

The global journal uses a fixed account/asset policy and owner USD limits. It reconstructs
NAV from integer asset units and rational USD-micro prices; assets round down and debts
round up. Cash-flow-adjusted negative NAV changes consume loss budget; gains never refill
it. Sum losses across accounts before testing limits. UTC+08 day boundaries require an
explicit boundary snapshot, rather than silently charging prior-day loss to the next day.

All account frames advance together. Their block identities, time skew, continuity and
source evidence are retained. The initial frame establishes the loss-budget inception;
it cannot prove pre-inception losses. Deposits/withdrawals and bridge classifications must
be independently replayed by the reader. Unresolved bridges block admission.

Snapshot ingestion, loss-latch evaluation and worst-case USD reservation share one SQLite
BEGIN IMMEDIATE transaction. Pending commitments survive restart and provider failure, and
nothing here is time-released: there is no timeout, and no caller can request a settlement.
A caller may explicitly release one commitment by request hash when it knows no transaction was
produced; a release only withdraws reserved capital and never weakens recorded losses or a halt.

The ceiling counts deployed investment plus pending commitments, so capital already spent would
otherwise be charged twice and compress the account's capacity permanently. Settlement is derived
inside the same transaction from what a frame itself shows: a commitment settles only against
*investment units its own account gained* between two consecutive reconciled frames, valued at the
new frame's prices and floored, and each observed unit settles at most one unit of pending
commitments in (account, request hash) order, so no observation is consumed twice. A rise in the
value of units already held is a price move rather than a deployment and settles nothing, cash and
debt units are never deployment, and without a previous frame nothing settles at all. A settlement
may be partial, and any unobserved remainder stays pending. Enforcement stays fail-closed in the
other direction too: `release()` refuses a commitment a frame already showed as deployed
investment, because that settlement is proof a transaction was produced.

The signer calls an independently configured risk guard after static checks and before
requesting any signature. Missing guard, reader failure or risk rejection denies. The guard
binds the full EIP-1559 request hash, independently priced maximum incremental commitment
including gas, account, chain, current block and global policy. Its policy digest is pinned
in the signer journal. An attempt that wins the nonce reservation but produces no transaction
releases its own reservation; a successful signature keeps it until an independent
reconciliation releases it. The default CLI loads no reader or guard and remains unable to sign.

Trust boundary: typed reconciliation inputs and hashes do not themselves prove raw-RPC
truth or inventory completeness. A production source must replay those proofs and run with
independent credentials/permissions. No caller-supplied JSON approval adapter is provided.
This change does not implement the missing all-asset chain indexer, independent LP prices,
bridge settlement service, OS isolation or production release approval.

## Implemented components (2026-09-23)

The reader boundary above now has one concrete implementation. It is retained-input replay,
not live indexing, and it holds no key and exposes no signing or broadcast path.

- `risk/portfolio_manifest.py` declares the reconciled accounts and assets and binds them to
  exactly one `GlobalRiskPolicy` inventory. Every declared asset re-derives from raw RPC
  before a frame is admitted, so a manifest that disagrees with the chain fails closed. Each
  `ChainInventory` requires two distinct providers and exactly one native gas asset; each
  ERC-20 asset pins `decimals` and `runtime_keccak`, and each asset pins a USD `feed` with its
  own `runtime_keccak` and `max_age_seconds`.
- `farming/account_inventory.py` captures and replays two-provider, block-pinned balances,
  decimals, code hashes and feed answers; the quorum rejects any disagreement or drift.
- `farming/account_flows.py` reconstructs per-asset movements from raw `Transfer` logs and
  explains native gas plus declared non-gas native units. Observed movement is the exact
  difference; unexplained residuals become `unresolved_operations` instead of zero. The credit the
  ledger consumes is `non_trading_flow_usd_micros`, built from every transfer-driven token delta
  (`net_units`), including transfers between monitored accounts, because the portfolio still holds
  those tokens. Valuing at the floor can only under-credit the owner, so it cannot hide a loss;
  gas and undeclared native movement are never credited. `TokenFlow.external_*` is reporting
  metadata for bridge/withdrawal attribution, not a ledger credit: using it would charge every
  transfer between monitored accounts as a loss.
- `execution/account_risk_reader.py` (`EvidenceReplayAccountRiskReader`) replays those retained
  proofs into one complete `PortfolioFrame`. `reconcile()` rebuilds the frame; it raises on any
  missing, stale, disagreeing or unexplained input. Retained proofs must come from exactly the
  manifest's declared `primary_source`/`secondary_source`: two arbitrary distinct providers are not
  enough, because the manifest names who the reconciliation trusts. `reconcile_and_price()`
  additionally returns
  the worst-case incremental commitment for one signing request, priced with ceiling rounding:
  `ceil(amount0_max) + ceil(amount1_max) + ceil(pricier pool currency, max_swap_input) +
  ceil(native, gas_limit * max_fee_per_gas + value)`. The swap term is denominated in the more
  valuable pool currency, which never understates the cap.

The reader only replays what an operator retains: it does not fetch, value or attest anything
itself, and it never weakens a ledger check.

### Declared LP positions (2026-09-24)

A manifest chain may also declare LP positions, and one declared position becomes one ledger asset:

- `risk/portfolio_manifest.py` `ManifestPosition` pins the position `asset_id`, the position
  manager, the state view, the pool manager, the ERC-721 `token_id`, the `pool_id` and both pool
  currencies, together with the state-view and pool-manager runtime hashes. Both currencies must be
  declared fungible assets of the same chain, a position may not reuse a fungible `asset_id`, and
  `ChainInventory.asset_ids` — and therefore the global policy inventory — covers positions too, so
  a declaration can never silently add an asset the ledger does not count.
- `farming/account_inventory.py` reads `ownerOf` for every declared position, so its units are
  observed from chain rather than assumed. Units must be exactly `1` at the reconciled block; a
  transferred or burned position refuses the frame instead of counting as zero.
- `farming/account_flows.py` reconstructs a position's movements from the ERC-721 `Transfer` logs of
  its own token id (four topics, empty data, `units` always 1), so a position leaving the account is
  a reconcilable movement rather than a silent drop.
- `execution/retained_position.py` (`value_retained_position`) values one declared position from
  retained proof only: two agreeing position snapshots at the frame block, the pool's slot0 window
  covering that block, and one fresh feed round per pool currency, all from exactly the declared
  providers. The result is floored leg by leg, so it can never over-state the position.
- `execution/account_risk_reader.py` requires the snapshot's own `ownerOf` to name the account, so
  ownership is proven on two independent capture paths instead of trusted once, and refuses a
  valuation that is not at the reconciled block or is not positive. The value enters `AccountFrame`
  as an `investment` asset with `decimals` 0, numerator equal to the floored USD micros and
  denominator 1, its own snapshot hash as evidence; `movements` still reconciles the position's own
  units against the inventory.

The declaration defines what the ledger counts: the reader neither discovers nor values a position
that the manifest does not declare.

### Manifest shape

`configs/account-risk.base.example.json` is the one manifest committed to this repository. Its
account is the owner-confirmed dedicated canary wallet, its provider aliases are the two Base
environment entries the capture reads from, its loss limits are the owner's USD 200/500 limits,
and its ETH/USD feed pin (address, `ETH / USD` description, runtime codehash, 3600s heartbeat)
was read from two independent Base mainnet providers at the same block before it was written.
`tests/unit/test_portfolio_manifest.py` re-parses the file and fails on any pin change, so the
example cannot rot silently. It is Base-only and native-only: it declares no ERC-20 because no
retained Base USDC/USD feed evidence exists, and a fabricated pin would be worse than an absent
one. The capture re-derives every declared value from raw RPC, so the example is an assertion
the chain must confirm, not a claim about the chain.

The shape below is the reference for any other chain. Pins shown are placeholders: a production
manifest must supply real, independently verified values, and the Robinhood feed still lacks them.
A declared position also has to appear in `policy.inventory` for its chain/account, because the
inventory must cover exactly what the manifest declares.

```json
{
  "policy": { "inventory": { "<chain_id>:<account>": ["native", "<asset_id>", "<position asset_id>"] }, "...": "GlobalRiskPolicy" },
  "chains": [
    {
      "chain_id": 0,
      "account": "0x<reconciled account>",
      "primary_source": "<provider-a>",
      "secondary_source": "<provider-b>",
      "assets": [
        {
          "asset_id": "native",
          "address": "0x0000000000000000000000000000000000000000",
          "decimals": 18,
          "runtime_keccak": null,
          "role": "cash",
          "feed": {
            "address": "0x<USD feed>",
            "description": "<feed description, e.g. ETH / USD>",
            "runtime_keccak": "0x<feed runtime codehash>",
            "max_age_seconds": 3600
          }
        }
      ],
      "positions": [
        {
          "asset_id": "<position asset id>",
          "position_manager": "0x<v4 position manager>",
          "position_manager_runtime_keccak": "0x<position manager runtime codehash>",
          "token_id": 0,
          "pool_id": "0x<pool id>",
          "state_view": "0x<v4 state view>",
          "state_view_runtime_keccak": "0x<state view runtime codehash>",
          "pool_manager": "0x<v4 pool manager>",
          "pool_manager_runtime_keccak": "0x<pool manager runtime codehash>",
          "currency0_id": "<asset id>",
          "currency1_id": "<asset id>"
        }
      ]
    }
  ]
}
```

The `policy` must be the full `GlobalRiskPolicy` (owner USD total/daily/cumulative limits plus
the same inventory), not a partial object; the manifest validator rejects any inventory that
does not match exactly.

### Observe-only reconciliation CLI

`scripts/run_account_risk_reconcile.py` captures two-provider pinned account inventory, replays
it into one portfolio frame and reports units, feeds, NAV, per-asset movement attribution and the
loss the pure ledger reconciliation would record. It never opens the risk journal, never reserves
capital, never admits a commitment and cannot sign or broadcast; `execution_eligible` is always
`false`. Usage:

```bash
python scripts/run_account_risk_reconcile.py \
  --manifest configs/account-risk.base.example.json \
  --output reports/evidence/account-risk-base-<date> \
  --rpc 8453:BASE_RPC_PRIMARY:BASE_RPC_SECONDARY \
  --block 8453=<pinned block> \
  --env-file .env \
  --interval-ms 3000
```

The `--rpc` provider names must be exactly the manifest's declared `primary_source` and
`secondary_source` for that chain; anything else is refused, so the retained proofs always satisfy
the trust assumption the manifest states. `--state <previous state.json>` chains the previous
ledger state: with it, the CLI also captures complete two-provider `Transfer` windows from the
prior frame's block to the new one, and the day/cumulative loss accumulates across runs instead of
restarting at inception. Native movement is never inferred — `--native <declarations.json>` supplies
`{"<chain_id>:<account>": {"gas_units": <int>, "non_trading_units": <int>}}`, and anything omitted
stays unresolved and blocks the frame. Native declarations are refused without `--state`, and both
values must be unsigned: the library accepts a signed non-gas attribution, but this observer cannot
tell a genuine native withdrawal from a native outflow that bought an investment and lost value on
the way, and crediting that outflow would erase the loss. A run whose account sends native out
beyond gas is therefore refused rather than under-reported.

Every declared position is captured as well: the CLI retains two-provider position snapshots plus a
two-provider slot0 window for each declared pool at the reconciled block, and the `inventory` block
of `report.json` lists the retained snapshot and pool-state hashes per position. A position whose
proof cannot be retained fails the run exactly like a missing fungible asset, so a declared position
can never degrade into an unvalued zero.

It writes `evidence.db`, `budget.db`, `inventory.json` and `report.json` into `--output` (which must
not already exist), and, when the frame is accepted, `frame.json` plus `state.json` for the next
run. Exit codes: `1` invalid configuration, `2` inventory capture failed (retained evidence
incomplete — this also covers any failure while collecting movement windows, so a refused run
retains whatever evidence existed and no frame), `3` the ledger refused the reconstructed frame.
A failed run writes only `report.json`, with `status` `capture_failed_redacted` and no digests, so it
cannot be mistaken for an accepted reconciliation. Provider endpoints are read only from
environment variable names, never printed. The `report.json` `unresolved` list records what this
observer still cannot establish.

Capture is one-shot by design: an exact capture may not reuse a recorder, because an accepted
quorum requires exactly one successful wire exchange per expected call. A retry inside a session
would append a second exchange and invalidate the capture, so `retries=0` and any transport-level
failure — a public provider dropping the connection, for example — aborts with exit `2` and a
redacted `report.json` rather than admitting partial evidence. The operator response is to re-run
the CLI at a fresh pinned block; a failed run leaves no frame and no state to chain from.

### Sustained shadow window before a canary (2026-09-24)

A canary needs more than one accepted reconciliation, so the precondition that a *sustained*
observation window exists is enforced in code rather than in a document:

- `risk/shadow_acceptance.py` is the gate, with no I/O, no key and no transaction. `ObservationRun`
  is one accepted observer run — pinned block and hash, block timestamp, the ledger `state.json`
  digest, the retained `report.json` digest, the run's cumulative loss and its unresolved count.
  `ShadowWindow` stores only the account key, the manifest and policy digests, the requirements and
  the runs: `first_block`, `last_block`, `last_timestamp`, `span_seconds` and
  `cumulative_loss_usd_micros` are derived properties, so a window cannot claim a span or a block
  range its own runs do not support, and its `payload` is a self-describing record whose digest
  binds the runs *and* the derived summary. The validator refuses an empty or too-short run set, a
  sequence that is not contiguous from 1, a halted or unresolved run, a block or time that does not
  strictly advance, a gap beyond `max_gap_seconds`, a repeated ledger `state_digest`, and a span
  below `min_span_seconds`.
- `canary_admission(window, ...)` is the decision. It refuses a window observed under a different
  manifest or policy digest, an account outside the policy inventory, a window whose last timestamp
  is in the future or older than `max_staleness_seconds`, and a cap larger than the approved total
  capital, the daily loss budget, or the remaining cumulative loss budget
  (`cumulative_loss + cap`), because a canary can lose all of its cap. Admission is a precondition
  only: capital authorization, signer policy, simulation and the operator broadcast gate all remain
  separate decisions, and `execution_eligible` stays false.
- `CANARY_WINDOW_REQUIREMENTS` (3 runs, 120s span, 300s maximum gap, 900s staleness) is a stated
  operator minimum, not a chain fact. A production canary should tighten or lengthen it, and the
  window records the requirements it was accepted under.
- `scripts/run_account_risk_shadow.py` produces a window, run from the repository root as
  `python -m scripts.run_account_risk_shadow`. Each attempt runs the observer once in an isolated
  subprocess at a freshly pinned block behind the primary source's tip. Only accepted runs enter the
  window: a capture failure (exit `2`) is retried at a *new* pinned block, an accepted capture the
  ledger refused (exit `3`) stops the loop immediately as a risk signal, and a configuration error
  (exit `1`) aborts. Attempt output is captured and discarded rather than persisted, because a
  provider error can echo an endpoint URL; a refused capture's own bounded reason is read from its
  retained `report.json`. It writes `attempts/`, `runs.jsonl`, and `window.json` plus `outcome.json`
  when the window is complete. Exit codes: `0` window recorded, `2` ledger refused, `3` incomplete,
  `1` invalid configuration.
- `scripts/check_canary_admission.py` is the operator-facing check: it reads one retained
  `window.json` with the manifest it claims to describe, rebuilds the window from the retained runs,
  requires the rebuilt record to equal the retained bytes (so an edited summary is rejected), and
  prints the decision with its reasons and `execution_eligible: false`. Exit codes: `0` admitted,
  `2` refused, `1` invalid input.

```bash
python -m scripts.run_account_risk_shadow \
  --manifest configs/account-risk.base.example.json \
  --out reports/evidence/account-shadow-base-<date> \
  --rpc 8453:BASE_RPC_PRIMARY:BASE_RPC_SECONDARY \
  --runs 3 --attempts-per-run 3 --run-interval-seconds 90 --tip-lag 5

python scripts/check_canary_admission.py \
  --window reports/evidence/account-shadow-base-<date>/window.json \
  --manifest configs/account-risk.base.example.json \
  --cap-usd 50
```

The run schedule is checked before the loop starts: the interval must be strictly below
`max_gap_seconds` and long enough that `(runs - 1) * interval` covers `min_span_seconds`. Both are
measured on the pinned block clock, so a real gap is the interval *plus* the observer's own duration:
an interval that reaches the maximum gap can never produce an accepted window, and refusing it before
the providers are contacted is cheaper than discovering it afterwards. For the same reason the loop
stops immediately — `incomplete`, with the runs it already accepted — when an accepted run lands
beyond the maximum gap, because a broken sequence cannot be healed by later runs.

#### Live mechanism demonstration (2026-09-24)

One real run of the loop against the two public Base providers the example manifest declares
produced window `abedce1f10fffeeb28514eafd8934068b0acf2bd6626ed1e5efc5cd074a09a65` for account
`8453:0xe653…e97d`: three accepted runs at blocks `51760279`, `51760358` and `51760414`, a 270s
span, cumulative loss 0 and no unresolved operations, with per-run ledger and report digests
retained. Seven attempts were needed: runs 1 and 2 each needed three attempts because the first two
attempts of each ended as `capture_failed_redacted` and were retried at fresh pinned blocks, so the
retry ladder is exercised by genuine public-provider flakiness rather than only by tests.
`check_canary_admission.py` then admitted a USD 50 cap and refused a USD 300 cap with exactly
`canary cap exceeds the daily loss budget`, against the owner's USD 200 daily limit.

This is a mechanism demonstration at the stated operator minimum, not canary acceptance: it covers
one native-only account over one short window, and the requirements are the repository default rather
than a production choice. The signing path now requires such an admitted window (below), but this
record is too short and too narrow to satisfy a real canary on its own.

### Live admission at the signing block (2026-09-25)

A signing request carries exactly one block, so the frame the signer's guard reserves against must be
observed at that block — not at whatever block an observer last ran. Two modules close that gap, and
they hold no key, never sign and never broadcast:

- `execution/account_capture.py` is the acquisition library the observer CLI and the admission reader
  now share: `one_shot_sessions()` builds one fresh session per declared provider (an exact capture may
  not reuse a recorder), `capture_inventory`/`capture_positions`/`capture_flows` retain the
  two-provider proofs, and `capture_observation()` assembles one complete `AccountObservation`. An
  inception baseline must be a zero-flow statement, so declaring native movement without a previous
  frame is refused rather than silently explained; with a previous frame the capture must advance it.
- `execution/live_account_reader.py` (`CaptureAccountRiskReader`) is an `AccountRiskReader` that
  captures the manifest's declared proofs at `request.signing.current_block.number`, replays them
  through `EvidenceReplayAccountRiskReader` and returns the priced worst-case commitment. The
  baseline it reconciles against is the ledger's own last committed frame, so the account advances
  along one lineage rather than against an unrelated snapshot.
- `scripts/run_lp_signer_sidecar.py` wires it: `--risk-manifest` plus `--risk-journal`, `--risk-store`,
  `--risk-budget`, `--risk-rpc CHAIN:PRIMARY:SECONDARY` (repeatable), `--risk-native`, an environment
  file, and the request-interval/request-cap pair. Admission options without a manifest, a manifest
  without a store or without its journal/budget ledger, and RPC names that are not the manifest's own
  declared providers are each refused at startup rather than partially wired.
- `scripts/assemble_lp_signing_request.py` produces the request file that CLI consumes.
  `execution/lp_signing_request.py` and `execution/lp_risk_decision.py` supply what the gate demanded
  and nothing wrote: one pinned simulation, one two-provider execution preflight and exactly one
  `risk-decision` record, all re-read from the store before they are bound. The command takes
  `--config` (the same sidecar config, so the fee caps a decision prices are exactly the caps the
  sidecar enforces), `--manifest`, the plan report, the evidence store and budget, one
  `--rpc`/`--block` per declared chain, and writes `--out` exclusively only when
  `authorize_lp_signing` authorizes the assembled request; a refusal exits 3 and writes no file at all.
  See `docs/architecture/LP_EXECUTION_PREPARATION.md` for its exact refusals.
- a configured signing authority additionally requires the shadow precondition, so the only
  configuration that can produce a real signature is unstartable without it: `--canary-window` names
  the retained `window.json` and `--canary-cap-usd` the cap it must admit. `load_shadow_window()`
  re-derives the record from its own runs and refuses a summary the runs do not support, an extra or
  missing field and a flipped `execution_eligible`; the window must carry requirements at least as
  strong as `CANARY_WINDOW_REQUIREMENTS`, so a record declaring a wider staleness or gap allowance is
  refused; and `canary_admission()` must admit the cap against the manifest's own policy. Supplying a
  window with no authority to consume it is itself a configuration error rather than a silent input.

```bash
python -m scripts.run_lp_signer_sidecar \
  --config configs/sidecar.json --request requests/<request>.json --out out/decision.json \
  --signer-socket /private/tmp/oasis-signer/signer.sock --signer-account <address> \
  --risk-manifest configs/account-risk.<chain>.json \
  --risk-journal out/global-risk.db --risk-store out/evidence.db --risk-budget out/budget.db \
  --risk-rpc <chain>:<PRIMARY_ENV>:<SECONDARY_ENV> \
  --risk-native out/native-declarations.json \
  --canary-window out/shadow/window.json --canary-cap-usd 50
```

The bounds are literal, and each one fails closed:

- the manifest must declare exactly one chain — a multi-chain frame cannot be pinned to one signing
  block, so it is refused rather than assembled from blocks of different heights;
- the capture must land on the request's own block number *and* hash, so a reorg, a stale tip, a
  provider that answers another height, or a same-height state the request does not name all refuse;
- the canary window must describe this manifest and policy, be no older than the allowance it carries,
  and admit the declared cap against the owner's total-capital, daily and cumulative loss budgets. The
  window is an operator record: re-derivation proves it is internally consistent and unedited, not that
  the observation happened, and the runs' own `state_digest`/`report_digest` are the bindings to the
  retained ledger state and report;
- native movement is an operator declaration. An undeclared native change stays unresolved and blocks
  the frame, so gas spent by an earlier signed transaction must be declared explicitly before the next
  request can be admitted; the receipt producer below derives exactly that one quantity from the
  transaction's own receipt;
- a declared position must already exist and be owned at the reconciled block. The reference
  deployment therefore mints the first canary position out of band, declares it in the manifest, and
  only then opens the admission ledger; starting the ledger afterwards is what keeps its day and
  cumulative loss history from beginning mid-life.

The last point is the honest cost of a fail-closed design: the ledger's loss only accrues from the
frames it observes, so an account funded after the ledger opens would show its deposit as an
unexplained movement instead of an inception NAV. Nothing in this path schedules itself, broadcasts a
signed transaction, or records a receipt; the admission it consumes reserves no capital.

### From a confirmed receipt to the next native declaration (2026-09-25)

The one quantity an operator still had to type by hand is now derived from the chain. The close is
deliberately narrow: it reads a receipt, it never writes one, and it holds no key.

- `execution/lp_broadcast.py` is the only place in the repository that may put a signed transaction on
  chain. It decodes one EIP-1559 envelope, requires every field to equal the sidecar request that
  produced it, applies the operator's own chain, target, fee and value allowlists, pins the endpoint's
  chain id and submits exactly once. Its append-only, hash-linked journal records request hash,
  transaction hash, recovered signer, outcome and reasons — never raw bytes, a signature or a key —
  and a broadcast whose result is unknown is journaled `unresolved` and never retried, because a
  transaction that may have landed must not be replayed blindly. `request_hash()` reports which
  admitted request a journaled transaction belongs to, so a later receipt can be attributed to its
  request without reopening the journal's own conclusion.
- `execution/lp_receipts.py` is the producer. `observe_receipt()` takes a transaction the operator's
  broadcast journal already sent, locates its block *from the receipt itself*, pins that height and
  hash, and reconstructs the whole native cost of that transaction at two independent providers
  through `farming/transaction_costs.py`: execution gas, plus the OP L1 fee and the pinned on-chain
  operator fee, the Nitro `gasUsedForL1` share, or Ethereum blob gas. The block is not an operator
  input on purpose — a declared height could disagree with the receipt that is supposed to be inside
  it. Both captures share the one pin, so a provider that does not see that block fails closed with
  `cost block changed`, and only exact agreement on payer, status and every cost component becomes an
  observation. Nothing is retained until every check has passed, so a refused receipt leaves no
  partial evidence.
- `gas_declarations()` turns observations into the declaration file the reconciliation observer
  consumes: one entry per manifest account, covering exactly the manifest's accounts, with the summed
  gas of the named transactions and non-gas movement always zero. Non-gas native movement is never
  derived — an account that also sent native value keeps its unexplained residual and its next frame
  is refused, rather than having that outflow reported as gas.
- `scripts/run_lp_receipts.py` is the operator entry point, run from the repository root as
  `python -m scripts.run_lp_receipts`. Every named transaction must already appear in the operator's
  broadcast journal, so a declaration can only ever describe a transaction this system signed and
  sent. Each one is read at its own receipt's block, and every open broadcast among them is closed
  from that same observed receipt. Exit codes: `0` declarations written, `2` a receipt was refused or
  unreadable, `1` invalid configuration.

```bash
python -m scripts.run_lp_receipts \
  --manifest configs/account-risk.<chain>.json --policy out/broadcast-policy.json \
  --journal out/broadcast.db --transaction <chain>:<transaction_hash> \
  --output out/receipts --rpc <chain>:<PRIMARY_ENV>:<SECONDARY_ENV>
python -m scripts.run_account_risk_reconcile ... --native out/receipts/declarations.json
```

`--output` is a directory that must not exist. It retains `declarations.json`, `evidence.db` with the
two providers' exact cost wire bytes, and a `report.json` naming each receipt's request hash, block,
payer, status and evidence hashes — always with `execution_eligible: false`. When a receipt is
refused the directory keeps only a redacted `report.json`: no declaration is written, and the endpoint
values a provider error might echo are never persisted.

## Unresolved

- Only the Base example manifest is committed, and it is an *example*: the account is the
  owner-confirmed dedicated wallet and the ETH/USD pin was verified from two Base mainnet
  providers, but no completed reconciliation run has been archived yet, its Base ERC-20 inventory
  is empty because no retained Base USDC/USD feed evidence exists, it declares no position, and no
  Robinhood feed is verified at all. Reconciliation over a declared inventory is therefore still not
  complete NAV.
- The example's declared inventory is what the ledger counts: any asset an operator forgets to
  declare is simply not observed, so the manifest is a completeness claim that only an
  independently operated reader can audit. The same holds for positions: a position that the
  manifest does not declare is never discovered, owned or valued by any of this code.
- A declared position's value is a floored claimable amount — principal plus uncollected fees, priced
  by the declared feeds at one pinned block — so it excludes exit gas and slippage, does not model
  impermanent loss against a benchmark, and is not a realized value. A positive valuation is
  therefore a lower bound, not a mark-to-market NAV.
- `state.json` chaining is an operator-run observer, not a supervisor: nothing schedules it, no
  alert fires on a refusal, and a halted `state.json` is only as durable as the filesystem it sits on.
- No shadow window is archived in this repository. The one live mechanism demonstration above is
  retained only under `reports/evidence/`, which is excluded from version control, and its
  requirements are the repository default: it establishes that the loop, the gate and the check run
  end to end against real providers, not that the window is long enough or that a canary would be
  safe. A window proves only repeated, unhalted, unresolved-free reconciliation of one declared
  account under one manifest and policy; it is not evidence of profitability or of complete NAV.
- The loop declares no native movement, so an account that spends gas or sends native value between
  two runs produces an unresolved residual and the frame is refused rather than under-reported. The
  window therefore fits a funded but idle account, and an active canary account would need per-interval
  declarations the loop does not collect — `scripts/run_lp_receipts.py` can now produce them for
  transactions this system sent, but the shadow loop has no flag to consume them and would still be
  refused by an unaccounted native transfer.
- Admission is consumed by exactly one path, and only when an operator configures it: a signing
  authority plus a risk manifest now requires an admitted window, but a window read by this path is
  still an operator record, so an operator who fabricates a consistent window can satisfy it. The
  stated requirements are a repository minimum, not a production choice, and the staleness bound is
  measured against the block timestamp of the last run, so a clock-skewed host can refuse or admit a
  window that a correct clock would judge differently. The admission is not recorded in the sidecar
  decision or its ledger, so the retained window file is currently the only evidence that a given
  signature passed this gate.
- Settlement is derived from unit increases alone, because the ledger sees a frame and not why the
  units arrived. Investment units worth an outstanding commitment's amount that reach the account
  from a gift or an unrelated transfer settle that commitment without any transaction-level evidence
  binding them to it, so the ceiling can then count less than the worst case if the settled
  commitment's own transaction is also still spendable. Closing that gap needs transaction-level
  deployment evidence, which this reader does not bind.
- The live admission path has not been run against real providers. `CaptureAccountRiskReader` and the
  sidecar's `--risk-*` wiring are covered by two-provider fixtures and an in-process end-to-end signing
  test, not by a funded canary request; the one live observation so far is the observe-only shadow
  window above, which uses a different entry point.
- The request producer is exercised by synthetic retained proofs and fixtures only. No request has been
  assembled against a funded live account, and neither of its two live captures (the account at the
  intent's own state block, the preflight at a later head) has been run against real providers, so the
  one path that can make a request signable is not yet demonstrated end to end.
- The receipt producer closes one loop, not the whole one, and nothing schedules it. It derives only
  `gas_units`: non-gas native movement is never derived from a receipt, so an account that also sent
  native value still needs an operator declaration and its next frame is refused until it has one.
  `gas_units` also counts every named transaction as gas, which is only sound because a broadcast this
  system sent is an executor call with no native value; a native transfer through the same account
  would be reported as gas rather than refused, and no code prevents an operator from naming such a
  transaction. An operator must run the producer for the transactions in question and then pass its
  output to the observer's `--native`; no component does either on its own, and the declaration is
  never written into the risk journal.
- The receipt's block is a provider claim, not a finalized header: the producer pins the height and
  hash two providers agree on and retains them, but nothing proves the block survived a reorg, and a
  reorged-out receipt would still be declared. The cost of a reverted transaction is declared too,
  which is correct — the gas was spent — but that means a canary can accumulate a continued native
  drawdown with no accepted position to show for it, and only the per-run reconciliation is expected
  to notice.
- The signer CLI loads a reader or a guard only when an operator supplies the `--risk-*` flags, and it
  admits a canary only when that operator also supplies an admitted shadow window; with neither it
  denies every attempt. The live path has no default configuration, and wiring a production reader
  still requires an independently operated source.
- `execution_eligible` and `lp_readiness` remain closed. Nothing here is a deployed kill switch,
  live NAV, settled PnL or production release approval.
