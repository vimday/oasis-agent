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
   STOP files read back, and the guard released. A failed or timed-out tick is
   still a failure even if cleanup succeeded. An unresolved signature or receipt
   requires separate reconciliation.

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
loss and a 1,800-second USD feed limit. The cumulative RPC ceiling is USD 1,
never refreshed per run. The bound reserves USD 0.020155 for the earlier
acceptance, USD 0.000050 for each of the four existing 4,096-slot service/source
read budgets, and another USD 0.000025 for a send for every lifetime strategy
primary slot. Each send needs a new primary gas estimation before submission.
The resulting worst-case reservation is USD 0.941755. This assumes the original
ledgers, the two named providers' published usage pricing, and no out-of-band
usage charged to this acceptance. It is a conservative usage bound, not a
provider invoice. If tariffs or provider routing change, review this bound before
running. References: [Alchemy pricing](https://www.alchemy.com/docs/reference/pricing-plans),
[Alchemy method costs](https://www.alchemy.com/docs/reference/compute-unit-costs),
[dRPC method costs](https://drpc.org/docs/pricing/compute-units).

For the read-only dashboard, set `databases.live_bounded_run` to that exact output
directory. The page distinguishes an issued start request, historical verified
stop with both STOPs, and unverified cleanup. It does not poll Docker, contact RPC,
infer that the services are currently running, or grant trading authority.
