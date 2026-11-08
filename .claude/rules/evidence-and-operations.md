---
paths:
  - "src/oasis_agent/{rpc,evidence,config,live}/**/*.py"
  - "src/oasis_agent/config.py"
  - "scripts/**/*.py"
  - "configs/**/*"
  - ".github/workflows/**/*"
---

# Evidence, infrastructure, and operations rules

- Every decision-critical read is block-pinned. Resolve block tag to number and hash first; reject
  incoherent multi-call snapshots. `latest` is acceptable for observation only, never settlement.
- Detect chain-ID mismatch, stale heads, block-hash change/reorg, missing receipts/logs, RPC disagreement,
  partial responses, rate limits, and unsupported methods. Retry idempotent operations only when the
  current runbook and authorization permit it, with a bounded deadline and backoff. A one-shot
  acceptance attempt stops on the first refusal or 429.
- Evidence identity includes kind, chain ID, block number/hash, source, request, response/payload hash,
  retrieval time, and schema version. Deduplication must never discard distinct provenance.
- Canonicalize JSON before hashing. Store raw response separately from parsed interpretation so decoders
  can be upgraded without rewriting history.
- Configuration is typed, `extra=forbid`, and fails closed. Reject unknown keys, ambiguous booleans,
  floats for money, negative/NaN/infinite limits, placeholder production endpoints, and secrets in files.
- Logs are structured and redacted. Never log authorization headers, RPC query secrets, signing material,
  raw signed transactions, or full sensitive configs.
- CI uses least privilege, no secrets for untrusted code, immutable dependency/action pins, reproducible
  lockfiles, dependency review, secret scanning, and artifact provenance.
- Define SLIs/SLOs for data freshness, quote latency, simulation success, landing success, reconciliation,
  and halt latency. Exhausted reliability or loss budget freezes rollout except risk-reducing fixes.
- Alerts must map to an immediate page, actionable ticket, or retained log. Avoid non-actionable noise.
- Incidents require containment, capital accounting, evidence preservation, blameless root cause,
  corrective tests, and explicit criteria for resuming execution.

