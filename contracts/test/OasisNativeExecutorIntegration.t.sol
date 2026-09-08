// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { Test } from "forge-std/Test.sol";
import { DeployPermit2 } from "permit2/test/utils/DeployPermit2.sol";
import { IAllowanceTransfer } from "permit2/src/interfaces/IAllowanceTransfer.sol";
import { PoolManager } from "@uniswap/v4-core/src/PoolManager.sol";
import { IHooks } from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import { LPFeeLibrary } from "@uniswap/v4-core/src/libraries/LPFeeLibrary.sol";
import { Hooks } from "@uniswap/v4-core/src/libraries/Hooks.sol";
import { TestERC20 } from "@uniswap/v4-core/src/test/TestERC20.sol";
import { Currency } from "@uniswap/v4-core/src/types/Currency.sol";
import { PoolKey } from "@uniswap/v4-core/src/types/PoolKey.sol";
import { PositionManager } from "@uniswap/v4-periphery/src/PositionManager.sol";
import { IPositionDescriptor } from "@uniswap/v4-periphery/src/interfaces/IPositionDescriptor.sol";
import { IWETH9 } from "@uniswap/v4-periphery/src/interfaces/external/IWETH9.sol";

import { OasisDynamicFeeHook } from "../src/OasisDynamicFeeHook.sol";
import { OasisHookFactory } from "../src/OasisHookFactory.sol";
import { OasisPositionExecutor } from "../src/OasisPositionExecutor.sol";
import { IOasisPositionManager } from "../src/interfaces/IOasisPositionManager.sol";

/// @notice Canonical native/ERC-20 canary lifecycle with real Permit2.
/// @dev Proves the exact native settlement shape used by MintOasisCanary and the persistent,
///      revocable operator approval required across successive recentered NFT generations.
contract OasisNativeExecutorIntegrationTest is Test {
    address private constant LP_OWNER = address(0x4000);
    address private constant GUARDIAN = address(0x3000);
    int24 private constant TICK_LOWER = -120;
    int24 private constant TICK_UPPER = 120;
    uint256 private constant INITIAL_LIQUIDITY = 1 ether;
    uint256 private constant RECENTER_LIQUIDITY = 0.1 ether;
    uint128 private constant MAX_AMOUNT = 1 ether;

    PoolManager private poolManager;
    IAllowanceTransfer private permit2;
    PositionManager private positionManager;
    OasisDynamicFeeHook private hook;
    OasisPositionExecutor private executor;
    TestERC20 private token1;
    PoolKey private key;

    function setUp() public {
        poolManager = new PoolManager(address(this));
        permit2 = IAllowanceTransfer(new DeployPermit2().deployPermit2());
        positionManager = new PositionManager(
            poolManager, permit2, 100_000, IPositionDescriptor(address(0)), IWETH9(address(0))
        );
        token1 = new TestERC20(type(uint128).max);

        OasisHookFactory factory = new OasisHookFactory();
        bytes32 codeHash = factory.initCodeHash(
            poolManager,
            IOasisPositionManager(address(positionManager)),
            address(this),
            address(this),
            GUARDIAN
        );
        hook = factory.deploy(
            _mineSalt(factory, codeHash),
            poolManager,
            IOasisPositionManager(address(positionManager)),
            address(this),
            address(this),
            GUARDIAN
        );
        executor = new OasisPositionExecutor(
            poolManager, positionManager, LP_OWNER, GUARDIAN, INITIAL_LIQUIDITY
        );
        key = PoolKey({
            currency0: Currency.wrap(address(0)),
            currency1: Currency.wrap(address(token1)),
            fee: LPFeeLibrary.DYNAMIC_FEE_FLAG,
            tickSpacing: 60,
            hooks: IHooks(address(hook))
        });
        hook.configurePool(key, _feeConfig());
        assertEq(poolManager.initialize(key, uint160(1 << 96)), 0);
        hook.setWhitelist(LP_OWNER, true);

        assertTrue(token1.transfer(LP_OWNER, MAX_AMOUNT));
        vm.deal(LP_OWNER, 2 ether);
        vm.startPrank(LP_OWNER);
        assertTrue(token1.approve(address(permit2), MAX_AMOUNT));
        permit2.approve(address(token1), address(positionManager), MAX_AMOUNT, type(uint48).max);
        // The recipient is the locally deployed canonical PositionManager and value is capped.
        // forge-lint: disable-start(arbitrary-send-eth)
        positionManager.modifyLiquidities{ value: MAX_AMOUNT }(
            _nativeMintPlan(), block.timestamp + 1
        );
        // forge-lint: disable-end(arbitrary-send-eth)
        permit2.approve(address(token1), address(positionManager), 0, 0);
        assertTrue(token1.approve(address(permit2), 0));
        positionManager.setApprovalForAll(address(executor), true);
        vm.stopPrank();
    }

    function test_nativeRecenterThenPausedRemovalLeavesNoExecutorOrPeripheryDust() public {
        assertEq(positionManager.ownerOf(1), LP_OWNER);
        assertEq(positionManager.getPositionLiquidity(1), INITIAL_LIQUIDITY);
        assertEq(address(positionManager).balance, 0);
        assertEq(address(executor).balance, 0);

        uint256 ownerNativeBefore = LP_OWNER.balance;
        uint256 ownerTokenBefore = token1.balanceOf(LP_OWNER);
        uint256 coreNativeBefore = address(poolManager).balance;
        uint256 coreTokenBefore = token1.balanceOf(address(poolManager));
        vm.prank(LP_OWNER);
        executor.execute(_intent(OasisPositionExecutor.Mode.RECENTER, 1, 1, RECENTER_LIQUIDITY));

        assertEq(positionManager.getPositionLiquidity(1), 0);
        assertEq(positionManager.ownerOf(2), LP_OWNER);
        assertEq(positionManager.getPositionLiquidity(2), RECENTER_LIQUIDITY);
        assertTrue(positionManager.isApprovedForAll(LP_OWNER, address(executor)));
        assertEq(
            LP_OWNER.balance - ownerNativeBefore, coreNativeBefore - address(poolManager).balance
        );
        assertEq(
            token1.balanceOf(LP_OWNER) - ownerTokenBefore,
            coreTokenBefore - token1.balanceOf(address(poolManager))
        );
        assertEq(address(positionManager).balance, 0);
        assertEq(address(executor).balance, 0);
        assertEq(token1.balanceOf(address(executor)), 0);

        hook.setRiskPaused(true);
        hook.setWhitelist(LP_OWNER, false);
        ownerNativeBefore = LP_OWNER.balance;
        ownerTokenBefore = token1.balanceOf(LP_OWNER);
        coreNativeBefore = address(poolManager).balance;
        coreTokenBefore = token1.balanceOf(address(poolManager));
        vm.prank(LP_OWNER);
        executor.execute(_intent(OasisPositionExecutor.Mode.REMOVE, 2, 2, 0));

        assertEq(positionManager.getPositionLiquidity(2), 0);
        assertEq(
            LP_OWNER.balance - ownerNativeBefore, coreNativeBefore - address(poolManager).balance
        );
        assertEq(
            token1.balanceOf(LP_OWNER) - ownerTokenBefore,
            coreTokenBefore - token1.balanceOf(address(poolManager))
        );
        assertEq(address(positionManager).balance, 0);
        assertEq(address(executor).balance, 0);
        assertEq(token1.balanceOf(address(executor)), 0);

        vm.prank(LP_OWNER);
        positionManager.setApprovalForAll(address(executor), false);
        assertFalse(positionManager.isApprovedForAll(LP_OWNER, address(executor)));
    }

    function _nativeMintPlan() private view returns (bytes memory) {
        bytes[] memory params = new bytes[](4);
        params[0] = abi.encode(
            key,
            TICK_LOWER,
            TICK_UPPER,
            INITIAL_LIQUIDITY,
            MAX_AMOUNT,
            MAX_AMOUNT,
            LP_OWNER,
            bytes("")
        );
        params[1] = abi.encode(key.currency0);
        params[2] = abi.encode(key.currency1);
        params[3] = abi.encode(key.currency0, LP_OWNER);
        return abi.encode(hex"02121214", params);
    }

    function _intent(
        OasisPositionExecutor.Mode mode,
        uint256 oldTokenId,
        uint64 nonce,
        uint256 liquidity
    ) private view returns (OasisPositionExecutor.Intent memory) {
        return OasisPositionExecutor.Intent({
            nonce: nonce,
            // A unix timestamp plus one minute fits in uint64 for centuries.
            // forge-lint: disable-next-line(unsafe-typecast)
            deadline: uint64(block.timestamp + 60),
            mode: mode,
            poolKey: key,
            oldTokenId: oldTokenId,
            tickLower: mode == OasisPositionExecutor.Mode.RECENTER ? int24(-60) : int24(0),
            tickUpper: mode == OasisPositionExecutor.Mode.RECENTER ? int24(180) : int24(0),
            liquidity: liquidity,
            amount0Min: 0,
            amount1Min: 0,
            amount0Max: MAX_AMOUNT,
            amount1Max: MAX_AMOUNT,
            maxSwapInput: 0,
            hookData: bytes("")
        });
    }

    function _mineSalt(OasisHookFactory factory, bytes32 codeHash)
        private
        view
        returns (bytes32 salt)
    {
        uint160 requiredFlags = factory.REQUIRED_HOOK_FLAGS();
        for (uint256 candidate = 0; candidate < 100_000; ++candidate) {
            salt = bytes32(candidate);
            bytes32 digest =
                keccak256(abi.encodePacked(bytes1(0xff), address(factory), salt, codeHash));
            if (uint160(uint256(digest)) & Hooks.ALL_HOOK_MASK == requiredFlags) return salt;
        }
        revert("SALT_NOT_FOUND");
    }

    function _feeConfig() private pure returns (OasisDynamicFeeHook.FeeConfig memory) {
        return OasisDynamicFeeHook.FeeConfig({
            baseFee: 500,
            mediumFee: 2_000,
            highFee: 5_000,
            mediumAmount0Threshold: 1 ether,
            highAmount0Threshold: 10 ether,
            mediumAmount1Threshold: 1 ether,
            highAmount1Threshold: 10 ether,
            ruleVersion: 1,
            configured: true
        });
    }
}
