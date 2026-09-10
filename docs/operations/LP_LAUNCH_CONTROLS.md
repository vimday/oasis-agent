# LP / Launch dashboard controls

The dashboard manages independent **LP shadow** and **Launch observe** campaigns. It does not
authorize signing, broadcasting, live position management or production deployment.

## Setup and operation

1. Configure the observation server using `configs/server.example.toml`. Set its operations token
   in the named environment variable before startup. Use loopback or an authenticated TLS proxy.
2. For LP, register retained JSONL inputs and their evidence database under `[[campaign_inputs]]`.
   Paths are resolved by the server configuration; the browser selects only registered names.
   A live feature/reconciliation producer is still required for sustained real-market observation.
3. Open the operations view and enter the operations token. It remains in browser memory.
4. Create a task, select LP or Launch, and load/edit its policy template. Configure the public wallet,
   RPC profiles, token contract and policy fields for Launch, or the registered input source and
   cycle count for LP. The policy editor accepts the complete validated JSON policy.
5. Save a version, apply that version, then start it. Editing a policy requires saving before start.
   Saving while a task runs creates a pending version; it does not alter that worker's configuration.
6. Stop waits for worker cleanup. Apply a previously saved revision number to roll back while stopped.
   Starting again creates a separate retained run. Inspect the selected task's output and snapshot.

## Configuration and numerical guarantees

Configuration updates use an expected revision to reject stale editors. Saved, applied and running
versions are shown separately. Each revision participates in an immutable hash chain. LP and Launch
policy documents travel as JSON strings so the browser does not round integer amounts above 2^53.

Launch v1 retains its original 18-decimal, one-billion reference supply serialization. Use v2 for
custom `token_decimals` and `reference_supply`. The monitor verifies on-chain decimals against
configuration and evaluates FDV thresholds using exact integer fractions. Changing metadata does
not establish support for arbitrary token behavior, ownership models or execution venues.

RPC profiles can be supplied through explicitly registered environment variables or the dashboard.
Dashboard URLs are session-only and disappear on server restart. Stored manifests retain endpoint
fingerprints; they never retain endpoint URLs. Replacing a profile affects subsequent workers.
Configuration export contains references rather than credentials.

## Process ownership and recovery

Start requests carry an idempotency identifier. Replaying one returns the same run, including after
completion. Distinct starts cannot overlap within a campaign. At most four managed runs are active.
Workers inherit only a bounded environment and fixed script arguments; browser-supplied commands,
filesystem paths and live modes are rejected. Launch workers always receive `--dry-run` and an empty
environment-file source. Private signing material is not passed to managed workers.

Each LP start copies its inputs and takes a SQLite evidence backup into a separate run directory.
Copying is off the event loop and bounded by size/time limits. Stop during preparation prevents
spawning. Stop after launch terminates and reaps the process group; service shutdown also awaits
cleanup. This operation stops observation and **does not unwind any on-chain position**.

The POSIX controller lock is inherited by workers. A replacement server fails closed while an
orphan worker still holds it. After the orphan exits, interrupted runs are marked failed on restart;
they never resume automatically. A persistence failure after spawning triggers worker cleanup.

State is retained below the operations database directory in `campaigns/`. Preserve this directory
with the operations database for audit and recovery. Bounds are 256 campaigns, 10,000 revisions per
campaign, 256 MiB per retained input/database and a 30-second evidence-copy deadline. Plan storage
retention externally; the manager does not delete historical evidence automatically.

## Remaining production requirements

The existing standalone Launch CLI is separate from this controller. Dashboard stop does not
control an independently started CLI. Managed live execution remains unavailable pending an isolated
policy-enforcing signer, target-specific release evidence, sustained shadow acceptance and explicit
production authorization. Repository tests and local UI checks do not establish live profitability.
