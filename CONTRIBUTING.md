# Contributing to Oasis Agent

Start with [the engineering constitution](AGENTS.md), [project charter](docs/PROJECT_CHARTER.md),
and [review policy](REVIEW.md). Research results and production authority are separate: a change
to an observation screen or simulator must not create a signing or broadcast path.

## Local workflow

Use Python 3.15+, Node.js 26 LTS for frontend validation, and the repository-pinned Foundry and
contract dependencies. Install locked dependencies with `make install` and `make contract-install`.
Inject only the credentials required by each process. Do not source a shared wallet-bearing `.env`
into observation or strategy services; keep signing material in an isolated signing service.
Use synthetic fixtures for local tests and demonstrations. Unix-socket tests need local socket-binding
permission on macOS/Linux; run them in a permitted local environment rather than skipping them.

`make run-server` no longer sources `.env`. Export the configured operations token and only the RPC
variables needed by the server before starting it. A clean service environment is required: variables
already exported by the parent shell remain visible to the server process. Prefer an absolute Python
executable in configured shadow commands; the default child PATH is the operating system default.

1. Inspect `git status` and relevant path-scoped rules before editing.
2. Describe a concrete invariant, failure scenario, or measured bottleneck.
3. Reproduce defects with a regression and make the smallest coherent change.
4. Run focused tests, then `make check`, `make security-check`, and `make contract-security-check`.
5. When measuring coverage, use `make check RUNNER='.venv/bin/python -m coverage run'`, then
   `.venv/bin/python -m coverage report`. Never report unavailable tooling as a passed gate.
6. Review the diff for amount precision, provenance, stale data, secrets and accidental authority.

Performance reports must record workload, environment, sample counts and the actual boundary timed.
`python -m scripts.benchmark_server_reads` creates temporary synthetic journals without RPC or keys.
Its direct-call latency does not measure API throughput, market freshness or execution latency.

## Working on the console

The console ships as local ES modules and CSS inside the Python wheel; it does not need a bundler
or remote runtime assets. See [the implemented read-model architecture](docs/architecture/CONSOLE_READ_MODEL.md).

- Declare API-to-view ownership in `server/console_contract.py` when adding an endpoint.
- Keep transport, paging, operation editors and view wiring in their respective static modules.
- Use the shared request client, integer-preserving parser and escaped rendering helpers.
- Add a behavior test for failures and out-of-order responses, not just a source-string assertion.
- Test empty, unconfigured, corrupt and expired states. Clear stale optimistic displays.
- Keep money as integer base units or explicit decimal strings. Browser charts are presentation only.
- Preserve unsubmitted editor state when refreshing runtime status.
- Confirm keyboard navigation, narrow layouts and the full evidence-detail flow in a browser.

`make frontend-check` checks every local JavaScript module and rejects missing or remote imports.
The Python test gate also executes Node behavior tests and verifies every API has a console owner.

## Pull requests and disclosure

Explain the concrete before/after behavior, tests run, measurable results, and remaining limitations.
Attach block-pinned evidence to chain claims; do not attach secrets or sensitive RPC URLs.
Use [the security policy](SECURITY.md) for private vulnerability disclosure.

Opening a PR is not authorization to deploy, sign, broadcast, change live configuration or spend
funds. Public release also requires the maintainers to select a license, confirm rights to included
material, and configure a private security-reporting channel. This engineering guide does not grant
a license or assert that those release steps are complete.
