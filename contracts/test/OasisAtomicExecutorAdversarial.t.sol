// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { Test } from "forge-std/Test.sol";
import { Ownable } from "@openzeppelin/contracts/access/Ownable.sol";
import { IERC20 } from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import { OasisAtomicExecutor } from "../src/OasisAtomicExecutor.sol";
import {
    IOasisV3CallbackReceiver,
    MockAtomicERC20,
    MockV2Pair,
    MockV3Pool
} from "./OasisAtomicExecutor.t.sol";

contract SpoofedCallbackCaller {
    function spoof(address target, int256 amount0Delta, int256 amount1Delta, bytes calldata data)
        external
    {
        IOasisV3CallbackReceiver(target).uniswapV3SwapCallback(amount0Delta, amount1Delta, data);
    }
}

/// @notice Adversarial V3 pool that attempts callback spoofing, delta manipulation, and reentrancy.
contract MaliciousV3Pool {
    enum Mode {
        DOUBLE_CALLBACK,
        WRONG_CALLER_CALLBACK,
        EXCESSIVE_PAYMENT,
        BOTH_POSITIVE_DELTAS,
        BOTH_NEGATIVE_DELTAS,
        WRONG_SIDE_POSITIVE_DELTA,
        REENTER_EXECUTE_CYCLE,
        REENTER_SWEEP_PROFIT,
        SKIP_CALLBACK_ZERO_OUT
    }

    address public immutable token0;
    address public immutable token1;
    uint160 public sqrtPriceX96;
    Mode public mode;
    SpoofedCallbackCaller public accomplice;
    bytes public reenterPayload;

    constructor(address token0_, address token1_, uint160 sqrtPriceX96_) {
        token0 = token0_;
        token1 = token1_;
        sqrtPriceX96 = sqrtPriceX96_;
        accomplice = new SpoofedCallbackCaller();
    }

    function setMode(Mode mode_) external {
        mode = mode_;
    }

    function setReenterPayload(bytes calldata payload) external {
        reenterPayload = payload;
    }

    function slot0() external view returns (uint160, int24, uint16, uint16, uint16, uint8, bool) {
        return (sqrtPriceX96, 0, 0, 1, 1, 0, true);
    }

    function swap(
        address recipient,
        bool zeroForOne,
        int256 amountSpecified,
        uint160,
        bytes calldata data
    ) external returns (int256 amount0, int256 amount1) {
        require(amountSpecified > 0, "EXACT_IN");
        // forge-lint: disable-next-line(unsafe-typecast)
        uint256 amountIn = uint256(amountSpecified);
        address tokenOut = zeroForOne ? token1 : token0;

        if (mode == Mode.SKIP_CALLBACK_ZERO_OUT) {
            return (0, 0);
        }

        // Send output tokens so only the callback / reentrancy defense is under test.
        require(IERC20(tokenOut).transfer(recipient, amountIn * 2), "TRANSFER_OUT");

        if (mode == Mode.DOUBLE_CALLBACK) {
            IOasisV3CallbackReceiver(msg.sender)
                .uniswapV3SwapCallback(
                    // forge-lint: disable-next-line(unsafe-typecast)
                    int256(amountIn),
                    0,
                    data
                );
            IOasisV3CallbackReceiver(msg.sender)
                .uniswapV3SwapCallback(
                    // forge-lint: disable-next-line(unsafe-typecast)
                    int256(amountIn),
                    0,
                    data
                );
        } else if (mode == Mode.WRONG_CALLER_CALLBACK) {
            accomplice.spoof(
                msg.sender,
                // forge-lint: disable-next-line(unsafe-typecast)
                int256(amountIn),
                0,
                data
            );
        } else if (mode == Mode.EXCESSIVE_PAYMENT) {
            IOasisV3CallbackReceiver(msg.sender)
                .uniswapV3SwapCallback(
                    // forge-lint: disable-next-line(unsafe-typecast)
                    int256(amountIn + 1),
                    0,
                    data
                );
        } else if (mode == Mode.BOTH_POSITIVE_DELTAS) {
            IOasisV3CallbackReceiver(msg.sender)
                .uniswapV3SwapCallback(
                    // forge-lint: disable-next-line(unsafe-typecast)
                    int256(amountIn),
                    // forge-lint: disable-next-line(unsafe-typecast)
                    int256(amountIn),
                    data
                );
        } else if (mode == Mode.BOTH_NEGATIVE_DELTAS) {
            IOasisV3CallbackReceiver(msg.sender)
                .uniswapV3SwapCallback(
                    // forge-lint: disable-next-line(unsafe-typecast)
                    -int256(amountIn),
                    // forge-lint: disable-next-line(unsafe-typecast)
                    -int256(amountIn),
                    data
                );
        } else if (mode == Mode.WRONG_SIDE_POSITIVE_DELTA) {
            IOasisV3CallbackReceiver(msg.sender)
                .uniswapV3SwapCallback(
                    // forge-lint: disable-next-line(unsafe-typecast)
                    -int256(amountIn),
                    // forge-lint: disable-next-line(unsafe-typecast)
                    int256(amountIn),
                    data
                );
        } else if (mode == Mode.REENTER_EXECUTE_CYCLE) {
            (bool ok, bytes memory err) = msg.sender.call(reenterPayload);
            if (!ok) {
                assembly ("memory-safe") {
                    revert(add(err, 32), mload(err))
                }
            }
        } else if (mode == Mode.REENTER_SWEEP_PROFIT) {
            OasisAtomicExecutor(msg.sender).sweepProfit(IERC20(token0), 1);
        }
        return (0, 0);
    }
}

contract OasisAtomicExecutorAdversarialTest is Test {
    address private constant OWNER = address(0x1111);
    address private constant GUARDIAN = address(0x2222);
    address private constant TREASURY = address(0x3333);
    address private constant REVENUE_SINK = address(0x4444);
    address private constant OPERATOR = address(0x5555);
    address private constant ATTACKER = address(0x9999);
    uint160 private constant INITIAL_SQRT_PRICE = uint160(1 << 96);

    OasisAtomicExecutor private executor;
    MockAtomicERC20 private tokenA;
    MockAtomicERC20 private tokenB;
    MockV3Pool private v3PoolAB;
    MockV2Pair private v2PoolAB;
    MaliciousV3Pool private maliciousV3;

    function setUp() public {
        vm.prank(OWNER);
        executor = new OasisAtomicExecutor(OWNER, GUARDIAN, TREASURY, REVENUE_SINK);

        tokenA = new MockAtomicERC20("Token A", "TKA");
        tokenB = new MockAtomicERC20("Token B", "TKB");
        v3PoolAB = new MockV3Pool(address(tokenA), address(tokenB), INITIAL_SQRT_PRICE, 2, 1, 1, 2);
        v2PoolAB = new MockV2Pair(address(tokenA), address(tokenB), 30);
        maliciousV3 = new MaliciousV3Pool(address(tokenA), address(tokenB), INITIAL_SQRT_PRICE);

        tokenB.mint(address(v3PoolAB), 1_000 ether);
        tokenA.mint(address(v3PoolAB), 1_000 ether);
        tokenB.mint(address(maliciousV3), 1_000 ether);
        tokenA.mint(address(maliciousV3), 1_000 ether);
        tokenA.mint(address(v2PoolAB), 1_000 ether);
        tokenB.mint(address(v2PoolAB), 1_000 ether);
        v2PoolAB.syncReserves();

        tokenA.mint(address(executor), 100 ether);

        vm.startPrank(OWNER);
        executor.setOperator(OPERATOR, true);
        executor.setApprovedToken(address(tokenA), true);
        executor.setApprovedToken(address(tokenB), true);
        executor.setApprovedPool(address(v3PoolAB), OasisAtomicExecutor.PoolKind.UNISWAP_V3);
        executor.setApprovedPool(address(v2PoolAB), OasisAtomicExecutor.PoolKind.UNISWAP_V2);
        executor.setApprovedPool(address(maliciousV3), OasisAtomicExecutor.PoolKind.UNISWAP_V3);
        vm.stopPrank();
    }

    function test_spoofedCallbackOutsideActiveSwapReverts() public {
        vm.prank(ATTACKER);
        vm.expectRevert(
            abi.encodeWithSelector(OasisAtomicExecutor.InvalidCallbackCaller.selector, ATTACKER)
        );
        executor.uniswapV3SwapCallback(1 ether, -1 ether, "");

        vm.prank(address(v3PoolAB));
        vm.expectRevert(
            abi.encodeWithSelector(
                OasisAtomicExecutor.InvalidCallbackCaller.selector, address(v3PoolAB)
            )
        );
        executor.uniswapV3SwapCallback(1 ether, -1 ether, "");
    }

    function test_callbackFromWrongPoolMidSwapReverts() public {
        maliciousV3.setMode(MaliciousV3Pool.Mode.WRONG_CALLER_CALLBACK);
        OasisAtomicExecutor.CycleParams memory params = _maliciousParams(1, 0);

        address accomplice = address(maliciousV3.accomplice());
        vm.prank(OPERATOR);
        vm.expectRevert(
            abi.encodeWithSelector(OasisAtomicExecutor.InvalidCallbackCaller.selector, accomplice)
        );
        executor.executeCycle(params);
    }

    function test_callbackDoubleInvocationReverts() public {
        maliciousV3.setMode(MaliciousV3Pool.Mode.DOUBLE_CALLBACK);
        OasisAtomicExecutor.CycleParams memory params = _maliciousParams(1, 0);

        vm.prank(OPERATOR);
        vm.expectRevert(
            abi.encodeWithSelector(
                OasisAtomicExecutor.InvalidCallbackCaller.selector, address(maliciousV3)
            )
        );
        executor.executeCycle(params);
    }

    function test_callbackExcessivePaymentAndInvalidDeltasRevert() public {
        maliciousV3.setMode(MaliciousV3Pool.Mode.EXCESSIVE_PAYMENT);
        vm.prank(OPERATOR);
        vm.expectRevert(
            abi.encodeWithSelector(
                OasisAtomicExecutor.ExcessiveCallbackPayment.selector, 10 ether + 1, 10 ether
            )
        );
        executor.executeCycle(_maliciousParams(1, 0));

        maliciousV3.setMode(MaliciousV3Pool.Mode.BOTH_POSITIVE_DELTAS);
        vm.prank(OPERATOR);
        vm.expectRevert(
            abi.encodeWithSelector(
                OasisAtomicExecutor.InvalidSwapDelta.selector, int256(10 ether), int256(10 ether)
            )
        );
        executor.executeCycle(_maliciousParams(1, 0));

        maliciousV3.setMode(MaliciousV3Pool.Mode.BOTH_NEGATIVE_DELTAS);
        vm.prank(OPERATOR);
        vm.expectRevert(
            abi.encodeWithSelector(
                OasisAtomicExecutor.InvalidSwapDelta.selector, -int256(10 ether), -int256(10 ether)
            )
        );
        executor.executeCycle(_maliciousParams(1, 0));

        maliciousV3.setMode(MaliciousV3Pool.Mode.WRONG_SIDE_POSITIVE_DELTA);
        vm.prank(OPERATOR);
        vm.expectRevert(
            abi.encodeWithSelector(
                OasisAtomicExecutor.InvalidSwapDelta.selector, -int256(10 ether), int256(10 ether)
            )
        );
        executor.executeCycle(_maliciousParams(1, 0));

        maliciousV3.setMode(MaliciousV3Pool.Mode.SKIP_CALLBACK_ZERO_OUT);
        vm.prank(OPERATOR);
        vm.expectRevert(
            abi.encodeWithSelector(
                OasisAtomicExecutor.ZeroOutputReceived.selector,
                address(maliciousV3),
                address(tokenB)
            )
        );
        executor.executeCycle(_maliciousParams(1, 0));
    }

    function test_reentrancyFromPoolAndTokenReverts() public {
        // 1. Pool attempts to reenter executeCycle with nonce + 1.
        vm.prank(OWNER);
        executor.setOperator(address(maliciousV3), true);
        maliciousV3.setMode(MaliciousV3Pool.Mode.REENTER_EXECUTE_CYCLE);
        maliciousV3.setReenterPayload(
            abi.encodeCall(OasisAtomicExecutor.executeCycle, (_validParams(2, 0)))
        );

        vm.prank(OPERATOR);
        vm.expectRevert(OasisAtomicExecutor.ReentrantCall.selector);
        executor.executeCycle(_maliciousParams(1, 0));

        // 2. Pool attempts to reenter sweepProfit.
        maliciousV3.setMode(MaliciousV3Pool.Mode.REENTER_SWEEP_PROFIT);
        vm.prank(OPERATOR);
        vm.expectRevert(OasisAtomicExecutor.ReentrantCall.selector);
        executor.executeCycle(_maliciousParams(1, 0));

        // 3. Token transfer hook attempts to reenter executeCycle -> blocked cleanly.
        tokenB.armReentrancy(
            address(executor),
            abi.encodeCall(OasisAtomicExecutor.executeCycle, (_validParams(2, 0)))
        );
        vm.prank(OWNER);
        executor.setOperator(address(tokenB), true);
        vm.prank(OPERATOR);
        executor.executeCycle(_validParams(1, 1 ether));
        assertTrue(tokenB.lastReenterReverted());
    }

    function test_invalidInputsUnauthorizedAndStaleStateRevert() public {
        // Unauthorized operator
        vm.prank(ATTACKER);
        vm.expectRevert(
            abi.encodeWithSelector(OasisAtomicExecutor.UnauthorizedOperator.selector, ATTACKER)
        );
        executor.executeCycle(_validParams(1, 0));

        // Expired deadline
        OasisAtomicExecutor.CycleParams memory expired = _validParams(1, 0);
        // forge-lint: disable-next-line(unsafe-typecast)
        expired.deadline = uint64(block.timestamp - 1);
        vm.prank(OPERATOR);
        vm.expectRevert(
            abi.encodeWithSelector(OasisAtomicExecutor.DeadlinePassed.selector, expired.deadline)
        );
        executor.executeCycle(expired);

        // Stale nonce after execution
        vm.prank(OPERATOR);
        executor.executeCycle(_validParams(5, 0));
        vm.prank(OPERATOR);
        vm.expectRevert(abi.encodeWithSelector(OasisAtomicExecutor.StaleNonce.selector, 5, 5));
        executor.executeCycle(_validParams(5, 0));

        // Unapproved pool
        OasisAtomicExecutor.CycleParams memory badPool = _validParams(6, 0);
        badPool.legs[0].pool = address(0xDEAD);
        vm.prank(OPERATOR);
        vm.expectRevert(
            abi.encodeWithSelector(OasisAtomicExecutor.UnapprovedPool.selector, address(0xDEAD))
        );
        executor.executeCycle(badPool);

        // Unapproved token
        OasisAtomicExecutor.CycleParams memory badToken = _validParams(6, 0);
        badToken.legs[0].tokenOut = address(0xBEEF);
        badToken.legs[1].tokenIn = address(0xBEEF);
        vm.prank(OPERATOR);
        vm.expectRevert(
            abi.encodeWithSelector(OasisAtomicExecutor.UnapprovedToken.selector, address(0xBEEF))
        );
        executor.executeCycle(badToken);

        // Invalid leg count (1 leg)
        OasisAtomicExecutor.CycleParams memory oneLeg = _validParams(6, 0);
        OasisAtomicExecutor.SwapLeg[] memory shortLegs = new OasisAtomicExecutor.SwapLeg[](1);
        shortLegs[0] = oneLeg.legs[0];
        oneLeg.legs = shortLegs;
        vm.prank(OPERATOR);
        vm.expectRevert(abi.encodeWithSelector(OasisAtomicExecutor.InvalidLegCount.selector, 1));
        executor.executeCycle(oneLeg);

        // Discontiguous leg path
        OasisAtomicExecutor.CycleParams memory brokenPath = _validParams(6, 0);
        brokenPath.legs[1].tokenIn = address(tokenA);
        vm.prank(OPERATOR);
        vm.expectRevert(OasisAtomicExecutor.InvalidCyclePath.selector);
        executor.executeCycle(brokenPath);

        // Invalid V2 fee bps (>= 10_000)
        OasisAtomicExecutor.CycleParams memory badFee = _validParams(6, 0);
        badFee.legs[1].v2FeeBps = 10_000;
        vm.prank(OPERATOR);
        vm.expectRevert(
            abi.encodeWithSelector(OasisAtomicExecutor.InvalidV2FeeBps.selector, uint24(10_000))
        );
        executor.executeCycle(badFee);

        // Invalid guard pool (V2 pair is not UNISWAP_V3)
        OasisAtomicExecutor.CycleParams memory badGuard = _validParams(6, 0);
        badGuard.guard.pool = address(v2PoolAB);
        vm.prank(OPERATOR);
        vm.expectRevert(
            abi.encodeWithSelector(OasisAtomicExecutor.InvalidGuardPool.selector, address(v2PoolAB))
        );
        executor.executeCycle(badGuard);
    }

    function test_feeOnTransferUnderDeliveryRevertsOnExpectedProfit() public {
        // 15% buy tax on tokenB received from V3 leg: 20 B quoted -> 17 B actually delivered.
        // Executor must pass only the real 17 B delta to V2 (~6.67 A profit) and reject 8 ether minNetProfit.
        tokenB.setSenderFeeBps(address(v3PoolAB), 1_500);
        uint256 balBefore = tokenA.balanceOf(address(executor));

        OasisAtomicExecutor.CycleParams memory highMin = _validParams(1, 8 ether);
        vm.prank(OPERATOR);
        vm.expectRevert();
        executor.executeCycle(highMin);
        assertEq(tokenA.balanceOf(address(executor)), balBefore);

        // Requiring 6 ether net profit succeeds and reports exact post-fee balance delta.
        OasisAtomicExecutor.CycleParams memory boundedMin = _validParams(1, 6 ether);
        vm.prank(OPERATOR);
        (bool executed, uint256 netProfit) = executor.executeCycle(boundedMin);
        assertTrue(executed);
        assertEq(tokenA.balanceOf(address(executor)) - balBefore, netProfit);
        assertLt(netProfit, 8 ether);

        // Also test fee-on-transfer directly on profitToken output from V2 leg (v2PoolAB -> executor).
        tokenB.setSenderFeeBps(address(v3PoolAB), 0);
        tokenA.setSenderFeeBps(address(v2PoolAB), 1_500);
        uint256 balAfterFirst = tokenA.balanceOf(address(executor));

        vm.prank(OPERATOR);
        vm.expectRevert();
        executor.executeCycle(_validParams(2, 8 ether));
        assertEq(tokenA.balanceOf(address(executor)), balAfterFirst);

        vm.prank(OPERATOR);
        (bool executed2, uint256 netProfit2) = executor.executeCycle(_validParams(2, 6 ether));
        assertTrue(executed2);
        assertEq(tokenA.balanceOf(address(executor)) - balAfterFirst, netProfit2);
        assertLt(netProfit2, 8 ether);
    }

    function test_compromisedOperatorAndGuardianLeastPrivilege() public {
        // Compromised operator cannot invoke any owner-plane administration or rescue.
        vm.startPrank(OPERATOR);
        vm.expectRevert(
            abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, OPERATOR)
        );
        executor.emergencyRescue(tokenA, 1 ether);

        vm.expectRevert(
            abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, OPERATOR)
        );
        executor.setApprovedPool(address(0x7777), OasisAtomicExecutor.PoolKind.UNISWAP_V2);

        vm.expectRevert(
            abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, OPERATOR)
        );
        executor.setApprovedToken(address(0x7777), true);

        vm.expectRevert(
            abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, OPERATOR)
        );
        executor.setPaused(true);

        vm.expectRevert(
            abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, OPERATOR)
        );
        executor.setOperator(ATTACKER, true);

        vm.expectRevert(
            abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, OPERATOR)
        );
        executor.transferOwnership(ATTACKER);
        vm.stopPrank();

        // Guardian can pause once, cannot unpause, cannot sweep/rescue, cannot execute.
        vm.startPrank(GUARDIAN);
        executor.emergencyPause();
        vm.expectRevert(OasisAtomicExecutor.NoStateChange.selector);
        executor.emergencyPause();

        vm.expectRevert(
            abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, GUARDIAN)
        );
        executor.setPaused(false);

        vm.expectRevert(
            abi.encodeWithSelector(OasisAtomicExecutor.UnauthorizedOperator.selector, GUARDIAN)
        );
        executor.sweepProfit(tokenA, 1 ether);

        vm.expectRevert(
            abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, GUARDIAN)
        );
        executor.emergencyRescue(tokenA, 1 ether);
        vm.stopPrank();
    }

    function _validParams(uint64 nonce, uint256 minNetProfit)
        private
        view
        returns (OasisAtomicExecutor.CycleParams memory)
    {
        OasisAtomicExecutor.SwapLeg[] memory legs = new OasisAtomicExecutor.SwapLeg[](2);
        legs[0] = OasisAtomicExecutor.SwapLeg({
            pool: address(v3PoolAB),
            tokenIn: address(tokenA),
            tokenOut: address(tokenB),
            zeroForOne: true,
            sqrtPriceLimitX96: INITIAL_SQRT_PRICE - 1,
            v2FeeBps: 0
        });
        legs[1] = OasisAtomicExecutor.SwapLeg({
            pool: address(v2PoolAB),
            tokenIn: address(tokenB),
            tokenOut: address(tokenA),
            zeroForOne: false,
            sqrtPriceLimitX96: 0,
            v2FeeBps: 30
        });

        // forge-lint: disable-next-line(unsafe-typecast)
        uint64 deadline = uint64(block.timestamp + 60);
        return OasisAtomicExecutor.CycleParams({
            nonce: nonce,
            deadline: deadline,
            profitToken: address(tokenA),
            initialAmount: 10 ether,
            minNetProfit: minNetProfit,
            guard: OasisAtomicExecutor.PrecheckGuard({
                pool: address(0), minSqrtPriceX96: 0, maxSqrtPriceX96: 0
            }),
            legs: legs
        });
    }

    function _maliciousParams(uint64 nonce, uint256 minNetProfit)
        private
        view
        returns (OasisAtomicExecutor.CycleParams memory)
    {
        OasisAtomicExecutor.CycleParams memory p = _validParams(nonce, minNetProfit);
        p.legs[0].pool = address(maliciousV3);
        return p;
    }
}
