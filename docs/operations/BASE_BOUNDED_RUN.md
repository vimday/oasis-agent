# One bounded Base live run

`make live-docker-readiness` is an offline status check. `make live-docker` is the
owner-operated live entry: run it only under current authorization for one Base
strategy tick. It may sign and submit once through the existing transaction
authority if all runtime and per-action checks pass. A completed hold does not
imply a transaction or a profit. Solana is outside this entry.

1. Stop the original two services with their existing 0600 STOP markers present.
   Keep the original state, receipt and RPC journals and the dedicated key
   volume. Use the selected local Docker Unix context. Export the existing
   `BASE_RPC_PRIMARY_ALT` (Alchemy) and `BASE_RPC_PRIMARY` (dRPC) from the secure
   operator environment; the command never sources a repository `.env`.
2. Build and review the local image against the intended tracked source tree.
   Use `make live-docker LIVE_IMAGE=… LIVE_START_OUTPUT=reports/runs/<new-run>`;
   create the parent directory if needed. The destination must be new and is
   never reused for a retry. The start guard is exclusive on that Docker daemon.
3. Review `report.json`. A successful invocation requires both stopped
   containers, exit code 0 for each, a resolved one-tick strategy report, both
   STOP files read back, matching receipt and financial history replay from
   both original journals, and the guard released. The post-stop replay is
   keyless, read-only, and network-isolated. A failed or timed-out tick is still
   a failure even if cleanup succeeded. If the authority still records a pending
   receipt after the strategy exits, the report records the unresolved service
   and the next start is blocked until separately authorized reconciliation.

The capture worker holds the original service locks and leaves both STOP files
in place during new, keyless two-provider acquisition. It replays both original
settlement histories and wallet anchors, then checks current runtime pins,
current wallet inventory and nonce, and complete NAV including uncollected LP
fees. At most 80 reads per source, 120 seconds, failure stops acquisition with no
retry. Successful wire and retained NAV artifacts are bound to the capture
report. The host and offline handoff recheck scope and freshness before launch. The
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
loss and a 1,800-second USD feed limit. The bounded entry uses the cumulative
USD 0.10 RPC ceiling for this acceptance. It carries forward the USD 0.020155
conservative cost allowance recorded in terminal evidence
`bc0069eb7645eae2b778149d01cd29827046bbdc124668eda607c61ff01630a0`.
The original strategy and authority ledger prefixes at sequences 2259 and 2011
were independently re-read offline and matched the retained counters and file
identities. The scope allows at most 750 further reservations per ledger.
At USD 0.000050 per reservation, including a separately reserved slot per
broadcast, the cumulative upper bound is USD 0.095155. The bound relies on
the retained conservative price assumptions for the two declared providers
and leaves provider invoice verification outstanding. Check rates and confirm
current authorization before online use.

The initial budget check is network-isolated and read-only. A different ledger
identity or prefix, insufficient allowance, or an unpriced provider prevents
capture. The capture worker and both services enforce the same limits in
their existing reservation transactions. Restarting keeps the remaining balance.
A failed request still consumes its reservation. Neither the ledger tables nor
their original request policies are migrated by the bounded entry.

Pricing assumptions refer to [Alchemy pricing](https://www.alchemy.com/docs/reference/pricing-plans),
[Alchemy method costs](https://www.alchemy.com/docs/reference/compute-unit-costs),
and [dRPC method costs](https://drpc.org/docs/pricing/compute-units).

For the read-only dashboard, set `databases.live_bounded_run` to that exact output
directory. The page distinguishes an issued start request, historical verified
stop with both STOPs, and unverified cleanup. It does not poll Docker, contact RPC,
infer that the services are currently running, or grant trading authority.
