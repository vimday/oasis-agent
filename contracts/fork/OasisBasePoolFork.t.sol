// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { IERC20 } from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import { IHooks } from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import { IPoolManager } from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import { StateLibrary } from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import { PoolSwapTest } from "@uniswap/v4-core/src/test/PoolSwapTest.sol";
import { PoolKey } from "@uniswap/v4-core/src/types/PoolKey.sol";
import { PoolIdLibrary } from "@uniswap/v4-core/src/types/PoolId.sol";
import { Currency } from "@uniswap/v4-core/src/types/Currency.sol";
import { SwapParams } from "@uniswap/v4-core/src/types/PoolOperation.sol";
import { IV4Quoter } from "@uniswap/v4-periphery/src/interfaces/IV4Quoter.sol";
import { IV4Router } from "@uniswap/v4-periphery/src/interfaces/IV4Router.sol";
import { OasisLifecycleBase } from "../test/shared/OasisLifecycleBase.sol";

interface IUniversalRouterExecution {
    function execute(bytes calldata commands, bytes[] calldata inputs, uint256 deadline)
        external
        payable;
}

/// @notice Local-only real-asset/pool fork. Artificial balances and self-generated flow are not PnL evidence.
contract OasisBasePoolForkTest is OasisLifecycleBase {
    using StateLibrary for IPoolManager;
    using PoolIdLibrary for PoolKey;
    IERC20 internal constant USDC = IERC20(0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913);
    address internal constant TRADER = address(0x5000);
    uint128 internal constant NATIVE_CAP = 0.03 ether;
    uint128 internal constant USDC_CAP = 100e6;
    uint256 internal constant REAL_LIQUIDITY = 1e14;
    address internal constant UNIVERSAL_ROUTER = 0x6fF5693b99212Da76ad316178A184AB56D299b43;
    IV4Quoter internal constant QUOTER = IV4Quoter(0x0d5e0F971ED27FBfF6c2837bf31316121532048D);

    function _realPool() internal pure returns (PoolKey memory) {
        return PoolKey({
            currency0: Currency.wrap(address(0)),
            currency1: Currency.wrap(address(USDC)),
            fee: 500,
            tickSpacing: 10,
            hooks: IHooks(address(0))
        });
    }

    function _prepareRealAssets() internal {
        assertEq(block.chainid, 8453);
        assertEq(
            address(USDC).codehash,
            bytes32(0xa6705a10bb756b5dea144591118be77d7af0c3eee3bf2dfe2583dcb0364fefab)
        );
        vm.deal(OWNER, NATIVE_CAP);
        vm.deal(TRADER, 0.1 ether);
        deal(address(USDC), OWNER, USDC_CAP);
        deal(address(USDC), TRADER, USDC_CAP);
        vm.startPrank(OWNER);
        assertTrue(USDC.approve(address(permit2), USDC_CAP));
        permit2.approve(address(USDC), address(positions), USDC_CAP, uint48(block.timestamp + 300));
        vm.stopPrank();
        vm.prank(TRADER);
        assertTrue(USDC.approve(address(swapper), USDC_CAP));
    }

    function _totals() internal view returns (uint256 nativeTotal, uint256 usdcTotal) {
        nativeTotal = OWNER.balance + TRADER.balance + address(manager).balance
            + address(positions).balance + address(swapper).balance;
        usdcTotal = USDC.balanceOf(OWNER) + USDC.balanceOf(TRADER)
            + USDC.balanceOf(address(manager)) + USDC.balanceOf(address(positions))
            + USDC.balanceOf(address(swapper));
    }

    function _assertTotals(uint256 nativeBefore, uint256 usdcBefore) internal view {
        (uint256 nativeAfter, uint256 usdcAfter) = _totals();
        assertEq(nativeAfter, nativeBefore, "native conservation including protocol cash");
        assertEq(usdcAfter, usdcBefore, "USDC conservation including protocol cash");
    }

    function _mintReal() internal returns (uint256 id) {
        PoolKey memory realKey = _realPool();
        (int24 lower, int24 upper) = _realRange(realKey);
        id = positions.nextTokenId();
        bytes[] memory params = new bytes[](4);
        params[0] = abi.encode(
            realKey, lower, upper, REAL_LIQUIDITY, NATIVE_CAP, USDC_CAP, OWNER, bytes("")
        );
        params[1] = abi.encode(realKey.currency0);
        params[2] = abi.encode(realKey.currency1);
        params[3] = abi.encode(realKey.currency0, OWNER);
        vm.prank(OWNER);
        positions.modifyLiquidities{ value: NATIVE_CAP }(
            abi.encode(hex"02121214", params), block.timestamp + 300
        );
    }

    /// @dev Test range follows the pinned pool so both asset legs are exercised.
    /// This is fixture construction, not a range recommendation for live capital.
    function _realRange(PoolKey memory realKey) internal view returns (int24 lower, int24 upper) {
        (, int24 tick,,) = manager.getSlot0(realKey.toId());
        int24 center = (tick / realKey.tickSpacing) * realKey.tickSpacing;
        lower = center - 100;
        upper = center + 100;
        assertLt(lower, tick);
        assertGt(upper, tick);
    }

    function _removeReal(uint256 id, uint256 amount) internal {
        PoolKey memory realKey = _realPool();
        bytes[] memory params = new bytes[](3);
        params[0] = abi.encode(id, amount, uint128(0), uint128(0), bytes(""));
        params[1] = abi.encode(realKey.currency0);
        params[2] = abi.encode(realKey.currency1);
        vm.prank(OWNER);
        positions.modifyLiquidities(abi.encode(hex"011212", params), block.timestamp + 300);
    }

    function _swapReal(bool zeroForOne, int256 amount) internal {
        (uint256 nativeBefore, uint256 usdcBefore) = _totals();
        uint256 traderNative = TRADER.balance;
        uint256 traderUsdc = USDC.balanceOf(TRADER);
        vm.prank(TRADER);
        swapper.swap{ value: zeroForOne ? 0.002 ether : 0 }(
            _realPool(),
            SwapParams({
                zeroForOne: zeroForOne,
                amountSpecified: amount,
                sqrtPriceLimitX96: zeroForOne
                    ? uint160(4295128740)
                    : uint160(1461446703485210103287273052203988822378723970341)
            }),
            PoolSwapTest.TestSettings({ takeClaims: false, settleUsingBurn: false }),
            bytes("")
        );
        if (zeroForOne) {
            assertLt(TRADER.balance, traderNative);
            assertGt(USDC.balanceOf(TRADER), traderUsdc);
            if (amount < 0) assertEq(traderNative - TRADER.balance, uint256(-amount));
            else assertEq(USDC.balanceOf(TRADER) - traderUsdc, uint256(amount));
        } else {
            assertGt(TRADER.balance, traderNative);
            assertLt(USDC.balanceOf(TRADER), traderUsdc);
            if (amount < 0) assertEq(traderUsdc - USDC.balanceOf(TRADER), uint256(-amount));
            else assertEq(TRADER.balance - traderNative, uint256(amount));
        }
        _assertTotals(nativeBefore, usdcBefore);
    }

    function test_realEthUsdcMintFourSwapsCollectRemoveRevoke() public {
        _prepareRealAssets();
        (uint256 nativeBefore, uint256 usdcBefore) = _totals();
        uint256 id = _mintReal();
        assertEq(positions.ownerOf(id), OWNER);
        assertEq(positions.getPositionLiquidity(id), REAL_LIQUIDITY);
        assertLt(OWNER.balance, NATIVE_CAP);
        assertLt(USDC.balanceOf(OWNER), USDC_CAP);
        _assertTotals(nativeBefore, usdcBefore);
        _swapReal(true, -int256(0.001 ether));
        _swapReal(false, -int256(2e6));
        _swapReal(true, int256(1e6));
        _swapReal(false, int256(0.0004 ether));
        uint256 ownerNative = OWNER.balance;
        uint256 ownerUsdc = USDC.balanceOf(OWNER);
        _removeReal(id, 0);
        assertGe(OWNER.balance, ownerNative);
        assertGe(USDC.balanceOf(OWNER), ownerUsdc);
        assertTrue(OWNER.balance > ownerNative || USDC.balanceOf(OWNER) > ownerUsdc);
        _assertTotals(nativeBefore, usdcBefore);
        _removeReal(id, REAL_LIQUIDITY);
        assertEq(positions.getPositionLiquidity(id), 0);
        _assertTotals(nativeBefore, usdcBefore);
        vm.startPrank(OWNER);
        permit2.approve(address(USDC), address(positions), 0, 0);
        assertTrue(USDC.approve(address(permit2), 0));
        vm.stopPrank();
        vm.prank(TRADER);
        assertTrue(USDC.approve(address(swapper), 0));
        (uint160 allowance,,) = permit2.allowance(OWNER, address(USDC), address(positions));
        assertEq(allowance, 0);
        assertEq(USDC.allowance(OWNER, address(permit2)), 0);
        assertEq(USDC.allowance(TRADER, address(swapper)), 0);
    }

    function test_realPoolMissingAllowanceRollsBackNativeAndNft() public {
        _prepareRealAssets();
        vm.prank(OWNER);
        assertTrue(USDC.approve(address(permit2), 0));
        (uint256 nativeBefore, uint256 usdcBefore) = _totals();
        uint256 nextId = positions.nextTokenId();
        uint256 ownerNative = OWNER.balance;
        uint256 ownerUsdc = USDC.balanceOf(OWNER);
        // Expect only the state-changing call to revert, after the helper's reads.
        PoolKey memory realKey = _realPool();
        (int24 lower, int24 upper) = _realRange(realKey);
        bytes[] memory params = new bytes[](4);
        params[0] = abi.encode(
            realKey, lower, upper, REAL_LIQUIDITY, NATIVE_CAP, USDC_CAP, OWNER, bytes("")
        );
        params[1] = abi.encode(realKey.currency0);
        params[2] = abi.encode(realKey.currency1);
        params[3] = abi.encode(realKey.currency0, OWNER);
        vm.prank(OWNER);
        vm.expectRevert();
        positions.modifyLiquidities{ value: NATIVE_CAP }(
            abi.encode(hex"02121214", params), block.timestamp + 300
        );
        assertEq(positions.nextTokenId(), nextId);
        assertEq(OWNER.balance, ownerNative);
        assertEq(USDC.balanceOf(OWNER), ownerUsdc);
        _assertTotals(nativeBefore, usdcBefore);
    }

    function test_standardRouterSettlesBothDirectionsAtThreeSizes() public {
        _prepareRealAssets();
        assertEq(
            UNIVERSAL_ROUTER.codehash,
            bytes32(0x27713951fb0660a1422b710122022d90723d883dc7b72949be79cb2957d234e0)
        );
        assertEq(
            address(QUOTER).codehash,
            bytes32(0x9a5c0cdd56325bef0e48cdab071a4b6a7f877e1271c2e08510998d724a038bb3)
        );
        vm.deal(TRADER, 1 ether);
        deal(address(USDC), TRADER, 1000e6);
        vm.startPrank(TRADER);
        assertTrue(USDC.approve(address(permit2), 1000e6));
        permit2.approve(address(USDC), UNIVERSAL_ROUTER, 1000e6, uint48(block.timestamp + 300));
        vm.stopPrank();
        uint128[3] memory nativeSizes = [uint128(0.001 ether), 0.01 ether, 0.1 ether];
        uint128[3] memory usdcSizes = [uint128(2e6), 20e6, 200e6];
        for (uint256 i; i < 3; ++i) {
            _standardRouterSwap(true, nativeSizes[i]);
            _standardRouterSwap(false, usdcSizes[i]);
        }
        vm.startPrank(TRADER);
        permit2.approve(address(USDC), UNIVERSAL_ROUTER, 0, 0);
        assertTrue(USDC.approve(address(permit2), 0));
        vm.stopPrank();
        (uint160 allowance,,) = permit2.allowance(TRADER, address(USDC), UNIVERSAL_ROUTER);
        assertEq(allowance, 0);
        assertEq(USDC.allowance(TRADER, address(permit2)), 0);
    }

    function _standardRouterSwap(bool zeroForOne, uint128 amountIn) internal {
        PoolKey memory realKey = _realPool();
        (uint256 quoted,) = QUOTER.quoteExactInputSingle(
            IV4Quoter.QuoteExactSingleParams(realKey, zeroForOne, amountIn, bytes(""))
        );
        require(quoted > 0 && quoted <= type(uint128).max, "INVALID_QUOTE");
        bytes[] memory params = new bytes[](3);
        params[0] = abi.encode(
            IV4Router.ExactInputSingleParams(
                realKey, zeroForOne, amountIn, uint128(quoted), bytes("")
            )
        );
        params[1] = abi.encode(zeroForOne ? realKey.currency0 : realKey.currency1, amountIn);
        params[2] = abi.encode(zeroForOne ? realKey.currency1 : realKey.currency0, quoted);
        bytes[] memory inputs = new bytes[](1);
        // Canonical periphery Actions: exact-in-single, settle-all, take-all.
        inputs[0] = abi.encode(hex"060c0f", params);
        (uint256 nativeBefore, uint256 usdcBefore) = _totals();
        uint256 traderNative = TRADER.balance;
        uint256 traderUsdc = USDC.balanceOf(TRADER);
        uint256 routerNative = UNIVERSAL_ROUTER.balance;
        uint256 routerUsdc = USDC.balanceOf(UNIVERSAL_ROUTER);
        vm.prank(TRADER);
        // Universal Router command 0x10 = V4_SWAP; no allow-revert flag.
        IUniversalRouterExecution(UNIVERSAL_ROUTER).execute{ value: zeroForOne ? amountIn : 0 }(
            hex"10", inputs, block.timestamp + 300
        );
        if (zeroForOne) {
            assertEq(traderNative - TRADER.balance, amountIn);
            assertEq(USDC.balanceOf(TRADER) - traderUsdc, quoted);
        } else {
            assertEq(traderUsdc - USDC.balanceOf(TRADER), amountIn);
            assertEq(TRADER.balance - traderNative, quoted);
        }
        assertEq(UNIVERSAL_ROUTER.balance, routerNative);
        assertEq(USDC.balanceOf(UNIVERSAL_ROUTER), routerUsdc);
        _assertTotals(nativeBefore, usdcBefore);
    }
}
