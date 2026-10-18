# Live prerequisites acceptance — 2026-10-18

The owner selected local Docker, authorized importing the existing `.env`
`OASIS_PRIVATE_KEY` into the authority-only volume, and approved **at most USD
0.10 for this acceptance's paid RPC reads**. These authorizations do not waive
the 1800-second price-age rule or authorize fabrication of receipt evidence.
No mainnet transaction was signed or broadcast. Release remains closed.

## Confirmed

### Production key configuration

`scripts/provision_live_key.py` reads host metadata only and invokes the
immutable runtime image. `scripts/import_live_key.py` extracts the one named
key inside a network-disabled container, requires a regular 0600 source,
rejects duplicate/missing/interpolated entries, verifies the configured public
address before writing, and exclusively installs a 0600 key under UID/GID
10001 in a 0700 directory. Existing volumes and files are never overwritten.
The source remains unchanged. The container does not sign.

A real Docker Desktop trial with an ephemeral random key exposed host-to-VM
UID mapping (501 on host, 0 inside the root provisioner). The corrected wrapper
checks both host ownership and mapped file metadata, then passes the observed
mapped UID to the importer. The full ephemeral import/read trial passed before
production provisioning. Its retained report hash is
`6881a2781ace20932a5b7caeeb48abf42e829ce9e3c95ec01ea3477c4e5321ed`.

The production import then succeeded for
`0xe653df20a8c525edf381942ffc47704b7bdfe97d` into `oasis-canary-keys`.
A second network-disabled container running UID 10001 loaded the installed file
and independently derived that address. Only public metadata is retained in
`reports/review-20261018/production-key-provision.json`.
Runtime image: `sha256:8830f53c748106ca5f13faddc6154e48d540a86e8cb908a22576f6dd086315c5`.
Production services were not started. This result applies to this local Docker
host, not every checkout or deployment.

### Two-source RPC capability and historical full cost

The fixed, free 1RPC recheck spent four calls in its existing durable eight-call
ledger (seven charged total). It supplied the chain, receipt and header, then
refused `debug_traceTransaction` with error -32600, method not allowed.

[dRPC's official Base endpoint](https://drpc.org/docs/base-api) and the existing
Alchemy endpoint each spent four calls on transaction
`0xb69f8a1235b357dadfd03cde94bafdc81ef0e9ad6e7d4685cc73166eae686fa6`.
Chain ID, full receipt, full header and full callTracer JSON matched exactly.
The trace contains **one frame**, so this establishes access for that object,
not nested LP completeness or sustained availability. Canonical trace hash:
`44ce5067287347e47d9006268da23ef81a31ae09751e6a36cb69033065aee96f`.
Raw reports are retained under `reports/review-20261018/{drpc,alchemy}-trace.json`.

Separately, the existing two-source evidence for real LP transaction
`0x22a7c0e715455f2ad99f57bcf7789edac2faf8c3506d1b911e8319bb7205c7f2`
at Base block 51949838 was replayed **offline**, with the original evidence
database unchanged. Both proofs derive 8,959,320,000,000 wei execution gas plus
441,778,148,158 wei L1 fee, zero operator fee and zero blob fee: total
**9,401,098,148,158 wei**. This was a third-party transaction, not canary cash.
Report: `reports/review-20261018/cost-recheck.json`; evidence hash
`ea1a8924faf9af8ca42b6e9f06b20a628d3869f972e455dace2e95ee879fe232`.

The distinct nested LP trace/full-cost acceptance scope locked that known
transaction and block, two endpoint fingerprints, nine requests per provider,
three-second spacing, zero retry and a separate sufficient budget before I/O.
It stopped on its first dRPC request without a retained response. Only one free
attempt was charged, and **no paid call** was made by that scope. It is refused,
not passed; do not relabel or restart it to evade the closed no-retry scope.
Scope/report/budget remain in
`reports/evidence/lp-trace-cost-acceptance-20261018`; report hash
`7d5cf1a239d753437193c6a2650cf6e8d97a401cfc2c08ae51ba14f9f3103292`.

**Paid use this acceptance: four requests total.** Reserving 1000 CU per request
at the published [USD 0.525 per million CU rate](https://www.alchemy.com/docs/reference/pricing-plans)
gives a conservative USD 0.0021 allowance. The
[method schedule](https://www.alchemy.com/docs/reference/compute-unit-costs)
lists trace at 40 billable CU (1000 is throughput CU). The nine-call scope's
unspent reserve was USD 0.004725; the combined predeclared allowance was USD
0.006825. These are public-rate estimates, not a verified account invoice.
The four-call paid diagnostic ledger is exhausted and retained unchanged.

### Process recovery

`test_live_receipt_process_recovery.py` starts two independent processes with
separate evidence and execution journals. Each acquires synthetic two-source
approval/cost/cash proof, then pauses after in-memory settlement but before
state publication. Both processes are SIGKILLed. Fresh processes load the
durable pending state, revalidate proofs and each count exactly one action and
218 wei total gas. Further reopen checks preserve those totals. No signer or
broadcast path is available in this test.

This extends the existing real Unix-socket lost-reply/restart test. It is a
synthetic process-recovery result, **not deployed dual-service reconciliation
of a real canary receipt**.

## Refused price acceptance and unresolved work

One bounded free Base/PublicNode NAV capture spent 24 and 23 requests, without
retry. Both sources agreed at Base 8453 block **52753512**, hash
`0xf64487759a7b042837304c1ad62f313dbdf6972b69c4deaaac41db4c67e49641`,
timestamp 1792296371. ETH/USD age was 736 seconds. USDC/USD round
55340232221128654907 had answer 99987656 (8 decimals), updated at 1792241461:
**54,910 seconds old**, exceeding 1800. NAV correctly refused. Raw wire and
report remain in `reports/evidence/live-nav-acceptance-20261018`; report hash
`40c9a947c0e1bfcfae93d2b46675d5af5b0a51791b43a6590dc81ec578c82d3a`.
The script subsequently gained a 120-second wall-clock head-age check before
and after capture; this change was tested offline, without repeating the capture.
Its scope is wallet assets only, not complete position/history acceptance.

The [Pyth Base sponsored feed list](https://docs.pyth.network/price-feeds/core/push-feeds/evm)
specifies a one-hour heartbeat, so it does not establish continuous compliance
with the current 1800-second limit. The pinned
[RedStone Base manifest](https://github.com/redstone-finance/redstone-oracles-monorepo/blob/ee55521d0949a9f944d9568bd34671ab14c56118/packages/relayer-remote-config/main/relayer-manifests-multi-feed/baseMultiFeed.json)
declares USDC feed `0xDd87FD0FD6F68AcB6897d05fCf31F3AB1165a49F`, inheriting a
24-hour heartbeat and 0.5% deviation. No alternative feed was pinned or deployed,
no freshness limit was widened, and no stablecoin peg was substituted.

Still required: nested full LP trace/cash acceptance from two reliable sources;
a validated fresh USD price source; history/funded-basis and full-position NAV
acceptance; production two-source simulation/freshness acceptance; and deployed
dual-service recovery against an actual canary receipt. The key prerequisite is
now met on this host, but these remaining conditions still block new actions.

## Validation

`make check`: **3015 Python tests and 138 subtests**, **70 Solidity tests**,
8 frontend modules, formatting/lint and mypy passed. Provisioning tests use
fresh random keys, never the production key. Bandit on the three new scripts
reported no medium/high issue. The code/config/test secret gate passed.
The final validation image
`sha256:fe577bfcb68f251b33ea22dabdabbe8abebf1099510a3295a190383c27668de4`
passed 59 selected Linux tests with network disabled, read-only root, non-root
UID, no capabilities and no-new-privileges, including both process recovery
tests and all provisioning tests. The later release-status wording change was
checked separately; it does not open the gate or alter execution behavior.
Runtime files, private material and raw reports are excluded from this commit.
