# LP execution preparation

2026-09-11. Design frozen before implementation; simulation preparation only.

## Outcome

Connect two-source position acquisition and retained LP shadow decisions to canonical unsigned
OasisPositionExecutor calldata. Reuse position raw-RPC reconstruction and the existing LP intent
model. No signing, nonce reservation, broadcasting or release promotion is part of preparation.

## Data flow and invariants

1. Resolve an explicitly configured chain, PositionManager, StateView, NFT and block number.
2. Acquire both sources concurrently (two bounded tasks, separate recorders). Each validates chain,
   uses canonical block-hash calls, rechecks the closing header and reconstructs from raw replies.
3. Retain both provider records. Equal aliases are rejected; different aliases alone are not proof
   of independent infrastructure. Agreement requires identical headers, StateView and full snapshots.
4. Load a fully validated shadow journal. Use only its latest decision and its matching latest health
   record. An earlier positive decision cannot bypass a subsequent HOLD/HALT or health fault.
5. Require the same chain/block/hash, matching pool and NFT owner, and a supported RECENTER or REMOVE.
   The existing range controller's rebalance gate must pass. HOLD, HALT, REDUCE and UNWIND do not get
   silently translated to a different executor operation. Unknown fee/range/amounts fail closed.
6. Require explicit integer base-unit settlement minima/maxima, liquidity, executor nonce, deadline
   and gas limit. Apply the independently supplied frozen signer policy bounds before encoding.
   The existing executor cannot swap inside the LP bundle: maxSwapInput is always zero.
7. Encode the exact canonical ABI and create a deterministic intent identity bound to decision,
   both position evidence identities, policy and all amount/nonce/deadline fields. Preserve the
   original shadow execution_eligible=False; the output is always simulation-only and not authorized.

Freshness is checked at preparation using explicit current time and bounded age. The same input has
one deterministic plan identity; reevaluation must not silently refresh its deadline. Runtime code
hashes in the policy are expected identities, not claims that this collector verified deployments.

## Acceptance

Test independent raw reconstruction, provider disagreement and reorg, wrong chain/owner/pool,
stale time, prior decisions after HALT, unsupported actions, exact integer widths, zero/max limits,
same-range recenter rejection and two-provider provenance retention. Differential-check Python
calldata against Foundry cast's independent canonical ABI encoder for RECENTER and REMOVE.

Run a complete synthetic capture -> shadow journal -> plan flow with no mutating RPC methods.
Expose a bounded CLI writing a new report exclusively and evidence to the explicit local store.
No network addresses or chain capabilities are inferred. A successful plan says nothing about
receipt settlement, sellability, economic return or production readiness.

Inspection found that existing LP cycles bind features to a block but do not identify a pool or NFT.
Before preparation, extend cycle input evidence with an optional explicit position scope (chain,
PositionManager, NFT, pool, owner). Legacy unscoped cycles remain valid for research, but preparation
rejects them. The scope is included in both canonical input evidence and the full-cycle digest.

## Implemented interface

- `models/position_scope.py`: frozen `(chain, PositionManager, NFT, pool, owner)` identity.
- `farming/position_quorum.py`: two concurrent, separately recorded acquisition sessions, raw
  reconstruction and exact full-snapshot agreement; preserves both source evidence references.
- `execution/lp_preparation.py`: `LpPlanBounds`, `LpSimulationPlan`, and
  `build_lp_simulation_plan`. Integer bounds are protocol-width checked; preparation never reserves
  an account nonce or changes release status. Executor nonce and account transaction nonce remain
  separate; the latter is unresolved in the simulation report.
- `ShadowJournal(..., read_only=True)` opens an existing query-only SQLite connection. Preparation
  reads latest health and decisions in one transaction. An eventual signer must revalidate current
  state; a report does not remain current just because it was valid at preparation time.
- `scripts/prepare_lp_simulation.py` is a bounded read-only acquisition/plan command. It requires
  explicit `--config`, `--cycle`, `--journal`, `--evidence`, `--report` and `--now` epoch seconds.
  The report is created exclusively; an existing report is never overwritten. RPC failure details
  are redacted. Endpoint values come only from `OASIS_LP_RPC_PRIMARY` and
  `OASIS_LP_RPC_SECONDARY`, which must both be present and distinct.

The strict preparation configuration contains exactly:

| Field | Value |
| --- | --- |
| `policy` | Complete `LpSignerPolicy` fields, including role-bound `executor`, `position_manager`, and `pool_manager` runtime address/code-hash pins plus amount/gas caps |
| `bounds` | Complete `LpPlanBounds`: executor_nonce, deadline, liquidity, amount0_min, amount1_min, amount0_max, amount1_max, gas_limit, max_age_seconds |
| `position_manager`, `state_view` | Explicit canonical deployment addresses |
| `token_id`, `block_number` | Explicit NFT and acquisition block |
| `shadow_max_age_seconds` | Must match the retained shadow journal policy |

The cycle file is one canonical `LpCycle` JSON object including `position_scope`; it must already
have been consumed through `consume_once` and be the journal's latest decision. No scope is inferred
from an address or patched into an existing decision. Preserve older code-locked journals and start
a reviewed new observation journal for changed code. `--now` supports deterministic simulation
preparation/replay; it is not an authoritative live execution clock.

A plan retains both reconstructed position evidence records and a canonical unsigned transaction.
It binds the reconstructed PositionManager address to the policy, but does not yet verify that configured
runtime hashes are the deployed bytecode, prove that providers are independently operated, reconcile
owner cash, simulate the transaction, or qualify economic edge. The report lists those missing gates
explicitly. This preparation path still has no signing or broadcast capability.

## Exact pinned simulation requirement

Simulation binds sender, target, value and gas limit as well as calldata and block. The first envelope
revision omitted some of those fields, so old v1 proofs cannot authorize a changed gas envelope and
must be reacquired, never relabelled. Runtime identity must likewise be a role/address/code-hash
mapping inside policy and intent—not a separate caller-supplied list—so equal bytecode deployed at a
different address cannot satisfy the wrong role.

The simulator reads all intent-bound runtimes at the same block, verifies the executor's manager
getters, submits only `eth_call` with the exact unsigned envelope and canonical hash pin, and rechecks
the header. Two independently recorded sessions must both reconstruct successfully from raw replies
before evidence is retained. No state overrides, pending state, signing or broadcasting are permitted.
This verifies the named deployment graph and exact call at that block; account nonce, independent risk,
gas cost, finality and economic gates stay unresolved.

## Pinned simulation implemented (2026-09-12)

`execution/lp_simulation.py` implements `simulate_lp_plan`. The policy contains exactly three
role-bound `runtime_pins` entries—`executor`, `position_manager`, and `pool_manager`—each with an
address and code hash. Roles and addresses must be unique, and the executor address must equal the
transaction target. The simulator derives its checks only from the prepared intent; the CLI has no
second caller-supplied pin list that could remap a hash to another deployment. It opens fresh recorded
sessions for simulation after position capture and retains a simulation section only after both
sources succeed.

Each session checks chain, opening header, all three role-bound runtimes, and the executor's
`POSITION_MANAGER()` and `POOL_MANAGER()` immutable getters at the same canonical block hash. The
getter results must equal the two manager pin addresses before the exact executor `eth_call` (including
from, to, zero value, gas, data and block-hash pin) is accepted. It then checks the closing header and
reconstructs every retained request/response from raw bytes. Different parent headers,
recorder-tampered requests/results, duplicate IDs, unexpected calls, dependency-address mismatch,
reverts and unexpected returndata are rejected. Both provider headers must agree before retention.

Successful evidence uses `oasis.lp-pinned-simulation.v3` and binds the complete call envelope plus
role/address/code-hash mapping. `authorize_lp_signing` requires identical role-bound pins in policy,
intent, simulation and preflight observations; their mappings are included in intent, simulation,
preflight and sidecar request hashes. The simulation plan and sidecar request/config wire schemas are
v2, and new execution-journal reservations are v3. Old envelope-only v2 simulation evidence, naked
code-hash intents and old sidecar wire payloads are incompatible and must be reacquired, not relabelled.

These are block-pinned read-only simulations, not transactions. The result's `execution_eligible`
remains False. It does not create the independent risk decision, account nonce reservation, deployment
relationship review, finality guarantees or owner-cash settlement needed for live execution.

## Execution preflight implemented (2026-09-25)

`authorize_lp_signing` always demanded `kind = "execution-preflight"` evidence and no production code
wrote any, so a real request could not exist. `execution/lp_preflight.py` is that producer.
`collect_lp_preflight_quorum()` takes the frozen `LpSignerPolicy` (only its chain, account, executor
and role-bound runtime pins are read; its capital limits stay with the signer gate) and one explicit
block height, and issues exactly eight calls per provider: `eth_chainId`, the opening header, one
`eth_getCode` per role-bound runtime, the executor's own `lastNonce()`, the signing account's
`eth_getTransactionCount`, and the closing header. Every numeric read carries the EIP-1898
`{"blockHash": ..., "requireCanonical": true}` pin of that same head, so no read can describe a
different state than the rest.

A capture re-derives every field from its retained bytes rather than trusting the parsed reply, and
refuses a chain other than the policy chain, a header that moved between the opening and closing read,
a live code hash that differs from its pin, a word that is not a canonical `uint64`, and a nonce or
address that cannot be the signing identity. A quorum requires two distinct source aliases and
identical headers and payloads, so a provider that disagrees on the head, the account nonce or the
executor nonce refuses rather than being averaged away.

`persist_lp_preflight_evidence()` retains both providers as `kind = "execution-preflight"` records at
schema version 2 whose `payload`, `interpretation` and content hash are the same bytes, produced by the
one public `preflight_payload()` that `preflight_evidence_hash()` also calls. Writer and verifier share
that definition on purpose: a producer whose bytes differed from the ones the gate re-derives would
retain evidence that can never authorize anything.
`replay_lp_preflight_evidence()` rebuilds a capture from its retained request/response envelopes alone,
recovers the head's parent hash from the raw header instead of a payload field, and refuses a payload
its own bytes contradict.

This closes one required evidence kind. It is not an execution permit and does not by itself make a
request signable: account nonce reservation, deployment relationship review, finality and the
economic gates stay unresolved, and `scripts/run_lp_signer_sidecar.py` and
`scripts/run_lp_broadcast.py` only read `requests/<request>.json`. The two remaining evidence kinds
and that assembly are implemented below.

## Risk decision and request assembly implemented (2026-09-25)

`execution/lp_risk_decision.py` is the producer of `kind = "risk-decision"` that the frozen gate
always demanded and nothing wrote. `decide_lp_risk()` accepts only an
`EvidenceReplayAccountRiskReader`—a duck-typed reader could price the same intent differently—and
refuses calldata that does not hash to the intent, a gas envelope other than the intent's own
`max_gas_units`, an untimestamped or foreign simulation block, and fewer than two distinct
simulation providers at that block. It prices by rebuilding a `current_block = simulation_block`
envelope with zeroed nonces, no evidence and `risk_approved=False` and calling the reader's own
`reconcile_and_price`, so the capital ceiling and the worst-case commitment are produced by the same
code the signer guard reserves against instead of a second valuation. `LpRiskDecision` refuses to
exist with `approved` inconsistent with its `reasons`, and the only two reasons are an unresolved
operation in the reconciled account and a worst case above the ceiling. `persist_lp_risk_decision()`
retains exactly one record at the simulation block, and `replay_lp_risk_decision()` rebuilds it from
its retained bytes through the one payload definition the gate re-derives: the gate compares
`"0x" + content_hash` with the request's `risk_decision_hash`, so a producer with its own bytes would
retain evidence that can never authorize anything.

`execution/lp_signing_request.py` assembles a request. `parse_prepared_simulation()` rebuilds one
plan and its two simulation references from a report's exact bytes, refusing a report that still
lists `pinned_simulation` as unresolved, another schema, a plan that is not simulation-only, a
nonzero value, an intent hash that does not match its own intent, calldata that does not match the
intent's hash, and evidence that is not two distinct sources at the plan block.
`assemble_lp_signing_request()` then re-reads every submitted reference from the evidence store
first—the gate matches by hash alone, so without this a reference that was never retained could be
smuggled in—and adds the checks the gate cannot make: the retained simulation payload must be
byte-for-byte the envelope this plan's own simulation must consist of, the preflight providers must be
exactly the manifest's declared pair and the preflight must observe the policy's own account,
executor and runtime pins, the manifest must describe exactly one chain and the plan's account, and
the intent's pool and token id must be a position that manifest declares. It returns the sidecar
request together with `authorize_lp_signing`'s own verdict, so a caller enforces the frozen policy
instead of re-deriving it. It holds no key, cannot sign, reserve a nonce or broadcast.

`scripts/assemble_lp_signing_request.py` is the operator command. It requires explicit `--config`,
`--manifest`, `--plan`, `--evidence`, `--budget`, `--out` and one `--rpc`/`--block` per declared
chain, keeps the gas envelope out of the operator's hands (the fee caps are the sidecar config's own
and the gas limit is the plan's own intent, so a retained decision cannot describe a cheaper
transaction than the one a signer would be handed), re-captures the manifest's declared two-provider
account proofs at the intent's own state block, prices the decision, collects a fresh two-provider
preflight at a later head, and writes `--out` exclusively only when the gate authorizes. A refused
bundle exits 3 and writes no file at all, so an unauthorized request never becomes a path a signer
could be pointed at; an invalid configuration or plan exits 1; a capture or write failure exits 2.

Nothing here reserves an account nonce, holds a key or broadcasts, and a retained request is still
not an execution permit. No request has yet been assembled against a funded live account, so the
accepted path is exercised by synthetic retained proofs only; `run_lp_signer_sidecar.py` still needs
an operator-supplied request file plus an admitted shadow window, and release approval, finality and
the economic gates remain unresolved.

## Verification completed 2026-09-12

- Regressions first reproduced missing preparation support, stale simulation proof acceptance after
  gas changes, and a recorded response changed after the transport returned success.
- Current full `make check` passed: **1,067 Python tests, 108 subtests**, frontend syntax smoke,
  and **70 contract tests**; Ruff, format and Mypy (135 source files) also passed.
- Python-generated RECENTER and REMOVE calldata matched the independent Foundry `cast calldata`
  encoder. Synthetic full-wire tests exercised both capture providers and simulation failures.
- `make security-check` passed: no known dependency vulnerabilities, no medium/high Bandit findings
  (44 low findings remain), secret gate passed.
- `make contract-security-check` passed: 80 contracts, 102 Slither detectors, zero results.
- Existing websockets deprecation and Forge lint/cache warnings remain. No live RPC observation,
  fork simulation, signing, broadcast, deployment or economic result was performed in this increment.
