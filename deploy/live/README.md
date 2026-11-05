# Local Docker canary packaging

The owner selected local Docker on 2026-10-17. This is a reproducible packaging
and confinement implementation, not permission to bypass the release gate.
The October 31 owner release is recorded; current runtime evidence, accounting,
cost and recovery checks must still pass before new live actions.

## One-command Docker entry

`make live-docker-readiness` prints the offline owner-release decision and the
same local configuration declaration shown by `/api/v1/live/status` and the
dashboard. Its `startup` object reports missing gas-oracle pins, invalid/missing
configuration and the still-unverified live prerequisites. Python exits 3 when
these local guards fail. Exit 0 means only `local_guards_passed: true`:
`startup_prerequisites_passed`, `execution_eligible` and `evidence_replayed`
remain false, including with all eight configured runtime roles. This default
does not inspect Docker, original state, STOP, keys, environment values or RPC.
`make live-docker` is the guarded startup
entry. A closed owner release refuses before Docker, credentials, output creation
or state access. An open owner release still requires the original-state and
runtime checks below. Neither command changes the release decision. The Python
CLI exits 3 for a refusal; Make propagates a failed target.

The dashboard labels these as the **local start configuration**, independently
of the observed state record. Complete configured pins do not establish approved
deployed bytecode. Image identity, original dual-service state, STOP controls,
fresh USD/NAV, runtime/simulation, receipt/fund recovery and binding the record to
the start configuration remain visibly unverified. No start/rearm operation was
added to the API or dashboard. See the
[read-only startup diagnostic record](../../docs/verification/LIVE_STARTUP_READINESS_20261105.md).

After completing current production acceptance under the recorded owner release, startup
requires explicit RPC environment variables, the existing four named state/socket/key
volumes, and a new output directory (`LIVE_START_OUTPUT`, default
`reports/runs/live-start`). It verifies the local Unix-socket Docker context,
expanded deployment policy and every tracked application file in `LIVE_IMAGE`
(default `oasis-live:local`), then uses the verified immutable image ID. Image,
volume, state and startup commands use the Unix socket selected at the beginning
of the attempt. The report records a digest of that endpoint. It sends a
fixed two-service Compose document through stdin; no RPC endpoint is saved in a
report. Only the two RPC variables reach the containers, and the strategy has no
key mount. All volumes are external to this startup operation: missing state cannot
silently become fresh journals or budgets. Existing project containers require
explicit recovery. The launcher checks their inventory again after preflight and
before recording a start attempt.

Before Compose starts either service, the verified candidate image checks both
original state volumes with no network, key/socket mount or RPC environment. The
volumes are read-only, both existing service locks are held shared, and missing,
empty, corrupt, shared or configuration-mismatched state/evidence/budget files
refuse. STOP, HALT, pending/signing uncertainty and unresolved accounting also
refuse. This checks retained startup prerequisites, not current NAV or execution
eligibility. It neither applies a feed migration nor clears a halt.

Production service commands require `--require-existing-runtime`. Each service
rechecks its original files under its exclusive lock and uses existing-only journal,
evidence and request-budget opens throughout execution; losing a file after the
precheck cannot silently initialize a replacement. Normal manual research setup
without this production flag retains its explicit initialization behavior. The flag
cannot be combined with reporting, readiness, reconciliation or state-edit commands.
An absent JSON projection needs a separate journal recovery operation before a
production start, even though read-only recovery can inspect the surviving journal.

The entry never builds/pulls, initializes state, imports keys or activates setup/
recovery profiles. It rechecks release and source/config identities immediately
before one bounded startup command. A persisted attempt precedes that command;
timeout or partial failure remains unresolved without a retry. A successful Compose
command is recorded as `start_command_completed`, **not** healthy services, real
receipt acceptance or execution permission. Resolve an uncertain attempt and inspect
the services before another operator-directed start. The existing direct host
`make live` entry is unchanged.

Current NAV capture also verifies the pinned PositionManager's `balanceOf(account)`
through both providers at the valuation block. Zero declared positions requires zero
NFTs; one declared position requires exactly one NFT plus its existing ownership and
liquidity proofs. Missing or additional NFTs halt valuation before a signing reservation.
This covers the configured manager, not undiscovered tokens or other protocols.
Version 1 NAV records remain available for historical replay, but cannot satisfy
current admission; new version 2 proofs add two reads per provider. Existing RPC
quotas are not increased. Signed-price candidate captures enforce the same inventory
check and remain excluded from production signing.

## Build and verify

Run the complete local development checks with a fresh report directory:

```sh
make live-quality QUALITY_OUTPUT=reports/runs/my-live-quality-check
```

This stops on the first failed/timed-out check and records each actual exit code
and log hash: Python/frontend, secret scanning, Bandit and expanded Compose
policy. Existing reports are never overwritten. A successful development check
does not open the production release gate or start services.

Check the expanded Compose policy without credentials, Docker daemon access or
RPC calls:

```sh
make live-deployment-check
```

This verifies all seven services both with absent RPC variables and with
synthetic endpoints. It checks private state/key mounts, keyless recovery,
offline status service, non-root execution and confinement settings. It also
requires each service's exact command/arguments, forbids Compose entrypoint
overrides and keeps setup/recovery behind their explicit profiles. Command
changes require a reviewed update to the verifier's independent contract. Its
v2 report binds both the Compose file and verifier source hashes. It starts
no service and grants no release approval. RPC values are passed through only
to networked services; missing values are rejected by their entry points before
network/key use. Offline setup/status no longer requires RPC credentials merely
to parse Compose. The check ignores the host `.env` and does not print endpoints.
The strategy CLI now resolves RPC URLs only from explicit environment variables;
it never falls back to reading the repository `.env`, which may also hold wallet
keys. Apply the same environment-only setup to direct local shadow/check/recovery
commands. Do not export a mixed wallet-key file into the strategy environment.

`make live-check` / `run_live_lp --mode live --check` performs network reads and
spends the existing `--state-dir/live.receipt-budget.db` quota. Direct reads and
retained acquisition share this same ledger across invocations. Missing, empty,
changed-policy or exhausted ledgers refuse without obtaining a fresh quota; do
not create/copy a replacement ledger to retry. The check never saves loop state
or contacts a signer. Its temporary evidence store does not retain production
acceptance proofs. Request counts are not a provider dollar-cost guarantee:
separately verify remaining monetary authorization before any online check.
Use `--readiness` or `make live-deployment-check` for offline diagnostics.

```sh
docker build --target runtime -f deploy/live/Dockerfile -t oasis-live:local .
.venv/bin/python -m scripts.verify_live_isolation \
  --image oasis-live:local --out reports/isolation-drill.json
```

The base image is pinned by digest and dependencies by lockfile hashes. The build
context is allowlisted: no `.env`, wallet files, runtime state, reports, `.git` or
host home directory is included. The validation target adds locked test tools;
it is a separate image and is not used by the production services.

The isolation verifier now inspects image defaults before creating any temporary
volume or container: no extra entrypoint, healthcheck or implicit volumes; exact
non-root user, working directory and readiness command; the pinned base's
environment plus exactly the three declared application variables. Duplicate or
additional environment entries refuse. The image must descend from the locally
available digest-pinned base layers; inspection never pulls a missing base.
The v2 report binds the immutable image/base IDs, configuration hash and verifier
source hash. This does not attest application source or dependency contents.
If the local daemon cannot resolve the base's digest reference, the verifier may
locate its image ID through the local base tag, but still requires that inspected
object's exact pinned repository digest. It never accepts the tag alone or pulls
a missing base. The new configuration checks and a current runtime image passed
real network-disabled isolation validation on 2026-10-23, as recorded in
[the image-contract report](../../docs/verification/LIVE_IMAGE_CONTRACT_20261023.md).

Before using a newly built runtime, compare its application files with the reviewed
checkout (use the immutable image ID when the daemon cannot resolve tags):

```sh
.venv/bin/python -m scripts.verify_live_image_sources \
  --image IMAGE_ID --out reports/runtime-source-check.json
```

This creates a stopped, network-disabled container with no mounted volumes, copies
its `/app` archive without extracting it, checks every regular file against tracked
local runtime inputs, and removes the container. Old, missing or additional files,
links, changed local inputs and unexpected image defaults refuse. It never starts
an image process. The build excludes generated `*.egg-info`, `.DS_Store`, bytecode
and caches.
The report binds file hashes and immutable image identity; it checks working-tree
bytes of tracked files, not Git approval, installed package bytes or production
eligibility. This is an operator preflight, not enforcement on arbitrary Compose
invocations. Existing release gates still apply.

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
NAV freshness expires at the earliest of the block's 120-second deadline and
either USD feed's `updatedAt` plus its configured age limit. Observation checks
the host clock after capture; the authority checks again after simulation and
durable reservation, immediately before key use. A recent block cannot extend a
nearly expired feed. Historical checkpoint replay still verifies the old baseline
without treating it as current admission evidence.
The fast profile is not evidence that a public endpoint supports that rate or
that 4096 requests have an acceptable monetary price.

Use free public services for discovery. The separate diagnostic below permits at
most four calls per invocation and eight total per provider in its durable ledger,
with no retry or polling. It accepts only the documented no-key endpoints, never
private URLs or credentials. The original repository-relative ledgers must already
exist; there is no budget override or automatic initialization. A missing, corrupt,
symlinked or insufficient ledger refuses before network use. Changing output or
working directories cannot renew quota. A refused diagnostic exits 3, even when it
successfully writes its report. Do not replace the ledger to evade an exhausted cap.

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

Historical owner feed policy (2026-10-28, now superseded by the current
1800/1800-second policy): ETH/USD kept an 1800-second ceiling; USDC/USD had its own
`usdc_oracle_max_age_seconds = 90000` (hard bound), because its Base round updates
on a 24-hour heartbeat and was fresh under 1800 s only about 2% of the day. The USDC
±200 bps answer band and 40 bps pool/oracle deviation check still apply. No peg is
substituted.

Historical acceptance permit (2026-10-28, superseded by the October 31 owner
release): `ACCEPTANCE_PERMIT` in
`src/oasis_agent/reporting/live_release.py` lets the services start and admits exactly
one `approve_usdc` (Permit2, `approval_usdc`) at account nonce 3. Every other action
kind or nonce still refuses with `live release blocked`; `new_actions_allowed` stays
false and the four blockers remained until that receipt was reconciled. This
describes the former permit, not today's release state.

## Current stopped-service recovery

New execution additionally requires the approved GasPriceOracle proxy code hash,
implementation storage word and implementation code hash, alongside the original
five pins. The committed five-pin configuration remains for historical replay and
read-only recovery; normal execution and one-command start now refuse it before
RPC or key use. Real fingerprints and a history-preserving runtime-policy epoch
transition are still required. Do not fill these entries from fixtures or replace
the original journals. See the
[gas-oracle implementation and real capture record](../../docs/verification/LIVE_GAS_ORACLE_RUNTIME_20261104.md).

`python -m scripts.review_live_gas_oracle` can replay two already-retained raw
six-read captures and write a fingerprint/config proposal in a new review
directory. It makes zero RPC requests and cannot approve or apply that proposal;
the wire format and remaining acceptance steps are documented in that record.
`python -m scripts.capture_live_gas_oracle --strategy-dir STRATEGY_DIR
--authority-dir AUTHORITY_DIR --rpc-profile PROFILE` now prints a separate offline
discovery plan. Its opt-in capture mode requires its own exact approved plan and
current zero-cost scope, using the same capture flags as recovery. It permits at
most 13 requests (strategy primary 7, secondary 6, authority 0), including one
head lookup, with zero retries and a 120-second hard cutoff. It keeps both
original service locks and STOP files, appends only to the original strategy
budget, and never changes production state or evidence. A successful parent
replay exports `gas-wire.json`, a new local `evidence.db` and `candidate.json`;
none is a runtime approval or configuration migration. This separate grant is
not included in a 197-request NAV/receipt grant. See the
[bounded discovery capture record](../../docs/verification/LIVE_GAS_DISCOVERY_CAPTURE_20261105.md).
Historical cash/checkpoint replay can now recognize the corresponding original
five-pin configuration by its exact hash. This does not change journal bindings
or apply a runtime-policy migration; new NAV still needs the target policy.
One owner-authorized secondary `eth_chainId` retry succeeded through the shared
HTTPS client. The collector now uses that same client, and its hashed plan
names the transport profile. This single read does not reopen the terminated
197-request campaign or establish complete source/NAV/receipt acceptance.

`python -m scripts.review_live_runtime_transition --strategy-dir STRATEGY_DIR
--authority-dir AUTHORITY_DIR --config PROPOSED_PLAIN_CONFIG
--references RUNTIME_REVIEW_REFS --gas-evidence GAS_EVIDENCE_DB` performs a
read-only five-to-eight-pin upgrade preflight. It requires same-block gas/NAV
proofs and both original halted service journals with STOP files. It prints
JSON only; even exit 0 leaves runtime approval, migration and execution false.
The coordinated same-block acquisition and paired runtime epoch writer remain
required. See the [runtime upgrade preflight record](../../docs/verification/LIVE_RUNTIME_TRANSITION_REVIEW_20261105.md).

Both USD feeds now require an age of at most **1800 seconds**, with USD 500 total
capital, USD 200 daily loss and USD 500 cumulative loss limits. Historical
90000-second USDC evidence remains replayable only in its original configuration
epoch. The dashboard also expires NAV at the earlier of its 120-second block
deadline and either feed's own deadline; a recent block does not refresh a feed.
Old observations without this metadata display as stale.

The recovery tools operate on both original private service directories, existing
lock files, STOP files, journals, evidence stores and budget ledgers. They must
run as the owner of those files in a controlled, keyless environment. No command
below starts services, removes STOP, signs, broadcasts, replenishes quota or reads
a repository `.env`. Use the original volumes, never copies of the budget ledgers.

1. `python -m scripts.capture_live_recovery --strategy-dir STRATEGY_DIR
   --authority-dir AUTHORITY_DIR --rpc-profile PROFILE`
   prints an offline plan from the original state. Its exact request counts and
   hash bind both journal tips and original budget histories. The separate
   `--capture --approved-plan-hash HASH --zero-cost-scope SCOPE --out NEW_CAPTURE_DIR`
   mode requires
   a verified, unexpired zero-cost scope for the two explicit RPC endpoints.
   It has a 120-second process deadline, makes no retries and refuses redirects.
   Scope declarations do not verify provider billing or create user authorization.
   A consumed plan cannot be reused by changing the output directory. Failure
   preserves raw attempted evidence and charged requests; it provides no rearm
   acceptance. The prior terminated campaign remains closed; printing a new
   offline plan does not authorize another attempt.
2. For an original journal still bound to the old USDC policy, review then apply
   `python -m scripts.migrate_live_feed_transition
   --strategy-state STRATEGY_DIR/live.state.json
   --strategy-evidence STRATEGY_DIR/live.receipts.db
   --authority-state AUTHORITY_DIR/state.json
   --authority-evidence AUTHORITY_DIR/evidence.db --fresh-nav FRESH_NAV_JSON`.
   Use the successful capture's `fresh-nav.json`; add `--apply --out NEW_ATTEMPT_DIR`
   only for the explicit application. Migration preserves basis, losses, STOP and HALT.
   Both halted journal epochs now commit in one SQLite ATTACH transaction, using
   existing DELETE journals on one filesystem with FULL synchronization. An
   interruption still requires inspection of both originals; recovery sidecars
   are refused rather than removed. A committed pair with incomplete JSON
   projections reports `projection_complete: false` and exits 3. See the
   [atomic feed migration record](../../docs/verification/LIVE_FEED_PAIR_ATOMIC_20261105.md).
   The fresh wallet must retain the nonce of its verified historical NAV or
   settled-cash checkpoint and advance its block height and timestamp. A direct child must
   also name that checkpoint as its parent. An unexplained nonce or conflicting
   block refuses before either migration write; see the
   [continuity regression record](../../docs/verification/LIVE_TRANSITION_CONTINUITY_20261105.md).
   Full live observation and signing admission compare against the verified
   wallet anchor and latch unexplained nonce or block changes before risk
   updates, decisions or key use. Recovery capture and rearm refuse the same
   inconsistencies; ordinary rearm does not clear this halt. Exact same-block recapture requires an
   identical wallet and header. See the
   [observation continuity record](../../docs/verification/LIVE_OBSERVATION_CONTINUITY_20261105.md).
3. `python -m scripts.rearm_live_services --strategy-dir STRATEGY_DIR
   --authority-dir AUTHORITY_DIR --rpc-profile PROFILE --references REFERENCES`
   performs offline review of the successful capture's `references.json` against
   both current strict-policy journals. Only an exact reviewed hash can be applied
   with `--apply --approved-review-hash HASH --out NEW_ATTEMPT_DIR`. It replays
   every original settlement's cash and complete gas, checks current two-provider
   receipts, nonce continuity and fresh full NAV, and only admits the explicitly
   supported observation/migration halts. An arbitrary risk or uncertain-execution
   halt requires its own recovery procedure.
4. Rearm uses one SQLite transaction across both original DELETE-mode journals.
   It preserves STOP, historical counters and risk basis; it never clears a loss
   latch. SQLite locking, filesystem durability, host clock and retained provider
   provenance remain trusted. Exit 3 after an attempted apply means inspect both
   originals, including surviving SQLite recovery files. A committed rearm with
   an incomplete JSON projection also exits 3 and must not be blindly repeated.
   Freshness is rechecked before commit; expired inputs require a new authorized
   capture, never a wider feed limit.

STOP removal and reviewed runtime activation are separate operations after all
current evidence and cost gates pass. The existing one-command startup rechecks
both original states. These recovery commands are implementation and synthetic
regression evidence; they do not establish production recovery, a full NFT unwind,
an economic edge or profitability. The optional OKX price guard accepts 1–200 bps
and needs its own verified free entitlement; it is not an execution or NAV service.

## Historical verification status

The dated results below remain historical evidence. The original two service
journals now contain five real settlements and NFT 3183370 still has liquidity;
current NAV, full unwind and stopped-service recovery remain separate acceptance
requirements. See the [current recovery follow-up](../../docs/verification/LIVE_RECOVERY_ALIGNMENT_20261104.md).

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

Both recovery commands require an existing, nonempty, configuration-bound execution
journal. A missing journal or a legacy JSON-only state is an unresolved recovery,
not "no pending transaction"; recovery never initializes or migrates that history.
A lost JSON projection is recoverable from an intact journal without resetting
losses or latches. When the validated journal has no pending or uncertain signature,
the command returns without requiring RPC endpoints or a budget ledger. Otherwise
reconciliation requires the existing service RPC budget and refuses a missing or
changed-policy ledger; do not create a replacement budget to retry.

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

### Lost signing replies

If strategy has an unresolved signing attempt, recover its public commitment
by the nonce in its original durable reservation. `authority-status` runs
`--status-only` with no network, no RPC environment and no key volume. It serves
only the existing configuration-bound journal through the private socket, holds
the normal authority lock, and exits after 12 connections or 60 seconds. Invalid
and signing requests consume a connection slot and are refused; slow partial
frames cannot extend the deadline. Neither a key nor new transaction signature
is needed to query this status.

```sh
docker compose -f deploy/live/compose.yaml stop authority strategy
docker compose -f deploy/live/compose.yaml --profile recovery up -d --no-deps authority-status
docker compose -f deploy/live/compose.yaml --profile recovery run --rm --no-deps strategy-status-recovery
docker compose -f deploy/live/compose.yaml stop authority-status
docker compose -f deploy/live/compose.yaml --profile recovery run --rm --no-deps authority-recovery
```

Strategy checks the returned account, chain, reserved nonce, target, calldata,
value, fee bounds and original wallet proof pair before adopting the retained
transaction hash. An unknown authority signing outcome, missing commitment or
disagreement preserves the strategy reservation. Adoption does not broadcast;
strategy must then independently prove the receipt and cash using its existing
RPC budget. Exit 3 means stop and inspect, not clear or re-sign the reservation.
The strategy requests that exact nonce, and the status endpoint validates the
entire configuration-bound journal before returning its historical commitment.
This also works if authority already settled and cleared pending, or has a newer
pending nonce. Conflicting commitments for one nonce, corrupt journal entries,
unknown signing outcomes and absent commitments fail closed. Lookup does not
restore authority pending or change counters, and never copies the authority's
settlement verdict: strategy must still independently prove receipt and cash.
The legacy status request without a nonce still returns current pending only.

The keyless SIGKILL/restart mechanism and remaining production limitations are
recorded in [the status recovery verification](../../docs/verification/LIVE_KEYLESS_STATUS_20261019.md).
Historical lookup is covered in
[the continuation verification](../../docs/verification/LIVE_HISTORICAL_RECOVERY_20261020.md).
