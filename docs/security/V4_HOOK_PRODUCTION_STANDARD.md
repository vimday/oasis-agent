# Uniswap v4 Hook Production Standard

**Phase:** Robinhood Chain automated LP  

Apply this checklist together with
`docs/strategies/ROBINHOOD_V4_AUTOLP_BLUEPRINT.md`. The blueprint defines what must be built and in
what order; this standard defines the minimum security evidence for releasing it. Hook governor,
LP-owner whitelist, Treasury, manual custody, agent canary, signer, and recovery authority must also
conform to `docs/security/CUSTODY_AND_AGENT_KEY_MODEL.md`.

**Status:** Normative pre-deployment checklist; the current repository does not yet satisfy it

This standard defines the evidence required before a v4 dynamic-fee hook may control production
capital. It complements the project charter and production control framework; it does not replace the
canonical, commit-pinned `v4-core`, `v4-periphery`, and official hook source.

## 1. Design objective

The first hook should do two narrow jobs: transparently gate liquidity additions by the final LP NFT
owner, and publish/enforce a deterministic bounded LP fee from publicly reproducible state. Range
management, hedging, custom accounting, incentives, token custody, and upgradeability remain off-hook
unless a separate threat model proves that on-chain composition is safer than separation.

Required properties:

- identical public inputs and rule version produce the identical fee;
- every effective fee can be quoted before submission and reconstructed after inclusion;
- a malformed, stale, manipulated, or unsupported input fails closed to a declared safe behavior;
- the hook cannot take arbitrary tokens, call arbitrary targets, discriminate among swapper wallets,
  or trap LP principal; the LP-owner allowlist is explicit and affects additions only;
- governance cannot silently change economics or expand authority;
- the measured benefit is net of flow loss, LVR, gas, rebalancing, and execution failures.

## 2. Canonical identity manifest

Before testing or deployment, freeze a machine-readable manifest containing:

- chain ID and genesis/network identity;
- deployment block policy and required finality;
- PoolManager address and runtime bytecode hash;
- `v4-core`, `v4-periphery`, hook-template, Solidity, Foundry, and library commits/versions;
- token0/token1 addresses, ordering, decimals, symbols as display-only metadata, runtime bytecode hashes,
  proxy/implementation/admin state, and token-behavior classification;
- currency type, fee field, tick spacing, hook address, PoolKey encoding, and derived PoolId;
- CREATE2 deployer, salt, init-code hash, predicted hook address, and permission-bit mask;
- constructor arguments, immutable values, roles, delays, pause/unwind paths, and source-verification hash.

Names, explorers, registries, frontend displays, and RPC defaults are discovery aids, not identity
proof. A mismatch between the signed manifest and live chain blocks deployment or execution.

## 3. Hook permission and callback conformance

- Derive the expected permission mask from the minimal implemented callbacks.
- Mine and independently recompute the CREATE2 address; assert that its low 14 bits exactly match the
  declared permissions.
- Prove every enabled callback returns the exact selector and ABI length required by pinned core.
- Prove disabled callbacks are not relied upon and that unexpected direct calls fail safely.
- Authenticate the exact PoolManager before parsing attacker-controlled callback data.
- Bind every callback to the allowed PoolKey/PoolId set where the hook is not intentionally generic.
- Bound `hookData` length, validate its version/domain before decoding, and reject trailing ambiguity.
- If any return-delta permission is enabled, specify conservation, sign, magnitude, recipient, settlement,
  and denial-of-service properties separately. The default Phase 1 design enables no return deltas.

The review must compare declared permissions, Solidity overrides, compiled dispatch surface, deployed
address bits, and actual PoolKey. Agreement in only one representation is insufficient.

### Permissioned-LP conformance

- Authenticate the PoolManager and one pinned PositionManager before reading position ownership.
- Derive token ID only from the pinned PositionManager salt convention; reject alternate callers and
  forged/malformed salts.
- Resolve the beneficial LP NFT owner with `ownerOf(tokenId)` and cross-check
  `getPoolAndPositionInfo(tokenId)` against callback PoolKey and position parameters.
- Test first mint, later increase, NFT transfer, revocation, alternate recipient, and direct-core calls.
- The guardian can revoke immediately but cannot add an address or raise any limit.
- Phase 1 enables no removal callback: whitelist revocation, pause, or lost governance must not block
  collection and removal by the legitimate position owner.
- Whitelist events and current state are part of the signed identity/config manifest.

## 4. Dynamic-fee semantics

- The pool fee uses the canonical dynamic-fee sentinel exactly.
- A `beforeSwap` fee override sets the canonical override flag, removes it with the canonical mask, and
  validates the resulting LP fee at or below `MAX_LP_FEE = 1_000_000` pips.
- Directional protocol fee is decoded from the correct packed half. Total swap fee follows canonical
  sequential composition; it is not the arithmetic sum of percentages.
- Test zero fee, one pip, configured min/max, one below/above every boundary, 100%, and invalid flag
  combinations.
- Enforce the core restriction around 100% fee and exact-output behavior.
- Quote and execution paths call the same fee implementation or are differential-tested on every build.
- State whether the fee is per-swap override or stored dynamic fee, who may update it, and the exact
  behavior if the hook returns no override.
- `amountSpecified` thresholds identify the specified token explicitly: exact-input uses the input
  token and exact-output uses the output token. Never compare one raw threshold across token0 and
  token1 base units; decimals, direction, and exact-input/output mode are part of the rule domain.

Project-specific gate: `TransparentFeeStrategy` and every configuration boundary must use the same
canonical economic maximum enforced by `uniswap_v4/fees.py`; wire-format capacity must never be used as
the economic maximum.

## 5. Fee signal safety

Every input to the fee rule declares:

- source, unit, scale, sign, lookback, update cadence, and valid range;
- block/time snapshot and maximum age;
- manipulation budget and whether the current swap can influence the observation;
- fallback and halt behavior;
- test vectors and monitoring metric.

Avoid instantaneous spot inputs that the triggering transaction can manipulate. Prefer lagged or
robust observations whose manipulation cost is measured, but explicitly model lag during jumps. Cap
the contribution and rate of change of every feature so one corrupt value cannot select an unbounded
fee. A fallback fee is allowed only if its loss is bounded for the current regime; otherwise revert or
pause new risk according to the predeclared policy.

Fee logic must be computationally bounded. No external call, unbounded loop, storage scan, or fragile
oracle dependency belongs in the swap callback. Gas griefing and hook reverts are economic and
availability attacks, not merely performance issues.

## 6. PoolManager accounting and token behavior

- Trace every action inside unlock as a per-currency ledger until all deltas settle to zero.
- Test exact-input and exact-output in both directions, including partial tick crossing and price limit.
- Never offset token0 against token1 or use a reporting-currency valuation to hide base-unit residual.
- Verify native currency behavior and every take/settle path used by routers or position managers.
- Reject unsupported tokens before capital is deposited. Test missing/false return values, transfer
  fees, rebasing, callbacks, blacklists, pauses, mint/burn authority, proxies, permit variants, unusual
  decimals, and balance changes not explained by transfers.
- Track protocol, LP, hook, and gas fees independently and reconcile them with events and balances.
- Pool initialization, liquidity changes, donation, and callback re-entry are in scope even when the
  candidate hook implements only `beforeSwap`.

## 7. Administration and emergency design

Prefer immutable configuration. If parameters are mutable:

- define distinct proposer, executor, pauser, unpauser, and treasury roles;
- apply least privilege and delayed execution to economic or authority-expanding changes;
- cap every value on-chain so compromised governance cannot select arbitrary economics;
- emit old/new values, rule version, activation time, and proposal identity;
- keep emergency pause capable of stopping new risk without blocking removal or safe unwind;
- require independent review and a canary for unpause, limit widening, and rule-version change;
- document liveness failure if a multisig, timelock, automation, or key becomes unavailable.

Do not add an upgradeable proxy by default. If upgradeability is unavoidable, storage-layout,
initializer, implementation allowlist, upgrade-delay, rollback, and governance-compromise tests become
mandatory, and the additional risk must be compared with redeploying a new pool/hook.

## 8. Verification campaign

### Deterministic tests

- canonical PoolKey/PoolId, permission bits, selectors, ABI lengths, fee masks, signed deltas, numeric
  widths, rounding, and effective-fee composition;
- quote/execution equivalence and event/reconstruction equivalence;
- access control, pause/unwind, rule-version, and configuration-transition tests.

### Stateful and adversarial tests

- invariant fuzzing for per-currency conservation, fee bounds, authorized state changes, callback
  authentication, and inability to strand deltas;
- malicious callback/token/router, re-entry, revert bubbling, gas grief, malformed `hookData`, direct
  callback calls, flash manipulation, same-block ordering, JIT liquidity, donation, and boundary ticks;
- differential tests against the exact pinned canonical libraries and reference hook patterns;
- pinned-fork lifecycle from deployment and pool initialization through deposits, swaps, fee changes,
  removals, pause, unwind, and post-state reconciliation;
- deterministic seed retention and long-running campaigns with coverage/corpus review.

### Required tools and review

- compiler warnings as errors where feasible, Foundry tests, Slither, invariant fuzzing with Foundry or
  Echidna, dependency and secret scans, bytecode/source comparison, and gas snapshots;
- two independent reviewers for capital-critical arithmetic, access control, callback surface, and
  deployment manifest;
- an external audit before material third-party or treasury capital, followed by regression tests for
  every accepted finding. An audit is evidence, not a warranty.

## 9. Economic validation

The hook is promoted only if an untouched, precommitted evaluation shows positive net excess return
against hold, passive LP, static-fee LP, and the identical range policy without dynamic fees.

Report:

- gross fees and routed volume by fee level;
- LVR/adverse selection, inventory PnL, IL, gas, rebalancing, hedge, revert, and opportunity costs;
- return distribution, confidence interval, expected shortfall, drawdown, turnover, capacity, and
  regime breakdown;
- sensitivity to each feature, bound, delay, competitor fee, and fee-volume elasticity assumption;
- unexplained accounting residual in token0 and token1 base units;
- exact conditions under which the fee rule loses to its baselines.

Do not annualize a short or selectively chosen interval as evidence of durable edge. The decision is
`reject` when precommitted criteria fail; inspecting the test window and changing thresholds creates a
new experiment.

## 10. Launch sequence

1. Produce the signed identity/deployment manifest and reproducible build.
2. Complete the verification campaign with no unresolved critical/high finding.
3. Replay historical blocks and demonstrate complete per-currency accounting.
4. Run pinned-fork lifecycle and failure drills.
5. Run live shadow quoting without signing; compare expected and observed pool behavior.
6. Deploy an isolated minimal-capital canary with immutable capital, loss, gas, and approval ceilings.
7. Observe for the precommitted duration and regime coverage; rehearse pause and full unwind.
8. Reconcile every unit of capital and obtain independent promotion approval.
9. Scale only capital, one bounded step at a time; any code/config/source change restarts the relevant
   canary evidence window.

Automatic halt conditions include changed bytecode/roles, stale or disputed state, sequencer/RPC
failure, unexpected fee, quote/execution divergence, accounting residual, invariant violation, gas or
revert spike, breached loss/drawdown/inventory limits, or unavailable pause/unwind path.

## 11. Deployment packet

No deployment is complete without:

- canonical identity and dependency manifest;
- threat model and security-property inventory;
- source, compiler settings, reproducible artifact, runtime bytecode, and source-verification hashes;
- complete test/analysis report with versions, seeds, fork block/hash, findings, and dispositions;
- locked strategy experiment and economic evaluation;
- signer/role/allowance/capital manifest;
- dashboards, alerts, kill switch, pause/unwind instructions, incident contacts, and drill evidence;
- shadow/canary report and independent approvals;
- explicit residual risks and expiry/review date.

Until the packet exists and every applicable gate passes, the only permitted labels are `research`,
`simulation`, or `shadow`.
