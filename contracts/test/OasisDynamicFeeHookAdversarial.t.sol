// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { Test } from "forge-std/Test.sol";
import { Vm } from "forge-std/Vm.sol";
import { PoolManager } from "@uniswap/v4-core/src/PoolManager.sol";
import { IHooks } from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import { IPoolManager } from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import { IUnlockCallback } from "@uniswap/v4-core/src/interfaces/callback/IUnlockCallback.sol";
import { Hooks } from "@uniswap/v4-core/src/libraries/Hooks.sol";
import { LPFeeLibrary } from "@uniswap/v4-core/src/libraries/LPFeeLibrary.sol";
import { PoolId, PoolIdLibrary } from "@uniswap/v4-core/src/types/PoolId.sol";
import { PoolKey } from "@uniswap/v4-core/src/types/PoolKey.sol";
import { ModifyLiquidityParams, SwapParams } from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {
    BeforeSwapDelta,
    BeforeSwapDeltaLibrary
} from "@uniswap/v4-core/src/types/BeforeSwapDelta.sol";
import { Currency } from "@uniswap/v4-core/src/types/Currency.sol";
import { TestERC20 } from "@uniswap/v4-core/src/test/TestERC20.sol";

import { OasisDynamicFeeHook } from "../src/OasisDynamicFeeHook.sol";
import { OasisHookFactory } from "../src/OasisHookFactory.sol";
import { IOasisPositionManager } from "../src/interfaces/IOasisPositionManager.sol";

/// @notice Adversarial coverage for the dynamic-fee hook (audit M4):
/// malformed/non-empty hookData, reentrancy surface, fee exact boundaries,
/// extreme tick identity, and differential PoolId against the canonical runtime.
/// @dev All scenarios are offline (fresh PoolManager deployments, no RPC).
contract AdversarialPositionManager is IOasisPositionManager {
    mapping(uint256 tokenId => address owner) private _owners;
    PoolKey private _key;
    uint256 private _info;

    function setPosition(uint256 tokenId, address owner, PoolKey calldata key, uint256 info)
        external
    {
        _owners[tokenId] = owner;
        _key = key;
        _info = info;
    }

    function ownerOf(uint256 tokenId) external view returns (address owner) {
        owner = _owners[tokenId];
        require(owner != address(0), "NOT_MINTED");
    }

    function getPoolAndPositionInfo(uint256)
        external
        view
        returns (PoolKey memory poolKey, uint256 packedPositionInfo)
    {
        return (_key, _info);
    }
}

/// @notice Router that attempts reentrancy and governance mutation inside the
/// PoolManager unlock window, exactly where a malicious token or callback would run.
contract HookAdversarialRouter is IUnlockCallback {
    IPoolManager public immutable manager;
    OasisDynamicFeeHook public immutable hook;
    bool public governanceAttemptReverted;
    bool public nestedUnlockReverted;

    constructor(IPoolManager manager_, OasisDynamicFeeHook hook_) {
        manager = manager_;
        hook = hook_;
    }

    function attemptNestedUnlock() external {
        // The unlock return value is irrelevant here: the goal is only to
        // observe the governance/nested-unlock attempts inside the callback.
        // forge-lint: disable-next-line(unused-return)
        manager.unlock("");
    }

    function unlockCallback(bytes calldata) external returns (bytes memory) {
        require(msg.sender == address(manager));
        // Governance mutation during the only window user code can run.
        try hook.setWhitelist(address(this), true) {
            governanceAttemptReverted = false;
        } catch {
            governanceAttemptReverted = true;
        }
        // Nested manager entry must be rejected by the core lock.
        try manager.unlock("") {
            nestedUnlockReverted = false;
        } catch {
            nestedUnlockReverted = true;
        }
        return bytes("");
    }
}

contract OasisDynamicFeeHookAdversarialTest is Test {
    using PoolIdLibrary for PoolKey;

    address private constant POOL_MANAGER_ADDRESS = address(0x1000);
    address private constant INITIALIZER = address(0x2000);
    address private constant GUARDIAN = address(0x3000);
    address private constant LP_OWNER = address(0x4000);
    address private constant OTHER = address(0x5000);
    uint256 private constant TOKEN_ID = 7;
    uint24 private constant BASE_FEE = 500;
    uint24 private constant MEDIUM_FEE = 2_000;
    uint24 private constant HIGH_FEE = 5_000;
    uint24 private constant MEDIUM_THRESHOLD = 1_000;
    uint24 private constant HIGH_THRESHOLD = 10_000;

    AdversarialPositionManager private positionManager;
    OasisHookFactory private factory;
    OasisDynamicFeeHook private hook;
    PoolKey private key;

    function setUp() public {
        positionManager = new AdversarialPositionManager();
        factory = new OasisHookFactory();
        bytes32 codeHash = factory.initCodeHash(
            IPoolManager(POOL_MANAGER_ADDRESS),
            positionManager,
            INITIALIZER,
            address(this),
            GUARDIAN
        );
        bytes32 salt = _mineSalt(factory, codeHash);
        hook = factory.deploy(
            salt,
            IPoolManager(POOL_MANAGER_ADDRESS),
            positionManager,
            INITIALIZER,
            address(this),
            GUARDIAN
        );
        key = PoolKey({
            currency0: Currency.wrap(address(0x10)),
            currency1: Currency.wrap(address(0x20)),
            fee: LPFeeLibrary.DYNAMIC_FEE_FLAG,
            tickSpacing: 60,
            hooks: IHooks(address(hook))
        });
        hook.configurePool(key, _config(1));
    }

    function test_malformedAndNonEmptyHookDataIsInertForSwaps() public {
        bytes[] memory payloads = _hookDataPayloads();
        // The payload table is the point of this test: every variant must hit
        // the same hook surface, so iterating is deliberate, not accidental.
        // forge-lint: disable-start(calls-loop)
        for (uint256 i = 0; i < payloads.length; ++i) {
            vm.prank(POOL_MANAGER_ADDRESS);
            (bytes4 selector, BeforeSwapDelta delta, uint24 overrideFee) =
                hook.beforeSwap(OTHER, key, _swap(-100_000), payloads[i]);
            assertEq(selector, IHooks.beforeSwap.selector);
            assertEq(BeforeSwapDelta.unwrap(delta), 0);
            assertEq(overrideFee, HIGH_FEE | LPFeeLibrary.OVERRIDE_FEE_FLAG);
        }
        // forge-lint: disable-end(calls-loop)
    }

    function test_malformedAndNonEmptyHookDataIsInertForAdds() public {
        hook.setWhitelist(LP_OWNER, true);
        positionManager.setPosition(TOKEN_ID, LP_OWNER, key, _positionInfo(-120, 120));
        bytes[] memory payloads = _hookDataPayloads();
        // Same deliberate payload-table iteration as the swap variant above.
        // forge-lint: disable-start(calls-loop)
        for (uint256 i = 0; i < payloads.length; ++i) {
            vm.prank(POOL_MANAGER_ADDRESS);
            bytes4 selector = hook.beforeAddLiquidity(
                address(positionManager), key, _addParams(-120, 120), payloads[i]
            );
            assertEq(selector, IHooks.beforeAddLiquidity.selector);
        }
        // forge-lint: disable-end(calls-loop)
        // hookData never bypasses the whitelist gate.
        positionManager.setPosition(TOKEN_ID, OTHER, key, _positionInfo(-120, 120));
        vm.prank(POOL_MANAGER_ADDRESS);
        vm.expectRevert(
            abi.encodeWithSelector(
                OasisDynamicFeeHook.PositionNotWhitelisted.selector, TOKEN_ID, OTHER
            )
        );
        // Revert assertion consumes the callback return path.
        // forge-lint: disable-next-line(unused-return)
        hook.beforeAddLiquidity(address(positionManager), key, _addParams(-120, 120), payloads[2]);
    }

    function test_reentrancySurfaceIsClosedDuringUnlockWindow() public {
        PoolManager realManager = new PoolManager(address(this));
        OasisHookFactory realFactory = new OasisHookFactory();
        bytes32 codeHash = realFactory.initCodeHash(
            realManager, positionManager, address(this), address(this), GUARDIAN
        );
        OasisDynamicFeeHook realHook = realFactory.deploy(
            _mineSalt(realFactory, codeHash),
            realManager,
            positionManager,
            address(this),
            address(this),
            GUARDIAN
        );
        PoolKey memory realKey = key;
        realKey.hooks = IHooks(address(realHook));
        realHook.configurePool(realKey, _config(1));
        int24 initializedTick = realManager.initialize(realKey, uint160(1 << 96));
        assertEq(initializedTick, 0);

        HookAdversarialRouter router = new HookAdversarialRouter(realManager, realHook);
        router.attemptNestedUnlock();
        assertTrue(router.governanceAttemptReverted());
        assertTrue(router.nestedUnlockReverted());
        assertFalse(realHook.isWhitelisted(address(router)));
        // The swap path is unaffected after the attempts.
        uint24 expected = BASE_FEE | LPFeeLibrary.OVERRIDE_FEE_FLAG;
        vm.prank(address(realManager));
        (bytes4 selector, BeforeSwapDelta delta, uint24 overrideFee) =
            realHook.beforeSwap(OTHER, realKey, _swap(-1), bytes(""));
        assertEq(selector, IHooks.beforeSwap.selector);
        assertEq(BeforeSwapDelta.unwrap(delta), 0);
        assertEq(overrideFee, expected);
    }

    function test_feeConfigRejectsEveryExactBoundaryViolation() public {
        OasisDynamicFeeHook.FeeConfig memory bad = _config(2);
        bad.highFee = hook.MAX_CONFIGURED_FEE() + 1;
        _expectConfigRevert(bad);

        bad = _config(3);
        bad.highFee = LPFeeLibrary.OVERRIDE_FEE_FLAG;
        _expectConfigRevert(bad);

        bad = _config(4);
        bad.mediumAmount0Threshold = bad.highAmount0Threshold;
        _expectConfigRevert(bad);

        bad = _config(5);
        bad.mediumAmount1Threshold = 0;
        _expectConfigRevert(bad);

        bad = _config(6);
        bad.ruleVersion = 0;
        _expectConfigRevert(bad);

        bad = _config(7);
        bad.configured = false;
        _expectConfigRevert(bad);

        bad = _config(8);
        bad.baseFee = bad.mediumFee + 1;
        _expectConfigRevert(bad);

        // Exact edge values that are legal remain accepted.
        OasisDynamicFeeHook.FeeConfig memory edge = _config(2);
        edge.baseFee = 0;
        edge.mediumFee = 1;
        edge.highFee = hook.MAX_CONFIGURED_FEE();
        hook.configurePool(key, edge);
        OasisDynamicFeeHook.FeeConfig memory stored = hook.feeConfig(key);
        assertEq(stored.ruleVersion, 2);
        assertEq(stored.baseFee, 0);
        assertEq(stored.highFee, hook.MAX_CONFIGURED_FEE());
        vm.prank(POOL_MANAGER_ADDRESS);
        // The delta element is intentionally skipped for the fee-edge assertion.
        // forge-lint: disable-next-line(unused-return)
        (bytes4 selector,, uint24 overrideFee) = hook.beforeSwap(OTHER, key, _swap(-1), bytes(""));
        assertEq(selector, IHooks.beforeSwap.selector);
        assertEq(overrideFee, 0 | LPFeeLibrary.OVERRIDE_FEE_FLAG);
    }

    function test_feeClassificationHitsExactThresholdBoundaries() public view {
        assertEq(_preview(-999), BASE_FEE);
        assertEq(_preview(-1_000), MEDIUM_FEE);
        assertEq(_preview(-9_999), MEDIUM_FEE);
        assertEq(_preview(-10_000), HIGH_FEE);
        assertEq(_preview(-10_001), HIGH_FEE);
        assertEq(_preview(0), BASE_FEE);
        assertEq(_preview(type(int256).max), HIGH_FEE);
        assertEq(_preview(type(int256).min), HIGH_FEE);
        // Exact-output positives specify token1 with symmetric thresholds here.
        assertEq(_preview(999), BASE_FEE);
        assertEq(_preview(1_000), MEDIUM_FEE);
        assertEq(_preview(10_000), HIGH_FEE);
    }

    function test_addFlowAcceptsExtremeAlignedTicksAndRejectsMismatch() public {
        int24 extremeLower = -887_220;
        int24 extremeUpper = 887_220;
        hook.setWhitelist(LP_OWNER, true);
        positionManager.setPosition(
            TOKEN_ID, LP_OWNER, key, _positionInfo(extremeLower, extremeUpper)
        );
        vm.prank(POOL_MANAGER_ADDRESS);
        bytes4 result = hook.beforeAddLiquidity(
            address(positionManager), key, _addParams(extremeLower, extremeUpper), bytes("")
        );
        assertEq(result, IHooks.beforeAddLiquidity.selector);

        vm.prank(POOL_MANAGER_ADDRESS);
        vm.expectRevert(
            abi.encodeWithSelector(OasisDynamicFeeHook.PositionRangeMismatch.selector, TOKEN_ID)
        );
        // Revert assertion consumes the callback return path; the multi-line
        // call form keeps the args readable while the directive covers it.
        // forge-lint: disable-start(unused-return)
        hook.beforeAddLiquidity(
            address(positionManager), key, _addParams(extremeLower, extremeUpper - 60), bytes("")
        );
        // forge-lint: disable-end(unused-return)
    }

    function test_poolIdDifferentialAgainstCanonicalRuntime() public {
        PoolManager realManager = new PoolManager(address(this));
        TestERC20 tokenA = new TestERC20(1e36);
        TestERC20 tokenB = new TestERC20(1e36);
        TestERC20 tokenC = new TestERC20(1e36);
        TestERC20 tokenD = new TestERC20(1e36);
        (Currency currency0, Currency currency1) = address(tokenA) < address(tokenB)
            ? (Currency.wrap(address(tokenA)), Currency.wrap(address(tokenB)))
            : (Currency.wrap(address(tokenB)), Currency.wrap(address(tokenA)));
        (Currency currency2, Currency currency3) = address(tokenC) < address(tokenD)
            ? (Currency.wrap(address(tokenC)), Currency.wrap(address(tokenD)))
            : (Currency.wrap(address(tokenD)), Currency.wrap(address(tokenC)));

        OasisHookFactory realFactory = new OasisHookFactory();
        bytes32 codeHash = realFactory.initCodeHash(
            realManager, positionManager, address(this), address(this), GUARDIAN
        );
        OasisDynamicFeeHook realHook = realFactory.deploy(
            _mineSalt(realFactory, codeHash),
            realManager,
            positionManager,
            address(this),
            address(this),
            GUARDIAN
        );
        PoolKey memory dynamicKey = PoolKey(
            currency2, currency3, LPFeeLibrary.DYNAMIC_FEE_FLAG, 60, IHooks(address(realHook))
        );
        realHook.configurePool(dynamicKey, _config(1));

        PoolKey[3] memory keys;
        keys[0] = PoolKey({
            currency0: currency0,
            currency1: currency1,
            fee: 3_000,
            tickSpacing: 60,
            hooks: IHooks(address(0))
        });
        keys[1] = PoolKey({
            currency0: currency0,
            currency1: currency1,
            fee: 3_000,
            tickSpacing: 1,
            hooks: IHooks(address(0))
        });
        keys[2] = dynamicKey;

        vm.recordLogs();
        // Each PoolKey variant (static fee, different spacing, dynamic hook) must
        // initialize against the canonical runtime; the loop is the test itself.
        // forge-lint: disable-start(calls-loop)
        for (uint256 i = 0; i < keys.length; ++i) {
            int24 tick = realManager.initialize(keys[i], uint160(1 << 96));
            assertEq(tick, 0);
        }
        // forge-lint: disable-end(calls-loop)
        uint256 matched = _countInitializeMatches(vm.getRecordedLogs(), keys);
        assertEq(matched, keys.length);
    }

    function test_nonCanonicalKeysRejectedByPinnedRuntime() public {
        PoolManager realManager = new PoolManager(address(this));
        TestERC20 tokenA = new TestERC20(1e36);
        TestERC20 tokenB = new TestERC20(1e36);
        Currency currency0 = address(tokenA) < address(tokenB)
            ? Currency.wrap(address(tokenA))
            : Currency.wrap(address(tokenB));
        Currency currency1 = address(tokenA) < address(tokenB)
            ? Currency.wrap(address(tokenB))
            : Currency.wrap(address(tokenA));

        // Reversed currency order.
        PoolKey memory reversed = PoolKey({
            currency0: currency1,
            currency1: currency0,
            fee: 3_000,
            tickSpacing: 60,
            hooks: IHooks(address(0))
        });
        vm.expectRevert(
            abi.encodeWithSelector(
                IPoolManager.CurrenciesOutOfOrderOrEqual.selector,
                Currency.unwrap(reversed.currency0),
                Currency.unwrap(reversed.currency1)
            )
        );
        // Revert assertion consumes the return path.
        // forge-lint: disable-next-line(unused-return)
        realManager.initialize(reversed, uint160(1 << 96));

        // Tick spacing below MIN_TICK_SPACING.
        PoolKey memory tooSmall = PoolKey({
            currency0: currency0,
            currency1: currency1,
            fee: 3_000,
            tickSpacing: 0,
            hooks: IHooks(address(0))
        });
        vm.expectRevert(abi.encodeWithSelector(IPoolManager.TickSpacingTooSmall.selector, int24(0)));
        // Revert assertion consumes the return path.
        // forge-lint: disable-next-line(unused-return)
        realManager.initialize(tooSmall, uint160(1 << 96));

        // Tick spacing above MAX_TICK_SPACING.
        PoolKey memory tooLarge = PoolKey({
            currency0: currency0,
            currency1: currency1,
            fee: 3_000,
            tickSpacing: 32_768,
            hooks: IHooks(address(0))
        });
        vm.expectRevert(
            abi.encodeWithSelector(IPoolManager.TickSpacingTooLarge.selector, int24(32_768))
        );
        // Revert assertion consumes the return path.
        // forge-lint: disable-next-line(unused-return)
        realManager.initialize(tooLarge, uint160(1 << 96));
    }

    function _countInitializeMatches(Vm.Log[] memory logs, PoolKey[3] memory keys)
        private
        pure
        returns (uint256 matched)
    {
        bytes32 signature = keccak256(
            "Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)"
        );
        for (uint256 i = 0; i < logs.length; ++i) {
            if (logs[i].topics.length != 4 || logs[i].topics[0] != signature) continue;
            for (uint256 j = 0; j < keys.length; ++j) {
                bytes32 canonical = PoolId.unwrap(keys[j].toId());
                // Independent reference: the canonical library hashes the in-memory
                // struct, which is byte-identical to abi.encode of the same key.
                bytes32 abiDigest = keccak256(abi.encode(keys[j]));
                assertEq(canonical, abiDigest, "library diverges from ABI reference");
                if (logs[i].topics[1] == canonical) {
                    ++matched;
                }
            }
        }
    }

    function _expectConfigRevert(OasisDynamicFeeHook.FeeConfig memory config) private {
        vm.expectRevert(
            abi.encodeWithSelector(OasisDynamicFeeHook.InvalidFeeConfiguration.selector)
        );
        hook.configurePool(key, config);
    }

    function _preview(int256 amount) private view returns (uint24 fee) {
        uint64 version;
        bytes32 stateHash;
        uint256 deadline;
        (fee, version, stateHash, deadline) = hook.previewFee(key, _swap(amount));
        assertEq(version, 1);
        assertNotEq(stateHash, bytes32(0));
        assertGt(deadline, 0);
    }

    function _hookDataPayloads() private view returns (bytes[] memory payloads) {
        payloads = new bytes[](5);
        payloads[0] = bytes("");
        payloads[1] = hex"deadbeef";
        payloads[2] = abi.encode(_swap(-1));
        payloads[3] = abi.encode(key);
        payloads[4] = new bytes(2048);
    }

    function _swap(int256 amount) private pure returns (SwapParams memory) {
        return SwapParams({
            zeroForOne: true, amountSpecified: amount, sqrtPriceLimitX96: uint160(1 << 96)
        });
    }

    function _addParams(int24 lower, int24 upper)
        private
        pure
        returns (ModifyLiquidityParams memory)
    {
        return ModifyLiquidityParams({
            tickLower: lower, tickUpper: upper, liquidityDelta: 1, salt: bytes32(TOKEN_ID)
        });
    }

    function _positionInfo(int24 lower, int24 upper) private pure returns (uint256) {
        uint256 info;
        assembly ("memory-safe") {
            info := or(shl(8, and(lower, 0xffffff)), shl(32, and(upper, 0xffffff)))
        }
        return info;
    }

    function _config(uint64 version) private pure returns (OasisDynamicFeeHook.FeeConfig memory) {
        return OasisDynamicFeeHook.FeeConfig({
            baseFee: BASE_FEE,
            mediumFee: MEDIUM_FEE,
            highFee: HIGH_FEE,
            mediumAmount0Threshold: MEDIUM_THRESHOLD,
            highAmount0Threshold: HIGH_THRESHOLD,
            mediumAmount1Threshold: MEDIUM_THRESHOLD,
            highAmount1Threshold: HIGH_THRESHOLD,
            ruleVersion: version,
            configured: true
        });
    }

    function _mineSalt(OasisHookFactory targetFactory, bytes32 codeHash)
        private
        view
        returns (bytes32 salt)
    {
        uint160 requiredFlags = targetFactory.REQUIRED_HOOK_FLAGS();
        for (uint256 candidate = 0; candidate < 100_000; ++candidate) {
            salt = bytes32(candidate);
            address predicted = address(
                uint160(
                    uint256(
                        keccak256(
                            abi.encodePacked(bytes1(0xff), address(targetFactory), salt, codeHash)
                        )
                    )
                )
            );
            if (uint160(predicted) & Hooks.ALL_HOOK_MASK == requiredFlags) {
                return salt;
            }
        }
        revert("SALT_NOT_FOUND");
    }
}
