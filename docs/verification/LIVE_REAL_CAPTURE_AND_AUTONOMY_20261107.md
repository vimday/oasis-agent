# Real gas discovery, refused NAV, and autonomy fixes — 2026-11-07

## Observed production evidence

The owner's new one-time authorization covered at most 209 free read requests
(105/104 across the two existing providers), 120 seconds, first failure stops,
no retry, plus the missing authority maintenance HALT. It did not authorize
starting services, applying a runtime transition, signing or broadcasting.

The existing local Docker strategy and authority were stopped. A network-disabled,
keyless maintenance container appended the missing HALT to the original authority
journal. The strategy was already halted. Only
`/authority/state.json.journal.sqlite` changed; both STOP files, state projections,
evidence stores and original budget ledgers remained unchanged during maintenance.
The subsequent approved joint plan hash was
`e39bf32c05cde15dcbab85bd4147755117558a6348a36eed4fda7edea46bdbdf`.

The collector used committed source `ef5429d9f8817afcf3a7659e77315537b2537744`
in immutable image
`sha256:44fbde1dd79549c0a40632496e0715692cce884730fd37456ed89212fd8ce74a`.
It had no key volume, signing socket or repository `.env`. It appended usage to
the original budget files and retained partial evidence in the original stores.
No budget was reset, replaced or copied.

At 2026-11-06 16:51 UTC (2026-11-07 locally), both declared providers agreed on
Base chain 8453, block **53967555**, hash
`0x3c0438aabdd157d094e7634f8c002c1bfc9e168600d8211ddfd7d73bf831cb4c`,
timestamp `1793983886`. They supplied matching gas-oracle discovery proofs:

| Binding | Observed value |
| --- | --- |
| Oracle | `0x420000000000000000000000000000000000000f` |
| Proxy runtime Keccak-256 | `0x1f958654ab06a152993e7a0ae7b6dbb0d4b19265cc9337b8789fe1353bd9dc35` |
| Implementation storage word | `0x0000000000000000000000004f1db3c6abd250ba86e0928471a8f7db3afd88f1` |
| Implementation runtime Keccak-256 | `0xe9fc7c96c4db0d6078e3d359d7e8c982c350a513cb2c31121adf5e1e8a446614` |

The exact six-read discovery per source replays from retained raw bytes. Its
candidate configuration hash is
`357cb773b1f05ea42f531be0e95fedd0d91a76456e184b4beb6ac2c429941202`.
This is observed code identity, not source verification, operator runtime approval
or an applied production configuration. Aliases alone do not prove infrastructure
independence.

The attempt **refused**, with worker, supervisor, container and host exit **3**.
The supervisor ran for 81.001 seconds; the host wrapper finished in 83.276 seconds
without its outer timeout. All 85 reserved requests have matching successful raw
response records. The first complete strategy NAV reconstruction then refused:

| Feed | Answer, 8 decimals | Updated timestamp | Age at pinned block |
| --- | ---: | ---: | ---: |
| ETH/USD | 222568883727 | 1793983607 | 279 seconds |
| USDC/USD | 99973480 | 1793969804 | **14082 seconds** |

Both sources returned those values. USDC exceeded its 1800-second limit. Offline
reconstruction through the production `capture_nav()` validator, using exactly
the retained request/response bytes and no network, independently reproduced
`NAV USDC / USD feed incomplete, stale or future`. No complete NAV was accepted.
The authority NAV and five-receipt stage were never reached. No transaction
simulation was part of this attempt.

The original budget increments were strategy **43/42**, authority **0/0**;
final cumulative counts are strategy **946/787**, authority **974/955**. Budget
and evidence file identities/permissions were preserved. State, journal, lock
and STOP snapshots remained unchanged throughout network collection. The one-time
authorization is terminal, including its unused allowance; no retry followed.
Incremental cost was declared zero by the owner. Provider billing was not
independently verified; the earlier eight paid requests and cumulative USD 0.10
cap remain unresolved and unchanged.

Raw artifacts remain local under
`reports/review-20261107/authorized-joint-001/`; they are not committed:

| Artifact | SHA-256 / self-hash |
| --- | --- |
| Authorization | `ce29ac6033eebd46f6dc91bf8431de4a1fc398ebf87bdd999505930788ce6a02` |
| Maintenance result | `fe643cc52b5d8fe24bd19f1cbca6b263bca231f778945189278a5bb9d865b0f9` |
| Capture terminal summary | `d1a7a4bd6903f471154cac44858d21b7a248dfc5d9332c63baac8830fc550f5b` |
| Raw wire JSONL file | `6b408400ee9b713049bc5cccd6a8bdc372c2a852339a3fff09cb286419a99038` |
| Offline diagnosis | `f5594cefaf460fdd281563a9588056a08e7349a40fe71b2d08c592cab1e684e1` |

## Autonomous runtime corrections

An authority receipt/ancestry check could set `halted` and raise before persisting
the state. The next request reloaded the earlier journal and lost the latch.
`TransactionAuthority.sign()` now saves an already-set HALT before propagating
these early `ChainError` failures. It retains pending commitments and risk state;
transport errors that have not set a HALT do not invent one. Regression cases
exercise both providers, pending identity conflicts, subsequent requests and a
new authority instance after restart.

The light in-range hold optimization previously relied on a display observation
timestamp up to an hour old. It now replays the retained complete NAV quorum and
checks the block/feed deadline before any light RPC and after those reads.
Missing, stale, future or inconsistent evidence falls through to full observation.
Display timestamps and restarts cannot renew evidence. The existing light-hold
tests were retained in a dedicated file alongside new deadline regressions.

Operational budget implication: with the configured 300-second poll interval and
the unchanged 120-second NAV head limit, each normal poll needs full observation.
A held-position NAV alone costs 36 requests per source, in addition to head,
receipt and possible action checks. The 4096 lifetime per-source allowance is not
evidence of 24-hour autonomy. Do not reset it or relax freshness to obtain uptime.

The recovery collector now labels the two known ETH/USDC incomplete/stale/future
refusals with fixed machine-readable codes. Only an exact `NavRefusal` with one
exact allowlisted string is mapped; other errors remain `capture_unresolved`.
No arbitrary exception is rendered, and requests, quotas and release gates are
unchanged. Synthetic regressions reproduce 85 successful responses followed by
the refusal and prove that neither authority nor receipt acquisition begins.
This new diagnostic did not exist in the image used for the real attempt above;
its specific real cause was established by the separately recorded offline replay.

The signed-price candidate path now applies ETH and USDC age policies separately
to authenticated witness timestamps, before acquisition, on replay and after slow
verification. It retains each feed's deadline in a v2 binding. Historical v1
payloads remain replayable only when both actual feed limits pass. This corrects
the former use of the ETH limit for USDC, including the standalone verifier's
completion check. The candidate remains outside production authority; historical
signatures and synthetic chain fixtures used in tests are not fresh-price evidence.

## Remaining release boundary

Fresh complete NAV requires a price source that actually meets the 1800-second
limit. Repeating a query cannot make this observed USDC round newer. Existing
signed-price candidate acquisition is isolated from production consumers; Pyth
field decoding is not signature/trust validation. A different price source needs
its own verified current input and consistent strategy/authority/recovery policy.

The gas pins still need review and a history-preserving runtime transition with
fresh full NAV. Real full-envelope dual-source simulation, the five current receipt
anchors, original action/cash history recovery, full unwind and restart acceptance
also remain open. A `burn` simulation cannot establish a complete recenter, which
requires an actual receipt followed by new balances before swap/mint decisions.
No service was started and no transaction signed or broadcast. USD 500 capital,
USD 200 daily loss, USD 500 cumulative loss and both 1800-second price limits remain
unchanged. These results do not establish profitability or production readiness.

The initial network-disabled image build failed while trying to resolve its pip
dependency layer. The subsequent normal build reused the cached, hash-locked
dependency layer and exited 0. Original failure logs remain in
`real-evidence-preparation/`; the successful build and source manifest remain in
`real-evidence-preparation-v2/`. The failed build was not an RPC capture. A later
host `py_compile` attempt could not write its cache under the sandbox; non-writing
AST checks subsequently exited 0. Neither failure is reported as a passed check.

## Development validation

The final `make live-quality` run exited **0**. Each of Python/frontend, secrets,
Bandit and offline Compose exited 0 without timeout or launch failure; every
command log hash was recomputed against the retained report. It passed 4692 tests
and 138 subtests, type checking across 251 source files and syntax checks for
eight frontend modules. The 11 warnings comprise the websockets deprecation and
ten fork-in-multithreaded-test-process warnings; they were not suppressed.
Bandit reported zero medium/high severity issues and 98 low severity issues.
The secret gate made no credential-verification requests and Compose made no
network calls or service starts.

All 678 frozen source, test, configuration and build/dependency inputs, including
the four new regression files, were unchanged during the gate. Its report is
`reports/review-20261107/live-evidence-autonomy-quality/report.json`, self-hash
`bdecfbc6f2074227b0edb6493ba8edba48f78af8634919d3563cda27e10fbb10`;
the outer exit record hash is
`cf02bf1800380c669131448dfda49882809cb96226886bf47d1329685087eacf`.

Before-fix failures and corrected test-helper/format failures remain in the
`authority-halt/`, `light-hold-freshness/`, `recovery-refusal-codes/` and
`signed-nav-feed-deadlines/` report directories. In particular, the initial
refusal-code helper failed on tuple/list equality and did not establish a code
regression; the corrected helper subsequently produced eight genuine missing-code
failures before the implementation. The final scoped suites passed 169, 187, 59
and 150 tests respectively (overlapping suites, not additive totals). Independent
code and artifact reviews found no blocking issue in these changes. Development
checks do not turn the separately recorded production capture exit 3 into a pass.
