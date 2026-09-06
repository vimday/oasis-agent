# Production Control Framework

**Status:** Normative target; controls marked missing are not implemented capabilities  
**Applies to:** every strategy, venue, signer, deployment, and capital allocation decision

This document turns industry practice into enforceable controls for Oasis Agent. A statement in an
AI instruction or design document is not a control by itself. A production control must have an
owner, a deterministic enforcement point, retained evidence, a test, and a rehearsed failure path.

## 1. Control doctrine

The system follows five rules:

1. **Safety is conjunctive.** All applicable gates must pass. A high score in one area cannot offset
   stale data, unreconciled positions, unsafe authority, or a failed security test.
2. **Truth, decision, and authority are separate.** Reconciliation establishes truth; strategy
   proposes; risk authorizes a bounded intent; execution performs it; signer policy limits capability.
3. **Loss of knowledge removes authority.** Unknown order state, stale prices, RPC disagreement,
   changed bytecode, or accounting residuals can only reduce permissions.
4. **Controls are independent of expected profit.** No forecast, model confidence, or human urgency
   can override a hard capital-safety invariant.
5. **Production changes are experiments with blast-radius limits.** Binary, configuration, model,
   fee-policy, data-source, and venue-adapter changes all use the same shadow/canary/rollback discipline.

## 2. Three independent control planes

| Plane | Responsibility | Must not be able to do alone |
|---|---|---|
| Strategy | Produce a typed proposal and explain the economic mechanism | Sign, broadcast, widen limits, or declare its own position truth |
| Risk and reconciliation | Reconstruct positions and approve a bounded intent from independent evidence | Modify strategy forecasts or hold raw signing keys |
| Signer and operations | Enforce destination, method, value, nonce, deadline, allowance, and capital policy | Invent trades or accept an unbounded generic call |

The same process, host, credential, or mutable database must not be the only implementation of all
three planes. Production recovery and kill capability must remain usable when the strategy process is
crashed, compromised, or producing invalid output.

## 3. Control record

Every production control has a versioned record with:

- control ID and risk addressed;
- deterministic owner and enforcement location;
- input schema, units, freshness, and failure semantics;
- threshold source and approval history;
- test IDs and last successful result;
- emitted evidence and retention location;
- alert owner, halt action, rollback, and recovery criteria;
- exceptions, expiry, and compensating controls.

An exception is never permanent. It names the capital ceiling, affected scope, approver, expiry, and
automatic action at expiry. Missing evidence means the control has not passed.

## 4. Mandatory control families

### CTRL-DATA — market and chain data

- Identify every datum by source, venue, instrument, semantic role, event time, receive time,
  sequence/version, and decision snapshot.
- Enforce freshness per instrument and field. Connection heartbeat or recent receipt time is not
  proof that the price, account, or order state changed correctly.
- Detect sequence gaps, frozen values, duplicate/out-of-order events, clock drift, crossed books,
  impossible values, source concentration, and provider disagreement.
- Pin chain decisions to block number and hash. Define finality and reorg behavior per chain.
- Record feed methodology, update triggers, market hours, deprecation behavior, and whether the feed
  is market price, exchange rate, NAV, index, mark, oracle, or executable quote.
- Require a predeclared response for every failure: reject, use a bounded independent fallback, or
  enter risk-reduction-only mode. Never silently substitute zero or the last known value.

Evidence: raw immutable payload, normalized record, health decision, source comparison, and the exact
snapshot consumed by strategy and risk.

### CTRL-STATE — orders, balances, positions, and accounting

- Build position truth from authoritative snapshots plus idempotently applied fills, not from desired
  orders or strategy-maintained estimates.
- Use deterministic client IDs and venue event IDs. Duplicate delivery must have exactly-once
  economic effect; missing acknowledgement becomes `UNKNOWN`.
- Reconcile open orders, cumulative fills, balances, positions, allowances, nonces, fees, funding,
  borrow, gas, and pending transactions after reconnect and restart.
- Maintain asset-separated ledgers in base units. A valuation price never permits token0 and token1
  to cancel an unexplained settlement residual.
- Freeze risk increase while residual exceeds its precommitted base-unit tolerance.

Evidence: append-only state transitions, raw venue/chain records, reconciliation result, residual,
and any operator action.

### CTRL-RISK — capital and market risk

- Enforce independent limits for transaction, position, strategy, venue, chain, token, account,
  collateral, approval, daily loss, drawdown, concentration, gross delta, net delta, basis, gas,
  slippage, liquidation distance, order rate, and unhedged-notional time.
- Calculate worst-case loss under partial fills, unavailable hedges, price gaps, congestion, reorg,
  funding/borrow shocks, correlated venue failure, and delayed capital transfer.
- Reserve unwind gas and hedge liquidity; they are not deployable strategy capital.
- Treat correlated contracts, bridges, RPC providers, cloud regions, signers, collateral, and oracle
  sources as shared failure domains even when strategies are labeled separately.
- Limits may tighten automatically. Widening hard limits requires reviewed configuration, approval,
  canarying, and a retained change record.

Evidence: risk decision with reasons, input snapshot hash, limit/config version, reserved capital,
stress result, and approval identity.

### CTRL-EXEC — execution and MEV

- Prefer atomic execution when the economic trade can be atomic. Model every non-atomic trade as a
  hedge group with explicit partial-fill, cancel, reject, timeout, and unknown states.
- Use executable bid/ask and depth, never mark/index/oracle prices, to claim an available edge.
- Bind an intent to exact venue/chain, account, instruments, side, maximum size/value, limit price,
  slippage, gas, nonce domain, deadline, calldata hash, and allowed state transition.
- Simulate at a pinned state and compare predicted versus included state. A private relay or bundle
  reduces some exposure but is not a guarantee of privacy, inclusion, price, or finality.
- Public fallback is a separate policy decision. It must not automatically expose a timed-out private
  transaction or profitable route.
- Confirm fills before generating hedge demand. Repeated correction from stale reported positions is
  prohibited.

Evidence: intent, simulation, authorization, submission, acknowledgement/fill, receipt/finality,
state drift, and post-trade reconciliation.

### CTRL-SIGN — keys and privileged authority

- Research, market-data, and strategy processes never receive raw private keys or seed material.
- The signer accepts typed, narrow intents rather than arbitrary calldata. It independently verifies
  allowlists, code hashes, selectors, value, allowance delta, nonce, expiry, capital, and rate limits.
- Separate hot execution authority, emergency pause, parameter administration, treasury movement,
  and upgrade authority. Apply least privilege, multi-party control, and delay according to impact.
- Emergency pause may stop new risk immediately; unpause and limit widening require stricter review.
- Rotate credentials, rehearse loss/recovery, and maintain an inventory of every key, role, API wallet,
  approval, and revocation path.

Evidence: policy decision, signer identity, signed-intent hash, role inventory, rotation record, and
periodic access review. Never retain raw secrets or signed payloads in general logs.

### CTRL-MODEL — research and strategy governance

- Pre-register the mechanism, dataset, trial count, split, costs, benchmarks, thresholds, and kill
  criteria before viewing the untouched test window.
- Preserve failed trials and correct conclusions for repeated search. Separate research code and
  production parameter artifacts.
- Require walk-forward and regime evaluation, stress tests, capacity estimates, confidence intervals,
  expected shortfall, maximum drawdown, and attribution residual.
- Detect training/serving skew, feature drift, cost drift, and performance decay in shadow and live
  operation. Automatic degradation may reduce size or stop; it may not search for looser limits.
- AI-generated signals are untrusted proposals. Models cannot promote themselves or change the loss
  function, risk limits, credentials, or production stage.

Evidence: locked experiment record, dataset and code hashes, complete trial ledger, evaluation report,
reviewer sign-off, and live drift report.

### CTRL-CHANGE — secure build and release

- Review source and generated artifacts; pin dependencies and CI actions immutably; generate an SBOM
  and verify compiler, optimizer, dependency, and source provenance for deployable artifacts.
- Run unit, type, lint, property, fuzz, invariant, static-analysis, differential, fork, and chaos tests
  appropriate to the changed risk surface.
- Treat config, model, feed, signer policy, token allowlist, and risk-limit changes like code releases.
- Use reproducible deployment manifests and verify deployed bytecode, constructor arguments, roles,
  approvals, pools, chain ID, and source verification.
- No author self-approves a capital-moving release. Critical contract and signer changes require an
  independent security review; material unresolved findings block release.

Evidence: reviewed diff, test manifest with tool versions/seeds, dependency provenance, signed release
and deployment manifests, bytecode comparison, and reviewer identities.

### CTRL-OPS — reliability and incident response

- Define SLIs/SLOs for data age, decision latency, execution, reconciliation, halt, signer, and unwind.
- Alerts must map to a page, a ticket with deadline, or retained diagnostic data. Page on symptoms
  tied to capital risk, not merely internal noise.
- Partition strategy capital and infrastructure failure domains to reduce blast radius.
- Run game days for stale/frozen feeds, RPC forks, sequencer failure, signer refusal, compromised API
  credentials, unknown orders, partial hedges, reorgs, fee spikes, and process restart.
- A burned loss, reliability, or reconciliation budget freezes promotion and capital scaling.
- Recovery requires root-cause correction, regression evidence, reconciled capital, restored authority,
  monitored canary, and independent approval—not merely service availability.

Evidence: SLO report, alert/action record, drill report, incident timeline, postmortem, corrective-action
closure, and recovery approval.

## 5. Promotion and capital scaling

The only allowed progression is:

```text
fixture -> replay -> pinned fork -> live shadow -> isolated canary -> bounded production -> scale
```

For each transition, freeze:

- artifact, dependency, configuration, data-schema, and strategy versions;
- capital, loss, drawdown, exposure, gas, approval, and order-rate ceilings;
- evaluation duration and minimum independent observations;
- success, automatic rollback, manual halt, and expiry conditions;
- reviewer and operator responsibility.

Scale one dimension at a time. Increasing capital while also changing code, parameters, data source,
venue, or signer policy destroys causal attribution and is prohibited. Rollback must be tested before
promotion. A rollback that strands inventory is not a safe rollback; its unwind path is part of the
release.

## 6. Phase 1 control-to-code map

This map describes the repository as currently structured. `Partial` does not authorize production.

| Control | Current evidence | Status | Required next evidence |
|---|---|---:|---|
| Hook permission decoding | `src/oasis_agent/evm/permissions.py` | Partial | Canonical vectors, deployed-address/code-hash binding, callback conformance |
| v4 fee encoding/math | `src/oasis_agent/uniswap_v4/fees.py` | Partial | Differential tests against pinned `v4-core`, end-to-end swap cases |
| v4 event replay | `src/oasis_agent/uniswap_v4/{events,replay}.py` | Partial | Persisted reorg rollback, receipt-state reconciliation, broader canonical/fork vectors |
| Immutable evidence identity | `src/oasis_agent/evidence/store.py`, `src/oasis_agent/rpc/collector.py` | Partial | Production provider adapters, archive acquisition, finality/reorg retention drills |
| LP accounting | `src/oasis_agent/farming/{positions,position_rpc,position_ledger,inventory,accounting_replay,counterfactual}.py` | Partial | Live trace attribution, checkpoint reconciliation, executable four-cell simulator, OOS evidence |
| Transparent fee proposal | `src/oasis_agent/farming/strategy.py` | Research only | canonical fee bounds, manipulation model, fee/flow response, OOS proof |
| Opportunity risk filter | `src/oasis_agent/risk/policy.py` | Research only | hierarchical limits, independent state, reservations, kill integration |
| Execution lifecycle | `src/oasis_agent/execution/state_machine.py` | Guard stub | simulation, intent binding, signer policy, broadcast/finality/reconciliation |
| Solidity hook | `contracts/src/{OasisDynamicFeeHook,OasisHookFactory}.sol`, canonical-core swap test, official v4-periphery PositionManager lifecycle, fuzz and stateful invariants | Partial | pinned Robinhood fork, real Permit2/malicious-token/reentry campaign, Slither/symbolic review, independent audit |
| Independent signer/monitor | none | Missing | isolated service/policy, inventory, drills, evidence |
| CI/release provenance | `.github/workflows/ci.yml`, locked npm graph, full-SHA actions | Partial | protected required checks, Python dependency lock, coverage/secret/dependency scans, artifact/SBOM provenance |

The current absence of a broadcast implementation is a safety property to preserve until all upstream
controls exist. Do not fill that gap ahead of state truth, risk, reconciliation, and signer policy.

## 7. Production readiness decision

A release decision is one of `REJECT`, `RESEARCH`, `SHADOW`, `CANARY`, or `BOUNDED_PRODUCTION`.
There is no `approve with unknowns` state. Every decision packet contains:

1. exact commit and dirty-tree status;
2. artifact and dependency provenance;
3. strategy experiment and threat-model versions;
4. control matrix with evidence hashes;
5. open findings and accepted residual risks;
6. capital and authority manifest;
7. monitoring, rollback, unwind, and incident contacts;
8. time-bounded independent approvals.

If any mandatory control is missing, stale, contradicted, or unverifiable, the maximum decision is the
last safe stage whose requirements still pass.
