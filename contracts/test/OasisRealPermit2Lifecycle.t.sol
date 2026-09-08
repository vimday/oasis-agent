// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { IAllowanceTransfer } from "permit2/src/interfaces/IAllowanceTransfer.sol";
import { DeployPermit2 } from "permit2/test/utils/DeployPermit2.sol";
import { PoolManager } from "@uniswap/v4-core/src/PoolManager.sol";
import { IHooks } from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import { IPoolManager } from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import { CustomRevert } from "@uniswap/v4-core/src/libraries/CustomRevert.sol";
import { Hooks } from "@uniswap/v4-core/src/libraries/Hooks.sol";
import { PositionManager } from "@uniswap/v4-periphery/src/PositionManager.sol";
import { IPositionManager } from "@uniswap/v4-periphery/src/interfaces/IPositionManager.sol";
import { IPositionDescriptor } from "@uniswap/v4-periphery/src/interfaces/IPositionDescriptor.sol";
import { IWETH9 } from "@uniswap/v4-periphery/src/interfaces/external/IWETH9.sol";
import { OasisDynamicFeeHook } from "../src/OasisDynamicFeeHook.sol";
import { OasisLifecycleBase } from "./shared/OasisLifecycleBase.sol";

/// @notice Offline canonical-runtime lifecycle. No public RPC or private keys.
/// The pinned upstream helper installs real Permit2 runtime, not a transfer mock.
/// Inherits the exact mint/bidirectional swap/collect/revoke/unwind fork scenario.
contract OasisRealPermit2LifecycleTest is OasisLifecycleBase {
    function setUp() public override {
        manager = new PoolManager(address(this));
        permit2 = IAllowanceTransfer(new DeployPermit2().deployPermit2());
        positions = new PositionManager(
            manager, permit2, 100_000, IPositionDescriptor(address(0)), IWETH9(address(0))
        );
        _setupPool();
    }

    function test_expiredPermit2AllowanceRejectsMintAtomically() public {
        vm.warp(block.timestamp + 2 days);
        (uint160 allowanceAmount, uint48 expiration, uint48 nonce) =
            permit2.allowance(OWNER, address(token0), address(positions));
        assertGt(allowanceAmount, 0);
        assertEq(nonce, 0);
        _expectMintFailure(
            abi.encodeWithSelector(IAllowanceTransfer.AllowanceExpired.selector, expiration)
        );
    }

    function test_revokedPermit2AllowanceRejectsMintAtomically() public {
        vm.startPrank(OWNER);
        permit2.approve(address(token0), address(positions), 0, 0);
        permit2.approve(address(token1), address(positions), 0, 0);
        vm.stopPrank();
        // The pinned Permit2 checks the remaining amount before the expiration,
        // so a zeroed allowance surfaces as InsufficientAllowance(0).
        _expectMintFailure(
            abi.encodeWithSelector(IAllowanceTransfer.InsufficientAllowance.selector, 0)
        );
    }

    function test_insufficientPermit2AllowanceRejectsMintAtomically() public {
        vm.prank(OWNER);
        permit2.approve(address(token0), address(positions), 1, type(uint48).max);
        _expectMintFailure(
            abi.encodeWithSelector(IAllowanceTransfer.InsufficientAllowance.selector, 1)
        );
    }

    function test_tokenReturningFalseRejectsMintAtomically() public {
        vm.mockCall(
            address(token0),
            abi.encodeWithSignature("transferFrom(address,address,uint256)"),
            abi.encode(false)
        );
        _expectMintFailure(abi.encodeWithSignature("Error(string)", "TRANSFER_FROM_FAILED"));
    }

    function test_tokenLyingAboutSettlementRejectsMintAtomically() public {
        // Permit2 accepts the success return, but the pool must detect missing cash.
        vm.mockCall(
            address(token1),
            abi.encodeWithSignature("transferFrom(address,address,uint256)"),
            abi.encode(true)
        );
        _expectMintFailure(abi.encodeWithSelector(IPoolManager.CurrencyNotSettled.selector));
        assertEq(token0.balanceOf(address(manager)), 0);
        assertEq(token1.balanceOf(address(manager)), 0);
    }

    function _mintPosition() internal {
        bytes[] memory params = new bytes[](3);
        params[0] =
            abi.encode(key, int24(-120), int24(120), LIQUIDITY, CAPITAL, CAPITAL, OWNER, bytes(""));
        params[1] = abi.encode(key.currency0);
        params[2] = abi.encode(key.currency1);
        vm.prank(OWNER);
        positions.modifyLiquidities(abi.encode(hex"021212", params), block.timestamp + 1);
    }

    function test_pauseAndRevokedAllowancesStillPermitFullExit() public {
        _mintPosition();
        hook.setRiskPaused(true);
        hook.setWhitelist(OWNER, false);
        vm.startPrank(OWNER);
        permit2.approve(address(token0), address(positions), 0, 0);
        permit2.approve(address(token1), address(positions), 0, 0);
        assertTrue(token0.approve(address(permit2), 0));
        assertTrue(token1.approve(address(permit2), 0));
        vm.stopPrank();
        uint256 before0 = token0.balanceOf(OWNER);
        uint256 before1 = token1.balanceOf(OWNER);
        uint256 core0 = token0.balanceOf(address(manager));
        uint256 core1 = token1.balanceOf(address(manager));
        _change(hex"01", LIQUIDITY);
        assertEq(positions.getPositionLiquidity(tokenId), 0);
        assertGt(token0.balanceOf(OWNER), before0);
        assertGt(token1.balanceOf(OWNER), before1);
        assertEq(token0.balanceOf(OWNER) - before0, core0 - token0.balanceOf(address(manager)));
        assertEq(token1.balanceOf(OWNER) - before1, core1 - token1.balanceOf(address(manager)));
    }

    function test_transferredNftCannotUsePreviousOwnersWhitelistButCanExit() public {
        _mintPosition();
        address recipient = address(0x5000);
        vm.prank(OWNER);
        positions.transferFrom(OWNER, recipient, tokenId);
        assertEq(positions.ownerOf(tokenId), recipient);
        vm.expectRevert(abi.encodeWithSelector(IPositionManager.NotApproved.selector, OWNER));
        _change(hex"01", LIQUIDITY); // former owner has no authority
        vm.startPrank(recipient);
        assertTrue(token0.approve(address(permit2), CAPITAL));
        assertTrue(token1.approve(address(permit2), CAPITAL));
        permit2.approve(address(token0), address(positions), CAPITAL, type(uint48).max);
        permit2.approve(address(token1), address(positions), CAPITAL, type(uint48).max);
        vm.stopPrank();
        assertTrue(token0.transfer(recipient, CAPITAL / 2));
        assertTrue(token1.transfer(recipient, CAPITAL / 2));
        bytes[] memory params = new bytes[](3);
        params[0] = abi.encode(tokenId, uint256(1), CAPITAL, CAPITAL, bytes(""));
        params[1] = abi.encode(key.currency0);
        params[2] = abi.encode(key.currency1);
        bytes memory hookReason = abi.encodeWithSelector(
            OasisDynamicFeeHook.PositionNotWhitelisted.selector, tokenId, recipient
        );
        vm.expectRevert(
            abi.encodeWithSelector(
                CustomRevert.WrappedError.selector,
                address(hook),
                IHooks.beforeAddLiquidity.selector,
                hookReason,
                abi.encodePacked(Hooks.HookCallFailed.selector)
            )
        );
        vm.prank(recipient);
        positions.modifyLiquidities(abi.encode(hex"001212", params), block.timestamp + 1);
        assertEq(positions.getPositionLiquidity(tokenId), LIQUIDITY);
        uint256 before0 = token0.balanceOf(recipient);
        uint256 before1 = token1.balanceOf(recipient);
        params[0] = abi.encode(tokenId, uint128(0), uint128(0), bytes(""));
        vm.prank(recipient);
        positions.modifyLiquidities(abi.encode(hex"031212", params), block.timestamp + 1);
        assertGt(token0.balanceOf(recipient), before0);
        assertGt(token1.balanceOf(recipient), before1);
        vm.expectRevert(abi.encodeWithSignature("Error(string)", "NOT_MINTED"));
        assertEq(positions.ownerOf(tokenId), address(0)); // query must revert after burn
    }

    function _expectMintFailure(bytes memory revertReason) internal {
        uint256 before0 = token0.balanceOf(OWNER);
        uint256 before1 = token1.balanceOf(OWNER);
        uint256 next = positions.nextTokenId();
        bytes[] memory params = new bytes[](3);
        params[0] =
            abi.encode(key, int24(-120), int24(120), LIQUIDITY, CAPITAL, CAPITAL, OWNER, bytes(""));
        params[1] = abi.encode(key.currency0);
        params[2] = abi.encode(key.currency1);
        vm.expectRevert(revertReason);
        vm.prank(OWNER);
        positions.modifyLiquidities(abi.encode(hex"021212", params), block.timestamp + 1);
        assertEq(positions.nextTokenId(), next);
        assertEq(token0.balanceOf(OWNER), before0);
        assertEq(token1.balanceOf(OWNER), before1);
    }
}
