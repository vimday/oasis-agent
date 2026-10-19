# Local Docker canary packaging

The owner selected local Docker on 2026-10-17. This is a reproducible packaging
and confinement implementation, not permission to bypass the release gate.
Both entry points still refuse new live actions before loading a key.

## Build and verify

```sh
docker build --target runtime -f deploy/live/Dockerfile -t oasis-live:local .
.venv/bin/python -m scripts.verify_live_isolation \
  --image oasis-live:local --out reports/isolation-drill.json
```

The base image is pinned by digest and dependencies by lockfile hashes. The build
context is allowlisted: no `.env`, wallet files, runtime state, reports, `.git` or
host home directory is included. The validation target adds locked test tools;
it is a separate image and is not used by the production services.

The isolation drill uses temporary named volumes with **synthetic text markers**,
no real key and no network. It checks non-root UID 10001, no effective Linux
capabilities, no-new-privileges, read-only root, separate state, absent host/Docker
socket mounts, and an authority-only read-only secret mount. It removes only its
own random-volume names. The successful 2026-10-17 local drill is retained in
`reports/review-20261017/isolation-budgeted.json`. The authority secret mount
also refuses writes by its owning UID.

The host and Docker administrator remain trusted. This protects the strategy
container from the authority's files; it does not protect them from a host admin
or anyone controlling the Docker daemon.

## Operator provisioning

`compose.yaml` gives strategy and authority different private state volumes and a
shared owner-only Unix socket. Only authority mounts the external
`oasis-canary-keys` volume. The operator must provision that volume with
`canary.key`, owner UID/GID 10001, mode 0600, using their secure key workflow.
Never copy a key into this repository or pass it through the assistant.

The one-shot importer can provision an existing owned 0600 key file. When the
owner explicitly chooses an existing `.env`, `--dotenv` extracts only
`OASIS_PRIVATE_KEY` inside a no-network, read-only-root container:

```sh
.venv/bin/python -m scripts.provision_live_key \
  --source /absolute/path/to/.env --dotenv --image oasis-live:local
```

The host wrapper checks metadata only. It checks the bind's mapped UID separately
because Docker Desktop maps a host-owned file to root inside its Linux VM. The
container validates the configured public address before writing, installs mode
0600 under UID/GID 10001 in a 0700 volume, then a second restricted container
verifies that the production UID can load it. It never signs or overwrites an
existing volume. A failed install preserves storage for operator inspection.
The source is preserved; it is never added to the image, argv or environment.
Only the provisioning container mounts the source file. Strategy never mounts it.

On 2026-10-18 the owner authorized this `.env` path and the dedicated production
account was successfully provisioned locally. This is an address/permissions
verification, not trading approval. See the
[acceptance record](../../docs/verification/LIVE_ACCEPTANCE_20261018.md).

Set `BASE_RPC_PRIMARY_ALT` and `BASE_RPC_PRIMARY` in the operator environment.
They must be independent providers with pinned reads, complete receipt costs,
full callTracer support and suitable latency. No private RPC URL is baked into
an image. The secret volume is not created by the initializer.

```sh
docker compose -f deploy/live/compose.yaml --profile setup run --rm initialize
```

This initializes only the private state and socket directories. Services do not
automatically restart, renew their signature allowance or reset their request
budgets. Production startup remains blocked until the remaining release evidence
is accepted. Do not remove that gate to test the image.

## RPC expenditure and freshness

The explicit canary profile uses a 250 ms per-provider interval and a lifetime
4096-request ceiling. Parallelism is only between independent providers; calls
within one provider remain ordered and charged to the same durable budget.
Direct head, nonce, code and receipt polling now spend from that same ledger
as raw evidence acquisition, with no automatic read retries. Changing the profile
on an existing budget fails; restarting never refunds it.
These quotas are per service ledger, not a dollar-denominated aggregate cap.
A failed live NAV observation now latches a durable halt before another poll;
operator review is required to resume. Pending receipt recovery remains bounded
by its existing timeout and request ledger.
The fast profile is not evidence that a public endpoint supports that rate or
that 4096 requests have an acceptable monetary price.

Use free public services for discovery. The separate diagnostic below permits at
most four calls per invocation and eight total per provider in its durable ledger,
with no retry or polling. It accepts only the documented no-key endpoints, never
private URLs or credentials. Do not replace the ledger to evade an exhausted cap.

```sh
.venv/bin/python -m scripts.probe_free_base_rpc --provider 1rpc \
  --transaction PUBLIC_TRANSACTION_HASH --out reports/free-rpc-probe.json
```

Endpoint sources: [Automata 1RPC](https://docs.1rpc.io/overview/supported-networks)
and [PublicNode](https://base.publicnode.com/). A listed endpoint is only a
candidate; trace and historical capabilities must be demonstrated by retained
responses. Existing 2026-10-17 diagnostics found one configured provider with a
trace response, the other without one, and PublicNode refused the sampled trace.
The bounded 1RPC probe obtained the sample receipt but failed to obtain its
matching block; it stopped after three charged requests, before tracing. This
is an unavailable observation, not proof that 1RPC never supports tracing.
No private/paid provider should be repeatedly probed or used as an automatic
fallback. Establish its tariff and a bounded spending budget before use.

The subsequent 2026-10-18 1RPC probe reached the trace method and received
`Method debug_traceTransaction not allowed`. A separately bounded dRPC/Alchemy
probe returned matching single-frame traces, receipts and headers. The attempted
nested LP trace/cost acceptance stopped on its first dRPC transport failure and
made no paid request. Capability for one simple transaction is not complete LP
cash acceptance. Existing closed diagnostic budgets must not be reset.

The USDC/USD observed round was about 60000 seconds old, beyond the existing
1800-second gate. Changing RPC providers cannot refresh an on-chain feed round.
A suitable fresh price source or a separately reviewed owner-approved feed policy
is required; no dollar peg or silent freshness relaxation is implemented.

## Verification status

The 2026-10-17 host tests passed for parallel capture, frozen-budget behavior, real socket
SIGKILL/restart with an ephemeral test key, and public-only probe request limits.
The Docker filesystem confinement drill and Linux validation-image tests passed.
The earlier approval-service quota error was resolved on continuation. No
production key was provisioned and no mainnet transaction was signed or broadcast.
Final test counts and image identities are recorded in
`docs/verification/LIVE_DEPLOYMENT_20261017.md`.
The 2026-10-18 follow-up
provisioned the owner-approved key and added simultaneous two-process receipt
publication crash/restart tests with synthetic proofs. Actual deployed
dual-service recovery against the canary's real receipt is still unaccepted.

## Bounded provider acceptance

`scripts.verify_live_providers` collects the same historical Base transaction,
complete OP fees and nested call tree from dRPC and Alchemy. It retains exact
requests/responses and replays them before accepting agreement. The CLI uses
one repository-bound lifetime budget (12 requests per source), independent of
the output directory or working directory. Ordinary transport reads may retry
once; trace never retries. A failed first source stops before paid-source use.
An existing output directory is terminal. Do not remove or replace the budget.

The initial tool validation retained seven dRPC responses, then received HTTP
408 for nested trace (eight free calls charged, zero paid calls). Its retained
fee proof replays offline and agrees with the older two-source historical cost.
The remaining four free calls cannot fund another complete nine-request run;
the tool refuses a renamed output directory. See
[the follow-up record](../../docs/verification/LIVE_ACCEPTANCE_20261019.md).

## Recover pending receipts while new actions are blocked

The authority now has `--reconcile-only`, matching the strategy's existing
recovery mode. It locks the authority state and binds its existing configuration,
then reconciles a retained pending transaction with the same two-source
receipt/cash proof requirements. It never loads a key or opens a signing socket.
It refuses an uncertain signing attempt and leaves it unresolved; this command
cannot reconstruct a transaction whose signature outcome was never retained.

After stopping the two regular services, run each one-shot recovery service:

```sh
docker compose -f deploy/live/compose.yaml stop authority strategy
docker compose -f deploy/live/compose.yaml --profile recovery run --rm --no-deps authority-recovery
docker compose -f deploy/live/compose.yaml --profile recovery run --rm --no-deps strategy-recovery
```

Recovery services mount only their own existing state volume, with neither the
key nor socket volume. They consume the original service RPC ledger without
resetting it. Exit 3 means unresolved; do not clear pending or retry in a loop.
An uncertain strategy signing attempt still needs separate authority-status
recovery. These commands do not reopen release gates or establish live acceptance.
