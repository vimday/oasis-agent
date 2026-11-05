# Joint stopped-service Gas/NAV acquisition — 2026-11-05

`scripts.capture_live_recovery` now has an explicit `--discover-gas-oracle`
mode. It resolves exactly one head and acquires the gas-oracle fingerprints,
both services' complete declared NAV and their retained settlement receipt
anchors at that head. The previous independent gas and NAV commands remain
available with their existing scopes. Their historical results and exhausted
grants are not reused as current evidence or authorization.

Default invocation is still an offline plan. The new plan schema is
`oasis.live-runtime-recovery-capture-plan.v1`; its purpose, request limits and
configuration digest enter its hash. An old 197-read plan hash is refused
before any request. With five retained settlement anchors, the new maximum is
209 requests: primary 105 and secondary 104; strategy budgets charge 56/55 and
authority budgets 49/49. Six additional reads per source use the strategy's
original budget ledger. There is one shared 120-second process-supervised
deadline, zero retries and no independent head lookup for gas discovery.
Fewer retained anchors produce a smaller exact plan, never extra allowance.

Both original service directories must be private, locked, stopped and halted.
Missing STOP, state, journal, evidence or budget artifacts remain refusals;
none is initialized or replaced. Original state/journal identity and hashes
remain locked, and original budget charges are append-only. No canary key or
repository `.env` is read. A retained zero-cost scope is an operator declaration,
not provider billing verification or authority to create a new grant. The
historical eight paid requests' costs remain unresolved; the cumulative $0.10
limit is unchanged. No production RPC was made for this implementation.

The source configuration must have its original five fingerprints and the
USD 500 total / USD 200 daily / USD 500 cumulative limits, with both feed age
ceilings exactly 1800 seconds. The gas sequence checks chain ID, opening full
header, nonempty proxy runtime, canonical nonzero implementation slot,
implementation runtime at the address derived from that source's slot, and
closing full header. Reads use EIP-1898 canonical block-hash pins. A first bad
reply or provider disagreement stops acquisition without retry. Both raw proofs
must validate before the attempt's separate `gas-evidence.db` is created.

The observed three pins are added to an in-memory candidate only. NAV then uses
that exact candidate hash, and its full header must equal the gas header,
including parent hash. Original NAV/cash history is replayed under its own
five-pin epoch (including the exactly recognized historical USDC 90000-second
variant). It is not relabeled. Current captures retain the strict feed bounds,
nonce/holdings continuity, loss checks, two-provider agreement and freshness.

Only a successful, timely worker can reach parent publication. The parent
re-derives the gas candidate from retained raw proof, matches source/target/head
digests and both NAV reference pairs, then invokes the existing read-only
runtime transition reviewer with the current clock. It publishes exactly:

- `fresh-nav.json` and `references.json` for the recovery capture;
- `runtime-target.json`, a **proposed plain config**, not an applied config;
- `runtime-references.json` and `runtime-review.json` for the offline preflight.

A late, corrupt or refused result removes only this attempt's incomplete
publication files. Raw candidate evidence, wire logs and budget charges remain.
Exit 0 means this capture/preflight boundary passed; `execution_eligible`,
`runtime_approved`, `configuration_applied` and migration/write-permit flags
remain false. No paired runtime epoch is written, STOP/HALT is not removed and
no service starts, signs, simulates or broadcasts. Full receipt-cost history,
simulation, unwind/restart and production runtime acceptance remain required.
An observed gas fingerprint is not source-code or runtime approval.

## Operator planning

Offline planning, on the original stopped directories:

```sh
python -m scripts.capture_live_recovery \
  --strategy-dir STRATEGY_DIR --authority-dir AUTHORITY_DIR \
  --discover-gas-oracle
```

Only after separate approval of that exact new plan and its current free quota,
the same command accepts `--capture --out NEW_ATTEMPT_DIRECTORY
--approved-plan-hash EXACT_NEW_HASH --zero-cost-scope RETAINED_SCOPE.json`.
The tool does not read RPC environment variables in plan mode. Capturing appends
proofs and request charges to the original evidence/budget ledgers; the broader
activity is read-only **on chain**, not a claim of zero local evidence writes.
Never point the ordinary startup command at `runtime-target.json` before the
history-preserving paired runtime transition is implemented and approved.

Subsequent implementation: the [paired epoch
command](PAIRED_RUNTIME_EPOCH_20261105.md) supplies the local writer. Its default
is read-only, and applying a production epoch still requires its own explicit
approval and current retained proof. No production migration was performed.

## Validation boundary

Seventeen new tests and 104 related tests passed with exit 0. They exercise the
strict and historical source epochs, exact 209-request original budget charges,
one head lookup, old-grant rejection before the first read, chain/header/runtime/
slot disagreement, first-failure stop, shared deadline, Gas/NAV parent mismatch,
unhalted/expanded-policy refusal, stale/tampered publication, actual process
supervision and offline CLI selection. Every chain reply is synthetic.

Initial lint failures (six import/line-length/regex issues, followed by two
context-manager style issues) were corrected without weakening rules. The
original logs are retained under `reports/review-20261105/joint-recovery/`.

The first full gate failed: 4453 tests passed, one failed because the new parent
publisher used a function-local repository import. Ruff alone had accepted its
annotation; the repository's structural gate correctly refused it. That run's
outer exit was 2, and subsequent secret/Bandit/Compose checks did not run.
The fix extracts the shared checkpoint into `scripts/live_recovery_checkpoint`
and uses module-level imports in both callers. Its function AST is identical
to the preceding committed verifier; no check is removed or relaxed. The four
import-structure gates and related collector/reviewer tests then passed all
43 tests. The failed full report and original 626-file lock remain retained;
the corrected run has a separate 627-file runtime/test lock.

The second full run also exited 2: 4449 passed, five failed. Two existing test
modules still injected synthetic source aliases into the collector rather than
the relocated checkpoint module. The fix changes only that import/injection
target; normalized module AST comparison confirms every other statement and
all 33 assertions are identical. All 50 tests in those modules plus the import
gates then passed. The second failed report and lock remain preserved. The final
full run uses `joint-recovery-quality-v3` and `source-lock-v3.json`.

Two synthetic packets contain ten original journal/proof/control files each.
They serialize no budget ledger. The smoke initializes separate disposable test
ledgers and checks their inode continuity and exact charges, with all real
transport and state-save methods forbidden. Synthetic feed/clock overrides are
explicit. This verifies the collector boundary, not real NAV, billing, provider
availability, source approval, profitability or production recovery.

The final full gate passed all four checks and outer Make with exit 0, no
timeout or launch failure: **4454 tests, 138 subtests**, 248 typed source files
and eight frontend modules. Ten WebSockets/multithreaded-fork deprecation
warnings remain. Bandit reports zero medium/high and 98 low findings; secret
scanning made no credential verification requests. Compose checks used
absent/synthetic RPC inputs and zero network calls. No rules or scanner
baselines were changed. Independent verification checked every command exit,
log hash, canonical report hash and all 627 frozen runtime/test inputs.

- Quality evidence hash:
  `b344c426b2d13a3075467aeb6f31b0170217a721fdf5e3d5d4e4746beab680b1`.
- Independent quality reverification hash:
  `ae4d7bb7e84ec80491f58367077319b5bb0931eb16be2fd76eaf35b2c9fdf18d`.
- Synthetic packet hash (zero serialized budget files):
  `3b9760859ecef0a2dedc03eca3047375f6e4e01859139863e2074cbac6f70e9c`.

This is internal self-review and offline engineering evidence. Production
volumes, canary keys and repository `.env` were not accessed; production RPC,
signatures, broadcasts, state migrations and service startups were zero.

## Committed candidate image

Implementation `830f22a4e404b70b29fa2149f8b2eb757c2cfc59` was the exclusive
Git-archive build context. Build (`--pull=false`), stopped-image source
verification, isolated Linux smoke and the enclosing builder all exited 0.

- Image: `sha256:6f32b3bea21947a41955b6666c8e204662a7fab5c7d21d7cc4aa477c7c85eb70`.
- Application manifest:
  `33a83955d4b7d83046a87b0cff20027edd8c0b95aa27fc4adc41346e083ea40f`.
- Candidate evidence, `reports/review-20261105/runtime-830f22a/summary.json`:
  `3f3a9cd0a1aca6498fd2dc355718ff674a3546ab5ebcb21ffd96188bcd526b19`.
- Independent reverification:
  `55a09b5d8f55f5321a520ef69fe0e662ae70f93e31673e676aa495bf0a01dc92`.

All 357 application files matched both committed and local bytes; all 359
archive files, including the Docker build files, matched that commit. Every
log, script, archive, manifest and canonical report hash was rechecked, along
with all 627 frozen inputs. A first host reverification invocation exited 1
because it omitted the project root from `PYTHONPATH`; its traceback remains
in the tool transcript and its refusal metadata is retained. The corrected
invocation exited 0. No candidate failure was counted as a pass.

The UID 10001 smoke used `--network=none`, a read-only root, dropped
capabilities, `no-new-privileges`, bounded PID/scratch limits and **no mounts**.
Both synthetic policy epochs completed 209 simulated reads using one head;
parent replay passed and expired publication refused. Disposable synthetic
budget inodes and exact charges were checked. Original synthetic state,
journals and STOP stayed unchanged. This is not production deployment or real
RPC acceptance. Dependencies, the base runtime and the Docker daemon remain
trusted; source verification covers application bytes.
