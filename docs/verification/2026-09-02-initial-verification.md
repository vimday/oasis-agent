# Initial Verification Record

**Date:** 2026-09-03
**Repository:** Oasis Agent local project
**Verification mode:** offline, deterministic fixtures only
**Status:** research foundation; not a production-readiness approval

## Commands run

```text
python3 -m pytest -q
```

Result: **48 tests passed, 0 failures**.

```text
python3 -m ruff check src tests scripts
python3 -m ruff format --check src tests scripts
python3 -m mypy src
python3 -m compileall -q src tests scripts
git diff --check
```

Result: **all passed**; mypy checked 39 source files.

```text
python3 -m oasis_agent.cli analyze-hook --fixture
python3 -m oasis_agent.cli report-benchmark --fixture --output /tmp/benchmark.md
python3 -m oasis_agent.cli execute
```

Results:

- fixture analysis identifies `mode=fixture`, `live_chain=false`, and `broadcast=false`;
- benchmark report explicitly labels itself fixture-only and not live-chain evidence or a backtest;
- execution exits non-zero with `broadcast is disabled in the initial release`.

## Controls exercised

- deterministic fixture labeling and non-broadcasting CLI boundary;
- strict configuration keys, booleans, finite nonnegative limits, and placeholder endpoint opt-in;
- evidence identity including block/source/retrieval/provenance hashes and tampered-reference rejection;
- malformed/non-object JSON-RPC response classification and bounded retry policy;
- v4 event and fee boundary tests, including directional protocol fees;
- per-token accounting and dynamic-fee regression tests.

## Scope limits

- No commit-pinned canonical `v4-core` or `v4-periphery` source is checked in or differentially verified.
- No live RPC acquisition, pinned fork execution, Solidity hook deployment, Slither, Echidna, or Foundry invariant campaign is claimed.
- No out-of-sample profitability, shadow, canary, independent approval, or production control packet exists.
- The benchmark report is deterministic fixture output; it is not a backtest, live-chain observation, or profitability proof.
- Execution remains disabled by default and no transaction was signed or broadcast.
