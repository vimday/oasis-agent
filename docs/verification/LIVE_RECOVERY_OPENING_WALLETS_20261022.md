# Offline opening-wallet prerequisites for recovery — 2026-10-22

## Problem and correction

At parent `03e1e51`, both reconciliation commands checked retained state history
and the frozen RPC request budget, but then opened an evidence store in ordinary
creation mode before verifying the original wallet proofs. Missing local evidence
could therefore create an empty database and lead to receipt RPC requests even
though cash reconciliation could never finish. Settlement still refused later;
the defect was avoidable acquisition and ambiguous recovery-file creation.

Both `run_live_lp --reconcile-only` and `run_live_authority --reconcile-only` now
check the existing evidence file before constructing RPC clients, acquiring cost
proofs or contacting authority status. A read-only SQLite snapshot replays the
two original wallet captures. The check requires distinct sources at the same
block, the configured chain/account/token/implementation/decimals scope, agreeing
full balances and runtime/header observations, and the reserved nonce. An unknown
strategy signing attempt additionally binds its declared block number/hash to
the wallet references. Missing files, symlink paths, empty or corrupt stores,
missing references and disagreements refuse with a fixed redacted diagnostic.

The prerequisite neither writes/migrates evidence nor changes pending state,
loss accounting, reservations or RPC counters. The existing budget check remains
in place. A validated idle journal still exits without evidence or RPC. Authority
attempts whose signing result is unknown remain refused as before.

This check does not replace later provider-identity, signed-commitment, receipt,
fee, canonicality, cash-flow or settlement checks. The existing service lock and
private-directory controls remain necessary: a read-only preflight is not an OS
guarantee against another process deleting files afterward. The ordinary live
loop and first-time initialization are not changed by this CLI prerequisite.

## Verification

The previous strategy CLI test expected an empty receipt database to be created
when opening evidence was absent. It now expects refusal with no database and
forbids construction of RPC/session/signer clients. Both-service tests cover
missing and empty evidence files with an otherwise valid existing budget, and
compare all retained state/budget files byte-for-byte after refusal.

Pure replay tests exercise both pending and uncertain-signature states with
synthetic raw wallet captures: valid quorum, missing/empty database, bad reference,
duplicate source, wrong nonce/account/block and individually valid but disagreeing
balances. State and local files remain byte-identical. Existing authority recovery
tests still settle synthetic receipts exactly once and preserve pending on cash
disagreement. None of these fixtures is production acceptance.

The first focused run exposed an incorrect call to a test fixture helper; after
adding its required second provider fixture, the focused suites passed. Mypy then
found an overly narrow inferred nonce type; explicit untrusted-input typing fixed
that error. Final focused tests, lint, formatting and mypy passed.

`make live-quality QUALITY_OUTPUT=reports/review-20261022/recovery-opening-wallets`
passed all four commands with exit code 0, no timeout and no launch failure:
Ruff/format/mypy, 3,315 tests and 138 subtests, eight frontend syntax checks, no
unreviewed secret candidates, no medium/high Bandit severity findings and seven
offline Compose service expansions. One dependency deprecation warning and 85
low severity Bandit findings remain. Raw logs and individual command hashes are
retained in the output directory. Report evidence hash:
`84a68303ec4c5e841a54efdc438ca9fa00ef336fe5b47f2fbe939551485cbf46`.

## Production boundary

No real RPC/oracle request, production key access, signature, broadcast or service
start occurred. No production evidence, state journal or budget was read/copied,
reset or changed. Previously recorded paid requests remain eight under the same
cumulative USD 0.10 authorization; actual billing remains unverified. The Docker
production image was not rebuilt.

Fresh USD/full NAV, production dual-source simulation/runtime evidence and real
canary dual-service receipt/cash recovery remain unaccepted. Capital/loss limits
remain USD 500/200 daily/500 cumulative, with the 1800-second feed-age maximum.
