# Final authority key-use boundary — 2026-10-22

## Reproduced defects

Two offline regressions against parent `9eb7ea2` reached the ephemeral test key
when the authority should have refused:

- A retained `signing_attempt: {}` passed the truthiness check. State parsing
  permits a dictionary with unknown contents, and recovery/precheck/dashboard
  already treat every non-null reservation as unresolved. The authority could
  overwrite this unresolved reservation and sign again.
- After simulation and durable reservation, a wall-clock step back to one second
  before the decision block passed the final age check. The earlier simulation
  boundary rejected negative ages, but the final key-use boundary checked only
  the upper limit.

The initial focused run failed exactly these two cases. A separate retained
pre-fix run selected the same two cases and exited 1; its unmodified log is
`reports/review-20261022/authority-key-boundary-before.log`, SHA-256
`3c7446fd331c9c04458516185a1de3a021f84c986736228f31403c3a323e9587`.
These failures used synthetic raw chain responses and a generated temporary test
key. They are not evidence of a production incident or a real transaction.

## Change and verification boundaries

The authority now refuses every non-null signing reservation, including an empty
object, before observing the chain. Immediately before key use it requires
`0 <= local_time - decision_block_time <= 120`, alongside its existing monotonic
lifetime and operator-stop checks. No freshness limit is widened.

Regression tests change the clock, authority expiry or stop file **after** the
reservation has actually been saved. Negative age, age 121 seconds, expiry and
stop all produce zero key calls and preserve the reservation. Restoring normal
conditions still refuses a retry; this change does not automatically clear an
uncertain signing outcome. Ages 0 and 120 seconds still admit the otherwise valid
synthetic proposal, retain its pending transaction and use the temporary key once.
Empty and nonempty unknown reservations remain unchanged on refusal.

The focused authority suite excluding the local socket case passed after the
fix. The full quality gate also includes the socket case.

`make live-quality QUALITY_OUTPUT=reports/review-20261022/authority-key-boundary`
passed all four commands with exit code 0, no timeout and no launch failure:
Ruff/format/mypy, 3,275 tests and 138 subtests, eight frontend modules, no
unreviewed secret candidates, no medium/high Bandit severity findings, and all
seven offline Compose service expansions. One dependency deprecation warning,
85 low severity Bandit findings and existing Bandit comment-parsing warnings
remain. Raw logs and per-command hashes are retained in the output directory.
Report evidence hash:
`260d867eb08cabcd8ce8ba3f67e1e5317aacf7796353306f460af14390790243`.

## Remaining production conditions

The prior dashboard correction made unknown signatures visible; it did not prove
that every authority rejection path was correct. This increment closes the two
specific authority defects above. It does not establish current USD prices/NAV,
production dual-source simulation/runtime evidence, real-canary dual-service
receipt/cash recovery, economic acceptance or release authority.

No real RPC/oracle call, production key-volume access, production journal/budget
change, production signature, broadcast or service start occurred. Test signatures
use only ephemeral generated keys. The existing Docker image was not rebuilt. The previous
eight paid requests still belong to the same cumulative USD 0.10 authorization;
their final billed cost is not established by this offline work. Capital remains
USD 500, daily loss USD 200, cumulative loss USD 500 and feed age 1800 seconds.
