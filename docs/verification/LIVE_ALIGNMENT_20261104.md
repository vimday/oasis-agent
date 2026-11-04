# Live boundary alignment — 2026-11-04

## Confirmed implementation and offline verification

The current change connects four previously inconsistent boundaries:

1. The historical USDC 90000-second journal policy can explicitly migrate to the
   owner-required 1800 seconds without deleting history or resetting funded basis.
   Both service locks, original journal tips, raw two-provider fresh NAV and risk
   continuity are required. Every committed migration remains HALTed. Partial
   two-journal application is recorded as unresolved and cannot restart trading.
2. Read-only precheck can replay original retained checkpoint proofs through a
   read-only evidence snapshot while writing new diagnostic captures only into
   its temporary store. Corrupt retained proof cannot be hidden by that overlay;
   a running strategy's exclusive lock refuses the diagnostic before RPC use.
3. Backend and dashboard both require a complete account observation within 120
   seconds. A newer tick-only range check no longer renews stale NAV status.
4. The optional OKX price guard requires an explicit free-only grant and its
   original persistent ledger before any HTTP request. It batches the two prices,
   bounds response/decimal inputs, rejects redirects and non-rational prices, and
   stops after failure. Grant loading also refuses symlinks and nonregular files
   without waiting on a FIFO. It supplies neither block-pinned NAV nor OKX execution.
   Provider free allowance remains an operator declaration requiring verification.

`make live-quality` exited 0. Its four subprocesses each exited 0 without launch
failure or timeout: Python/frontend, secret scanning, Bandit and Compose checks.
Python results: **3857 tests passed, 138 subtests passed**; mypy accepted 238 source
files; frontend syntax accepted eight modules. The report and every referenced
log hash were re-derived after completion:

```text
reports/review-20261104/live-alignment-quality-locked/report.json
evidence_hash: a0fb3df9c8cbf62f2670ecb29b32f9b6353882fc68a0cdf29789171cd6b3e877
```

`make contract-check` also exited 0: 70 contract tests passed, followed by
`forge lint`. Its explicit exit record is
`reports/review-20261104/feed-migration/contract-exit-verified.json`; the log SHA-256
is `d67bf02fc10dc6d94cba387b0986765b13bc3f40d090a60e6b4193eff6f8998e`.
Foundry warned that the sandbox could not persist its external signature cache;
the tests and lint completed successfully. The original earlier contract log is
retained; the repeated check captures an otherwise unavailable tool exit result.

Focused migration/precheck/CLI checks passed 91 tests. Independent internal review
covered state continuity, old-writer refusal, proof expiry under lock, projection
interruption, shared two-service scope and partial failure. This is engineering
review and synthetic regression evidence, not an external audit or production
acceptance. Original focused logs remain under
`reports/review-20261104/feed-migration/`.

## Production evidence boundary

A keyless, network-disabled Docker read replayed both original volume journals
and historical checkpoints. All 13 inspected files, including the original
budget ledgers, were byte-identical before and after. Both service reviews still
refuse with `fresh_strict_nav_required`. Neither service was migrated, started,
signed through or broadcast through during this work.

The retained review report has evidence hash
`332545f1bd0cd431581ee0324986cc4f1cadd488321241fd3bf76302c0b9353f`.
The original artifact is
`reports/review-20261104/feed-migration/retained-review.json`.
An explicit exit-recording repeat used the same read-only/no-network boundary and
again left all 13 files unchanged. It exited 3 as expected for missing fresh NAV,
with no stderr. Its report hash is
`1040e50964aabdd5994c29008fcfae398273d1d5fa08b57774c896901b30f235`,
under `retained-exit-verified.json`; `retained-exit-status.json` records the exit.

Current production limitations remain fresh complete USD/NAV under both
1800-second ceilings, current two-source acquisition/runtime acceptance, real
receipt recovery including full unwind, and a reviewed operational restart.
Historical receipt/checkpoint replay does not prove present NAV, availability,
profitability or unwind acceptance. Paid RPC requests made by this change: zero.
No existing request grant or budget was reset, copied or replaced.

The original production audit already records five real settlements in both
services: USDC approval, two Permit2 approvals, swap and mint. NFT 3183370 still
has liquidity. This supersedes the earlier statement that the dedicated account
had no real transactions; it does not establish a complete unwind or current
receipt finality. The existing October 31 USD 500 canary approval is recorded in
`reporting/live_release.py`; no new general trading approval is implied or needed
merely to recognize it. Current acquisition scope and verified service costs are
still required, and cannot be inferred from leftover request counts.

The OKX guard is optional for the native Base path and remains disabled until its
own query credentials and verified free entitlement are available. This change
does not implement OKX swap/broadcast, custody or payment capabilities.

## Corrections and retained failures

The failed `focused-pair.log` compared prices whose rounded NAV was unchanged;
the corrected case changes a priced USDC leg. The failed `focused-cli.log` reused
an old-policy reader after a successful migration; rejection is the intended
behavior, and the corrected assertion uses a new reader. Their original logs
are retained, followed by passing `focused-pair-fixed.log` and
`focused-cli-fixed.log`. Earlier intermediate lint and integration failures are
also retained; only the final verified reports above establish passing checks.

The October 17 release document is now explicitly labeled a historical snapshot.
The recorded October 31 owner approval remains distinct from today's closed
runtime gates. No prior failed or stale evidence has been promoted to acceptance.
Final internal review additionally requested persistence of the attempt directory's
parent before migration; that improvement was implemented before final validation.
