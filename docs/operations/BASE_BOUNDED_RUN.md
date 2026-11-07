# Bounded Base live runs

`make live-docker-readiness` is an offline status check. `make live-docker` is the
owner-operated live entry. Run it under current authorization for the selected
number of Base strategy ticks. The default is one. Set `LIVE_TICKS=2` or
`LIVE_TICKS=3` to opt into a bounded multi-tick run. Each tick can submit at most
one transaction through the existing authority. A completed hold does not imply
a transaction or a profit.

1. Stop the original two services with their existing 0600 STOP markers present.
   Keep the original state, receipt and RPC journals and the dedicated key
   volume. Use the selected local Docker Unix context. Export the existing
   `BASE_RPC_PRIMARY_ALT` (Alchemy) and `BASE_RPC_PRIMARY` (dRPC) from the secure
   operator environment; the command never sources a repository `.env`.
2. Build and review the local image against the intended tracked source tree.
   Use `make live-docker LIVE_IMAGE=… LIVE_START_OUTPUT=reports/runs/<new-run> LIVE_TICKS=1`;
   create the parent directory if needed. The destination must be new and is
   never reused for a retry. The start guard is exclusive on that Docker daemon.
3. Review `report.json`. A successful invocation requires both stopped
   containers, exit code 0 for each, all requested ticks reported, both
   STOP files read back, matching receipt and financial history replay from
   both original journals, and the guard released. The post-stop replay is
   keyless, read-only, and network-isolated. It also checks that both ledgers
   added the number of settlements reported by the strategy. If either service
   retains a pending receipt, the report identifies the unresolved service and
   the next start requires separately authorized reconciliation.

The strategy uses the same capital, loss, gas, and action limits in every tick.
It reobserves the account before proposing an action, and the authority
independently reconstructs the proposal and verifies the same envelope with
two RPC sources before signing. An ordinary pending receipt uses a subsequent
tick for reconciliation alone. Once the strategy commits a receipt, the
authority independently verifies and commits the same transaction. Both sides
must agree on the receipt block, outcome, inventory and gas accounting before
the strategy advances. A refusal, uncertain submission, revert, disagreement
or exhausted run with pending work stops the session without resubmission.
The interval remains 300 seconds. The host waits at most 210, 720 or 1,230
seconds for one, two or three ticks respectively. The authority has its own
shorter lifetime and cannot sign more transactions than requested ticks.

The capture worker holds the original service locks and leaves both STOP files
in place during new, keyless two-provider acquisition. It replays both original
settlement histories and wallet anchors, then checks current runtime pins,
current wallet inventory and nonce, and complete NAV including uncollected LP
fees. At most 80 reads per source, 120 seconds, failure stops acquisition with no
retry. Successful wire and retained NAV artifacts are bound to the capture
report. If a provider returns HTTP 429, the failed capture retains the affected
source alias and the dashboard reports rate limiting before launch. A 429 during
a runtime light hold stops the bounded run before a full observation can issue
more reads. The host and offline handoff recheck scope and freshness before launch. The
strategy starts only after the authority reports that its local socket is ready,
while the retained NAV is still fresh. Separate runtime and signer checks still
gate each proposed action.

Each STOP is hard-linked to a private attempt-specific name on its **original**
volume before either STOP is lifted. Cleanup stops the identified strategy and
authority containers and restores each marker independently, preserving the
original bytes and inode. A host or daemon loss (including host SIGKILL) can
prevent verification. Treat a missing terminal record, an unresolved stop, or a
retained guard as a recovery incident: inspect the retained plan and container
identities, stop both services, verify and restore the two original markers from
their attempt-specific links, reconcile uncertain state, then resolve the guard.
Do not start the services or remove the links or guard blindly. Ordinary run
failure, timeout, SIGTERM and SIGINT go through the same cleanup path.

Risk limits remain USD 500 total capital, USD 200 daily loss, USD 500 cumulative
loss and a 1,800-second USD feed limit. The bounded entry uses a cumulative
USD 3 RPC ceiling for this acceptance. It carries forward the USD 0.020155
conservative cost allowance recorded in terminal evidence
`bc0069eb7645eae2b778149d01cd29827046bbdc124668eda607c61ff01630a0`.
The original strategy and authority ledger prefixes at sequences 2259 and 2011
were independently re-read offline and matched the retained counters and file
identities. The scope allows at most 4,600 strategy and 3,600 authority
reservations after those original prefixes, including all reservations already
recorded after them.
At USD 0.000050 per reservation, including a separately reserved slot per
broadcast, the cumulative upper bound is USD 0.430155. The bound relies on
the retained conservative price assumptions for the two declared providers
and leaves provider invoice verification outstanding. Check rates and confirm
current authorization before online use.

The initial budget check is network-isolated and read-only. A different ledger
identity or prefix, insufficient allowance, or an unpriced provider prevents
capture. Admission checks the total and each provider's balance on each ledger.
It includes the maximum NAV capture, one action per tick, independent signing
simulation, receipt polling, and cash reconciliation over the full supported
128-block interval. It also accounts for rechecking the existing anchors and
any new anchors from earlier ticks. An operator-run unsigned rehearsal uses the
strategy ledger and must fit alongside that admission budget before it starts.
The worker rechecks the runtime allowance after capture, and STOP handoff
requires the exact budget state recorded by that capture. Both services enforce
their limits in the existing reservation transactions. Restarting keeps the remaining balance.
A failed request still consumes its reservation. Neither the ledger tables nor
their original request policies are migrated by the bounded entry.

Pricing assumptions refer to [Alchemy pricing](https://www.alchemy.com/docs/reference/pricing-plans),
[Alchemy method costs](https://www.alchemy.com/docs/reference/compute-unit-costs),
and [dRPC method costs](https://drpc.org/docs/pricing/compute-units).

For the read-only dashboard, set `databases.live_bounded_run` to that exact output
directory. The page shows requested ticks at startup and the observed tick
and reconciliation counts once a terminal strategy record is available. It
distinguishes an issued start request, a pre-start budget
refusal, a verified stop with both STOPs, and unverified cleanup. If the
post-stop replay still requires reconciliation, the page identifies the service
when the terminal report identifies it. The display uses a fixed set of status
messages from the retained report. The evidence path and raw diagnostics stay
local.
