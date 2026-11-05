# Paired halted runtime epoch — 2026-11-05

`python -m scripts.migrate_live_runtime_transition` supplies the previously
missing paired five-to-eight-pin journal transition. Default mode performs the
existing read-only preflight. Explicit `--apply` replays the exact approved
retained review and appends both original journals in one transaction. It
preserves HALT, funded basis, external flows, native gas, holdings, receipt
anchors, original events and budget ledgers. No production migration was made.

This is a local state-writing command, not a read-only RPC grant. It never
acquires chain data, opens an RPC budget, holds a key, signs, broadcasts, starts
a service, removes STOP or clears HALT. No repository `.env` or canary key is
read. Configuration files are not applied or rewritten. The source target and
the exact review hash need explicit operator migration approval; observation
of a gas fingerprint does not approve its source code or authorize trading.
`execution_eligible` and `runtime_approved` remain false after application.

## Exact policy and state boundary

The target must have all eight validated fingerprints, both feed ceilings
exactly 1800 seconds and the owner's USD 500 total / USD 200 daily / USD 500
cumulative limits. Its original five-pin policy must match the journal binding
exactly, optionally with the precisely recognized historical USDC 90000-second
age. Only the three gas pins may be added; the historical feed age may tighten.
Account, pool, original pins, gas, approvals, risk and every other policy field
remain bound. An already expanded policy cannot be replaced by another epoch.

The new pure `runtime_epoch` verifier binds the service's exact source tip and
snapshot hash, the complete target config, both service NAV pairs, the shared
gas pair, and the complete retained preflight including its approved canonical
digest. It requires matching source/target hashes, source aliases, blocks,
observed gas pins, raw NAV result hashes, funded basis and external flows.
The preflight must be a passed, non-executing result with a bounded deadline
no later than block time +120 seconds. Ambiguous boolean/integer report values,
changed approvals, foreign refs, pending operations and unhalted sources refuse.

Its only financial delta uses the existing integer checkpoint calculation.
The historical feed transition now calls that same extracted calculation. AST
comparison confirms its original validation prefix and checkpoint statements
are unchanged apart from extracted parameters and return value; the extraction
review hash is `20d30ec777f52fe2c38bc7457f8428b71daae367de02a92c72f757ceafb9a2ee`.
Risk previews must equal a fresh derivation; neither basis nor external flows
can be rebased. Daily rollover follows the existing runtime convention. History,
gas, nonce/cash anchors, inventory and every other state field remain unchanged.

Journal replay accepts exactly these sequences: one historical strict-feed
epoch, one runtime epoch, or strict-feed followed by runtime. It reads at most
three metadata rows to reject an inventory above the two-epoch bound. Unknown
schemas, repeats, reversals, orphan records and unsupported third epochs refuse.
Existing events and transition metadata remain append-only; their original
bytes and hash links are retained. New ordinary state snapshots still use CAS.

## Raw review, locks and commit

The application re-derives the retained report from raw proof at its original
`reviewed_at` to check its exact canonical digest, then independently checks
the **current** clock throughout application. It cannot extend the original
deadline. Byte comparisons also distinguish values Python equality conflates.

Both original directories must be private and owned, with their existing
state, journal, proof store, lock, STOP and budget files. Gas proof must be a
distinct artifact. Files are checked for aliases and identity/change metadata;
proofs, STOP and budgets cannot change between review and commit. Persistent
SQLite header versions reject WAL before opening a reader that might create
recovery sidecars. Missing inputs or hot journals require inspection, not recreation.
The header guard follows the published [SQLite file format](https://www.sqlite.org/fileformat2.html).

After raw replay, the writer obtains both exclusive nonblocking service locks
and checks the approved source tips/states again. It uses one SQLite `ATTACH`
transaction, requires DELETE rollback mode and FULL synchronization on both,
and refuses journals on different filesystems. Each source is re-read under
the transaction before staging. Freshness, original artifacts, target/ref hashes
and both staged record hashes are checked again before a single COMMIT.
Either both epochs commit or neither does under SQLite's rollback protocol.
This depends on the filesystem/VFS honoring SQLite locks and fsync; it is not
a hardware power-loss certification.
The underlying assumptions are SQLite's [rollback and multi-file commit
protocol](https://www.sqlite.org/atomiccommit.html).

Only after both authoritative journals commit are JSON projections updated.
A projection failure returns a committed-but-incomplete result and exit 3;
both journals remain authoritative and HALT stays set. Retrying migration is
refused. A process death before commit leaves recovery artifacts for explicit
inspection. The command never deletes sidecars or automatically retries.

## Operator command

The inputs can be the joint collector's `runtime-target.json`,
`runtime-references.json`, `gas-evidence.db` and `runtime-review.json`.
Default preflight accepts no apply-only arguments:

```sh
python -m scripts.migrate_live_runtime_transition \
  --strategy-dir STRATEGY_DIR --authority-dir AUTHORITY_DIR \
  --config PROPOSED_PLAIN_CONFIG --references RUNTIME_REFERENCES \
  --gas-evidence GAS_EVIDENCE_DB
```

After explicit approval of that exact retained proposal, append
`--apply --review APPROVED_REVIEW.json --approved-review-hash EXACT_HASH
--out NEW_ATTEMPT_DIRECTORY`. The attempt must be new, canonical and outside
both original directories. Before any journal write, an unresolved attempt
marker and its directory entries are fsynced. Failures after that marker report
an unresolved outcome requiring inspection of both originals. Exit 0 means
the review passed or both halted epochs and projections completed; it never
means live execution is permitted. Default five-pin configuration is unchanged.
Retain both original proof stores, the separate gas-evidence database and the
input/attempt records. The journal's content-addressed references and review
hash do not replace their raw proof. Migration copies or deletes none of them.

## Validation and limitations

Thirty-seven new cases and 123 related tests passed, exit 0. They cover both
source feed epochs, exact history/budget preservation, prior feed + runtime,
unsupported third transitions, accounting/approval/ref tampering, boolean
ambiguity, stale input, WAL/recovery sidecars, missing budgets, different-filesystem
refusal, peer rejection, commit refusal, post-stage expiry/proof mutation,
staged-record mutation, default/explicit CLI and projection failure.

Three actual SIGKILL cases stop after first staging, both stagings and commit.
Before commit, application refuses the retained hot artifacts without modifying
them; explicit fixture inspection of both originals recovers the old chains.
After commit, old projections still lead to both new authoritative halted
chains. These are disposable synthetic tests, not production recovery drills.

Initial formatting refused an indentation error; lint/style and two nullable
block type errors were corrected. A targeted Bandit check found one medium SQL
construction warning, resolved by choosing fixed SQL statements without a
suppression. The first expanded run had three crash fixtures refuse before
staging because the child omitted its synthetic feed-runtime override. That
fixture was corrected explicitly; no production pin was changed. All original
failure logs remain under `reports/review-20261105/runtime-epoch/`.

The full gate passed all four checks and outer Make with exit 0, no timeout or
launch failure: **4491 tests, 138 subtests**, 249 typed source files and eight
frontend modules. Ten existing WebSockets/multithreaded-fork deprecation warnings
remain. Bandit reports zero medium/high and 98 low findings, with no scanner
rule/baseline change. Secret scanning made no credential verification requests;
Compose used absent/synthetic inputs with zero network calls. Every exit, log
hash, canonical report and all 631 frozen runtime/test inputs were rechecked.

- Full quality evidence hash:
  `8770cdc6855ccef68c575569e2347474b5dd66fd45591c957434660b881540f1`.
- Independent quality reverification:
  `8a1fbd8f6e01f4e1ef8d7564b1965522af85178bbd5f41c60378effdf43b3e61`.
- Three synthetic packets, eleven journal/proof/control files each, **zero
  serialized budget files**:
  `f8eb8427e818238eed6a94fc3b238f8eb00cb65cedfbb4095ee3dbd724edbae9`.

The host smoke uses explicit synthetic feed/clock substitutions and disposable
test budgets. During migration, real network, RPC, evidence writes and budget
opening are forbidden. Strict and prior-feed cases return CLI 0; the deliberately
failed historical projection returns the expected CLI 3 while both journals
remain committed. Expired/repeated attempts refuse, source event prefixes and
capital basis remain intact, and budgets stay byte-identical with the same inodes.
This is internal self-review, not an external audit or operator release.

Production volumes, canary keys and `.env` were not accessed. Production RPC,
funding, transfers, signatures, broadcasts, service starts and migrations were
zero. Fresh real USD/NAV/gas evidence, production runtime/simulation acceptance,
full current receipt recovery and unwind/restart still remain required. The
eight historical paid requests' costs remain unresolved and the cumulative
$0.10 cap is unchanged. This implementation neither spends nor renews a grant.
