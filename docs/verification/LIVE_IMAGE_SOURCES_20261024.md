# Runtime application identity — 2026-10-24

The existing image contract validated container defaults and pinned base ancestry,
but explicitly did not check application identity. A locally tagged old image
could therefore pass those configuration checks while omitting current NAV or
budget fixes. A new standalone operator preflight,
`scripts.verify_live_image_sources`, compares an immutable runtime image's `/app`
with the working-tree bytes of its tracked runtime inputs: `src`, `scripts`, the
runtime dependency lockfile and the two deployed configuration files.

It validates the existing runtime contract, creates a stopped container with no
network or mounts, and uses Docker copy to obtain a temporary tar archive. It
never starts an image process, extracts paths onto the host, mounts a key volume,
or emits file contents. Archive parsing rejects extra/missing/changed files,
links, devices, traversal, duplicate paths and oversized inputs. The copy has a
60-second timeout and the completed archive is limited to 32 MB before parsing;
this is not an in-flight disk quota. Local inputs are checked again after the
comparison. Cleanup must succeed before a passing report is written. A failed
check exits 3 and emits no success report; prior output files cannot be replaced.

The report binds the immutable image ID, expected file hashes, manifest hash,
verifier hash and image configuration contract. This is application-byte
comparison against the checkout, not Git approval, dependency-byte verification,
proof of harmless code, or production release authorization. Base runtime,
installed dependencies and Docker remain trusted. It is a documented operator
preflight, not enforcement on arbitrary direct Compose invocations.

## Actual local results

- The previous runtime `b50e6359...1b0483` was refused (exit 3). The detailed
  diagnostic reported an unexpected archive file/type/size; the old image is not
  accepted merely because its default argv and base layer match.
- The initial `--network=none` build failed during pip installation because that
  build mode did not reuse the cached install layer. Its DNS retries could not
  leave the disabled build network. No RPC or credential endpoint was used.
- Building with the normal network setting reused the existing dependency layer
  (`CACHED`); no dependency download actually occurred. The candidate `ea74fc6c...9a32f9`
  still refused because macOS `.DS_Store` files had entered the application copy.
- Build ignores now exclude `*.egg-info` and `.DS_Store`, in addition to bytecode
  and caches. These are build-context exclusions, not ignored mismatches in the
  verifier. Unknown application files still refuse.
- The rebuilt runtime **`sha256:8839d0243a9a83af34b58afb9184b4bbdb75c2cc2d6794e953df025a42ddef5d`**
  passed comparison of **328 files**. Manifest hash:
  `97d099b5fc616b78a4dab27abe5e452a95c2c692132d0078585bf4c92404d54f`.
  Report `reports/review-20261024/runtime-source-clean.json`, evidence hash
  `79d3ae61d78f04a001c22fffc04923140c9761e829205e9656b07a2de5c65069`.
- The same immutable image passed the real local confinement drill with disposable
  synthetic marker volumes, no network and no existing production mounts. Report
  `runtime-isolation.json` in the same directory; evidence hash
  `4ae3fe852abc84a3b2f8f41aa723cb4bcbf2869eeae86eb2677d7537ebcf5f94`.
- Its default readiness command, run without network or volumes, returned expected
  exit **3** and all four production blockers. This is correct refusal, not live
  acceptance. Raw result: `runtime-readiness.json` in that directory.
- `oasis-live:local` now identifies the verified runtime. The old ID is retained
  as `oasis-live:before-source-check-20261024` (and its previous dated tag).
  No strategy/signing service was started, restarted or deployed.

Build logs are retained as `runtime-build.log` and `runtime-build-clean.log` in
`reports/review-20261024`. Failed diagnostics remain in the original command
outputs and are not reclassified as passes. Both successful evidence report
hashes were independently recalculated. No existing key volume was read.

## Development validation and limits

48 focused source/contract cases pass. The full `make live-quality` report at
`reports/review-20261024/image-sources` has four exit-0 commands, no timeout or
launch failure, and verified raw log hashes: **3466 tests and 138 subtests** pass;
compile, Ruff/format, mypy, eight frontend module syntax checks, secret scanning,
Bandit and offline seven-service Compose validation pass. One existing websockets
deprecation warning and 86 low-severity Bandit findings remain, with no medium/high
findings. Quality evidence hash:
`fc224efd64593cb9e2e5d9b453808a77851495f802ab522a8a44c77f162538e7`.
The final `.DS_Store` build exclusion was added after that full run and verified
by the successful real rebuild and byte comparison; Python code did not change.

No RPC request, signature or transaction occurred. The cumulative paid-RPC budget,
capital/loss limits and feed-age limit are unchanged. Fresh signed-price access,
complete dedicated-account NAV, current production dual-source simulation/runtime
acceptance and dedicated-account receipt/cash recovery remain unresolved. The
source comparison and isolation drill do not replace any of those conditions.
