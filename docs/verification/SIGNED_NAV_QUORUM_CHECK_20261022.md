# Signed NAV quorum consumer check — 2026-10-22

Continues [the signed-price NAV candidate](SIGNED_NAV_CANDIDATE_20261021.md).
That producer checked agreement during capture, but its single-reference replay
did not establish that a consumer had both matching provider proofs or that the
candidate was still fresh when consumed. This increment adds a complete-pair
offline check and a current-host-clock operator command. It opens no release gate.

## Confirmed implementation

`verify_signed_nav_quorum()` requires exactly two references in the expected
provider order, distinct aliases, one block and one shared signed-price binding.
Both references are replayed from their original raw evidence. Their exact wallet
references must cover the pair named by the binding. The caller must explicitly
declare its expected position (or `None`); omitting a declared LP, changing its
fields or passing noninteger fields refuses. Aliases are operator expectations,
not independent proof that two providers have different infrastructure.

Both reconstructed values and the raw core-state results must agree. A fee-growth
difference that rounds to the same USD NAV still refuses. Two individually valid
captures with different price bindings cannot be spliced into one quorum.

The consumer clock must be an integer, cannot precede the capture clock, and must
put the account block within 120 seconds, with no future block. The original
price packages are authenticated again against that clock and the configured
price age, still capped at 1,800 seconds. Capture-time freshness is never reused
as consumer-time freshness. A passing result retains exact evidence references,
account/block/configuration, inventory value, price verification and a report hash.
It always carries `execution_eligible: false`.

`ReadOnlyEvidenceStore` opens an existing SQLite database using `mode=ro`, enables
`query_only` and holds a read transaction. It does not create or migrate tables.
Concurrent WAL appends cannot change the snapshot after its first read. The
existing writable store and its migration behavior remain available to producers.

## Operator entry point

```sh
.venv/bin/python -m scripts.verify_signed_nav \
  --input /absolute/path/to/request.json \
  --config /absolute/path/to/live-config.json \
  --evidence /absolute/path/to/evidence.db \
  --out /absolute/path/to/new-check.json
```

The request has exactly these fields:

```json
{
  "schema": "oasis.signed-nav-check-input.v1",
  "evidence": ["first complete EvidenceRef object", "second complete EvidenceRef object"],
  "sources": ["first declared provider alias", "second declared provider alias"],
  "position": null
}
```

The two string placeholders above must be replaced with the complete reference
objects returned by the candidate producer; they are not accepted inputs. When
an LP is declared, `position` must instead contain its integer `token_id`, `lower`,
`upper`, `liquidity` and `opened_at` fields. The command does not discover assets
or create a candidate. The supplied configuration is parsed through the same
strict parser as the production configuration.

Request/configuration files are bounded regular files, at most 64,000 bytes;
symlinks, FIFOs and duplicate JSON fields refuse. The database must already exist.
There is no historical-time flag, RPC endpoint, credential loader, signer or
broadcast option. It samples host time before replay and checks it again before
publishing success, refusing expiry or a clock regression during replay. Host
clock integrity remains an external deployment prerequisite, not authenticated
network time. Reports are exclusive new files; previous reports are preserved.
Exit 0 means the candidate check passed at the recorded times, exit 3 means
verification refused. Neither result is a production execution permit. Failure
diagnostics never echo arbitrary input or database contents.

## Evidence scope and remaining work

Positive tests combine the existing authentic historical oracle signatures with
synthetic chain data and controlled test clocks. The real CLI under the current
host clock refuses that historical candidate. Tests also cover independently valid
but conflicting proofs, duplicated/foreign source aliases, declared position
omission, capture-clock regression, account/price expiry boundaries, expiry while
replaying, read-only snapshot consistency, input redaction and output preservation.

No real RPC, price gateway, production key volume, signature or broadcast was used.
Paid acceptance usage remains eight requests under the same cumulative USD 0.10
budget. No budget ledger was reset, copied or replaced. Capital/loss limits remain
USD 500 / USD 200 daily / USD 500 cumulative. No production Docker image was rebuilt
or service started for this offline command.

Fresh authenticated prices, source-policy review, bounded production acquisition,
complete-account/history reconciliation, signer integration, current simulation/
runtime evidence and real-canary receipt/cash recovery are still required. This
command validates declared ETH/USDC and one declared LP only; it cannot certify
that undeclared holdings are absent or that the strategy is profitable.

## Validation result

The focused consumer/store suite passed 34 tests and three subtests. The complete
`make live-quality QUALITY_OUTPUT=reports/review-20261022/signed-nav-quorum` run
passed all four commands with exit code 0, no timeout and no launch failure:

- Ruff, formatting, mypy, 3,226 tests and 138 subtests; one dependency deprecation
  warning remains. Eight frontend modules passed syntax checking.
- Secret scanner: no unreviewed candidates and no credential verification calls.
- Bandit: zero medium/high severity findings; 85 low severity findings remain.
- Offline Compose expansion: seven services, absent and synthetic RPC cases;
  no Docker service was started and no chain request was made.

The output directory retains every command log and its digest. Report evidence
hash: `82065d8deb60546bd75052c4b9faf55851263e665fe06289028d5205d243344e`.
These checks validate code and synthetic/historical test cases, not production
source reliability, current real-account NAV or profitable operation.
