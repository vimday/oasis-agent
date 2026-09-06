# On-chain Incident Response Runbook

This runbook is prepared before production deployment. Addresses, roles, contacts, and exact commands
must be filled from the signed deployment manifest; never improvise them during an incident.

## Severity

- **SEV-0:** active or imminent unauthorized fund movement, signer compromise, arbitrary execution,
  accounting invariant failure with live exposure, or unsafe admin/hook takeover.
- **SEV-1:** material loss risk, incorrect quotes/risk decisions, oracle/RPC corruption, sustained
  simulation-to-chain divergence, or inability to halt/unwind.
- **SEV-2:** degraded data/execution reliability with bounded exposure and intact safety gates.
- **SEV-3:** non-capital-impacting defect or suspicious signal requiring investigation.

## Immediate response

1. Declare incident ID, severity, commander, scribe, UTC start time, and communication channel.
2. Stop new risk through the independent kill switch. Do not destroy evidence or blindly submit
   competing transactions.
3. Snapshot chain head/hash, pending and recent transaction hashes, nonces, balances, positions,
   allowances, bytecode/implementation/admin state, configs, binaries, logs, and RPC responses.
4. Estimate capital at risk by chain/token/account and identify the safest authorized unwind path.
5. Separate confirmed facts, inferences, and unknowns. Record every action with actor, timestamp,
   expected outcome, actual outcome, and transaction/evidence hash.
6. Notify the explicitly configured private security and operational contacts. Do not expose an
   exploitable condition publicly while funds remain at risk.

## Containment rules

- Safety actions require the authority defined in the deployment manifest. An AI recommendation is
  never authority to sign, pause, revoke, upgrade, transfer, or broadcast.
- Use pre-reviewed pause/unwind/revoke procedures only. Simulate at the current pinned block and
  verify calldata, target, chain ID, nonce, fee bounds, balance effects, and signer policy.
- If signer compromise is suspected, isolate it and migrate authority through the pre-approved path.
- If evidence or RPC integrity is suspect, require agreement from independent providers and local
  verification before acting.
- A feed reconnect, heartbeat, recent packet, or repeated unchanged value does not prove the affected
  symbol or account state is current. Do not re-enable exposure until snapshot/fill/order/position
  reconciliation completes.
- Never repeatedly trade to correct a reported position mismatch while earlier correction orders may
  be live, filled, or unknown. Include their worst-case fills, freeze new risk, and resolve state first.

## Investigation

- Reproduce on an isolated pinned fork; never probe live third-party funds.
- Build a precise timeline from signed transactions and canonical chain data.
- Trace every affected currency independently through PoolManager deltas, transfers, fees, positions,
  approvals, and gas.
- For cross-venue incidents, reconstruct every hedge group using primary fills, hedge fills, duplicate
  and missing callbacks, open orders, authoritative position snapshots, funding/borrow/FX, and venue
  event/receive times. Measure peak gross/net exposure and unhedged-notional-seconds.
- Identify root cause and contributing failures across contract, off-chain code, model, data,
  infrastructure, review, deployment, and monitoring.
- Preserve failed hypotheses and avoid modifying source evidence.

## Recovery gates

Execution remains disabled until all applicable conditions hold:

- exposure and balances are reconciled with no unexplained material residual;
- the root cause has a regression test and the fix passes unit, fuzz, invariant, static analysis,
  fork, shadow, and canary checks appropriate to its scope;
- compromised secrets/roles are rotated and allowances/authorities verified;
- deployed bytecode and configuration match a reviewed manifest;
- monitoring detects recurrence and kill/unwind paths have been rehearsed;
- an independent reviewer approves recovery and capital limits restart at canary level.

## Post-incident

Write a blameless report covering impact, timeline, detection, response, root cause, contributing
conditions, why controls failed, corrective actions with owners/deadlines, and lessons. Track actions
to closure. Update threat models, invariants, tests, runbooks, and agent instructions where the same
mistake could recur.
