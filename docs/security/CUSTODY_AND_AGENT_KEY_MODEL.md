# Custody, LP Whitelist, and Agent Key Model

**Status:** Normative Phase 1 authority model  
**Updated:** 2026-09-04  
**Scope:** Hook administration, LP ownership, agent automation, and capital isolation on each selected EVM chain; Robinhood is the initial benchmark

## 1. Security objective

The strategy process may discover and execute profitable LP actions, but compromise of any one agent
process or operational key must not expose the treasury, hook governance, or more than the explicitly
funded experiment capital.

The core rule is:

```text
capital ownership != hook administration != strategy decision != transaction signing
                  != emergency authority != revenue custody
```

An “agent-controlled wallet” means an address whose narrowly scoped signing policy serves approved
agent intents. It does not mean the model, research process, repository, prompt, or ordinary runtime
can read a raw private key.

## 2. Address and authority roles

| Role | Holds material capital? | May add LP? | May trade/rebalance? | May change hook? | Normal signer |
|---|---:|---:|---:|---:|---|
| Treasury | Yes | No by default | No | No | Offline/hardware-backed multisig |
| Manual LP owner | Bounded allocation | Yes, whitelisted | Human-approved | No | User-controlled external wallet/Safe |
| Agent canary account | Very small | Only after promotion | Bounded autonomous actions | No | Isolated policy signer |
| Hook governor | No trading capital | Manages whitelist only | No | Timelocked bounded config | Separate multisig |
| Emergency guardian | Gas only | No | Risk reduction/pause only | Revoke/pause/tighten only | Separate emergency key/multisig |
| Revenue sink | Receives sweeps | No | No | No | Treasury-controlled |

No address combines treasury, agent hot key, and hook governor roles. The agent account is never the
owner of the hook, factory, timelock, treasury, revenue sink, or emergency guardian.

## 3. Staged rollout

### Stage A — human custody, agent has no key

- Deploy the audited hook through the hook-governor multisig.
- Pin the intended Uniswap PositionManager and configure the LP-owner whitelist.
- Whitelist only the user's external Manual LP owner.
- The user creates and owns the LP NFT through the pinned Uniswap interface, SDK-built transaction,
  or Safe transaction.
- The agent reads state, backtests, shadows, proposes range/fee actions, and prepares unsigned intents.
- A human reviews and signs every liquidity change. The agent cannot move funds.

This stage proves the pool, whitelist, PositionManager, accounting, UI/SDK routing, and economic policy
without introducing an autonomous signing failure mode.

### Stage B — isolated autonomous canary

- Create a new agent account unrelated to the user's treasury or personal wallet.
- Whitelist that address only after Stage A and shadow gates pass.
- Fund it manually with no more than the approved canary cap plus a bounded ETH gas reserve.
- It owns only its own canary LP NFT and token inventory; it receives no allowance or pull authority
  from the Manual LP owner or Treasury.
- The agent may operate only its own PoolKey, token set, position IDs, actions, and amount bounds.
- Profit above the configured operating ceiling is swept only to the immutable Revenue sink.
- Refill is manual and requires a new operator decision; losses cannot trigger automatic treasury
  replenishment.

Compromise of this key is modeled as complete loss of the canary wallet. The system is safe only if
that loss is acceptable without emergency funding or contagion.

### Stage C — policy-controlled automation

After autonomous canaries demonstrate reliability, replace or wrap the simple agent account with a
smart account or signing sidecar that enforces policy independently of strategy code:

- chain ID and expiry;
- exact PoolManager, hook, PositionManager, router, token, and Revenue-sink allowlists;
- permitted selectors/actions only;
- per-transaction, per-position, per-token, per-day, and total-account value limits;
- maximum approvals, fee, slippage, price impact, gas, range movement, and rebalance frequency;
- pinned code/config hashes and fresh simulation block;
- no ERC-721 transfer except to the fixed emergency/Treasury destination;
- no arbitrary calls, delegatecall, upgrades, bridging, withdrawals, or new token approvals;
- monotonically tightening pause/revocation path.

The strategy still cannot raise these limits. Capital increases remain manual and stepwise.

### Stage D — bounded scale

Only after the hook, executor, signer, reconciliation, kill switch, and unwind drills survive Stage C
may the autonomous allocation rise. The majority of capital should still remain external. Mature
automation is a reason to improve policy isolation, not to place the treasury private key in the agent.

## 4. LP whitelist design

### 4.1 Recommended semantics

Version 1 is public for swaps and permissioned for liquidity additions:

1. `beforeAddLiquidity` authenticates that the caller is the canonical PoolManager.
2. It requires the PoolManager `sender` to be the one pinned PositionManager.
3. It obtains the position token ID from the exact pinned PositionManager salt convention.
4. It calls the pinned PositionManager's `ownerOf(tokenId)` and verifies the returned LP NFT owner is
   whitelisted.
5. It verifies `getPoolAndPositionInfo(tokenId)` and the callback PoolKey/range/position parameters are
   mutually consistent.
6. It accepts only positive additions through this callback path.

This permits the user's external address or Safe to create and own a position through Uniswap's
PositionManager while the shared PositionManager itself remains public.

The exact first-mint ordering matters: the NFT must exist and have its final owner before the hook
calls `ownerOf`. This is verified against the pinned periphery commit and in a Robinhood fork; it is
not assumed from the benchmark bytecode.

### 4.2 Removal must remain permissionless for the owner

Do not enable `beforeRemoveLiquidity` in Version 1. Revoking an address, pausing dynamic logic, losing
the governor, or taking the agent offline must not trap a legitimate position. Position removal and
collection must remain available through the canonical PositionManager and normal ownership rules.

Emergency revocation therefore has asymmetric behavior:

```text
revoked account: cannot add/recenter/increase risk
revoked account: can remove/collect
agent signer policy: may transfer only to the fixed recovery destination
```

### 4.3 Whitelist administration

- Only the Hook governor may propose additions.
- Normal additions/removals use a timelock and emit indexed events.
- The guardian may immediately revoke but cannot add an address.
- Duplicate/no-op changes revert or emit an explicitly testable result.
- Zero address, hook itself, PoolManager, arbitrary routers, and the governor are rejected as LP owners
  unless a separately reviewed use case proves otherwise.
- Current whitelist state is included in every strategy decision and deployment evidence manifest.
- A parameter/whitelist change invalidates outstanding intents and requires fresh simulation.

### 4.4 What the whitelist does not guarantee

An LP-owner whitelist prevents unauthorized additions only. By itself it does not:

- protect a whitelisted private key;
- stop the owner transferring an already-created LP NFT;
- limit approvals granted by the LP owner;
- cap the value added by an allowed owner;
- prove that the token, router, PositionManager, hook, or pool is safe;
- ensure Uniswap's web interface exposes or routes through the custom-hook pool;
- prevent loss from inventory, LVR, price gaps, or malicious tokens.

Capital caps, signer policy, NFT-transfer restrictions, contract allowlists, and economic risk controls
remain mandatory.

## 5. Benchmark-hook evidence

The observed hook at `0x1191Ac2561817686EFE4296B37BA5a5419Eaa880` is useful as a behavioral
reference, not code to trust or copy blindly.

At the time of review:

- Blockscout showed deployed bytecode with “Verify & publish,” not verified Solidity source.
- Its address flags enable `beforeInitialize`, `beforeAddLiquidity`, and `beforeSwap`.
- Read-only calls returned PositionManager `0x58daec3116aae6d93017baaea7749052e8a04fa7`.
- Its ABI selectors include `positionManager()`, `registrar()`, `isWhitelisted(address)`, and
  `setWhitelist(address,bool)`.
- Historical hook events showed four addresses set to boolean `true`; read-only
  `isWhitelisted(address)` calls returned true for all four.
- The `beforeAddLiquidity` bytecode path authenticates PoolManager/PositionManager and invokes
  `ownerOf(uint256)` and `getPoolAndPositionInfo(uint256)` before consulting whitelist storage.

This is strong evidence that it gates additions by the PositionManager NFT owner. It is not equivalent
to verified source or a formal proof of every branch. A local fork must reproduce authorized mint/add,
unauthorized add, transfer, revoke, collect, remove, and emergency behavior before we adopt the pattern.

## 6. Agent account capital policy

The Stage B cap is the smallest of:

```text
operator-approved absolute loss budget
5 basis points of liquid portfolio NAV
1 basis point of verified two-sided exit depth at the approved maximum impact
maximum amount that can be lost without touching operating or emergency reserves
```

Additional constraints:

- volatile-token value is stressed to zero;
- stablecoin value is stressed for depeg, freeze, blacklist, and issuer failure;
- gas reserve is separate from strategy NAV and sufficient for multiple failed attempts plus removal;
- daily gross turnover and gas caps prevent a compromised loop from churning the full balance;
- maximum position count and token count begin at one;
- unknown or unpriced assets have zero collateral value and block new exposure;
- the signer rejects a transaction that leaves the account above its total value ceiling.

Balances over the operating ceiling trigger a proposal to sweep only to the preconfigured Revenue
sink. The sweep cannot be redirected by the agent or hook governor.

## 7. Private-key handling

- Never store a private key or mnemonic in Git, configuration files, fixtures, reports, logs, shell
  history, prompts, crash dumps, telemetry, or the evidence store.
- The research/indexing/model process never receives signing material.
- A separate signer accepts typed intents and independently rechecks policy before signing.
- Domain separation includes chain ID, account, intent type, config version, nonce, expiry, and
  calldata hash.
- No blind signing, `eth_sign`, arbitrary typed data, unlimited Permit2/ERC-20 approval, or arbitrary
  target transaction is allowed.
- Production keys have documented creation, backup, rotation, revocation, and compromise procedures.
- Logs record request/decision/signature hashes and rejection reasons, never secret material.
- The signer may be fronted by an independent reconciled-account risk guard that must durably
  reserve capital, after full-account reconciliation and cross-chain loss/capital-ceiling checks,
  before any key authority is called. An attempt that produces no transaction releases its
  reservation; a successful signature keeps it until an independent reconciliation releases it.
  No guard reader implementation is loaded by default, so this boundary fails closed until an
  operator supplies one that replays complete inventory and raw proofs; the repository now ships one
  such reader for the live path (see below), but nothing wires it unless the operator asks for it.

For an early EOA canary, “agent owns the key” is still implemented through an isolated signer process.
If that isolation is unavailable, autonomous execution remains disabled; putting a raw key directly
inside the agent process is not an acceptable shortcut.

The repository now carries one bounded *reference* implementation of that separate process:
`src/oasis_agent/execution/lp_signer_daemon.py`, started by the operator via
`scripts/run_lp_signer_daemon.py`. It holds exactly one key in its own memory, serves one canonical
32-byte digest request per connection over an owner-only Unix socket (0700 directory, 0600 endpoint),
enforces a single-account allowlist, a signature budget and a wall-clock time to live, and appends an
audit of addresses, digests, signature hashes and fixed refusal reasons — never key material, request
bodies or provider diagnostics. It signs digests, never transactions, and cannot tell what it
authorized: admission therefore stays in the sidecar, and a returned signature is never an execution
permit. The key may be read from one owner-only 0600 file or one environment variable of that
process. A local EOA key file is the *least* isolated configuration this socket contract supports;
an HSM, cloud KMS or hardware wallet can serve the same contract unchanged. The reference daemon is
not yet a supervised production service: it has no restart supervision and no OS-level confinement
beyond file ownership and socket permissions.

The operator CLI (`scripts/run_lp_signer_sidecar.py`) can now wire both halves — the socket authority
and the independent admission boundary — so a request that clears the static policy, the fee caps, the
nonce journal and the live reconciled-account reservation does produce a signature. With either half
absent it denies, and its default invocation loads neither. The boundary a live run builds is
`execution/live_account_reader.py` over `execution/account_capture.py`: it captures the
`PortfolioManifest`'s declared two-provider proofs at the signing request's own block, replays them
through the evidence-bound reader and prices the worst-case commitment before the authority is called.
Its bounds are literal and each fails closed — the manifest must declare exactly one chain, because one
signing request carries one block; a declared position must already be owned at that block, so the
reference deployment mints the first position out of band and only then opens the ledger; and native
gas spent by an earlier signed transaction must be declared explicitly, because an undeclared native
change is left unresolved and blocks the next frame. Producing a signature is still not an execution
permit, and this CLI never broadcasts.

## 8. Agent transaction policy

Every signed intent must bind:

- chain/block/hash and finality assumption;
- account, nonce, deadline, and idempotency key;
- PoolKey/PoolId, hook, PositionManager, position token ID, and code hashes;
- current and proposed ticks/liquidity;
- exact/max token inputs, minimum outputs, approval delta, and recipient;
- maximum dynamic fee, slippage, price impact, gas, and L1-data cost;
- expected events, token balances, NFT owner, and post-state;
- strategy/config/model/evidence hashes and risk-decision ID.

Signing requires a fresh pinned simulation. Completion requires receipt, event, balance, NFT, and
position reconciliation. A timeout or unknown status is never retried as a new economic action until
the original nonce and state are resolved.

## 9. Compromise and recovery

### Agent key suspected compromised

1. Guardian revokes the agent LP address and pauses new agent intents.
2. Signer refuses all transactions except the fixed recovery/removal path.
3. Resolve pending nonce and transaction state from independent providers.
4. Remove liquidity or transfer the LP NFT to the fixed recovery address under bounded simulation.
5. Revoke ERC-20, Permit2, PositionManager, router, and module approvals.
6. Sweep remaining tokens/ETH to the Revenue sink or recovery address.
7. Rotate the signer and do not reuse the address until post-incident approval.

### Governor suspected compromised

Pause new additions, reject all new config versions, inspect whitelist events, and migrate to a new
immutable hook/pool if governance integrity cannot be proven. The governor must not have custody of LP
NFTs or treasury capital, limiting the direct blast radius.

### Agent process malfunction without key compromise

Disable signing, preserve evidence, reconcile positions, and let the external owner remove liquidity.
No liveness dependency in the hook may prevent exit.

## 10. Mandatory fork and canary tests

- authorized first mint/add through the exact PositionManager;
- unauthorized EOA, router, alternate PositionManager, direct PoolManager caller, and forged salt;
- existing-position increase after NFT transfer to whitelisted and non-whitelisted owners;
- whitelist revocation with successful collect/remove afterward;
- governor/guardian role separation and forbidden guardian addition;
- paused fee logic with successful exit;
- compromised-agent attempts to transfer NFT, approve arbitrary spender, bridge, call arbitrary target,
  raise caps, redirect sweep, or drain Treasury;
- duplicate/replaced transaction, nonce race, stale simulation, reorg, and signer restart;
- total-loss canary scenario with Treasury and Manual LP owner unaffected;
- fresh agent-key rotation and recovery from backup procedures.

Automation promotion is blocked until these behaviors are executable tests, not just documentation.
