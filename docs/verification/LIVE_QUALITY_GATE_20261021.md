# Development quality gate correction — 2026-10-21

The owner explicitly authorized hourly implementation, testing, commit and SSH
push until one-command live acceptance is complete. The existing thread
heartbeat was updated in place. This does not expand the paid RPC budget,
capital/loss limits or production release authority. This increment makes no
RPC request and does not start production services or read wallet keys.

## Correction and cause

Commit `8a25401` contained an inaccurate statement that the secret gate passed.
The retained `reports/review-20261021/compose-policy/final-secrets.log` actually
contains two unreviewed candidates. A shell command group continued after the
scanner failed, and its final successful command supplied the reported exit
status. The original log is preserved; the previous verification document now
records the failure explicitly.

Source inspection confirms these two exact values are nonsecrets:

- `scripts/verify_live_compose.py`: `canary-keys` is a public Docker volume name.
- `tests/unit/test_live_compose.py`: `synthetic-marker` is deliberately supplied
  to verify rejection of a key-bearing environment in a negative test.

The scanner's SHA-1 fingerprints were checked against those exact strings before
adding their path/type/fingerprint entries to the reviewed baseline. No detector,
path or rule was disabled, and no unrelated candidate was accepted.

## Fail-fast orchestration

`make live-quality QUALITY_OUTPUT=<new-directory>` runs Python/frontend checks,
secret scanning, Bandit, then the expanded Compose check. Every child process's
exit code, launch failure or timeout is checked separately. Any failure stops
the sequence. The script exits 3 on failure; Make also returns a nonzero status.
Its report records executed and planned checks, elapsed time and SHA-256 for
each retained log. A later command cannot mask an earlier failed command.

Each check has a 600-second limit. A timeout kills the check's entire process
group, including test children, and records failure. Launch failure also records
failure. Output directories are created exclusively so a rerun cannot overwrite
prior evidence. No successful report authorizes live execution:
`execution_eligible` is always false. The check requires the local Docker Compose
CLI but never connects to a production RPC or starts Docker services.

Seven regression cases exercise real child-process success/failure codes, a
missing executable, timeout with descendant termination, exclusive reports,
log/evidence hashes and invalid timeout bounds. These are local synthetic process
tests, not live acceptance.

The first actual run under `reports/review-20261021/quality-gate-correction/`
detected two new lint issues. It recorded `python_frontend` exit code 2 and did
not run later checks. That failed result is preserved. The lint issues were
fixed before running the complete gate in a new `quality-gate-correction-r2/`
directory. That final run completed successfully: all four commands exited 0,
with 3,178 Python tests and 138 subtests passing, eight frontend modules passing
syntax checks, no unreviewed secret candidates, no medium/high Bandit findings,
and both Compose policy cases passing. Its `report.json` retains the individual
exit codes and log hashes. The outer Make command also exited 0. No failed
result was overwritten or reclassified as a pass.

Fresh price acquisition/NAV integration, production provider/simulation evidence
and real-canary dual-service receipt/cash recovery remain incomplete. Nothing in
this correction opens the four release gates or establishes trading returns.
