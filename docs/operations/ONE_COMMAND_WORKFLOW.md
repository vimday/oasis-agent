# One-command retained LP workflow

On macOS, double-click `Oasis.command` to run and open the result page.

Run from the repository root with the installed locked dependencies:

```sh
make run
```

The result is `reports/runs/current/report.html` with a machine report beside it.
This extends the existing `scripts.run_lp_research` entry point. Its older
`--config` acquisition mode is unchanged; `--workflow` selects the offline
integrated path and cannot be combined with acquisition or RPC arguments.
`make verify-run` executes `make check` before this workflow.

The current configuration pins the already retained Base market request and raw
SQLite evidence. It replays the market and coverage diagnostic, then reports the
missing account and position inputs. Exit 3 means required inputs are blocked;
exit 2 means configuration/integrity refused; exit 130 means stopped. These
are CLI exit codes; Make reports a nonzero recipe as its own exit 2. No missing
input becomes zero. A completed seven-stage **engineering** workflow returns 0,
even if its coverage diagnostic rejects the economic dataset. Every result
separately keeps target/OOS/canary/execution false and `net_pnl: null`.

## Connected steps

1. **Market:** reconstruct all declared market windows from raw provider evidence.
2. **Coverage:** run the existing locked dataset coverage assessment on those windows.
3. **Account:** reconstruct opening/closing manifest inventory and complete outer
   account gas, including reverted and unrelated transactions. This covers declared
   assets, not unobserved internal cash. Reconcile the two frames through the
   existing risk policy, retain this window's gross losses, and stop downstream
   steps if its loss latch or deployed-capital ceiling refuses. These run-local
   frames do not reset or replace any production/global risk journal; prior
   loss history remains a live-release prerequisite. The owner's 500/200/500 USD
   limits cannot be raised in a workflow configuration.
4. **Position:** reconstruct LP cash/claims and gas; require the same gas request,
   declared NFT, exact opening/closing position and pool proofs, USD policies and
   endpoint valuations as the account step. Account NAV already observes gas paid;
   this step never debits that NAV again.
5. **Cycle:** use the existing cycle assembler, bound to the workflow's final
   market request, account close and exact NFT proofs. Inventory and reference
   prices are derived; candidate economic terms remain operator declarations.
6. **Shadow:** consume that generated cycle into the existing hash-linked journal.
7. **Preparation:** reconstruct retained positions and generate bounded simulation
   calldata from that journal's latest eligible proposal. HOLD/HALT, wrong owner,
   expired clocks, unsupported actions and policy violations refuse this step.
   It does not run an RPC simulation or assemble a signing request.

Coverage rejection is retained without preventing an observation-only engineering
replay. Missing/failed prerequisites prevent dependent stages from running.
Independent account diagnostics can still finish when market replay fails. Full
cost allocation, fair baseline economics, trial/OOS acceptance, source/access/
router/depth admission, funded finality/exit and operator release remain separate
unmet acceptance gates. This entry point provides no release or trading authority.

## Strict workflow input

`configs/workflow.current.json` is the real retained-input configuration. Each
asset is `{ "path": "relative/file.json", "sha256": "64 lowercase hex" }`,
or `{ "path": "relative/file.json", "checkpoint": "relative/pins.json" }`.
Exactly one pin method is required. A checkpoint is the existing filename-to-SHA256
map; its entry is subject to the identical digest constraint and its whole bytes
are pinned in the run lock. Editing it refuses restart. The bundled config uses
this method so public evidence hashes do not require a secret-scanner allowlist change.
Paths resolve against the repository, must stay inside it, and only JSON/SQLite
files are accepted. Unknown keys, non-strict values, mismatched pins, symlinks,
active WAL/journal files and oversized assets refuse before creating run state.

The envelope `oasis.lp-workflow.v1` requires all these keys:

| Field | Input contract |
| --- | --- |
| `market_windows` | Ordered `{request, evidence}` pairs; requests use `scripts.build_lp_market_window.Request` |
| `account` | `{request, evidence}`; `oasis.account-risk-gas-input.v1` |
| `position` | `{request, evidence}`; `oasis.position-account-gas-input.v1` |
| `cycle` | `{request, evidence, declaration}`; existing `AssemblyRequest` and cycle declaration |
| `shadow` | `{policy, now}`; existing Shadow policy and explicit integer epoch |
| `preparation` | Asset containing `{policy, bounds, positions, now}`; existing `LpSignerPolicy`, `LpPlanBounds`, two position references and the same Shadow clock |

Unavailable stages are explicitly `null`; unavailable markets use an empty array.
Such a run is blocked rather than a passing demonstration. Populate inputs only
from acquired raw evidence under a genuinely distinct, prelocked purpose and
budget. This workflow never captures, retries, restores or creates a budget, and
cannot request expired/exhausted objects through another endpoint.

```sh
make run WORKFLOW=configs/my-reviewed-workflow.json RUN_OUTPUT=reports/runs/my-run
```

Source databases are opened read-only for a coherent SQLite backup. Existing
adapters may retain derived records only in the private backup. No migration or
write targets the original source store. Config, source bytes, Python version,
source-code hashes and dependency lockfiles are pinned before stage execution.
Only one writer owns a run. On successful or blocked completion, all run artifacts
are sealed. Running the same command again verifies the entire seal and returns
the retained result without repeating a stage. Changed inputs/code require a new
reviewed run directory; this is not a new financial acquisition budget.

Interrupted stages, partial input snapshots, failed partial run integrity and
output collisions preserve their files and refuse automatic restart. Inspect the
retained stage/status and partial outputs before a new reviewed run. SIGTERM and
Ctrl-C retain a stopped report. A process killed before the stop handler finishes
leaves `active.json`; no resume treats it as successful. These filesystem and
Python controls are not OS-level confinement.

The seven-stage integration tests use synthetic wire proofs, include failed gas,
produce a real Shadow journal and bounded calldata, forbid socket connections,
and exercise cross-stage rejection and restart integrity. They establish the
engineering connection; they do not supply real economic or canary acceptance.

A retained, explicitly synthetic demonstration is also available:

```sh
make run WORKFLOW=configs/workflow.synthetic.json RUN_OUTPUT=reports/runs/synthetic-only
```

That bundle and its completed result contain manufactured raw proofs for engineering
verification only. Its seven completed stages do not change any financial gate.
The default command continues to use the real retained market input and shows its
missing account/position prerequisites. Neither command downloads replacement data.
