# Authority socket exchange deadlines — 2026-10-22

## Reproduced problem

At parent `8554d7e`, the transaction-authority client set one socket timeout but
did not impose an absolute exchange deadline. Each connect/send/receive operation
could consume that timeout again. A reply arriving in small chunks could keep
the strategy or recovery client waiting beyond its requested bound.

The authority server likewise read a whole frame without an absolute deadline.
A partial request could occupy the single-threaded service beyond its lifetime.
Signing dispatch already rechecked the service deadline, but status dispatch did
not. This is a protocol availability/control defect; it is not evidence of a
production signature or an observed attack.

The initial regression run exited 1 with nine failures: two client timeout cases,
late status dispatch and six invalid timeout settings. The unmodified pre-fix log
is `reports/review-20261022/authority-socket-deadline-before.log`, SHA-256
`e95ab490729cdfdf9e09f8a30868bf18a9123e8e91e787529d97874dfd4e5270`.
The late signing-dispatch case already refused before this change.

## Changed behavior

The client accepts a finite numeric timeout in `(0, 240]` seconds, rejecting
booleans and malformed values before socket access. After checking the private
socket path, one monotonic deadline covers connect, send and the entire reply.
Remaining time is recalculated before each operation and after each received
chunk. A complete frame arriving exactly at expiry is refused. No reconnect or
request retry is added.

Authority request reads expire at the earlier of service expiry or 240 seconds
after connection acceptance. Both status and signing dispatch check the service
deadline. Reply delivery is bounded by the remaining service lifetime and five
seconds; an expired service closes the connection without delivering a reply.
Existing signature counting still occurs before delivery, so losing a reply does
not restore signing capacity or authorize another signature.

The shared frame reader retains its five-second default idle wait for the
keyless status server. The transaction client explicitly uses its configured
timeout instead, preserving legitimate first responses taking longer than five
seconds. Callback execution is not forcibly interrupted by this transport change;
the independent transaction authority retains its own pre-key lifetime checks.
Local filesystem metadata checks and JSON parsing are outside the network timeout.

## Verification

Deterministic socket/monotonic-clock tests cover fragmented replies, time spent
across connect/send/read, exact-deadline arrival, ordinary responses, a six-second
first response under a ten-second timeout, late/valid status and signing dispatch,
and invalid numeric/type bounds. These tests use synthetic socket data and never
construct a production key or broadcastable transaction. The existing status
deadline test now supplies clock readings for the new post-receive check.

The focused non-socket selection passed 25 tests. Its first lint run found one
overlong condition, which was simplified; the final focused lint/format checks
passed. The initial full-quality invocation did **not** execute: automatic
approval review hit its usage limit. This was a review-service failure, not a
safety rejection or a test pass. The same authorized check was subsequently
retried through normal approval.

`make live-quality QUALITY_OUTPUT=reports/review-20261022/authority-socket-deadline`
passed all four commands with exit code 0, no timeout and no launch failure:
Ruff/format/mypy, 3,293 tests and 138 subtests (including actual temporary local
socket and process-recovery tests), eight frontend syntax checks, no unreviewed
secret candidates, no medium/high Bandit severity findings and seven offline
Compose service expansions. One dependency deprecation warning and 85 low
severity Bandit findings remain. Raw logs and individual command hashes are
retained in the output directory. Report evidence hash:
`06352a1d1e2b3e1ce912ed832800cfa9e601b41379870ae141fe28c2d098b3d1`.

## Production boundary

No real RPC/oracle request, production key-volume access, production signature,
broadcast or service start occurred. No production journal or RPC budget was
created, reset, copied or modified. The previous eight paid requests still use
the same cumulative USD 0.10 authorization, with actual billing unresolved.
The Docker production image has not been rebuilt for this source change.

Fresh USD prices/full NAV, production two-source simulation/runtime evidence and
real-canary dual-service receipt/cash recovery remain unresolved. Capital remains
USD 500, daily loss USD 200, cumulative loss USD 500 and feed age 1800 seconds.
This offline transport correction does not satisfy those release conditions.
