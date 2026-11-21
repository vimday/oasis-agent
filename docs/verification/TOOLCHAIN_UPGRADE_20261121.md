# 2026-11-21 toolchain upgrade

## Scope

Upgraded runtimes, CI actions, and locked dependencies to current stable releases. No strategy,
risk, accounting, authority, or trading behavior changed. Python source edits are mechanical ruff
fixes for the new target version, plus one `SIM102` merge in `native_payments.py` and updated
Docker-image assertions. This record grants no signing, broadcast, or deployment authority.

## Versions

- Python 3.12 to 3.15.2 (`requires-python >=3.15`, `make install`, CI, both Docker images).
- uv 0.12.23 to 0.13.9; `uv.lock`, `requirements.lock`, and `requirements-dev.lock` regenerated.
- web3 7.16 to 8.0.0; remaining Python dependencies resolved to their latest compatible releases.
- Ruff and mypy target `py315`; 34 lint fixes and reformat across `src`, `tests`, and `scripts`.
- Rust 1.95.0 to 1.100.0; `sha2` 0.10 to 0.11 and `sha3` 0.10 to 0.12 (`digest` 0.11).
- Foundry 1.8.4 to 1.10.0.
- Node.js 22.14.0 to 26.13.1 (CI only; local frontend validation used Node 23.11.0).
- Actions pinned by full SHA: checkout v7.0.1, setup-python v7.1.0, setup-node v7.1.0,
  foundry-toolchain v1.9.2.
- Base images pinned by digest: `python:3.15-slim` and `rust:1.100-slim-bookworm`.

## Exceptions

- Solidity remains 0.8.26 because the pinned Uniswap v4-core 1.0.2 and v4-periphery 1.0.3
  dependencies use `pragma solidity =0.8.26`. Attempting 0.8.39 fails on those exact pragmas.
- Slither resolves to 0.10.3 because 0.11.6 requires `web3>=7.10,<8`; the project now requires
  `web3>=8.0`. The resolved version passes the contract security gate.
- `ckzg` 2.1.8 publishes no CPython 3.15 wheel, so both Dockerfiles install `build-essential` in
  the same layer as the hashed `pip` install and purge it before the layer closes. The CI image
  build exercises this path.

## Verification

- `make lint format typecheck`: pass (ruff, 879 files formatted, 359 mypy sources).
- `uv run python -m pytest`: 6,514 passed, 138 subtests passed, 10 warnings, 322.53s.
- `make security-check`: pass (pip-audit 0 vulnerabilities, bandit, secret gate).
- `make contract-check`: 17 suites, 125 tests passed; `forge fmt --check` and `forge lint` pass.
- `make contract-security-check`: pass, informational findings only.
- `make rust-check`: fmt, clippy, and 18 tests passed.
- `make frontend-check` and `make profit-deployment-check`: pass.
- `git diff --check`: clean.
- Docker images were not built locally because the Docker daemon is unavailable; CI builds
  `deploy/profit-engine/Dockerfile` on every push.

## Authority boundary

This is an environment and dependency change. It does not authorize a live run, and it does not
change the release gates, capital limits, or the requirement for current evidence and operator
approval before any capital action.
