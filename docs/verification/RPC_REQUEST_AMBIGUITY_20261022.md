# Unambiguous budgeted observation requests — 2026-10-22

## Reproduced gap

At parent `b1d6fa9`, `BudgetedPoster` parsed JSON with Python's default duplicate
field handling, checked the resulting method/options and forwarded the original
bytes. A request with a disallowed method followed by an allowed duplicate
`method` passed the local check. A provider interpreting duplicates differently
could therefore receive a different method or trace policy from the one checked.
This is a boundary weakness, not proof that any production provider uses that
interpretation. Normal `JsonRpcTransport` requests do not generate duplicate keys.

The same parser accepted nonstandard `NaN`, `Infinity` and `-Infinity` literals.
Fourteen pre-fix regression cases reached a synthetic HTTP stub and incorrectly
created or spent local test quotas. Their run exited 1; the preserved raw log is
`reports/review-20261022/rpc-request-ambiguity-before.log`, SHA-256
`8d0a74557765e81d8597ec97c20b55ad6923d628b5507ec23523e29aa3ea006f`.
No real network, signed bytes or production budget was used in that reproduction.

## Change and checks

The observation wrapper now rejects duplicate keys at every object depth and
the three nonstandard numeric literals before opening the quota ledger or invoking
HTTP. Escaped spellings of the same key also count as duplicates after JSON
decoding. Error messages are fixed and do not echo request contents. Valid
requests retain their original bytes, existing method allowlist, trace bounds,
pre-network quota charging and no-refund behavior.

Tests cover duplicate top-level methods/params, escaped method keys, nested call
fields, nonstandard constants and duplicate tracer/timeout/`onlyTopCall` options.
For both absent and previously spent synthetic ledgers, rejected requests make
zero HTTP calls and leave the ledger absent or byte-identical. Existing successful
requests still forward their exact bytes; failures still spend quota, and direct
broadcast methods remain forbidden by this observation wrapper.

The focused budget/direct-read suites passed 48 cases and mypy passed all 231
source files. This increment is not a complete JSON-RPC schema validator and does
not change the separate broadcast adapter.

`make live-quality QUALITY_OUTPUT=reports/review-20261022/rpc-request-ambiguity`
passed all four commands with exit code 0, no timeout and no launch failure:
Ruff/format/mypy, 3,357 tests and 138 subtests, eight frontend syntax checks, no
unreviewed secret candidates, no medium/high Bandit severity findings and seven
offline Compose service expansions. One dependency deprecation warning and 85
low severity Bandit findings remain. Raw logs and individual command hashes are
retained in the output directory. Quality report evidence hash:
`c1236961dc61cbc78f27e82d031d1cc043987123dee14e4bedbb44cb68bf18bf`.

## Production boundary

No real RPC/oracle request, key-volume access, production signature, broadcast,
service start or image rebuild occurred. Production budgets were not opened,
copied, reset or replaced. The previously recorded eight paid requests remain
under the same cumulative USD 0.10 authorization; actual billing is unresolved.
USD 500 capital, USD 200 daily loss, USD 500 cumulative loss and the 1800-second
feed-age limit remain unchanged.

Fresh USD/full NAV, current production dual-source simulation/runtime evidence
and real-canary dual-service receipt/cash recovery remain unaccepted. These local
regressions are development evidence only and do not open a release gate.
