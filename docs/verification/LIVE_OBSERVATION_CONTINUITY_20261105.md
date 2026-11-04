# Live observation and signing continuity — 2026-11-05

## Confirmed downstream defect

The prior feed-migration fix returned a verified historical wallet anchor. Other
consumers still discarded it. Full live observation could accept an unchanged
balance/position with an unexplained nonce or inconsistent block progression,
even with a retained historical NAV. The authority checked its transaction nonce
against the new capture, but did not compare that capture with the old account
nonce. Agreement between two providers was insufficient to explain the change.

Seven synthetic raw-proof regressions failed against `cddc9da`: five reached a
forbidden key-provider spy (nonce advance/regression, equal/regressed height and
wrong adjacent parent), one reported the read-only precheck as healthy, and one
reached strategy decision. Three valid observation cases passed. The spy never
produced a signature; this does not claim a production trade or actual loss.
The original exit-1 log SHA-256 is
`61de41da0b3224101e1594ede93c9321192f93bf03e78a89dba21ade5e74b04a`.

An isolated baseline harness also loaded the original collector checkpoint and
rearm service functions from `cddc9da`, without replacing working files or any
production state. The final harness reproduced four failures: the collector
accepted two changed post-cash nonces when old NAV references were cleared, and
both services agreed on a conflicting height/parent that passed rearm review.
Its exit-1 log SHA-256 is
`37dd4033ffa37902f71579db9e851f28baa538fa253ad2192f769203fda186ef`.
This was review only; no baseline rearm application was executed.

The first harness failed before tests because its temporary module lacked a
package for relative imports. A second harness's asymmetric rearm cases reached
the existing cross-service disagreement guard, so those failures were not proof
of bypassing review. The final cases make both services agree on the same bad
head. All original harnesses/logs remain in
`reports/review-20261105/observation-continuity/`; no failed evidence was replaced.

## Correction and authority boundary

`wallet.require_wallet_continuity()` now supplies one I/O-free predicate over
already replayed wallets. Their account, asset, policy and provider scope must
still be bound independently by their raw-proof consumers. It requires equal
canonical nonces and the same chain, advancing height and timestamp, and the
correct parent for an adjacent block. Explicit same-block permission accepts
only an identical complete wallet and header. It performs no acquisition and
does not prove ancestry across a multi-block gap.

Full `LiveLoop.observe()` obtains its historical anchor from the original NAV
or last settled action's cash proof before using new data. A continuity failure
sets and records a fixed HALT before risk checkpoint replacement or strategy
decision. The tick handler preserves this specific halt. The transaction
authority persists an observation's HALT before returning its refusal, without
creating a nonce reservation or contacting the provider. The next request
refuses before further acquisition. Ordinary transient observation failure
without a HALT does not save state or contact the provider.

The new halt is absent from the ordinary rearm allowlist, which was not widened.
Independent receipt/history, gas, inventory, risk, runtime, simulation, release
and envelope checks remain required. No new release or transaction permission
was created. A still-open owner release does not explain an account nonce change.

The read-only recovery collector and rearm review consume the same anchor even
after a confirmed action clears old NAV references. They retain their fixed
redacted refusal messages and all existing full-history/loss checks. Exact
same-block post-cash recapture remains possible; feed migration retains its
stricter requirement for a newer block. Read-only operational precheck benefits
from observation refusal without persisting the in-memory halt. Shadow mode,
pending-transaction resolution and transaction encoding were not changed.

## Validation and retained failures

The first related suite retained two failures: the sandbox refused a local Unix
test socket, and a rearm test expected the established `reconcile` diagnostic.
The fixed diagnostic was preserved and the socket suite rerun with local test
permission. The expanded suite passed 243 tests, exit 0. Subsequent targeted
checks passed 33 new cases, including a transient-failure no-save test. Ruff and
strict mypy checks passed. Synthetic tests exercise both key-boundary refusal
and unchanged risk, proof references, wallet units and gas totals after HALT.

The final full gate at `observation-continuity-quality-v1/report.json` passed
all four checks with exit 0 and no timeout/launch failure. The outer Make process
also exited 0: **4415 tests, 138 subtests**, 248 typed Python source files and
eight frontend modules. Nine existing deprecation warnings remained. Bandit
reported zero medium/high and 98 low findings. Secret scanning made no credential
verification requests; Compose verification used absent/synthetic RPC values
and no network calls. No secret-scanner exception or rule was changed.

Canonical quality evidence hash:
`062287ab43246d521713430f8c62b2a19b7b8e26e11892a406c380a87fc88e51`.
Independent reverification checked each exit/log hash, the outer log/exit,
canonical report and all 623 frozen runtime/test files; its record hash is
`f2780fe924f88af694b68aca80449541ad620fb291e5fd0aedf47445c2f26e6f`.
This is internal self-review, not an external audit or a capital-moving release.

No production volume, canary key or repository `.env` was opened; no original budget was reset,
copied or replaced. Production RPC, signatures and broadcasts were zero. USD
500 total capital, USD 200 daily loss, USD 500 cumulative loss and both
1800-second feed ceilings are unchanged. Fresh production USD/NAV, real gas
runtime fingerprints and the five-to-eight policy upgrade, simulation and
current receipt/unwind/restart acceptance remain outstanding. No previously
consumed RPC grant was reopened by this code or its tests.
