# Local deployment and RPC expenditure validation — 2026-10-17

## Confirmed

- Local Docker runtime and validation images build from a digest-pinned base and
  hash-locked dependencies. Compose validates using placeholder endpoints, with
  no production credentials resolved or printed.
- Runtime image `sha256:c98f187400d63b6cf2b027eae60049140cc7e1a9db33f9a7b44fec00951045d8`
  passes the confinement drill: UID 10001, zero effective capabilities,
  no-new-privileges, read-only root, separate private state volumes, no host
  repository or Docker socket, and an authority-only secret volume which refuses
  writes by its owning UID. The secret was a non-key text marker.
- Retained drill: `reports/review-20261017/isolation-budgeted.json`, evidence hash
  `8b70dc3c4b297291f9c2f9c3e05e933d6560ec8e06fd976b204e41211fe96f80`.
- Validation image `sha256:0a965015300481957fa41c62a79f8bdfd60b947455be5a8d421f2e4ecf1e8ad1`
  passes 74 tests under network isolation and a read-only root. They include a
  real authority process losing a socket reply, SIGKILL, and a new process
  recovering the same public transaction commitment without another key call.
  Its source snapshot precedes only a formatting-only release-message change.
- The runtime's default readiness command exits 3 offline, reports four blockers
  and `new_actions_allowed: false`. No key volume is required for that check.
- Python regression: 2971 tests and 138 subtests pass; lint, formatting and mypy
  pass. All 70 Solidity tests and the full `make check` gate pass. The frontend
  syntax check passes for eight modules. Dependency audit
  reports no known vulnerabilities; Bandit reports zero medium/high findings;
  the secret gate passes without credential verification requests.

## Expenditure control

Both providers acquire concurrently, while calls within each provider retain
their order and frozen durable quota. Evidence persistence stays on the calling
thread; failure waits for both workers and never accepts a partial quorum.
Action preparation replays its decision-block wallet proof instead of reacquiring
the same data.

Direct head, nonce, runtime, allowance and receipt reads now share the same
`live.receipt-budget.db` as retained acquisition in the live CLI and authority.
Failed requests spend quota; direct reads never retry; restarts cannot refund
quota or change its policy. A failed live NAV observation latches the strategy's
halt before another observation poll. Pending recovery retains its existing
timeout and consumes the same quota. Broadcasting remains a separate exactly-once
path. Quotas are per service, not an aggregate dollar spending limit; the shadow
loop is outside this new live-client wiring.

The default pacing remains 3000 ms; `configs/live.rpc.canary.json` explicitly
selects 250 ms with the same 4096-request ceiling. Public endpoints have not been
accepted for that sustained rate. No price-unknown configured provider was called
during this follow-up.

One official free 1RPC endpoint probe charged exactly three requests to the
durable diagnostic ledger: chain identity and the sample receipt were available,
but the corresponding block could not be obtained. The probe stopped before
trace and did not retry. Report: `reports/review-20261017/free-1rpc.json`.
This is a capability diagnostic, not evidence of permanent provider incapability.
The diagnostic only accepts two published no-key endpoints, permits at most four
requests per invocation and eight total per provider, and never falls back to a
private endpoint. Earlier PublicNode diagnostics also failed to establish usable
trace support. Neither result qualifies a production provider pair.

## Unresolved release conditions

1. Production two-provider callTracer and complete Base OP receipt costs must
   pass retained replay, including a representative owner action. Local Anvil
   fixtures and synthetic zero OP metadata do not meet this condition.
2. Production authority key provisioning and independent, timely RPC simulation
   remain unvalidated. No production key has entered the agent or these images.
3. The last USDC/USD observation was about 60000 seconds old, exceeding the frozen
   1800-second maximum. A suitable fresh source or an explicitly reviewed policy
   change is required; changing RPC does not update an on-chain round. Actual
   account history and complete position valuation still need acceptance.
4. The deployed strategy and authority still need a joint recovery/reconciliation
   drill with real retained receipts. The synthetic process test validates the
   crash mechanism, not that production acceptance.

No mainnet transaction was signed or broadcast, no new capital was deployed,
and no profitability or production readiness is claimed. Docker host/admin
access remains trusted. These proofs narrow the outstanding work; they do not
open the release gate.
