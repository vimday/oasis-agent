# Dynamic Fee Controller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current threshold-only dynamic-fee prototype with a bounded, replayable hybrid controller whose Python quote and Solidity `beforeSwap` decision share the same fee semantics, while `afterSwap` maintains pool-local on-chain statistics and a restricted publisher supplies explicitly versioned lagged risk features.

**Architecture:** The hook remains immutable with respect to PoolManager, PositionManager, initializer, and hook address. It gains only the `afterSwap` permission and a narrowly scoped `StatePublisher` role. `afterSwap` updates pool-local callback-derived flow/amount-pressure EWMA (the canonical callback does not expose post-swap price, so price volatility remains a published lagged feature). The publisher records bounded fixed-point inventory, volatility, toxicity, and rebalance features for the already-bound PoolId. `beforeSwap` reads a single pre-swap snapshot and applies one deterministic integer policy with canonical v4 fee-override encoding. Python exposes the same integer policy for replay and differential testing; Decimal values remain at the research/valuation boundary only.

**Tech Stack:** Python 3.11+, dataclasses, `Decimal` only for explicit research inputs, pytest/unittest, ruff, mypy; Solidity 0.8.26, Foundry, pinned `@uniswap/v4-core` 1.0.2, pinned `@uniswap/v4-periphery` 1.0.3, OpenZeppelin 5.4.0, Foundry fuzz/invariant tests.

## Global Constraints

- Preserve all existing user and concurrent changes; never reset, checkout, or overwrite unrelated work.
- No deployment, signing, broadcasting, live configuration, key access, or capital movement.
- Do not create a public vault, unrestricted call surface, upgradeable proxy, arbitrary callback, or token custody path.
- The hook remains `RESEARCH` only; fixture, replay, and local-fork output must never be labeled live evidence or profitability proof.
- Use canonical v4 source checked out under `contracts/node_modules`; do not reimplement callback or fee semantics from memory.
- Use integer base units and explicit fixed-point scales in settlement code; never use binary floats for fee, amount, risk, or PnL decisions.
- `beforeSwap` can read only state established before the current swap; `afterSwap` updates state for the next swap.
- State is isolated by the bound PoolId; cross-pool, stale, future-dated, out-of-order, overflowed, or malformed state fails closed.
- External published state cannot change fee bounds, PoolKey, whitelist, guardian, pause state, or execution authority.
- Every code change receives focused tests first, then the relevant full gate; no commit is created in this session.
- Final verification must run: `python3 -m pytest -q`, `python3 -m ruff check src tests scripts`, `python3 -m ruff format --check src tests scripts`, `python3 -m mypy src`, `cd contracts && forge fmt --check`, `cd contracts && forge build`, `cd contracts && FOUNDRY_PROFILE=ci forge test`, `cd contracts && forge lint`, and `git diff --check`.

## File Map

- Modify `src/oasis_agent/uniswap_v4/fees.py`: canonical fee constants, override validation, fixed-point policy helpers, and shared fee composition semantics.
- Modify `src/oasis_agent/models/finance.py`: typed controller input/state representation with explicit units and bounds.
- Modify `src/oasis_agent/farming/strategy.py`: Python implementation of the exact integer controller and quote validity/state hash.
- Modify `src/oasis_agent/models/v4.py`: callback/state vocabulary and exact v4 fee sentinel constants if needed by consumers.
- Modify `src/oasis_agent/uniswap_v4/replay.py`: replay the controller state transition after swaps without using post-swap information for the current fee. Price volatility is treated as a lagged published feature because pinned v4 `afterSwap` arguments do not include post-swap price.
- Add or modify `tests/unit/test_dynamic_fee.py`, `tests/unit/test_fee_boundaries.py`, `tests/unit/test_v4.py`, and add `tests/unit/test_fee_controller_vectors.py`: failing tests, boundary tests, and Python reference vectors.
- Modify `contracts/src/OasisDynamicFeeHook.sol`: hybrid controller storage, publisher access control, `afterSwap`, deterministic fee calculation, stale-state behavior, and permissions.
- Modify `contracts/src/OasisHookFactory.sol`: exact required hook flags including `AFTER_SWAP_FLAG`.
- Add or modify `contracts/test/OasisDynamicFeeHook.t.sol`: lifecycle, access-control, callback, quote, stale-state, and four swap-mode tests.
- Modify `contracts/test/OasisDynamicFeeHookInvariant.t.sol`: stateful invariants for bounds, monotonic publication, PoolId isolation, and no fee override outside canonical limits.
- Add `contracts/test/OasisDynamicFeeHookVectors.t.sol`: Solidity-side vectors matching Python expected outputs.
- Modify `docs/verification/2026-09-05-hook-contract-foundation.md`, `docs/verification/2026-09-04-project-audit.md`, and `reports/benchmark-hook-analysis.md`: accurately record the new local research boundary and unresolved production gates.
- Modify `docs/strategies/ROBINHOOD_V4_AUTOLP_BLUEPRINT.md` only where implementation status or controller interface is now concretely supported; do not mark any production gate complete.
- Modify `Makefile` only if a quality command currently reports a weaker result than the actual gate; preserve build-system usage.

---

### Task 1: Freeze Canonical Fee and Controller Invariants in Tests

**Files:**
- Modify: `tests/unit/test_fee_boundaries.py`
- Modify: `tests/unit/test_dynamic_fee.py`
- Modify: `tests/unit/test_v4.py`
- Create: `tests/unit/test_fee_controller_vectors.py`

**Interfaces:**
- Consumes current `validate_pool_fee`, `validate_fee_override`, `TransparentFeeStrategy`, `PoolState`, and `SwapDirection`.
- Produces executable invariants that later Python and Solidity implementations must satisfy.

- [ ] **Step 1: Write failing tests for canonical fee encoding and all four swap modes**

Add tests with these exact assertions using the current public API for the first red test:

```python
@pytest.mark.parametrize(
    ("zero_for_one", "amount_specified", "specified_token"),
    [(True, -1, "token0"), (True, 1, "token1"),
     (False, -1, "token1"), (False, 1, "token0")],
)
def test_amount_specified_selects_the_named_token(
    zero_for_one: bool, amount_specified: int, specified_token: str
) -> None:
    assert zero_for_one in (True, False)
    assert amount_specified in (-1, 1)
    assert specified_token in ("token0", "token1")
    assert not hasattr(TransparentFeeStrategy(100, 10_000, 500), "quote_controller")
```

Use distinct token0/token1 thresholds and assert each mode selects the threshold for the specified asset, not the direction alone. Add cases for amount `-1`, `1`, zero, threshold minus one, threshold, threshold plus one, `1_000_000`, and invalid fee `1_000_001`.

Add tests asserting:

```python
assert validate_fee_override(0x400000 | 1_000_000) == 1_000_000
with pytest.raises(FeeEncodingError):
    validate_fee_override(0x400000 | 1_000_001)
with pytest.raises(FeeEncodingError):
    validate_fee_override(0x800000 | 2_500)
```

The last case proves the dynamic sentinel cannot be returned as an override fee. Add a test that `-2**255` magnitude is handled without overflow and a test that invalid booleans/types fail.

- [ ] **Step 2: Write failing tests for fixed-point feature bounds and deterministic state hash**

Define expected scale `FEATURE_SCALE = 10**18` in the test fixture and require:

```python
state = ControllerState(
    inventory_skew=-FEATURE_SCALE,
    volatility=FEATURE_SCALE,
    toxicity=FEATURE_SCALE,
    rebalance_incentive=FEATURE_SCALE,
    observed_at=1_000,
    published_at=1_001,
    source_epoch=1,
    rule_version=1,
)
assert policy.quote(...).state_hash == policy.quote(...).state_hash
```

Add failures for skew outside `[-FEATURE_SCALE, FEATURE_SCALE]`, non-negative features outside their maximum, future timestamps, zero versions, and state from another PoolId.

- [ ] **Step 3: Write failing reference vectors**

Create a table containing at least these vectors with explicit expected pips after the coefficient table is frozen in Task 2. The initial red test must use the current public API and assert the new controller entry point is present before implementation:

```python
VECTOR_CASES = (
    {
        "base_fee": 500,
        "volatility": 0,
        "inventory_skew": 0,
        "toxicity": 0,
        "rebalance_incentive": 0,
        "amount_specified": -1,
        "zero_for_one": True,
        "expected_fee": 500,
    },
    {
        "base_fee": 500,
        "volatility": 10**18,
        "inventory_skew": 0,
        "toxicity": 0,
        "rebalance_incentive": 0,
        "amount_specified": -1,
        "zero_for_one": True,
        "expected_fee": 500,
    },
)

@pytest.mark.parametrize("case", VECTOR_CASES)
def test_controller_vectors(case: dict[str, int | bool]) -> None:
    with pytest.raises(AttributeError):
        TransparentFeeStrategy(100, 10_000, 500).quote_controller(case)
```

Task 2 replaces this red contract with explicit expected values from the frozen formula and typed arguments; no implementation-derived expected values are permitted.

- [ ] **Step 4: Run the focused tests and verify they fail for the intended missing behavior**

Run:

```bash
python3 -m pytest -q tests/unit/test_fee_boundaries.py tests/unit/test_dynamic_fee.py tests/unit/test_v4.py tests/unit/test_fee_controller_vectors.py
```

Expected: failures identify absent controller state/policy interfaces or incorrect current semantics; no unrelated collection or import failure is acceptable.

### Task 2: Implement the Shared Integer Fee Policy in Python

**Files:**
- Modify: `src/oasis_agent/uniswap_v4/fees.py`
- Modify: `src/oasis_agent/models/finance.py`
- Modify: `src/oasis_agent/farming/strategy.py`
- Modify: `src/oasis_agent/models/v4.py`
- Test: `tests/unit/test_fee_boundaries.py`, `tests/unit/test_dynamic_fee.py`, `tests/unit/test_fee_controller_vectors.py`

**Interfaces:**
- Consumes the tests from Task 1.
- Produces `FEATURE_SCALE`, `MAX_FEATURE`, `ControllerState`, `FeePolicyConfig`, and `TransparentFeeStrategy.quote_controller(...) -> FeeQuote` with integer-compatible outputs.

- [ ] **Step 1: Add typed fixed-point state and configuration validation**

Add frozen dataclasses with these fields and validation:

```python
FEATURE_SCALE = 10**18

@dataclass(frozen=True, slots=True)
class ControllerState:
    pool_id: str
    inventory_skew: int       # [-FEATURE_SCALE, FEATURE_SCALE]
    volatility: int           # [0, FEATURE_SCALE]
    toxicity: int             # [0, FEATURE_SCALE]
    rebalance_incentive: int  # [0, FEATURE_SCALE]
    observed_at: int
    published_at: int
    source_epoch: int
    rule_version: int
    chain_id: int

@dataclass(frozen=True, slots=True)
class FeePolicyConfig:
    min_fee: int
    base_fee: int
    max_fee: int
    volatility_weight: int
    inventory_weight: int
    toxicity_weight: int
    incentive_weight: int
    amount_weight: int
    medium_amount0_threshold: int
    high_amount0_threshold: int
    medium_amount1_threshold: int
    high_amount1_threshold: int
    max_state_age_seconds: int
    quote_validity_seconds: int
    rule_version: int
```

Reject booleans, negative limits, non-finite Decimal values at the existing research boundary, non-monotonic fee bounds, threshold inversions, zero versions, and values that exceed the declared fixed-point or uint widths.

- [ ] **Step 2: Implement one pure integer controller function**

Implement:

```python
def calculate_controller_fee(
    config: FeePolicyConfig,
    state: ControllerState,
    *,
    amount_specified: int,
    zero_for_one: bool,
    observed_at: int,
    pool_id: str,
    chain_id: int,
) -> int:
    ...
```

Use this exact sequence:

1. Validate `amount_specified` as an `int` and reject bool.
2. Require matching chain and PoolId.
3. Require `0 <= observed_at - state.published_at <= max_state_age_seconds`; reject future state and timestamp regression.
4. Select the specified token as `token0` when `zero_for_one == (amount_specified < 0)`, otherwise `token1`.
5. Use absolute amount with `abs(-2**255)` safe in Python and cap it at the configured high threshold for the amount component.
6. Compute the weighted numerator using integer multiplication by `FEATURE_SCALE`; divide once with floor rounding toward the conservative higher fee for positive risk contributions.
7. Add the amount-band contribution and clamp to `[min_fee, max_fee]`, then validate against `MAX_LP_FEE`.
8. Return the unflagged fee pips; the caller adds `OVERRIDE_FEE_FLAG` only when encoding a v4 hook response.

The formula must be explicit and shared in a module-level function; do not retain separate arithmetic in `TransparentFeeStrategy.quote`.

- [ ] **Step 3: Keep legacy research quote behavior only as an explicit adapter**

Make `TransparentFeeStrategy.quote` call the new integer policy after converting its Decimal inputs to validated fixed-point values with a documented round-up conversion. Preserve the existing public fields needed by current tests, but include `pool_id`, `chain_id`, and `rule_version` in the hash input. If a caller has no controller state, raise a descriptive error instead of silently treating missing features as zero.

- [ ] **Step 4: Implement the encoded override helper**

Add:

```python
def encode_fee_override(fee_pips: int) -> int:
    return validate_fee_override(fee_pips) | OVERRIDE_FEE_FLAG
```

Ensure `validate_fee_override` removes only the override bit using `REMOVE_OVERRIDE_MASK`, rejects any other reserved high bit combination, and enforces `MAX_LP_FEE` after masking.

- [ ] **Step 5: Run the focused Python tests**

Run:

```bash
python3 -m pytest -q tests/unit/test_fee_boundaries.py tests/unit/test_dynamic_fee.py tests/unit/test_v4.py tests/unit/test_fee_controller_vectors.py
python3 -m ruff check src/oasis_agent/uniswap_v4/fees.py src/oasis_agent/models/finance.py src/oasis_agent/farming/strategy.py tests/unit/test_dynamic_fee.py tests/unit/test_fee_controller_vectors.py
python3 -m mypy src/oasis_agent/uniswap_v4/fees.py src/oasis_agent/models/finance.py src/oasis_agent/farming/strategy.py
```

Expected: all focused tests pass; no type or lint error.

### Task 3: Add Replay-Exact Post-Swap State Transitions

**Files:**
- Modify: `src/oasis_agent/uniswap_v4/replay.py`
- Modify: `src/oasis_agent/models/finance.py`
- Create or modify: `tests/unit/test_replay.py`, `tests/unit/test_dynamic_fee.py`

**Interfaces:**
- Consumes `calculate_controller_fee` and `ControllerState` from Task 2.
- Produces `apply_after_swap_state(...) -> ControllerState` and replay output that records pre-swap state hash, settled fee, and post-swap state separately.

- [ ] **Step 1: Write failing replay tests**

Cover same-block swaps, timestamp gaps, zero price movement, opposite directions, liquidity zero, extreme amount, and out-of-order events. Assert that the fee recorded for swap N uses the state before swap N and that the EWMA state produced by swap N is used only by swap N+1.

- [ ] **Step 2: Implement bounded EWMA transition**

Implement a pure transition with:

```python
def apply_after_swap_state(
    state: ControllerState,
    *,
    previous_sqrt_price_x96: int,
    sqrt_price_x96: int,
    amount0: int,
    amount1: int,
    liquidity: int,
    block_timestamp: int,
) -> ControllerState:
    ...
```

Use integer absolute price movement bounded by the configured maximum, clamp timestamp gaps to the maximum decay interval, preserve state on same-timestamp zero-information swaps, and reject invalid signed deltas or zero/overflowed price values. Do not infer inventory or future toxicity from the current swap.

- [ ] **Step 3: Wire the reducer without changing event ordering**

Extend `PoolReplayState` with the controller state and pre/post hashes. In `reduce_swap`, calculate and record the fee from the incoming pre-swap state, then apply the post-swap transition after validating the event. Never sort by timestamp; use `(block.number, transaction_index, log_index)`.

- [ ] **Step 4: Run replay-focused verification**

Run:

```bash
python3 -m pytest -q tests/unit/test_replay.py tests/unit/test_dynamic_fee.py tests/unit/test_flow_replay.py
```

Expected: all replay tests pass and the existing event-ordering tests remain green.

### Task 4: Extend the Solidity Hook with Hybrid State and Exact Permissions

**Files:**
- Modify: `contracts/src/OasisDynamicFeeHook.sol`
- Modify: `contracts/src/OasisHookFactory.sol`
- Test: `contracts/test/OasisDynamicFeeHook.t.sol`

**Interfaces:**
- Consumes canonical `IHooks.afterSwap` and `Hooks.AFTER_SWAP_FLAG` from the checked-out v4-core.
- Produces public `controllerState(bytes32)`, `publishedState(bytes32)`, `publishState(...)`, `previewFee(...)`, and an authenticated `afterSwap(...)` implementation.

- [ ] **Step 1: Write failing Solidity tests for flags and publisher access**

Add tests that assert:

```solidity
assertTrue(hook.getHookPermissions().afterSwap);
assertEq(uint160(address(hook)) & Hooks.AFTER_SWAP_FLAG, Hooks.AFTER_SWAP_FLAG);
vm.expectRevert(OasisDynamicFeeHook.UnauthorizedStatePublisher.selector);
hook.publishState(...);
```

Also test the factory rejects a CREATE2 address missing `AFTER_SWAP_FLAG`, and the constructor accepts only the exact permission mask.

- [ ] **Step 2: Add least-privilege state roles and bounded structs**

Add immutable/configured publisher state and these storage shapes:

```solidity
struct PublishedState {
    int256 inventorySkew;
    uint256 volatility;
    uint256 toxicity;
    uint256 rebalanceIncentive;
    uint64 observedAt;
    uint64 publishedAt;
    uint64 sourceEpoch;
    uint64 ruleVersion;
    bytes32 stateHash;
}

struct OnchainState {
    uint256 volatility;
    uint256 flowPressure;
    uint64 lastTimestamp;
    uint64 lastBlockNumber;
    bytes32 lastTransactionHash;
}
```

Use `FEATURE_SCALE = 1e18`, `MAX_FEATURE = FEATURE_SCALE`, and `MAX_STATE_AGE` as hard constants. `setStatePublisher` is owner-only and rejects zero/self/core addresses; it cannot mutate fee configuration or pause state. Emit old/new publisher and full state metadata.

- [ ] **Step 3: Implement strict `publishState` validation**

Require the bound PoolId, exact chain ID, nonzero rule version, `publishedAt >= previous.publishedAt`, `observedAt <= publishedAt <= block.timestamp`, source epoch monotonicity, all feature bounds, and `publishedAt + MAX_STATE_AGE >= block.timestamp` only at quote time rather than publication time. Compute `stateHash = keccak256(abi.encode(block.chainid, address(this), poolId, all fields))` and reject a caller-supplied mismatch if the API includes a supplied hash.

- [ ] **Step 4: Implement `afterSwap` with no external calls**

Use the canonical signature:

```solidity
function afterSwap(
    address,
    PoolKey calldata key,
    SwapParams calldata params,
    BalanceDelta swapDelta,
    bytes calldata
) external onlyPoolManager returns (bytes4, int128)
```

Validate the bound PoolKey, derive the post-swap movement only from callback inputs and the pool state available through the callback contract’s explicitly permitted read path, clamp the timestamp gap, update bounded EWMA/flow pressure, emit a compact state-update event, and return `(IHooks.afterSwap.selector, 0)`. Do not call PositionManager, ERC-20s, or arbitrary addresses from this callback.

- [ ] **Step 5: Update factory permissions and compile**

Set `REQUIRED_HOOK_FLAGS` to include `Hooks.AFTER_SWAP_FLAG`. Set `getHookPermissions().afterSwap = true`; all other permissions remain false unless required by canonical callback behavior. Remove the current `HookNotImplemented` afterSwap path.

Run:

```bash
cd contracts && forge fmt --check
cd contracts && forge build
```

Expected: strict build succeeds and the new callback interface matches v4-core 1.0.2.

### Task 5: Implement Solidity Deterministic Fee Calculation and Fail-Closed Behavior

**Files:**
- Modify: `contracts/src/OasisDynamicFeeHook.sol`
- Modify: `contracts/test/OasisDynamicFeeHook.t.sol`
- Create: `contracts/test/OasisDynamicFeeHookVectors.t.sol`

**Interfaces:**
- Consumes the state and permission surface from Task 4.
- Produces `previewFee(PoolKey, SwapParams)`, `beforeSwap`, and fee outputs equivalent to Python vectors from Task 2.

- [ ] **Step 1: Write failing tests for all four amount/direction modes**

For each of `(zeroForOne, amountSpecified)` = `(true,-x)`, `(true,+x)`, `(false,-x)`, `(false,+x)`, configure distinct token thresholds and assert the selected fee. Add tests for threshold boundaries, zero, `type(int256).min`, max configured fee, stale published state, future published state, and `riskPaused`.

- [ ] **Step 2: Replace threshold-only preview arithmetic**

Implement a private pure/view helper with the same operation order as Python:

```solidity
function _calculateFee(
    FeeConfig memory config,
    PublishedState memory published,
    OnchainState memory onchain,
    SwapParams calldata params
) private view returns (uint24 feePips) { ... }
```

Use a safe absolute-value helper for `int256.min`, select the specified token using `params.zeroForOne == (params.amountSpecified < 0)`, compute each fixed-point contribution with checked intermediate bounds, floor once, clamp to configured min/max, and validate with `LPFeeLibrary.validate`. If external state is stale, use only the declared conservative base fallback when the base fee is within the hard loss bound; otherwise revert with `PublishedStateStale`.

- [ ] **Step 3: Make `previewFee` and `beforeSwap` call the same helper**

`previewFee` returns `(feePips, ruleVersion, stateHash, validUntilTimestamp)` and `beforeSwap` returns `feePips | LPFeeLibrary.OVERRIDE_FEE_FLAG`. The hash must include the on-chain state and pause bit actually used by the helper. No duplicate fee branch is allowed.

- [ ] **Step 4: Add Python/Solidity vector parity**

Encode the exact Task 2 vector cases in `OasisDynamicFeeHookVectors.t.sol` and assert the Solidity result equals the explicit expected pips. Keep vectors in base units and fixed-point integers; do not derive expected values from the implementation under test.

- [ ] **Step 5: Run contract-focused tests and fuzz**

Run:

```bash
cd contracts && forge test --match-path test/OasisDynamicFeeHook.t.sol -vv
cd contracts && forge test --match-path test/OasisDynamicFeeHookVectors.t.sol -vv
cd contracts && FOUNDRY_PROFILE=ci forge test --match-test testFuzz -vv
```

Expected: all four modes, boundary tests, vectors, and fuzz tests pass with no revert-data or arithmetic failures.

### Task 6: Strengthen Hook Invariants and Adversarial Coverage

**Files:**
- Modify: `contracts/test/OasisDynamicFeeHookInvariant.t.sol`
- Modify: `contracts/test/OasisDynamicFeeHook.t.sol`
- Modify: `tests/unit/test_dynamic_fee.py`
- Modify: `tests/unit/test_replay.py`

**Interfaces:**
- Consumes completed controller implementation from Tasks 3–5.
- Produces evidence that malicious inputs cannot widen fees, cross pools, regress state, or make quote and settlement disagree.

- [ ] **Step 1: Add stateful invariant handlers**

Exercise arbitrary bounded publication attempts, swaps in both directions, pause/unpause, and same-block calls. Assert:

```solidity
assertLe(fee, hook.MAX_CONFIGURED_FEE());
assertLe(fee, 1_000_000);
assertGe(fee, config.baseFee);
assertGe(state.publishedAt, previousPublishedAt);
assertEq(state.poolId, boundPoolId);
```

Also assert no state-changing function callable by the publisher changes `boundPoolId`, fee bounds, whitelist, guardian, or pause state.

- [ ] **Step 2: Add adversarial callback tests**

Direct calls to every callback from a non-PoolManager must revert with `CallerNotPoolManager`. Malformed hookData must not change the result or bypass state checks. Reentrant PositionManager/oracle/token mocks must not be called by `afterSwap` or `beforeSwap`; assert zero external call count. State publication with future timestamps, duplicate epochs, overflow values, and wrong PoolId must revert.

- [ ] **Step 3: Add exact-output and extreme signed amount properties**

Use `type(int256).min`, `type(int256).max`, `-1`, `0`, `1`, threshold boundaries, and a nonzero `sqrtPriceLimitX96`. Assert no arithmetic panic, fee remains canonical, and the specified-token mapping remains unchanged.

- [ ] **Step 4: Run the full contract campaign**

Run:

```bash
cd contracts && FOUNDRY_PROFILE=ci forge test
cd contracts && forge lint
```

Record any lint note separately from security conclusions; do not weaken tests or suppress a warning unless the suppression is scoped and justified in the diff.

### Task 7: Reconcile Reports and Verification Documents with Actual Capability

**Files:**
- Modify: `docs/verification/2026-09-05-hook-contract-foundation.md`
- Modify: `docs/verification/2026-09-04-project-audit.md`
- Modify: `reports/benchmark-hook-analysis.md`
- Modify: `docs/strategies/ROBINHOOD_V4_AUTOLP_BLUEPRINT.md`

**Interfaces:**
- Consumes test output and exact implementation state from Tasks 1–6.
- Produces truthful research-status documentation, not a production approval or profitability claim.

- [ ] **Step 1: Add a confirmed/inferred/unresolved matrix**

For the controller explicitly label:

- `confirmed`: local canonical-core callback signature, permission mask, fee override bound, four-mode token selection, state hash construction, publisher monotonicity, and local invariant results;
- `inferred`: economic interpretation of fixed-point features and expected elasticity response;
- `unresolved`: Robinhood fork lifecycle, real router reach, independent reference price, live token behavior, position-level fee reconciliation, out-of-sample net excess return, external audit, shadow/canary evidence.

- [ ] **Step 2: Correct stale claims**

Remove or rewrite any statement implying the old threshold-only hook implements volatility, toxicity, inventory, fee elasticity, or production readiness unless the statement now cites the exact new implementation and still says `RESEARCH`. Do not call local vectors chain evidence.

- [ ] **Step 3: Update the blueprint only for implemented boundaries**

Document that `afterSwap` updates callback-derived statistics for the next swap and that publisher state is lagged, bounded, PoolId-scoped, and fail-closed. Keep all fork, routing, economic, security-campaign, and canary gates open until their evidence exists.

- [ ] **Step 4: Verify documentation consistency**

Run:

```bash
rg -n "production|live|profitable|volatility|toxicity|inventory|afterSwap|RESEARCH|fixture|fork|shadow" docs/verification reports docs/strategies/ROBINHOOD_V4_AUTOLP_BLUEPRINT.md
```

Review every changed occurrence for confirmed/inferred/unresolved classification.

### Task 8: Run Full Quality Gates and Adversarial Diff Review

**Files:**
- Modify only files identified by failing quality gates; do not perform unrelated cleanup.

**Interfaces:**
- Consumes all prior tasks.
- Produces verified working-tree evidence and a residual-risk list; no commit.

- [ ] **Step 1: Run the complete project gates through the Makefile**

Run:

```bash
make test
make lint
make format
make typecheck
make contract-check
git diff --check
```

Expected: all commands exit zero. If `contract-check` emits lint notes, record the exact notes and distinguish them from errors.

- [ ] **Step 2: Run mandatory direct commands for exact reporting**

Run:

```bash
python3 -m pytest -q
python3 -m ruff check src tests scripts
python3 -m ruff format --check src tests scripts
python3 -m mypy src
cd contracts && forge fmt --check
cd contracts && forge build
cd contracts && FOUNDRY_PROFILE=ci forge test
cd contracts && forge lint
git diff --check
```

- [ ] **Step 3: Inspect the final diff for capital-safety regressions**

Check explicitly for raw keys/secrets, arbitrary external calls, unrestricted approvals, changed default endpoints, use of `latest` for settlement, cross-token arithmetic, binary floats, missing PoolId checks, stale-state fail-open paths, missing exact-output coverage, and report language that overstates evidence.

- [ ] **Step 4: Produce the completion report without claiming production readiness**

Include exact commands/results, modified files, assumptions, confirmed behavior, unresolved gates, residual risks, and the explicit statement that no commit, deployment, signing, broadcasting, or capital movement occurred.

Do not run `git commit`. Before any future commit, follow the repository instruction to ask: `代码已验证通过。是否创建 commit？`
