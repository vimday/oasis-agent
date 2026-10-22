# Runtime image configuration contract — 2026-10-23

## Change and scope

The Compose checker validates service argv, but Docker prepends an image's own
ENTRYPOINT to those arguments. The isolation drill previously inspected only the
image ID and then started temporary containers. A changed entrypoint, healthcheck,
implicit volume or environment could therefore change the drill/startup behavior
before image configuration was checked.

The verifier now inspects the selected local image and the explicitly pinned
Python base image. Before creating resources it requires:

- no image entrypoint, healthcheck or implicit volumes;
- exact user `10001:10001`, working directory `/app`, and default readiness argv;
- exactly the inherited base environment plus `PYTHONDONTWRITEBYTECODE=1`,
  `PYTHONUNBUFFERED=1`, `PYTHONPATH=/app/src:/app`, without duplicate keys;
- immutable SHA-256 image identifiers and the pinned base's layer prefix.

It uses the validated immutable image ID for subsequent operations, so a changed
tag cannot redirect those operations. No inspected environment values are emitted;
the v2 result retains only their enclosing configuration hash. The report also
binds the verifier source hash and explicitly says source identity is unverified.
An existing output report refuses before inspection or any repeated drill.

This verifies deployment defaults, not executable-file provenance, dependency
integrity, the absence of malicious image code, or production release readiness.
The Docker daemon and digest-resolved base image remain trusted. No new live gate
is opened. No key, transaction, feed policy, capital limit or RPC allowance changes.

## Reproduction and completed checks

Nine regression cases run against the prior HEAD verifier (`1499f27`) fail because
it attempts resource creation before refusing mutated image defaults. All calls
in those regressions are mock Docker calls; no container runs. The pre-change log
is retained at `reports/review-20261023/image-contract/before.log`, SHA-256
`72d9f6868d86f97d42fdf8db0c640d007452a7feaffb2af23fda0b0faa94fa86`.

The final offline checks all exited 0:

- 64 image-contract/Compose tests, including environment ambiguity, base mismatch,
  immutable image use, redaction and temporary-resource cleanup;
- whole-repository compile, Ruff/format, mypy (231 source files), frontend syntax
  (eight modules);
- secret scanning with no unreviewed candidates and no credential validation calls;
- Bandit with zero medium/high severity findings (85 low remain);
- real offline Compose expansion, seven services, absent/synthetic RPC cases.

The command logs and their SHA-256 values are retained in
`reports/review-20261023/image-contract/report.json`. This scoped report explicitly
sets `full_suite_run: false` and `docker_runtime_verified: false`; it must not be
reported as the complete live-quality gate or a real isolation-drill result.

## Blocking condition and next validation

An earlier daemon listing succeeded and listed the existing local runtime image
and `oasis-canary-keys` volume, with no running containers shown. An image-ID
inspection first returned no-such-image while the later inventory listed the tag;
a follow-up exact-ID inspection was not executed because automatic approval
review failed with its usage-limit error. This was an approval-service failure,
not a finding that the Docker read was unsafe. No bypass was attempted. The key
volume was never mounted or read.

At the initial checkpoint, the changes were local and uncommitted. The remaining work was to complete the full
`make live-quality` run under the approved local-socket test permissions, then
inspect/rebuild the runtime and validation images from the reviewed source and run
the actual network-disabled isolation/recovery checks without production volumes.
Those checks were prerequisites to committing/pushing this increment.
The existing consent for commit/SSH push remains valid; the approval-service
availability was the execution blocker, not missing user consent. The continuation below records its resolution.

Fresh authenticated USD price input and a verifiable paid-RPC tariff/remaining
budget are still missing. Paid acceptance requests remain eight under the same
cumulative USD 0.10 ceiling. Production dual-source simulation, complete funds
reconciliation and dedicated-account real receipt recovery remain outstanding.

## Continuation: real local Docker verification

At the 2026-10-22 19:44 UTC continuation, automatic approvals succeeded again.
Docker still listed tags/repository digests but returned `No such image` when
inspected by those references. Inspection by the listed image IDs succeeded.
The first real isolation invocation exited 1 before any volume/container creation
because the pinned base reference could not resolve. That failure remains in the
conversation's original tool output; it is not an isolation pass.

`inspect_base()` now supports this local-store case without weakening the pin:
a failed digest-reference inspection can locate exactly one local image ID using
the base tag, but its inspected `RepoDigests` must contain the exact predeclared
`python@sha256:...` digest. Missing/ambiguous candidates, absent or foreign digests
refuse. No pull is attempted. Five offline regressions cover this locator path;
the focused image/Compose suite now passes 69 tests.

The previous runtime image and the newly built runtime image both passed the v2
configuration and real filesystem-isolation checks. All drill containers used
`--network=none`, read-only roots and synthetic temporary volumes. Only the
verifier's random temporary volumes were removed. The actual canary key volume
and both production state volumes were never mounted or changed.

- Previous runtime ID: `sha256:e33540e0b2a0fcefcdf018c797a725d5d68ed1dd6ef346324d058596dfaa1b7e`.
  Isolation report hash: `76071849cade059325a89ebca292e06c499feb3c52979d4c5ad240eea550ada4`.
- New runtime ID: `sha256:b50e63597083f348ac0c83a3bf71cb1b0e2a392fdcce412198e17c035c1b0483`.
  Build tag: `oasis-live:image-contract-20261023`.
  Isolation report hash: `45920ab9cba4be64f4ed1d4c4b96964610dd6c6a7f7441572f3f780215a5d30b`.
- New validation ID: `sha256:552226a2d802bf80979d48e1383cbe2c0e63bdb1a671a5546959e4fb16d79c17`.
  Build tag: `oasis-live-validation:image-contract-20261023`.
- Verifier source hash: `4fff9c7d5771c398b2c151411ab57a5badf6c6e1460fb83e0264d584db5b77e5`.

The first runtime build used `--network=none` and exited 1 at the locked dependency
installation (`aiohappyeyeballs==2.7.1` unavailable in that mode). Its full failure
log is retained as `image-contract/runtime-build.log`. A normal-network build
then exited 0; its log records the dependency-install layer as **CACHED**, rather
than a new dependency download. The validation target also built successfully.
All dependency declarations and base-image pins remained unchanged. Build logs
are retained separately, never overwritten.

The validation image ran 139 focused tests successfully in 28.26 seconds, with
network disabled, UID/GID 10001, read-only root, all capabilities dropped, no host
or production-volume mounts, and temporary state in `/tmp`. These include process
SIGKILL/lost-reply recovery, two-ledger receipt recovery, recovery prerequisites,
signing deadlines and signed NAV acquisition/replay. Signing-related tests use
only ephemeral fixture keys and synthetic chain data. They do not establish real
canary receipt/cash recovery.

The new runtime image's default readiness command, under the same no-network/no-
volume constraints, exited **3**, correctly reporting `new_actions_allowed: false`
and all four blockers. This is an expected blocked readiness result, not a passed
production release. Its JSON is retained as `image-contract/runtime-readiness.json`.

The final full `make live-quality` run used the fresh directory
`reports/review-20261023/image-contract-final`. All four check commands exited 0,
with no timeout/launch failure and each retained log digest verified:
3,411 tests and 138 subtests, Ruff/format/mypy, eight frontend modules, secret gate,
zero medium/high Bandit findings (85 low remain), and seven Compose services.
One existing dependency deprecation warning remains. Final report evidence hash:
`87f9b03f2f796e8750b14d42e64973954088652040879f4d7b525af591aee433`.
The earlier full run is preserved in `image-contract-full`; the final run is the
one covering the digest-locator follow-up. Production evidence requirements and
the cumulative RPC spending constraints remain unchanged.
