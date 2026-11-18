// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { Test } from "forge-std/Test.sol";
import { StdInvariant } from "forge-std/StdInvariant.sol";

import { OasisAtomicExecutor } from "../src/OasisAtomicExecutor.sol";
import {
    MockAtomicERC20,
    MockSlipstreamPool,
    MockV2Pair,
    MockV3Pool
} from "./OasisAtomicExecutor.t.sol";

contract AtomicExecutorHandler is Test {
    uint160 private constant INITIAL_SQRT_PRICE = uint160(1 << 96);

    OasisAtomicExecutor public immutable executor;
    MockAtomicERC20 public immutable tokenA;
    MockAtomicERC20 public immutable tokenB;
    MockAtomicERC20 public immutable tokenC;
    MockV3Pool public immutable v3PoolAB;
    MockSlipstreamPool public immutable slipPoolBC;
    MockV2Pair public immutable v2PoolAB;
    MockV2Pair public immutable v2PoolCA;

    address public immutable ownerAddr;
    address public immutable guardianAddr;
    address public immutable operatorAddr;

    uint256 public immutable initialCapital;
    uint256 public cumulativeRecordedProfit;
    uint64 public lastConsumedNonce;

    constructor(
        OasisAtomicExecutor executor_,
        MockAtomicERC20 tokenA_,
        MockAtomicERC20 tokenB_,
        MockAtomicERC20 tokenC_,
        MockV3Pool v3PoolAB_,
        MockSlipstreamPool slipPoolBC_,
        MockV2Pair v2PoolAB_,
        MockV2Pair v2PoolCA_,
        address ownerAddr_,
        address guardianAddr_,
        address operatorAddr_,
        uint256 initialCapital_
    ) {
        executor = executor_;
        tokenA = tokenA_;
        tokenB = tokenB_;
        tokenC = tokenC_;
        v3PoolAB = v3PoolAB_;
        slipPoolBC = slipPoolBC_;
        v2PoolAB = v2PoolAB_;
        v2PoolCA = v2PoolCA_;
        ownerAddr = ownerAddr_;
        guardianAddr = guardianAddr_;
        operatorAddr = operatorAddr_;
        initialCapital = initialCapital_;
    }

    function executeTwoHop(uint256 amountSeed) external {
        if (executor.paused()) return;
        uint256 bal = tokenA.balanceOf(address(executor));
        if (bal < 0.01 ether) return;
        uint256 maxTrade = bal < 2 ether ? bal : 2 ether;
        uint256 amountIn = bound(amountSeed, 0.01 ether, maxTrade);
        uint64 nextNonce = lastConsumedNonce + 1;

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
        OasisAtomicExecutor.CycleParams memory params = OasisAtomicExecutor.CycleParams({
            nonce: nextNonce,
            deadline: deadline,
            profitToken: address(tokenA),
            initialAmount: amountIn,
            minNetProfit: 0,
            guard: OasisAtomicExecutor.PrecheckGuard({
                pool: address(v3PoolAB),
                minSqrtPriceX96: INITIAL_SQRT_PRICE - 10,
                maxSqrtPriceX96: INITIAL_SQRT_PRICE + 10
            }),
            legs: legs
        });

        vm.prank(operatorAddr);
        try executor.executeCycle(params) returns (bool executed, uint256 netProfit) {
            lastConsumedNonce = nextNonce;
            if (executed) {
                cumulativeRecordedProfit += netProfit;
            }
        } catch {
            // Bounded handler gracefully absorbs pool depletion without aborting invariant run.
        }
    }

    function executeThreeHop(uint256 amountSeed) external {
        if (executor.paused()) return;
        uint256 bal = tokenA.balanceOf(address(executor));
        if (bal < 0.01 ether) return;
        uint256 maxTrade = bal < 2 ether ? bal : 2 ether;
        uint256 amountIn = bound(amountSeed, 0.01 ether, maxTrade);
        uint64 nextNonce = lastConsumedNonce + 1;

        OasisAtomicExecutor.SwapLeg[] memory legs = new OasisAtomicExecutor.SwapLeg[](3);
        legs[0] = OasisAtomicExecutor.SwapLeg({
            pool: address(v3PoolAB),
            tokenIn: address(tokenA),
            tokenOut: address(tokenB),
            zeroForOne: true,
            sqrtPriceLimitX96: INITIAL_SQRT_PRICE - 1,
            v2FeeBps: 0
        });
        legs[1] = OasisAtomicExecutor.SwapLeg({
            pool: address(slipPoolBC),
            tokenIn: address(tokenB),
            tokenOut: address(tokenC),
            zeroForOne: true,
            sqrtPriceLimitX96: INITIAL_SQRT_PRICE - 1,
            v2FeeBps: 0
        });
        legs[2] = OasisAtomicExecutor.SwapLeg({
            pool: address(v2PoolCA),
            tokenIn: address(tokenC),
            tokenOut: address(tokenA),
            zeroForOne: true,
            sqrtPriceLimitX96: 0,
            v2FeeBps: 30
        });

        // forge-lint: disable-next-line(unsafe-typecast)
        uint64 deadline = uint64(block.timestamp + 60);
        OasisAtomicExecutor.CycleParams memory params = OasisAtomicExecutor.CycleParams({
            nonce: nextNonce,
            deadline: deadline,
            profitToken: address(tokenA),
            initialAmount: amountIn,
            minNetProfit: 0,
            guard: OasisAtomicExecutor.PrecheckGuard({
                pool: address(slipPoolBC),
                minSqrtPriceX96: INITIAL_SQRT_PRICE,
                maxSqrtPriceX96: INITIAL_SQRT_PRICE
            }),
            legs: legs
        });

        vm.prank(operatorAddr);
        try executor.executeCycle(params) returns (bool executed, uint256 netProfit) {
            lastConsumedNonce = nextNonce;
            if (executed) {
                cumulativeRecordedProfit += netProfit;
            }
        } catch {
            // Bounded handler gracefully absorbs pool depletion without aborting invariant run.
        }
    }

    function executeEarlyAbort() external {
        if (executor.paused()) return;
        uint64 nextNonce = lastConsumedNonce + 1;
        v3PoolAB.setSqrtPriceX96(INITIAL_SQRT_PRICE + 500);

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
        OasisAtomicExecutor.CycleParams memory params = OasisAtomicExecutor.CycleParams({
            nonce: nextNonce,
            deadline: deadline,
            profitToken: address(tokenA),
            initialAmount: 0.1 ether,
            minNetProfit: 0,
            guard: OasisAtomicExecutor.PrecheckGuard({
                pool: address(v3PoolAB),
                minSqrtPriceX96: INITIAL_SQRT_PRICE - 10,
                maxSqrtPriceX96: INITIAL_SQRT_PRICE + 10
            }),
            legs: legs
        });

        vm.prank(operatorAddr);
        (bool executed,) = executor.executeCycle(params);
        require(!executed, "EXPECTED_ABORT");
        lastConsumedNonce = nextNonce;
        v3PoolAB.setSqrtPriceX96(INITIAL_SQRT_PRICE);
    }

    function sweepToRevenueSink(uint256 amountSeed) external {
        if (executor.paused()) return;
        uint256 bal = tokenA.balanceOf(address(executor));
        if (bal == 0) return;
        uint256 amount = bound(amountSeed, 1, bal);
        vm.prank(operatorAddr);
        executor.sweepProfit(tokenA, amount);
    }

    function rescueToTreasury(uint256 amountSeed) external {
        uint256 bal = tokenA.balanceOf(address(executor));
        if (bal == 0) return;
        uint256 amount = bound(amountSeed, 1, bal);
        vm.prank(ownerAddr);
        executor.emergencyRescue(tokenA, amount);
    }

    function togglePause(bool shouldPause) external {
        bool currentlyPaused = executor.paused();
        if (shouldPause && !currentlyPaused) {
            vm.prank(guardianAddr);
            executor.emergencyPause();
        } else if (!shouldPause && currentlyPaused) {
            vm.prank(ownerAddr);
            executor.setPaused(false);
        }
    }
}

contract OasisAtomicExecutorInvariantTest is StdInvariant, Test {
    address private constant OWNER = address(0x1111);
    address private constant GUARDIAN = address(0x2222);
    address private constant TREASURY = address(0x3333);
    address private constant REVENUE_SINK = address(0x4444);
    address private constant OPERATOR = address(0x5555);
    uint160 private constant INITIAL_SQRT_PRICE = uint160(1 << 96);
    uint256 private constant INITIAL_CAPITAL = 1_000 ether;

    OasisAtomicExecutor private executor;
    MockAtomicERC20 private tokenA;
    AtomicExecutorHandler private handler;

    function setUp() public {
        vm.prank(OWNER);
        executor = new OasisAtomicExecutor(OWNER, GUARDIAN, TREASURY, REVENUE_SINK);

        tokenA = new MockAtomicERC20("Token A", "TKA");
        MockAtomicERC20 tokenB = new MockAtomicERC20("Token B", "TKB");
        MockAtomicERC20 tokenC = new MockAtomicERC20("Token C", "TKC");

        MockV3Pool v3PoolAB =
            new MockV3Pool(address(tokenA), address(tokenB), INITIAL_SQRT_PRICE, 2, 1, 1, 2);
        MockSlipstreamPool slipPoolBC = new MockSlipstreamPool(
            address(tokenB), address(tokenC), INITIAL_SQRT_PRICE, 1, 1, 1, 1
        );
        MockV2Pair v2PoolAB = new MockV2Pair(address(tokenA), address(tokenB), 30);
        MockV2Pair v2PoolCA = new MockV2Pair(address(tokenC), address(tokenA), 30);

        tokenB.mint(address(v3PoolAB), 1_000_000 ether);
        tokenA.mint(address(v3PoolAB), 1_000_000 ether);
        tokenC.mint(address(slipPoolBC), 1_000_000 ether);
        tokenB.mint(address(slipPoolBC), 1_000_000 ether);
        tokenA.mint(address(v2PoolAB), 1_000_000 ether);
        tokenB.mint(address(v2PoolAB), 1_000_000 ether);
        v2PoolAB.syncReserves();
        tokenC.mint(address(v2PoolCA), 1_000_000 ether);
        tokenA.mint(address(v2PoolCA), 1_000_000 ether);
        v2PoolCA.syncReserves();

        tokenA.mint(address(executor), INITIAL_CAPITAL);

        vm.startPrank(OWNER);
        executor.setOperator(OPERATOR, true);
        executor.setApprovedToken(address(tokenA), true);
        executor.setApprovedToken(address(tokenB), true);
        executor.setApprovedToken(address(tokenC), true);
        executor.setApprovedPool(address(v3PoolAB), OasisAtomicExecutor.PoolKind.UNISWAP_V3);
        executor.setApprovedPool(address(slipPoolBC), OasisAtomicExecutor.PoolKind.UNISWAP_V3);
        executor.setApprovedPool(address(v2PoolAB), OasisAtomicExecutor.PoolKind.UNISWAP_V2);
        executor.setApprovedPool(address(v2PoolCA), OasisAtomicExecutor.PoolKind.UNISWAP_V2);
        vm.stopPrank();

        handler = new AtomicExecutorHandler(
            executor,
            tokenA,
            tokenB,
            tokenC,
            v3PoolAB,
            slipPoolBC,
            v2PoolAB,
            v2PoolCA,
            OWNER,
            GUARDIAN,
            OPERATOR,
            INITIAL_CAPITAL
        );

        bytes4[] memory selectors = new bytes4[](6);
        selectors[0] = AtomicExecutorHandler.executeTwoHop.selector;
        selectors[1] = AtomicExecutorHandler.executeThreeHop.selector;
        selectors[2] = AtomicExecutorHandler.executeEarlyAbort.selector;
        selectors[3] = AtomicExecutorHandler.sweepToRevenueSink.selector;
        selectors[4] = AtomicExecutorHandler.rescueToTreasury.selector;
        selectors[5] = AtomicExecutorHandler.togglePause.selector;
        targetContract(address(handler));
        targetSelector(FuzzSelector({ addr: address(handler), selectors: selectors }));
    }

    function invariant_totalCapitalMonotonicallyNonDecreasing() public view {
        uint256 total = tokenA.balanceOf(address(executor)) + tokenA.balanceOf(REVENUE_SINK)
            + tokenA.balanceOf(TREASURY);
        assertGe(total, handler.initialCapital());
        assertEq(total, handler.initialCapital() + handler.cumulativeRecordedProfit());
    }

    function invariant_custodyAndControlPlaneLocked() public view {
        assertEq(executor.TREASURY(), TREASURY);
        assertEq(executor.REVENUE_SINK(), REVENUE_SINK);
        assertEq(executor.owner(), OWNER);
        assertEq(executor.guardian(), GUARDIAN);
        assertEq(executor.lastNonce(), handler.lastConsumedNonce());
        assertEq(tokenA.balanceOf(OPERATOR), 0);
        assertEq(tokenA.balanceOf(GUARDIAN), 0);
    }
}
