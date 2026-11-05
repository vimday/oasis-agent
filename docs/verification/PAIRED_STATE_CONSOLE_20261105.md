# Paired state inspection in the live console

## Scope and confirmed behavior

The console previously projected only the strategy JSON. A paired runtime epoch
can commit both authoritative journals and then fail to replace one JSON
projection. A recent strategy JSON or static eight-pin configuration cannot
establish whether those two journals and their projections agree.

`scripts.inspect_live_state --pair-status-out NEW_FILE` now exports
`oasis.live-pair-inspection.v1`. It replays both original journals and supported
epochs through the existing verifier, validates each projection as a historical
member of its own journal, and records the latest canonical state/projection
hashes, terminal configuration/tip, runtime-epoch presence and redacted flags.
The originals must be distinct private owned regular files, rollback format,
without recovery sidecars, and unchanged across both complete replays. Immutable
read-only SQLite handles avoid recovery/sidecar writes. No budget or RPC evidence
store is opened, no history is copied or reset, and no HALT/STOP is cleared.

`databases.live_pair_inspection` connects that artifact to the existing GET-only
`/api/v1/live/status`. The read worker validates a duplicate-free strict schema,
canonical content hash, exact observation-only flags, and coherent projection
hashes. It derives mismatches against the local startup configuration and
marks inspections older than 300 seconds stale. This is a declaration reader:
it does not authenticate the exporter or replay journals or raw RPC itself.
The new frontend section distinguishes configuration mismatch, lagging JSON,
expired/invalid inspection and configuration/projection agreement at inspection
time. It retains HALT, pending, signing uncertainty and gas-completeness blockers.
Malformed API data clears prior displayed results. Inspection timestamps never
renew the separate 120-second NAV deadline or 1,800-second ETH/USDC feed limits.
All responses retain `execution_eligible: false` and `evidence_replayed: false`.

The default CLI invocation without the new output option retains its existing
inspection schema and exit behavior. The new option uses the immutable exporter
directly and prints the normalized pair-status view; unresolved flags still exit
3 even when a valid artifact was exported. Output files are created exclusively.
See `docs/operations/LIVE_CONSOLE.md` for paths and operator invocation.

## Review correction and retained evidence

An intermediate local implementation routed the new CLI option through the old
inspection function first. That old function opens ordinary `mode=ro` SQLite
connections, so the new option could not claim to exclude recovery-sidecar
behavior on an unresolved WAL input. This implementation was not committed,
deployed or run against production state.

The v1 full quality run was deliberately stopped to correct that route; it is a
failure, not an acceptance result. Its outer exit was 2, `python_frontend` was
terminated with -15, and its original logs remain under
`reports/review-20261105/pair-console-quality-v1/`. Report hash:
`fe9b075f27bc35e26a8985e19644514b9cac81029257bcbca2583ed9c2c3f196`.
The independently retained negative regression
`pair-console/cli-route-before.log` exited 1: it rejects any SQLite connection
that lacks `mode=ro&immutable=1`, then observes the intermediate CLI refuse.
After correction that same regression requires exit 0 and exactly two immutable
connections. This proves routing; it is not a production WAL recovery experiment.

Earlier targeted failures were test-setup errors: two guessed authority paths
instead of the canonical `startup_state.paths()` path, and one attempted new
configuration with USDC age 90,000, which the current policy correctly refused.
Those fixtures were corrected without changing production policy. The corrected
118-test targeted run exited 0; its complete output is retained at
`reports/review-20261105/pair-console/targeted-after.log`.

## Production limits

Only synthetic/disposable original journals and fixture chain evidence are used
for this change's acceptance. No production RPC, production state/key volume,
funding, signature, broadcast, migration or service start occurred. The owner
release recorded on 2026-10-31 remains distinct from current runtime admission.
The dedicated account's five historical transactions are not new current-state
proof. Fresh production USD/NAV, current dual-source gas/runtime/simulation and
real receipt/fund/unwind recovery acceptance remain unresolved. Existing paid
RPC totals and terminal one-time acquisition ledgers are not reset or reused.

## Development acceptance

The corrected `make live-quality` run exited 0, as did each recorded check:
Python/frontend, secrets, Bandit and Compose. Python reported **4,527 passed,
138 subtests passed**, with the same ten known fixture/fork deprecation warnings;
Mypy checked 250 source files and the frontend syntax gate checked eight local
modules. A separate local verification script rechecked every command exit, timeout flag,
canonical report hash, log hash and all **663** frozen runtime/test/config bytes.
No frozen byte changed during or after this run.

- Quality: `reports/review-20261105/pair-console-quality-v2/report.json`, hash
  `866640f9383352a1662c310dddff5d6539a52018432072e7c2e3521401f6006f`.
- Independent retained-result check:
  `reports/review-20261105/pair-console/quality-review.json`, hash
  `0b81dfdbfb1fedb19469f665300612d10ce26d105f159872e9afc8d1ca1d8505`.
- Negative regression log hash:
  `5893ed8b10c6750aa489708cafe15452ef133d5fddb9ec5473e269283eef276a`.

The independent check is a second local verification of retained results, not
another human/AI reviewer or an external security audit. These checks establish
the offline implementation boundary, not current production acceptance.

## Immutable candidate verification

Implementation commit: `5147fb88823b05ddcbda138311e2b701a3919c8c`.
The checked candidate is
`sha256:8cdaecc6273e4a2b552ccdca539fc93d84744574b8fda5a2410aca68df88f5d3`.
All **360** application files match the tracked local bytes and that commit;
all **362** regular Git archive inputs match the commit and local bytes too.
Source manifest hash:
`a5d8785219ebaf988ff2d32ac78d00605b2dd3b727fa6815cbdb9652bebb0373`.

The local Linux container ran as UID 10001 with network disabled, a read-only
root, dropped capabilities, no-new-privileges, bounded tmpfs/pids and no mounts.
It created only disposable synthetic journals. The actual export CLI returned
**3 as expected** for retained HALTs and a lagging strategy JSON; the encompassing
smoke verifier exited 0 after checking that refusal, both original journal/JSON
bytes, stale/configuration/invalid views, redaction and zero budget files.
The build, stopped-image source verifier and smoke wrapper each exited 0.
This candidate was not deployed and is not evidence of live profitability.

The initial build with build-time `--network=none` failed, exit 1, when the pip
dependency layer missed cache and DNS was unavailable. Its original archive,
script and logs remain at `reports/review-20261105/pair-console-5147fb8/`;
build log hash `1dde7a52ad637c4c4932f004cd201cd96a0f1d42a368cd7c6102847f7e19b3a4`.
The corrected build used the existing Dockerfile's ordinary dependency build
with `--require-hashes` and `--pull=false`; its execution smoke still had no
network. No runtime source, lockfile or production RPC policy was changed for
the build. Dependency build traffic is not production RPC acceptance.

- Candidate: `reports/review-20261105/pair-console-5147fb8-v2/summary.json`,
  hash `982bbd25471e1fcad5f6680cdff7e24317c0a593e93c8c05ec51fb7fd102ac30`.
- Separate local re-verification of archive/commit/source, every log and exit,
  expected refusal and all 663 frozen inputs:
  `reports/review-20261105/pair-console-5147fb8-v2/reverification.json`,
  hash `6fef0448d2f96c306844c1b7c2ba8e75e8084cb0089f4257aa440f2f0415832d`.

The documentation-only follow-up changes no tested runtime, test or configuration
bytes. The four unrelated untracked verification documents remain unmodified
and excluded from this task's commits.
