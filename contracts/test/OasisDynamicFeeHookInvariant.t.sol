// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { Test } from "forge-std/Test.sol";

import { IHooks } from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import { IPoolManager } from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import { Hooks } from "@uniswap/v4-core/src/libraries/Hooks.sol";
import { LPFeeLibrary } from "@uniswap/v4-core/src/libraries/LPFeeLibrary.sol";
import { Currency } from "@uniswap/v4-core/src/types/Currency.sol";
import { PoolId, PoolIdLibrary } from "@uniswap/v4-core/src/types/PoolId.sol";
import { PoolKey } from "@uniswap/v4-core/src/types/PoolKey.sol";
import { SwapParams } from "@uniswap/v4-core/src/types/PoolOperation.sol";

import { OasisDynamicFeeHook } from "../src/OasisDynamicFeeHook.sol";
import { OasisHookFactory } from "../src/OasisHookFactory.sol";
import { IOasisPositionManager } from "../src/interfaces/IOasisPositionManager.sol";

contract HookInvariantPositionManager is IOasisPositionManager {
    function ownerOf(uint256) external pure returns (address) {
        revert("UNUSED");
    }

    function getPoolAndPositionInfo(uint256) external pure returns (PoolKey memory, uint256) {
        revert("UNUSED");
    }
}

contract HookGovernanceHandler {
    OasisDynamicFeeHook public immutable hook;
    PoolKey private _key;
    uint64 private _version = 1;

    constructor(OasisDynamicFeeHook hook_, PoolKey memory key_) {
        hook = hook_;
        _key = key_;
    }

    function acceptOwnership() external {
        hook.acceptOwnership();
    }

    function reconfigure(uint256 seed) external {
        uint24 first = _boundedFee(seed);
        uint24 second = _boundedFee(seed >> 24);
        uint24 third = _boundedFee(seed >> 48);
        (uint24 baseFee, uint24 mediumFee, uint24 highFee) = _sort(first, second, third);
        uint128 medium0 = _boundedThreshold(seed >> 72, 1e24) + 1;
        uint128 high0 = medium0 + _boundedThreshold(seed >> 120, 1e24) + 1;
        uint128 medium1 = _boundedThreshold(seed >> 168, 1e24) + 1;
        uint128 high1 = medium1 + _boundedThreshold(seed >> 216, 1e12) + 1;

        unchecked {
            ++_version;
        }
        hook.configurePool(
            _key,
            OasisDynamicFeeHook.FeeConfig({
                baseFee: baseFee,
                mediumFee: mediumFee,
                highFee: highFee,
                mediumAmount0Threshold: medium0,
                highAmount0Threshold: high0,
                mediumAmount1Threshold: medium1,
                highAmount1Threshold: high1,
                ruleVersion: _version,
                configured: true
            })
        );
    }

    function setPaused(bool paused) external {
        if (hook.riskPaused() != paused) hook.setRiskPaused(paused);
    }

    function setWhitelist(uint256 seed, bool allowed) external {
        address account = _boundedAccount(seed);
        if (hook.isWhitelisted(account) != allowed) hook.setWhitelist(account, allowed);
    }

    function _boundedFee(uint256 value) private pure returns (uint24) {
        value %= 100_001;
        // Modulo bounds value far below uint24.max, so narrowing is lossless.
        // forge-lint: disable-next-line(unsafe-typecast)
        return uint24(value);
    }

    function _boundedThreshold(uint256 value, uint256 modulus) private pure returns (uint128) {
        value %= modulus;
        // Both call-site moduli are below uint128.max, so narrowing is lossless.
        // forge-lint: disable-next-line(unsafe-typecast)
        return uint128(value);
    }

    function _boundedAccount(uint256 seed) private pure returns (address) {
        uint256 value = 0x10_000 + (seed % 10_000);
        // The constructed value is at most 0x1270f and therefore fits uint160 exactly.
        // forge-lint: disable-next-line(unsafe-typecast)
        return address(uint160(value));
    }

    function _sort(uint24 a, uint24 b, uint24 c)
        private
        pure
        returns (uint24 low, uint24 middle, uint24 high)
    {
        if (a > b) (a, b) = (b, a);
        if (b > c) (b, c) = (c, b);
        if (a > b) (a, b) = (b, a);
        return (a, b, c);
    }
}

contract OasisDynamicFeeHookInvariantTest is Test {
    using PoolIdLibrary for PoolKey;

    address private constant POOL_MANAGER = address(0x1000);
    address private constant INITIALIZER = address(0x2000);
    address private constant GUARDIAN = address(0x3000);

    OasisDynamicFeeHook private hook;
    HookGovernanceHandler private handler;
    PoolKey private key;

    function setUp() public {
        HookInvariantPositionManager positionManager = new HookInvariantPositionManager();
        OasisHookFactory factory = new OasisHookFactory();
        bytes32 codeHash = factory.initCodeHash(
            IPoolManager(POOL_MANAGER), positionManager, INITIALIZER, address(this), GUARDIAN
        );
        hook = factory.deploy(
            _mineSalt(factory, codeHash),
            IPoolManager(POOL_MANAGER),
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
        hook.configurePool(key, _initialConfig());

        handler = new HookGovernanceHandler(hook, key);
        hook.transferOwnership(address(handler));
        handler.acceptOwnership();

        bytes4[] memory selectors = new bytes4[](3);
        selectors[0] = HookGovernanceHandler.reconfigure.selector;
        selectors[1] = HookGovernanceHandler.setPaused.selector;
        selectors[2] = HookGovernanceHandler.setWhitelist.selector;
        targetContract(address(handler));
        targetSelector(FuzzSelector({ addr: address(handler), selectors: selectors }));
    }

    function invariant_configAndEveryQuotedFeeRemainBounded() public view {
        OasisDynamicFeeHook.FeeConfig memory config = hook.feeConfig(key);
        assertTrue(config.configured);
        assertEq(hook.boundPoolId(), PoolId.unwrap(key.toId()));
        assertGt(config.ruleVersion, 0);
        assertLe(config.baseFee, config.mediumFee);
        assertLe(config.mediumFee, config.highFee);
        assertLe(config.highFee, hook.MAX_CONFIGURED_FEE());
        assertGt(config.mediumAmount0Threshold, 0);
        assertGt(config.highAmount0Threshold, config.mediumAmount0Threshold);
        assertGt(config.mediumAmount1Threshold, 0);
        assertGt(config.highAmount1Threshold, config.mediumAmount1Threshold);

        _assertQuote(config, true, -1);
        _assertQuote(config, true, type(int256).max);
        _assertQuote(config, false, -1);
        _assertQuote(config, false, type(int256).max);
    }

    function invariant_hookAuthoritySurfaceRemainsMinimal() public view {
        Hooks.Permissions memory permissions = hook.getHookPermissions();
        assertTrue(permissions.beforeInitialize);
        assertTrue(permissions.beforeAddLiquidity);
        assertTrue(permissions.beforeSwap);
        assertFalse(permissions.beforeRemoveLiquidity);
        assertFalse(permissions.afterRemoveLiquidity);
        assertFalse(permissions.beforeSwapReturnDelta);
        assertFalse(permissions.afterSwapReturnDelta);
        assertEq(uint160(address(hook)) & Hooks.ALL_HOOK_MASK, hook.REQUIRED_HOOK_FLAGS());
    }

    function _assertQuote(
        OasisDynamicFeeHook.FeeConfig memory config,
        bool zeroForOne,
        int256 amount
    ) private view {
        SwapParams memory params = SwapParams({
            zeroForOne: zeroForOne, amountSpecified: amount, sqrtPriceLimitX96: uint160(1 << 96)
        });
        (uint24 fee, uint64 version, bytes32 stateHash, uint256 deadline) =
            hook.previewFee(key, params);
        assertEq(version, config.ruleVersion);
        assertNotEq(stateHash, bytes32(0));
        assertEq(deadline, block.timestamp + hook.QUOTE_VALIDITY_SECONDS());
        if (hook.riskPaused()) {
            assertEq(fee, config.baseFee);
        } else {
            assertTrue(fee == config.baseFee || fee == config.mediumFee || fee == config.highFee);
        }
    }

    function _mineSalt(OasisHookFactory factory, bytes32 codeHash)
        private
        view
        returns (bytes32 salt)
    {
        uint160 requiredFlags = factory.REQUIRED_HOOK_FLAGS();
        for (uint256 candidate = 0; candidate < 100_000; ++candidate) {
            salt = bytes32(candidate);
            address predicted = _create2Address(address(factory), salt, codeHash);
            if (uint160(predicted) & Hooks.ALL_HOOK_MASK == requiredFlags) return salt;
        }
        revert("SALT_NOT_FOUND");
    }

    function _create2Address(address deployer, bytes32 salt, bytes32 codeHash)
        private
        pure
        returns (address)
    {
        bytes32 digest = keccak256(abi.encodePacked(bytes1(0xff), deployer, salt, codeHash));
        // CREATE2 specifies the low 160 bits of this digest as the deployed address.
        // forge-lint: disable-next-line(unsafe-typecast)
        return address(uint160(uint256(digest)));
    }

    function _initialConfig() private pure returns (OasisDynamicFeeHook.FeeConfig memory) {
        return OasisDynamicFeeHook.FeeConfig({
            baseFee: 500,
            mediumFee: 2_000,
            highFee: 5_000,
            mediumAmount0Threshold: 1_000,
            highAmount0Threshold: 10_000,
            mediumAmount1Threshold: 1_000,
            highAmount1Threshold: 10_000,
            ruleVersion: 1,
            configured: true
        });
    }
}
