# Deployment command verification — 2026-10-22

## Reproduced gap

The previous expanded Compose check verified confinement/mounts/environment and
looked for status/recovery flags, but did not verify the actual executable and
complete argument list. A replacement Python program with `--reconcile-only`
as an unused argument still passed the recovery check. Arbitrary authority/setup
commands, a Compose entrypoint override and recovery in the default activation
group could also pass. This made a successful configuration check weaker than
the intended startup contract; no altered production service was started.

Five regression cases against parent `2e13810` all failed because the old checker
accepted those mutations. The preserved pre-fix log is
`reports/review-20261022/compose-entrypoints-before.log`, SHA-256
`b4a3dade8aa2a67bfc11344981232f06923550a0979465213053e16805251968`.

## Change

The verifier now has an independent exact argument vector for every service:
the directory-permission initializer, transaction authority, strategy, keyless
authority status and all three recovery entries. It refuses a different command,
any non-null Compose entrypoint override, a changed or duplicated argument, and
a different activation profile. Only authority/strategy belong to the default
group; setup and recovery retain their explicit profiles. Signature count, TTL,
configuration/profile path, state directory, key-file path and socket selection
therefore cannot change while preserving a passing check.

The existing Compose file and runtime entry points are unchanged. Legitimate
command changes, including argument reordering, require reviewing both the
deployment and verifier contract. The v2 report includes the verifier source hash
beside the Compose hash, so an older v1 report cannot be mistaken for this expanded
check. Historical reports remain unchanged.

## Verification

The focused suite passed 41 cases before the final v2 provenance assertions were
added. In addition to the five reproduced failures, twenty mutation cases replace
or duplicate bounded command arguments. Existing isolation/environment mutation
tests remain. Real local `docker compose config` expansion passed with absent
RPC variables and with synthetic `.invalid` endpoints; it contacted no daemon,
credential service or RPC endpoint and started no container.

An initial lint failure for implicit string concatenation was corrected with
explicit parentheses. Final focused lint/format passed.

`make live-quality QUALITY_OUTPUT=reports/review-20261022/compose-entrypoints`
passed all four commands with exit code 0, no timeout and no launch failure:
Ruff/format/mypy, 3,340 tests and 138 subtests (including the final v2 assertions),
eight frontend syntax checks, no unreviewed secret candidates, no medium/high
Bandit severity findings and seven real offline Compose service expansions. One
dependency deprecation warning and 85 low severity Bandit findings remain. The
directory retains raw logs and per-command hashes. Quality report evidence hash:
`a3a720e0ab9ce5836ad4d7100ab2fa037b6f6de94b2f9ee3f0a692f47a217cf0`.
The v2 deployment report hash is
`9cd7056a66bb1c14f4aad399ec7d297d5218826ba5a4f0777523fb63f5b9bf4d`,
binding verifier source hash
`1a310f07e8db9e7b61680b5d2a06075d599e37d5c7b32480e199185fa223ba6d`.

## Limits

This is configuration validation, not a sandbox or image/runtime attestation. It
does not inspect an image's embedded entrypoint or prove that the running artifact
matches source. Existing container isolation checks, image provenance and release
evidence remain separate. A host/Docker administrator remains trusted. No checker
can stop an operator from launching a different unverified file or changing both
the policy and checker; the source hash makes that change distinguishable.

No production key volume, state history or RPC budget was accessed or modified.
No real RPC/oracle request, production signature, broadcast, deployment or image
rebuild occurred. The eight previously recorded paid requests still belong to the
same cumulative USD 0.10 authorization, with billed cost unverified. USD 500
capital, USD 200 daily loss, USD 500 cumulative loss and 1800-second feed age remain
unchanged. Fresh USD/full NAV, production two-provider simulation/runtime proof
and real-canary receipt/cash recovery are still unaccepted.
