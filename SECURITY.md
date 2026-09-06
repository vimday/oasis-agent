# Security Policy

Oasis Agent is pre-production research software that may eventually control irreversible on-chain
capital. Treat every vulnerability affecting signing, transaction construction, accounting, risk
gates, evidence integrity, strategy validity, hook behavior, or dependency provenance as security
sensitive.

## Supported status

No released version is currently approved for production capital. The repository's observation and
offline simulation paths are the only supported modes until the promotion gates in
`docs/PROJECT_CHARTER.md` have passed.

## Reporting a vulnerability

Do not publish exploit details, private keys, sensitive RPC endpoints, vulnerable live addresses,
or transaction payloads in a public issue. Use the repository host's private security-advisory
channel when available, or contact the maintainers through an agreed private channel. If no private
channel is configured, report only that a private channel is needed and withhold technical details.

Include, where safe:

- affected commit/version and component;
- impact and capital-at-risk estimate;
- minimal local or fork reproduction with no live exploitation;
- violated invariant and prerequisites;
- evidence hashes, chain/block context, and relevant logs with secrets removed;
- proposed containment and regression test.

Never test a suspected vulnerability against third-party funds or production contracts without
explicit authorization.

## Response priorities

1. Prevent further capital deployment and preserve evidence.
2. Revoke or reduce exposed authority where doing so is explicitly authorized.
3. Reconcile positions, balances, pending transactions, and capital at risk.
4. Reproduce safely on a pinned fork and determine root cause.
5. Add a failing regression/invariant test, implement the smallest fix, and obtain independent review.
6. Resume only after the incident runbook's exit criteria pass.

See `docs/runbooks/INCIDENT_RESPONSE.md` for the operational procedure.

