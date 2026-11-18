// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { Test } from "forge-std/Test.sol";
import { IERC20 } from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import { OasisAtomicExecutor } from "../src/OasisAtomicExecutor.sol";

interface IOasisV3CallbackReceiver {
    function uniswapV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata data)
        external;
}

/// @notice Lightweight ERC-20 test token with optional transfer fee and reentrancy hook.
contract MockAtomicERC20 is IERC20 {
    string public name;
    string public symbol;
    uint8 public immutable decimals = 18;
    uint256 public totalSupply;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    uint256 public outboundFeeBps;
    address public taxedSender;
    address public reenterTarget;
    bytes public reenterData;
    bool public reenterArmed;
    bool public lastReenterReverted;

    constructor(string memory name_, string memory symbol_) {
        name = name_;
        symbol = symbol_;
    }

    function mint(address to, uint256 amount) external {
        totalSupply += amount;
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }

    function setOutboundFeeBps(uint256 feeBps_) external {
        require(feeBps_ < 10_000, "FEE_TOO_HIGH");
        outboundFeeBps = feeBps_;
        taxedSender = address(0);
    }

    /// @notice Restrict transfer fee to transfers originating from `sender` (AMM-buy tax model).
    /// forge-lint: disable-next-line(missing-zero-check)
    function setSenderFeeBps(address sender, uint256 feeBps_) external {
        require(feeBps_ < 10_000, "FEE_TOO_HIGH");
        taxedSender = sender;
        outboundFeeBps = feeBps_;
    }

    /// @dev Test helper intentionally allows clearing `target` to `address(0)`.
    /// forge-lint: disable-next-line(missing-zero-check)
    function armReentrancy(address target, bytes calldata data) external {
        reenterTarget = target;
        reenterData = data;
        reenterArmed = true;
    }

    function disarmReentrancy() external {
        reenterArmed = false;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        _move(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        require(allowed >= amount, "ALLOWANCE");
        if (allowed != type(uint256).max) {
            unchecked {
                allowance[from][msg.sender] = allowed - amount;
            }
        }
        _move(from, to, amount);
        return true;
    }

    function _move(address from, address to, uint256 amount) private {
        require(balanceOf[from] >= amount, "BALANCE");
        unchecked {
            balanceOf[from] -= amount;
        }
        bool applyFee = outboundFeeBps > 0 && (taxedSender == address(0) || from == taxedSender);
        uint256 fee = applyFee ? (amount * outboundFeeBps) / 10_000 : 0;
        uint256 received = amount - fee;
        balanceOf[to] += received;
        if (fee > 0) {
            totalSupply -= fee;
        }
        emit Transfer(from, to, received);

        if (reenterArmed && reenterTarget != address(0)) {
            reenterArmed = false;
            (bool ok,) = reenterTarget.call(reenterData);
            lastReenterReverted = !ok;
        }
    }
}

/// @notice Constant-product Uniswap V2-compatible pair mock enforcing K-invariant and custom fee bps.
contract MockV2Pair {
    address public immutable token0;
    address public immutable token1;
    uint24 public feeBps;
    uint112 private _reserve0;
    uint112 private _reserve1;

    constructor(address token0_, address token1_, uint24 feeBps_) {
        require(feeBps_ < 10_000, "FEE_TOO_HIGH");
        token0 = token0_;
        token1 = token1_;
        feeBps = feeBps_;
    }

    function syncReserves() external {
        // Test fixture reserves stay well below uint112.max.
        // forge-lint: disable-next-line(unsafe-typecast)
        _reserve0 = uint112(IERC20(token0).balanceOf(address(this)));
        // forge-lint: disable-next-line(unsafe-typecast)
        _reserve1 = uint112(IERC20(token1).balanceOf(address(this)));
    }

    function getReserves()
        external
        view
        returns (uint112 reserve0, uint112 reserve1, uint32 blockTimestampLast)
    {
        // forge-lint: disable-next-line(unsafe-typecast)
        return (_reserve0, _reserve1, uint32(block.timestamp));
    }

    function swap(uint256 amount0Out, uint256 amount1Out, address to, bytes calldata) external {
        require(amount0Out > 0 || amount1Out > 0, "INSUFFICIENT_OUT");
        require(amount0Out < _reserve0 && amount1Out < _reserve1, "INSUFFICIENT_LIQ");

        if (amount0Out > 0) {
            require(IERC20(token0).transfer(to, amount0Out), "TRANSFER_0");
        }
        if (amount1Out > 0) {
            require(IERC20(token1).transfer(to, amount1Out), "TRANSFER_1");
        }

        uint256 balance0 = IERC20(token0).balanceOf(address(this));
        uint256 balance1 = IERC20(token1).balanceOf(address(this));
        uint256 amount0In =
            balance0 > _reserve0 - amount0Out ? balance0 - (_reserve0 - amount0Out) : 0;
        uint256 amount1In =
            balance1 > _reserve1 - amount1Out ? balance1 - (_reserve1 - amount1Out) : 0;
        require(amount0In > 0 || amount1In > 0, "INSUFFICIENT_IN");

        uint256 balance0Adjusted = (balance0 * 10_000) - (amount0In * uint256(feeBps));
        uint256 balance1Adjusted = (balance1 * 10_000) - (amount1In * uint256(feeBps));
        require(
            balance0Adjusted * balance1Adjusted >= uint256(_reserve0) * uint256(_reserve1) * 1e8,
            "K"
        );

        // forge-lint: disable-next-line(unsafe-typecast)
        _reserve0 = uint112(balance0);
        // forge-lint: disable-next-line(unsafe-typecast)
        _reserve1 = uint112(balance1);
    }
}

/// @notice Base V3-style swap mock supporting deterministic quoted execution and adversarial modes.
abstract contract BaseMockV3Pool {
    address public immutable token0;
    address public immutable token1;
    uint160 public sqrtPriceX96;
    uint256 public zeroForOneRateNum;
    uint256 public zeroForOneRateDen;
    uint256 public oneForZeroRateNum;
    uint256 public oneForZeroRateDen;

    constructor(
        address token0_,
        address token1_,
        uint160 sqrtPriceX96_,
        uint256 zeroForOneRateNum_,
        uint256 zeroForOneRateDen_,
        uint256 oneForZeroRateNum_,
        uint256 oneForZeroRateDen_
    ) {
        token0 = token0_;
        token1 = token1_;
        sqrtPriceX96 = sqrtPriceX96_;
        zeroForOneRateNum = zeroForOneRateNum_;
        zeroForOneRateDen = zeroForOneRateDen_;
        oneForZeroRateNum = oneForZeroRateNum_;
        oneForZeroRateDen = oneForZeroRateDen_;
    }

    function setSqrtPriceX96(uint160 sqrtPriceX96_) external {
        sqrtPriceX96 = sqrtPriceX96_;
    }

    function swap(
        address recipient,
        bool zeroForOne,
        int256 amountSpecified,
        uint160,
        bytes calldata data
    ) external returns (int256 amount0, int256 amount1) {
        require(amountSpecified > 0, "EXACT_IN_ONLY");
        // Positivity checked directly above.
        // forge-lint: disable-next-line(unsafe-typecast)
        uint256 amountIn = uint256(amountSpecified);
        uint256 amountOut = zeroForOne
            ? (amountIn * zeroForOneRateNum) / zeroForOneRateDen
            : (amountIn * oneForZeroRateNum) / oneForZeroRateDen;

        address tokenIn = zeroForOne ? token0 : token1;
        address tokenOut = zeroForOne ? token1 : token0;
        uint256 inBalBefore = IERC20(tokenIn).balanceOf(address(this));

        if (amountOut > 0) {
            require(IERC20(tokenOut).transfer(recipient, amountOut), "TRANSFER_OUT");
        }

        // Exact-in callback delta signs mirror Uniswap V3 Pool.swap.
        // forge-lint: disable-next-line(unsafe-typecast)
        amount0 = zeroForOne ? int256(amountIn) : -int256(amountOut);
        // forge-lint: disable-next-line(unsafe-typecast)
        amount1 = zeroForOne ? -int256(amountOut) : int256(amountIn);
        IOasisV3CallbackReceiver(msg.sender).uniswapV3SwapCallback(amount0, amount1, data);

        uint256 inBalAfter = IERC20(tokenIn).balanceOf(address(this));
        require(inBalAfter >= inBalBefore + amountIn, "IIA");
    }
}

/// @notice Standard 7-word Uniswap V3 `slot0()` mock.
contract MockV3Pool is BaseMockV3Pool {
    constructor(
        address token0_,
        address token1_,
        uint160 sqrtPriceX96_,
        uint256 zeroForOneRateNum_,
        uint256 zeroForOneRateDen_,
        uint256 oneForZeroRateNum_,
        uint256 oneForZeroRateDen_
    )
        BaseMockV3Pool(
            token0_,
            token1_,
            sqrtPriceX96_,
            zeroForOneRateNum_,
            zeroForOneRateDen_,
            oneForZeroRateNum_,
            oneForZeroRateDen_
        )
    { }

    function slot0() external view returns (uint160, int24, uint16, uint16, uint16, uint8, bool) {
        return (sqrtPriceX96, 0, 0, 1, 1, 0, true);
    }
}

/// @notice 6-word Aerodrome Slipstream `slot0()` mock.
contract MockSlipstreamPool is BaseMockV3Pool {
    constructor(
        address token0_,
        address token1_,
        uint160 sqrtPriceX96_,
        uint256 zeroForOneRateNum_,
        uint256 zeroForOneRateDen_,
        uint256 oneForZeroRateNum_,
        uint256 oneForZeroRateDen_
    )
        BaseMockV3Pool(
            token0_,
            token1_,
            sqrtPriceX96_,
            zeroForOneRateNum_,
            zeroForOneRateDen_,
            oneForZeroRateNum_,
            oneForZeroRateDen_
        )
    { }

    function slot0() external view returns (uint160, int24, uint16, uint16, uint16, bool) {
        return (sqrtPriceX96, 0, 0, 1, 1, true);
    }
}

contract OasisAtomicExecutorTest is Test {
    address private constant OWNER = address(0x1111);
    address private constant GUARDIAN = address(0x2222);
    address private constant TREASURY = address(0x3333);
    address private constant REVENUE_SINK = address(0x4444);
    address private constant OPERATOR = address(0x5555);
    address private constant NEW_OWNER = address(0x6666);

    uint160 private constant INITIAL_SQRT_PRICE = uint160(1 << 96);

    OasisAtomicExecutor private executor;
    MockAtomicERC20 private tokenA;
    MockAtomicERC20 private tokenB;
    MockAtomicERC20 private tokenC;
    MockV3Pool private v3PoolAB;
    MockSlipstreamPool private slipPoolBC;
    MockV2Pair private v2PoolAB;
    MockV2Pair private v2PoolCA;

    function setUp() public {
        vm.prank(OWNER);
        executor = new OasisAtomicExecutor(OWNER, GUARDIAN, TREASURY, REVENUE_SINK);

        tokenA = new MockAtomicERC20("Token A", "TKA");
        tokenB = new MockAtomicERC20("Token B", "TKB");
        tokenC = new MockAtomicERC20("Token C", "TKC");

        // V3 AB gives 2 B per 1 A; V2 AB gives ~1 A per 1 B at skewed reserves -> profitable!
        v3PoolAB = new MockV3Pool(address(tokenA), address(tokenB), INITIAL_SQRT_PRICE, 2, 1, 1, 2);
        // Slipstream BC gives 1 C per 1 B (6-word slot0).
        slipPoolBC = new MockSlipstreamPool(
            address(tokenB), address(tokenC), INITIAL_SQRT_PRICE, 1, 1, 1, 1
        );
        // V2 pools: token0 < token1 ordering handled explicitly via zeroForOne in legs.
        v2PoolAB = new MockV2Pair(address(tokenA), address(tokenB), 30);
        v2PoolCA = new MockV2Pair(address(tokenC), address(tokenA), 30);

        tokenB.mint(address(v3PoolAB), 1_000 ether);
        tokenA.mint(address(v3PoolAB), 1_000 ether);
        tokenC.mint(address(slipPoolBC), 1_000 ether);
        tokenB.mint(address(slipPoolBC), 1_000 ether);

        // Skew V2 AB reserves so 1 B buys ~0.95 A (while 1 A bought 2 B on V3 -> ~1.88 A out!).
        tokenA.mint(address(v2PoolAB), 1_000 ether);
        tokenB.mint(address(v2PoolAB), 1_000 ether);
        v2PoolAB.syncReserves();

        // Skew V2 CA reserves so 1 C buys ~0.95 A (3-hop: 1 A -> 2 B -> 2 C -> ~1.88 A).
        tokenC.mint(address(v2PoolCA), 1_000 ether);
        tokenA.mint(address(v2PoolCA), 1_000 ether);
        v2PoolCA.syncReserves();

        tokenA.mint(address(executor), 100 ether);

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
    }

    function test_twoHopArbitrageV3ToV2() public {
        uint256 balBefore = tokenA.balanceOf(address(executor));

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

        OasisAtomicExecutor.CycleParams memory params = OasisAtomicExecutor.CycleParams({
            nonce: 1,
            deadline: uint64(block.timestamp + 60),
            profitToken: address(tokenA),
            initialAmount: 10 ether,
            minNetProfit: 8 ether,
            guard: OasisAtomicExecutor.PrecheckGuard({
                pool: address(v3PoolAB),
                minSqrtPriceX96: INITIAL_SQRT_PRICE - 100,
                maxSqrtPriceX96: INITIAL_SQRT_PRICE + 100
            }),
            legs: legs
        });

        // Expected V3 out: 20 ether B; V2 out: (20e18 * 9970 * 1000e18) / (1000e18 * 10000 + 20e18 * 9970)
        uint256 v2InWithFee = 20 ether * 9_970;
        uint256 expectedOut = (v2InWithFee * 1_000 ether) / ((1_000 ether * 10_000) + v2InWithFee);
        uint256 expectedProfit = expectedOut - 10 ether;

        vm.expectEmit(true, true, true, true, address(executor));
        emit OasisAtomicExecutor.CycleExecuted(
            1, address(tokenA), 10 ether, expectedProfit, OPERATOR
        );

        vm.prank(OPERATOR);
        (bool executed, uint256 netProfit) = executor.executeCycle(params);

        assertTrue(executed);
        assertEq(netProfit, expectedProfit);
        assertEq(tokenA.balanceOf(address(executor)), balBefore + expectedProfit);
        assertEq(executor.lastNonce(), 1);
    }

    function test_threeHopArbitrageV3ToSlipstreamToV2() public {
        uint256 balBefore = tokenA.balanceOf(address(executor));

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

        OasisAtomicExecutor.CycleParams memory params = OasisAtomicExecutor.CycleParams({
            nonce: 1,
            deadline: uint64(block.timestamp + 60),
            profitToken: address(tokenA),
            initialAmount: 10 ether,
            minNetProfit: 8 ether,
            guard: OasisAtomicExecutor.PrecheckGuard({
                pool: address(slipPoolBC),
                minSqrtPriceX96: INITIAL_SQRT_PRICE,
                maxSqrtPriceX96: INITIAL_SQRT_PRICE
            }),
            legs: legs
        });

        vm.prank(OPERATOR);
        (bool executed, uint256 netProfit) = executor.executeCycle(params);

        assertTrue(executed);
        assertGt(netProfit, 8 ether);
        assertEq(tokenA.balanceOf(address(executor)), balBefore + netProfit);
        assertEq(executor.lastNonce(), 1);
    }

    function test_earlyAbortPrecheckMissBelowAndAboveUnder65kGas() public {
        // Warm lastNonce to measure steady-state early abort probe gas AND verify cold nonce too.
        OasisAtomicExecutor.CycleParams memory baseParams = _sampleTwoHopParams(1, 0);
        vm.prank(OPERATOR);
        (bool firstExecuted,) = executor.executeCycle(baseParams);
        assertTrue(firstExecuted);

        uint256 balBefore = tokenA.balanceOf(address(executor));

        // Out-of-band below minSqrtPriceX96 on 7-word V3 pool.
        v3PoolAB.setSqrtPriceX96(INITIAL_SQRT_PRICE - 500);
        OasisAtomicExecutor.CycleParams memory lowParams = _sampleTwoHopParams(2, 0);

        vm.expectEmit(true, true, false, true, address(executor));
        emit OasisAtomicExecutor.EarlyAborted(2, address(v3PoolAB), INITIAL_SQRT_PRICE - 500);

        vm.prank(OPERATOR);
        uint256 gasBefore = gasleft();
        (bool lowExecuted, uint256 lowProfit) = executor.executeCycle(lowParams);
        uint256 gasUsedLow = gasBefore - gasleft();

        assertFalse(lowExecuted);
        assertEq(lowProfit, 0);
        assertEq(tokenA.balanceOf(address(executor)), balBefore);
        assertEq(executor.lastNonce(), 2);
        assertLt(gasUsedLow, 65_000);

        // Out-of-band above maxSqrtPriceX96 on 7-word V3 pool.
        v3PoolAB.setSqrtPriceX96(INITIAL_SQRT_PRICE + 500);
        OasisAtomicExecutor.CycleParams memory highParams = _sampleTwoHopParams(3, 0);

        vm.prank(OPERATOR);
        uint256 gasBeforeHigh = gasleft();
        (bool highExecuted, uint256 highProfit) = executor.executeCycle(highParams);
        uint256 gasUsedHigh = gasBeforeHigh - gasleft();

        assertFalse(highExecuted);
        assertEq(highProfit, 0);
        assertEq(tokenA.balanceOf(address(executor)), balBefore);
        assertEq(executor.lastNonce(), 3);
        assertLt(gasUsedHigh, 65_000);
    }

    function test_slipstreamSixWordSlot0EarlyAbortUnder65kGas() public {
        slipPoolBC.setSqrtPriceX96(INITIAL_SQRT_PRICE + 500);

        OasisAtomicExecutor.SwapLeg[] memory legs = new OasisAtomicExecutor.SwapLeg[](2);
        legs[0] = OasisAtomicExecutor.SwapLeg({
            pool: address(slipPoolBC),
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
            nonce: 1,
            deadline: deadline,
            profitToken: address(tokenA),
            initialAmount: 10 ether,
            minNetProfit: 0,
            guard: OasisAtomicExecutor.PrecheckGuard({
                pool: address(slipPoolBC),
                minSqrtPriceX96: INITIAL_SQRT_PRICE - 100,
                maxSqrtPriceX96: INITIAL_SQRT_PRICE + 100
            }),
            legs: legs
        });

        uint256 balBefore = tokenA.balanceOf(address(executor));
        vm.prank(OPERATOR);
        uint256 gasBefore = gasleft();
        (bool executed, uint256 netProfit) = executor.executeCycle(params);
        uint256 gasUsed = gasBefore - gasleft();

        assertFalse(executed);
        assertEq(netProfit, 0);
        assertEq(tokenA.balanceOf(address(executor)), balBefore);
        assertEq(executor.lastNonce(), 1);
        assertLt(gasUsed, 65_000);
    }

    function test_coldNonceEarlyAbortAlsoUnder65kGas() public {
        v3PoolAB.setSqrtPriceX96(INITIAL_SQRT_PRICE + 999);
        OasisAtomicExecutor.CycleParams memory params = _sampleTwoHopParams(1, 0);
        uint256 balBefore = tokenA.balanceOf(address(executor));

        vm.prank(OPERATOR);
        uint256 gasBefore = gasleft();
        (bool executed, uint256 netProfit) = executor.executeCycle(params);
        uint256 gasUsed = gasBefore - gasleft();

        assertFalse(executed);
        assertEq(netProfit, 0);
        assertEq(tokenA.balanceOf(address(executor)), balBefore);
        assertEq(executor.lastNonce(), 1);
        assertLt(gasUsed, 65_000);
    }

    function test_insufficientNetProfitRevertsAndPreservesInitialBalance() public {
        uint256 balBefore = tokenA.balanceOf(address(executor));
        OasisAtomicExecutor.CycleParams memory params = _sampleTwoHopParams(1, 50 ether);

        vm.prank(OPERATOR);
        vm.expectRevert();
        executor.executeCycle(params);

        assertEq(tokenA.balanceOf(address(executor)), balBefore);
        assertEq(executor.lastNonce(), 0);
    }

    function test_sweepProfitEmergencyRescuePauseAndOwnable2Step() public {
        // 1. Profitable cycle creates profit.
        OasisAtomicExecutor.CycleParams memory params = _sampleTwoHopParams(1, 8 ether);
        vm.prank(OPERATOR);
        (bool executed, uint256 netProfit) = executor.executeCycle(params);
        assertTrue(executed);

        // 2. Operator sweeps profit exclusively to REVENUE_SINK.
        vm.prank(OPERATOR);
        executor.sweepProfit(tokenA, netProfit);
        assertEq(tokenA.balanceOf(REVENUE_SINK), netProfit);
        assertEq(tokenA.balanceOf(TREASURY), 0);
        assertEq(tokenA.balanceOf(OPERATOR), 0);

        // 3. Guardian emergency pause halts operator execution and sweep.
        vm.prank(GUARDIAN);
        executor.emergencyPause();
        assertTrue(executor.paused());

        OasisAtomicExecutor.CycleParams memory blockedParams = _sampleTwoHopParams(2, 0);
        vm.prank(OPERATOR);
        vm.expectRevert(OasisAtomicExecutor.ExecutorPaused.selector);
        executor.executeCycle(blockedParams);

        vm.prank(OPERATOR);
        vm.expectRevert(OasisAtomicExecutor.ExecutorPaused.selector);
        executor.sweepProfit(tokenA, 1 ether);

        // 4. Owner can emergencyRescue exclusively to TREASURY even while paused.
        uint256 remaining = tokenA.balanceOf(address(executor));
        vm.prank(OWNER);
        executor.emergencyRescue(tokenA, remaining);
        assertEq(tokenA.balanceOf(TREASURY), remaining);
        assertEq(tokenA.balanceOf(address(executor)), 0);

        // 5. Owner unpauses and transfers ownership via 2-step handoff.
        vm.startPrank(OWNER);
        executor.setPaused(false);
        executor.transferOwnership(NEW_OWNER);
        vm.stopPrank();
        assertEq(executor.owner(), OWNER);

        vm.prank(NEW_OWNER);
        executor.acceptOwnership();
        assertEq(executor.owner(), NEW_OWNER);
    }

    function test_rejectsPlainEthTransfers() public {
        vm.deal(OPERATOR, 1 ether);
        vm.prank(OPERATOR);
        (bool ok,) = address(executor).call{ value: 0.1 ether }("");
        assertFalse(ok);
    }

    function _sampleTwoHopParams(uint64 nonce, uint256 minNetProfit)
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
                pool: address(v3PoolAB),
                minSqrtPriceX96: INITIAL_SQRT_PRICE - 100,
                maxSqrtPriceX96: INITIAL_SQRT_PRICE + 100
            }),
            legs: legs
        });
    }
}
