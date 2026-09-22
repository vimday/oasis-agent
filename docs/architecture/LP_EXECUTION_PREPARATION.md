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
