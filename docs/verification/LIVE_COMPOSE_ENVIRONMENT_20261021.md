# Deployment environment boundary — 2026-10-21

This increment repairs a local Docker startup dependency and removes the
strategy CLI's implicit `.env` read. It does not accept any of the four remaining
production release blockers. No RPC or production signing request was made.
The existing paid acceptance ledger and USD 0.10 total authorization are unchanged.

## Behavior

The previous Compose common environment used required-value interpolation.
Compose interpolates the whole document before selecting a service, so even
`authority-status` and `initialize`, which explicitly have no environment and no
network, required RPC credentials just to parse the deployment configuration.

The common environment now names the two variables for pass-through. They are
resolved only into services that inherit that environment; setup/status retain
an empty environment. This does not supply a default URL. The execution entry
points still reject absent or invalid endpoints before network or key use.
The production release gate still runs before those checks. A negative test
temporarily replaces the release function in memory and proves that empty,
whitespace and malformed URLs cannot reach key loading or RPC budget creation.

`scripts/run_live_lp.py` now obtains endpoints exclusively from the explicitly
named environment variables. Previously, missing variables caused it to read
the entire repository `.env`, which can also contain a wallet private key.
Removing that fallback keeps unrelated credential file contents out of strategy
memory. Missing/empty variables produce a fixed error that does not echo input.
This applies to local shadow/check/recovery invocations as well as live mode.
Operators must pass only required RPC variables, not export a whole mixed
wallet-key file. The isolated authority's existing key provisioning is unchanged.

## Reproducible deployment check

`make live-deployment-check` expands all seven Compose services twice: with no
RPC environment and with synthetic `.invalid` endpoints. It strips inherited
credentials and Compose overrides, passes `/dev/null` as the env file, checks
the expanded service environment and volume identities, non-root execution,
read-only root, capability/no-new-privileges settings, resource limits, restart
policy, offline status/setup and keyless recovery commands. It creates no
containers or volumes and does not require a running Docker daemon. Output
contains no endpoint or credential values. A mismatch fails with a fixed error.

This is a configuration check, not full OS isolation or production acceptance.
The host and Docker administrator remain trusted. The separate isolation drill
still tests running containers with temporary synthetic markers, no network,
read-only root, separate private state and an authority-only read-only key mount.

## Evidence

Local results are retained in `reports/review-20261021/compose-policy/`.

- `report.json`: both actual Compose expansions passed, evidence hash
  `dc3a64653cf68bfeff8c672bee8f27e29b6d4a19a4d3439e57006d9f5eeb64f0`;
  Compose source SHA-256
  `ec6d95f3108c5f81e8a8b5d42be454005680191c3a95090a7dd913b357b93057`.
- `final-build.log`: local runtime image rebuilt successfully with locked
  dependencies. Final image ID:
  `sha256:e33540e0b2a0fcefcdf018c797a725d5d68ed1dd6ef346324d058596dfaa1b7e`.
  The earlier `build.log` image predates the `.env` fallback removal.
- `final-isolation.json`: final image confinement passed with synthetic markers;
  no production key/state volume was mounted. The drill removed only its own
  generated resources.
- `final-image-gates.json`: final image's strategy and authority entry points
  refused new live actions without network, state or key mounts; direct missing
  RPC resolution also refused with the fixed environment-only error.
- `checks.log`: complete Python lint/format/type checks, 3,166 tests and 138
  subtests, plus syntax checks for eight frontend modules, passed. After removing
  the `.env` fallback, `environment-tests.log` records 93 focused CLI, precheck,
  receipt-cost and environment-boundary tests passing, including five new tests.
- Correction: `final-secrets.log` records **two unreviewed candidates**, so the
  earlier statement that this gate passed was incorrect. They are the public
  `canary-keys` volume name and a `synthetic-marker` test value. The previous
  multi-command shell returned the later command's success and masked this
  failure. Preserve that failed log. Exact fingerprint review and a new
  fail-fast quality run are recorded in
  [the quality-gate follow-up](LIVE_QUALITY_GATE_20261021.md).
  `final-bandit.log` reported no medium/high severity findings. Final Ruff and
  formatting passed.

Fresh USD prices and full account NAV, production provider/simulation evidence,
and actual canary receipt/cash recovery are still required. No live service was
started, no real key was read, and no transaction was signed or broadcast.
