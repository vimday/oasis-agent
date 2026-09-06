// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { Ownable } from "@openzeppelin/contracts/access/Ownable.sol";
import { PoolManager } from "@uniswap/v4-core/src/PoolManager.sol";
import { IHooks } from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import { IPoolManager } from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import { Hooks } from "@uniswap/v4-core/src/libraries/Hooks.sol";
import { LPFeeLibrary } from "@uniswap/v4-core/src/libraries/LPFeeLibrary.sol";
import { PoolSwapTest } from "@uniswap/v4-core/src/test/PoolSwapTest.sol";
import { TestERC20 } from "@uniswap/v4-core/src/test/TestERC20.sol";
import { BalanceDelta } from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import { BeforeSwapDelta } from "@uniswap/v4-core/src/types/BeforeSwapDelta.sol";
import { Currency } from "@uniswap/v4-core/src/types/Currency.sol";
import { PoolId, PoolIdLibrary } from "@uniswap/v4-core/src/types/PoolId.sol";
import { PoolKey } from "@uniswap/v4-core/src/types/PoolKey.sol";
import { ModifyLiquidityParams, SwapParams } from "@uniswap/v4-core/src/types/PoolOperation.sol";

import { OasisDynamicFeeHook } from "../src/OasisDynamicFeeHook.sol";
import { OasisHookFactory } from "../src/OasisHookFactory.sol";
import { IOasisPositionManager } from "../src/interfaces/IOasisPositionManager.sol";

struct VmLog {
    bytes32[] topics;
    bytes data;
    address emitter;
}

interface Vm {
    function prank(address sender) external;
    function expectRevert(bytes calldata revertData) external;
    function recordLogs() external;
    function getRecordedLogs() external returns (VmLog[] memory logs);
}

contract MockPositionManager is IOasisPositionManager {
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

contract OasisDynamicFeeHookTest {
    using PoolIdLibrary for PoolKey;

    Vm private constant VM = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    address private constant POOL_MANAGER_ADDRESS = address(0x1000);
    address private constant INITIALIZER = address(0x2000);
    address private constant GUARDIAN = address(0x3000);
    address private constant LP_OWNER = address(0x4000);
    address private constant OTHER = address(0x5000);
    uint256 private constant TOKEN_ID = 7;
    uint24 private constant BASE_FEE = 500;
    uint24 private constant MEDIUM_FEE = 2_000;
    uint24 private constant HIGH_FEE = 5_000;
    int24 private constant TICK_LOWER = -120;
    int24 private constant TICK_UPPER = 120;

    MockPositionManager private positionManager;
    OasisHookFactory private factory;
    OasisDynamicFeeHook private hook;
    PoolKey private key;

    function setUp() public {
        positionManager = new MockPositionManager();
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

    function test_permissionMaskIsMinimalAndRemovalIsNotHooked() public view {
        Hooks.Permissions memory permissions = hook.getHookPermissions();
        require(permissions.beforeInitialize);
        require(permissions.beforeAddLiquidity);
        require(permissions.beforeSwap);
        require(!permissions.beforeRemoveLiquidity);
        require(!permissions.beforeSwapReturnDelta);
        require(uint160(address(hook)) & Hooks.ALL_HOOK_MASK == factory.REQUIRED_HOOK_FLAGS());
    }

    function test_feeBandsAreDeterministicAndBounded() public view {
        (uint24 low, uint64 lowVersion, bytes32 lowHash, uint256 lowDeadline) =
            hook.previewFee(key, _swap(-999));
        (uint24 medium, uint64 mediumVersion, bytes32 mediumHash, uint256 mediumDeadline) =
            hook.previewFee(key, _swap(-1_000));
        (uint24 high, uint64 highVersion, bytes32 highHash, uint256 highDeadline) =
            hook.previewFee(key, _swap(type(int256).min));
        require(low == BASE_FEE);
        require(medium == MEDIUM_FEE);
        require(high == HIGH_FEE);
        require(lowHash == mediumHash && mediumHash == highHash);
        require(lowVersion == 1 && mediumVersion == 1 && highVersion == 1);
        require(lowDeadline == mediumDeadline && mediumDeadline == highDeadline);
    }

    function test_beforeSwapOverridesBothExactInputAndExactOutput() public {
        VM.prank(POOL_MANAGER_ADDRESS);
        (bytes4 exactInputSelector, BeforeSwapDelta exactInputDelta, uint24 exactInputFee) =
            hook.beforeSwap(OTHER, key, _swap(-10_000), "");
        VM.prank(POOL_MANAGER_ADDRESS);
        (bytes4 exactOutputSelector, BeforeSwapDelta exactOutputDelta, uint24 exactOutputFee) =
            hook.beforeSwap(OTHER, key, _swap(10_000), "");

        require(exactInputSelector == IHooks.beforeSwap.selector);
        require(exactOutputSelector == IHooks.beforeSwap.selector);
        require(BeforeSwapDelta.unwrap(exactInputDelta) == 0);
        require(BeforeSwapDelta.unwrap(exactOutputDelta) == 0);
        uint24 expected = HIGH_FEE | LPFeeLibrary.OVERRIDE_FEE_FLAG;
        require(exactInputFee == expected && exactOutputFee == expected);
    }

    function test_feeThresholdsUseTheSpecifiedTokenBaseUnits() public {
        OasisDynamicFeeHook.FeeConfig memory config = _config(2);
        config.mediumAmount1Threshold = 1_000_000;
        config.highAmount1Threshold = 10_000_000;
        hook.configurePool(key, config);

        // zeroForOne exact-input specifies token0; exact-output specifies token1.
        require(_preview(_swapDirection(true, -10_000)) == HIGH_FEE);
        require(_preview(_swapDirection(true, 10_000)) == BASE_FEE);
        // oneForZero exact-input specifies token1; exact-output specifies token0.
        require(_preview(_swapDirection(false, -10_000)) == BASE_FEE);
        require(_preview(_swapDirection(false, 10_000)) == HIGH_FEE);
    }

    function test_realPoolManagerDispatchesHookAndEmitsOverriddenFee() public {
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
        TestERC20 token = new TestERC20(0);
        PoolKey memory realKey = PoolKey({
            currency0: Currency.wrap(address(0)),
            currency1: Currency.wrap(address(token)),
            fee: LPFeeLibrary.DYNAMIC_FEE_FLAG,
            tickSpacing: 60,
            hooks: IHooks(address(realHook))
        });
        realHook.configurePool(realKey, _config(1));
        int24 initializedTick = realManager.initialize(realKey, uint160(1 << 96));
        require(initializedTick == 0);

        PoolSwapTest router = new PoolSwapTest(realManager);
        uint160 priceLimit = uint160((1 << 96) - 1);
        VM.recordLogs();
        BalanceDelta delta = router.swap(
            realKey,
            SwapParams({ zeroForOne: true, amountSpecified: -1, sqrtPriceLimitX96: priceLimit }),
            PoolSwapTest.TestSettings({ takeClaims: false, settleUsingBurn: false }),
            ""
        );
        require(BalanceDelta.unwrap(delta) == 0);
        _assertNoFillSwapLog(VM.getRecordedLogs(), address(realManager), BASE_FEE, priceLimit);
    }

    function test_previewRejectsForeignOrStaticPoolKeys() public {
        PoolKey memory foreignHook = key;
        foreignHook.hooks = IHooks(OTHER);
        VM.expectRevert(abi.encodeWithSelector(OasisDynamicFeeHook.InvalidPoolHook.selector, OTHER));
        // Revert assertion consumes the callback return path.
        // forge-lint: disable-next-line(unused-return)
        hook.previewFee(foreignHook, _swap(-1));

        PoolKey memory staticFee = key;
        staticFee.fee = BASE_FEE;
        VM.expectRevert(
            abi.encodeWithSelector(OasisDynamicFeeHook.PoolMustUseDynamicFee.selector, BASE_FEE)
        );
        // Revert assertion consumes the callback return path.
        // forge-lint: disable-next-line(unused-return)
        hook.previewFee(staticFee, _swap(-1));
    }

    function test_onlyInitializerCanInitializeConfiguredDynamicPool() public {
        VM.expectRevert(
            abi.encodeWithSelector(OasisDynamicFeeHook.UnauthorizedInitializer.selector, OTHER)
        );
        VM.prank(POOL_MANAGER_ADDRESS);
        // Revert assertion consumes the callback return path.
        // forge-lint: disable-next-line(unused-return)
        hook.beforeInitialize(OTHER, key, uint160(1 << 96));

        VM.prank(POOL_MANAGER_ADDRESS);
        bytes4 result = hook.beforeInitialize(INITIALIZER, key, uint160(1 << 96));
        require(result == IHooks.beforeInitialize.selector);
    }

    function test_whitelistedNftOwnerCanAddAndOtherOwnerCannot() public {
        hook.setWhitelist(LP_OWNER, true);
        positionManager.setPosition(TOKEN_ID, LP_OWNER, key, _positionInfo(TICK_LOWER, TICK_UPPER));
        VM.prank(POOL_MANAGER_ADDRESS);
        bytes4 result = hook.beforeAddLiquidity(
            address(positionManager), key, _liquidityParams(TICK_LOWER, TICK_UPPER), ""
        );
        require(result == IHooks.beforeAddLiquidity.selector);

        positionManager.setPosition(TOKEN_ID, OTHER, key, _positionInfo(TICK_LOWER, TICK_UPPER));
        VM.expectRevert(
            abi.encodeWithSelector(
                OasisDynamicFeeHook.PositionNotWhitelisted.selector, TOKEN_ID, OTHER
            )
        );
        VM.prank(POOL_MANAGER_ADDRESS);
        bytes4 unauthorizedResult = hook.beforeAddLiquidity(
            address(positionManager), key, _liquidityParams(TICK_LOWER, TICK_UPPER), ""
        );
        require(unauthorizedResult == bytes4(0));
    }

    function test_addRequiresCanonicalPositionManagerAndExactPositionBinding() public {
        hook.setWhitelist(LP_OWNER, true);
        positionManager.setPosition(TOKEN_ID, LP_OWNER, key, _positionInfo(TICK_LOWER, TICK_UPPER));

        VM.expectRevert(
            abi.encodeWithSelector(OasisDynamicFeeHook.WrongPositionManager.selector, OTHER)
        );
        VM.prank(POOL_MANAGER_ADDRESS);
        // Revert assertion consumes the callback return path.
        // forge-lint: disable-next-line(unused-return)
        hook.beforeAddLiquidity(OTHER, key, _liquidityParams(TICK_LOWER, TICK_UPPER), "");

        PoolKey memory otherKey = key;
        otherKey.currency1 = Currency.wrap(address(0x30));
        positionManager.setPosition(
            TOKEN_ID, LP_OWNER, otherKey, _positionInfo(TICK_LOWER, TICK_UPPER)
        );
        VM.expectRevert(
            abi.encodeWithSelector(OasisDynamicFeeHook.PositionPoolMismatch.selector, TOKEN_ID)
        );
        VM.prank(POOL_MANAGER_ADDRESS);
        bytes4 poolMismatchResult = hook.beforeAddLiquidity(
            address(positionManager), key, _liquidityParams(TICK_LOWER, TICK_UPPER), ""
        );
        require(poolMismatchResult == bytes4(0));

        positionManager.setPosition(TOKEN_ID, LP_OWNER, key, _positionInfo(-180, TICK_UPPER));
        VM.expectRevert(
            abi.encodeWithSelector(OasisDynamicFeeHook.PositionRangeMismatch.selector, TOKEN_ID)
        );
        VM.prank(POOL_MANAGER_ADDRESS);
        bytes4 rangeMismatchResult = hook.beforeAddLiquidity(
            address(positionManager), key, _liquidityParams(TICK_LOWER, TICK_UPPER), ""
        );
        require(rangeMismatchResult == bytes4(0));
    }

    function test_guardianCanOnlyReduceAuthority() public {
        hook.setWhitelist(LP_OWNER, true);
        VM.prank(GUARDIAN);
        hook.guardianRevoke(LP_OWNER);
        require(!hook.isWhitelisted(LP_OWNER));

        VM.expectRevert(
            abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, GUARDIAN)
        );
        VM.prank(GUARDIAN);
        hook.setWhitelist(LP_OWNER, true);

        VM.expectRevert(
            abi.encodeWithSelector(OasisDynamicFeeHook.UnauthorizedGuardian.selector, OTHER)
        );
        VM.prank(OTHER);
        hook.guardianRevoke(LP_OWNER);
    }

    function test_feeConfigRequiresOwnerIncreasingVersionAndHardCap() public {
        VM.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, OTHER));
        VM.prank(OTHER);
        hook.configurePool(key, _config(2));

        VM.expectRevert(
            abi.encodeWithSelector(OasisDynamicFeeHook.InvalidFeeConfiguration.selector)
        );
        hook.configurePool(key, _config(1));

        OasisDynamicFeeHook.FeeConfig memory excessive = _config(2);
        excessive.highFee = hook.MAX_CONFIGURED_FEE() + 1;
        VM.expectRevert(
            abi.encodeWithSelector(OasisDynamicFeeHook.InvalidFeeConfiguration.selector)
        );
        hook.configurePool(key, excessive);

        hook.configurePool(key, _config(2));
        OasisDynamicFeeHook.FeeConfig memory configured = hook.feeConfig(key);
        require(configured.ruleVersion == 2);
    }

    function test_firstConfigurationPermanentlyBindsOnePool() public {
        PoolKey memory otherPool = key;
        otherPool.currency1 = Currency.wrap(address(0x30));
        bytes32 requestedPoolId = PoolId.unwrap(otherPool.toId());
        bytes32 expectedBoundPoolId = PoolId.unwrap(key.toId());
        require(hook.boundPoolId() == expectedBoundPoolId);

        bytes memory expectedError = abi.encodeWithSelector(
            OasisDynamicFeeHook.PoolBindingImmutable.selector, expectedBoundPoolId, requestedPoolId
        );
        VM.expectRevert(expectedError);
        hook.configurePool(otherPool, _config(2));

        VM.expectRevert(expectedError);
        // Revert assertion consumes the return path.
        // forge-lint: disable-next-line(unused-return)
        hook.feeConfig(otherPool);
    }

    function testFuzz_feeClassificationIsBounded(int256 amount) public view {
        uint24 actual = _previewAndValidate(amount);
        uint256 magnitude = _absolute(amount);
        uint24 expected =
            magnitude >= 10_000 ? HIGH_FEE : magnitude >= 1_000 ? MEDIUM_FEE : BASE_FEE;
        require(actual == expected);
        require(actual >= 100 && actual <= hook.MAX_CONFIGURED_FEE());
    }

    function testFuzz_feeIsMonotoneByTradeMagnitude(uint128 first, uint128 second) public view {
        uint128 low = first <= second ? first : second;
        uint128 high = first <= second ? second : first;
        uint24 lowFee = _previewAndValidate(int256(uint256(low)));
        uint24 highFee = _previewAndValidate(int256(uint256(high)));
        require(lowFee <= highFee);
    }

    function test_pauseStopsAddsButKeepsBaseFeeSwapPath() public {
        hook.setWhitelist(LP_OWNER, true);
        positionManager.setPosition(TOKEN_ID, LP_OWNER, key, _positionInfo(TICK_LOWER, TICK_UPPER));
        VM.prank(GUARDIAN);
        hook.emergencyPause();

        VM.expectRevert(
            abi.encodeWithSelector(OasisDynamicFeeHook.LiquidityAdditionPaused.selector)
        );
        VM.prank(POOL_MANAGER_ADDRESS);
        bytes4 pausedResult = hook.beforeAddLiquidity(
            address(positionManager), key, _liquidityParams(TICK_LOWER, TICK_UPPER), ""
        );
        require(pausedResult == bytes4(0));

        VM.prank(POOL_MANAGER_ADDRESS);
        (bytes4 selector, BeforeSwapDelta delta, uint24 overrideFee) =
            hook.beforeSwap(OTHER, key, _swap(-100_000), "");
        require(selector == IHooks.beforeSwap.selector);
        require(BeforeSwapDelta.unwrap(delta) == 0);
        require(overrideFee == (BASE_FEE | LPFeeLibrary.OVERRIDE_FEE_FLAG));
    }

    function test_callbacksRejectDirectCallers() public {
        VM.expectRevert(
            abi.encodeWithSelector(OasisDynamicFeeHook.CallerNotPoolManager.selector, address(this))
        );
        // Revert assertion consumes the callback return path.
        // forge-lint: disable-next-line(unused-return)
        hook.beforeSwap(OTHER, key, _swap(-1), "");
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

    function _swap(int256 amount) private pure returns (SwapParams memory) {
        return _swapDirection(true, amount);
    }

    function _swapDirection(bool zeroForOne, int256 amount)
        private
        pure
        returns (SwapParams memory)
    {
        return SwapParams({
            zeroForOne: zeroForOne, amountSpecified: amount, sqrtPriceLimitX96: uint160(1 << 96)
        });
    }

    function _liquidityParams(int24 lower, int24 upper)
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
            mediumAmount0Threshold: 1_000,
            highAmount0Threshold: 10_000,
            mediumAmount1Threshold: 1_000,
            highAmount1Threshold: 10_000,
            ruleVersion: version,
            configured: true
        });
    }

    function _preview(SwapParams memory params) private view returns (uint24 fee) {
        uint64 version;
        bytes32 stateHash;
        uint256 deadline;
        (fee, version, stateHash, deadline) = hook.previewFee(key, params);
        require(version == 2 && stateHash != bytes32(0) && deadline > 0);
    }

    function _absolute(int256 value) private pure returns (uint256) {
        // The branch proves the signed value is non-negative.
        // forge-lint: disable-next-line(unsafe-typecast)
        if (value >= 0) return uint256(value);
        unchecked {
            // Adding one before negation makes type(int256).min representable.
            // forge-lint: disable-next-line(unsafe-typecast)
            return uint256(-(value + 1)) + 1;
        }
    }

    function _previewAndValidate(int256 amount) private view returns (uint24 fee) {
        uint64 version;
        bytes32 stateHash;
        uint256 deadline;
        (fee, version, stateHash, deadline) = hook.previewFee(key, _swap(amount));
        require(version == 1);
        require(stateHash != bytes32(0));
        // Exact timestamp equality is intentional: this test verifies deterministic quote expiry.
        // forge-lint: disable-next-line(block-timestamp)
        require(deadline == block.timestamp + hook.QUOTE_VALIDITY_SECONDS());
    }

    function _assertNoFillSwapLog(
        VmLog[] memory logs,
        address emitter,
        uint24 expectedFee,
        uint160 expectedSqrtPriceX96
    ) private pure {
        bytes32 signature = keccak256(
            "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"
        );
        bool found = false;
        bytes memory data;
        for (uint256 i = 0; i < logs.length; ++i) {
            if (
                logs[i].emitter == emitter && logs[i].topics.length == 3
                    && logs[i].topics[0] == signature
            ) {
                data = logs[i].data;
                found = true;
            }
        }
        require(found);
        (
            int128 amount0,
            int128 amount1,
            uint160 sqrtPriceX96,
            uint128 liquidity,
            int24 tick,
            uint24 fee
        ) = abi.decode(data, (int128, int128, uint160, uint128, int24, uint24));
        require(amount0 == 0 && amount1 == 0);
        require(sqrtPriceX96 == expectedSqrtPriceX96);
        require(liquidity == 0 && tick == -1);
        require(fee == expectedFee);
    }
}
