# Verified wallet continuity during halted feed migration — 2026-11-05

## Confirmed defect and bounded correction

Review of the pending five-to-eight runtime-policy upgrade found an earlier
continuity defect in the implemented strict-feed migration. Its raw two-provider
NAV review checked balances, position identity, timestamps and settlement
anchors, but did not compare the new account nonce with its historical wallet
checkpoint. Two providers agreeing on an unexplained nonce change could therefore
produce a passing review and a migrated, still-halted journal. This was not a
signing or broadcast bypass; later independent rearm checks remain separate.
Accepting an unresolved checkpoint is nevertheless a defect in the migration
gate. A newer timestamp also did not by itself prove a greater block height or
the correct parent for a direct-child block.

`LiveLoop._check_nav_checkpoint()` now returns the wallet already reconstructed
from the original configuration's raw NAV proof. When a confirmed action has
cleared those NAV references, it returns the wallet reconstructed from that
action's retained cash proofs instead. The cash pair also explicitly agrees on
nonce. No summary, caller-supplied nonce, current-policy relabeling or network
request supplies this anchor. Existing loop callers still verify and discard
the return value. Signing, broadcasting, transaction construction and policy
bounds are unchanged.

`feed_transition.review_service()` consumes that verified anchor and refuses:

- any nonce advance or regression while the old holdings are being preserved;
- a new proof at the same or a lower block height;
- a new block timestamp that does not exceed its wallet anchor's timestamp,
  including when settled cash is newer than the last risk observation;
- a direct-child block whose parent is not the historical block hash.

The existing current-time/feed deadlines, whole holdings, position, settlement
anchors and integer loss checks still apply. This compares direct neighbors
only; it does not prove ancestry across a multi-block gap or current finality.
Both migration application paths use the review and refuse before their first
transition write. Risk basis, external flows, gas history, STOP and HALT remain
unchanged on refusal. A successful migration still remains halted and is not
an execution permit.

## Regression evidence and scope

The first test attempt retained five fixture failures because the synthetic
post-approval helper had no funded risk baseline. A second attempt retained six
fixture failures because its NAV helper's synthetic runtime did not match the
configuration. Those failures are not evidence of the production defect. The
fixture was corrected by deriving its baseline from a retained synthetic NAV
and using matching synthetic pins; production pins and validation were unchanged.
The third before-change run then reproduced **nine actual assertion failures,
four passes**, exit 1, including both services accepting the same unexplained
nonce. Its log SHA-256 is
`e358be64410cbe75fd6acb1b41a220e818f61852dda9055909c410580299b34e`.
All three original logs remain under
`reports/review-20261105/transition-continuity/`.

After the first correction and additional single-service refusal cases, the focused
suite passed **159 tests**, exit 0. The first complete quality gate passed all
four checks, **4380 tests**, 138 subtests, 248 typed source files and eight
frontend modules, with nine existing deprecation warnings; its evidence hash is
`d92e07868ed3bf21f91a8af848a0d41ba9454cfa87d261de5aab69d64fae8a6d`.
Logs remain in `transition-continuity-quality-v1/`. This pass describes the
intermediate version, before the following additional timestamp guard.

Further review identified that a settled-cash anchor may be newer than its risk
observation. A new NAV later than the risk time but at the cash block's own time
still passed. Two timestamp cases reproduced one actual failure and one existing
refusal (exit 1); the retained `timestamp-before.log` SHA-256 is
`c89646862888901462de03cbb9e16a69ca06ff84e3175cba1d2842665cb2ba56`.
The final guard compares against the cash/NAV anchor's own timestamp too.

The resulting focused suite passed **161 tests**, exit 0. It covers NAV and settled-cash anchors,
nonce advance/regression, equal/regressed heights, a wrong adjacent parent,
valid direct children, offline replay and unchanged journal/projection/STOP/proof
bytes after refusal. Ruff and targeted strict mypy both exited 0. These are
synthetic regression proofs, not current chain or production acceptance.

The recovery README also removes its stale assertion that no online scope had
ever been executed by the collector. Its preceding historical account already
records the terminated campaign and separately authorized single-source retry.
The replacement explicitly keeps that campaign closed and says a new offline
plan is not another grant; original wording remains in Git history.

The final `transition-continuity-quality-v2/report.json` passed all four planned
checks, exit 0 with no launch failure or timeout; the enclosing Make invocation
also exited 0. Python/frontend passed **4382 tests, 138 subtests**, 248 typed
source files and eight frontend modules. The nine deprecation warnings were
existing WebSockets/multithreaded-fork warnings. Bandit found zero medium/high
issues and 98 low issues, not zero findings overall. Secret scanning performed
no credential verification requests. Compose verification used only absent and
synthetic RPC configuration and performed zero network calls.

The canonical quality evidence hash is
`443bf1950f59522ac34faf707c5b7d409b2db1d3778bf630b390a8e290f288be`.
Independent reverification checked every exit and log hash, the outer log and
exit, the canonical report and all 16 frozen input hashes; its record hash is
`6407214ae1005453e3104009b37c717065c059381aae6b55aff73f037e0dd5cc`.
AST review confirms that only the two checkpoint-verification methods and the
feed-transition review function changed. This is internal self-review, not an
independent security audit or operator approval of a capital-moving release.

## Committed Linux image verification

Implementation commit `e5a69daa1b055b29e0cabbf154a6982a49a5c8a1` was built
exclusively from its Git archive with `--pull=false` and the cached hash-locked
dependency layer. Build, stopped-image source verification, the keyless Linux
smoke harness and the enclosing builder each exited 0.

- Image: `sha256:56eb8798be883c01611d3a5749bb57ce82f4c7a4c76997033ea1bcce64d1242f`.
- Source manifest: `aed7d89a1ce3b74d924cbaa9dc691f806e4009b6147b29925aca62e384f7a783`.
- `reports/review-20261105/runtime-e5a69da/summary.json` evidence hash:
  `3e7da6671a85f3d8dc20d9c10047c641d97c649b85994e83609aac9f71f46f2b`.
- Independent artifact reverification hash:
  `4b305225c06c506901d93604cb3a8667c82eea5e0392e05e40841e74b8f82073`.

All 355 application files matched both committed and local bytes. All 357
archived files, including the two Docker build files, matched the implementation
commit. Log, script, archive, source-manifest and canonical report hashes were
independently checked. Dependencies, base runtime and the local Docker daemon
remain trusted inputs; application source identity is not their verification.

The smoke used UID 10001, no network, a read-only root, no capabilities,
`no-new-privileges`, a bounded PID count and temporary scratch space. It mounted
no source, production, key or socket volume. Six explicitly synthetic retained
raw-NAV scenarios were replayed from a scratch SQLite store: nonce advance and
regression, equal and regressed heights, wrong direct-child parent and a valid
direct child. The first five refused; the valid case produced only a preview.
Both state and evidence bytes stayed unchanged. Its feed pin override belongs
only to the disposable synthetic verification process; production pins were
unchanged. Settled-cash and timestamp boundaries were covered by the host suite,
not this six-case image smoke. All outputs keep execution eligibility false.
This is an image candidate, not production deployment or real chain acceptance.

No new RPC authorization was consumed, no original production volume was opened,
and no service, key, configuration, budget, STOP, signature or transaction was
changed. The existing USD 500 capital, USD 200 daily loss, USD 500 cumulative loss
and both 1800-second feed ceilings remain unchanged. The planned runtime-policy
upgrade review/application, fresh USD/NAV, three real gas-oracle pins, production
simulation and current receipt/unwind/restart acceptance remain outstanding.
