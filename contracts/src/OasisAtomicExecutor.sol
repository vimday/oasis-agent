// SPDX-License-Identifier: BUSL-1.1
pragma solidity 0.8.26;

import { Ownable } from "@openzeppelin/contracts/access/Ownable.sol";
import { Ownable2Step } from "@openzeppelin/contracts/access/Ownable2Step.sol";
import { IERC20 } from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import { SafeERC20 } from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

/// @notice Minimal Uniswap V2-compatible pair interface used for atomic legs.
interface IV2Pair {
    function getReserves()
        external
        view
        returns (uint112 reserve0, uint112 reserve1, uint32 blockTimestampLast);

    function swap(uint256 amount0Out, uint256 amount1Out, address to, bytes calldata data) external;
}

/// @notice Minimal Uniswap V3 / Aerodrome Slipstream state interface for slot0 prechecks.
/// @dev Declaring only the leading `sqrtPriceX96` word decodes cleanly on both 7-word
///      Uniswap V3 `slot0()` and 6-word Aerodrome Slipstream `slot0()` implementations.
interface IV3PoolState {
    function slot0() external view returns (uint160 sqrtPriceX96);
}

/// @notice Minimal Uniswap V3 / Aerodrome Slipstream pool swap interface.
interface IV3Pool {
    function swap(
        address recipient,
        bool zeroForOne,
        int256 amountSpecified,
        uint160 sqrtPriceLimitX96,
        bytes calldata data
    ) external returns (int256 amount0, int256 amount1);
}

/// @notice Bounded atomic multi-hop arbitrage executor with dual-plane custody and early-abort precheck.
/// @dev Custody invariant: operator-triggered profit sweeps route exclusively to immutable
///      `REVENUE_SINK`, and owner-only emergency rescues route exclusively to immutable `TREASURY`.
///      There is no arbitrary recipient withdrawal surface, no ETH receive surface, and no
///      arbitrary call proxy.
contract OasisAtomicExecutor is Ownable2Step {
    using SafeERC20 for IERC20;

    /// @notice Supported AMM pool adapters (`UNISWAP_V3` covers Uniswap V3 and Slipstream).
    enum PoolKind {
        NONE,
        UNISWAP_V2,
        UNISWAP_V3
    }

    /// @notice Optional slot0 price-band precheck executed before any swap leg.
    struct PrecheckGuard {
        address pool;
        uint160 minSqrtPriceX96;
        uint160 maxSqrtPriceX96;
    }

    /// @notice Single swap leg inside an atomic cycle.
    struct SwapLeg {
        address pool;
        address tokenIn;
        address tokenOut;
        bool zeroForOne;
        uint160 sqrtPriceLimitX96;
        uint24 v2FeeBps;
    }

    /// @notice Full bounded cycle intent submitted by an authorized operator or owner.
    struct CycleParams {
        uint64 nonce;
        uint64 deadline;
        address profitToken;
        uint256 initialAmount;
        uint256 minNetProfit;
        PrecheckGuard guard;
        SwapLeg[] legs;
    }

    uint256 private constant _BPS_DENOMINATOR = 10_000;
    uint256 private constant _MAX_INT256 = (uint256(1) << 255) - 1;
    uint8 private constant _UNLOCKED = 0;
    uint8 private constant _LOCKED = 1;

    error ZeroAddress();
    error ZeroAmount();
    error NoStateChange();
    error ExecutorPaused();
    error ReentrantCall();
    error UnauthorizedOperator(address caller);
    error UnauthorizedGuardian(address caller);
    error StaleNonce(uint64 nonce, uint64 lastNonce);
    error DeadlinePassed(uint64 deadline);
    error InvalidLegCount(uint256 count);
    error InvalidCyclePath();
    error UnapprovedToken(address token);
    error UnapprovedPool(address pool);
    error InvalidGuardPool(address pool);
    error InvalidGuardBounds(uint160 minSqrtPriceX96, uint160 maxSqrtPriceX96);
    error InvalidV2FeeBps(uint24 feeBps);
    error InsufficientBalance(address token, uint256 available, uint256 required);
    error InsufficientLiquidity(address pool);
    error InsufficientOutputAmount(address pool);
    error ZeroOutputReceived(address pool, address tokenOut);
    error AmountOverflow(uint256 amount);
    error InvalidCallbackCaller(address caller);
    error InvalidSwapDelta(int256 amount0Delta, int256 amount1Delta);
    error ExcessiveCallbackPayment(uint256 owed, uint256 maxAllowed);
    error InsufficientNetProfit(uint256 balBefore, uint256 balAfter, uint256 minNetProfit);

    event GuardianSet(address indexed oldGuardian, address indexed newGuardian);
    event OperatorSet(address indexed operator, bool allowed, address indexed actor);
    event ApprovedPoolSet(address indexed pool, PoolKind kind, address indexed actor);
    event ApprovedTokenSet(address indexed token, bool allowed, address indexed actor);
    event PausedSet(bool paused, address indexed actor);
    event ProfitSwept(
        address indexed token, address indexed revenueSink, uint256 amount, address indexed actor
    );
    event EmergencyRescued(
        address indexed token, address indexed treasury, uint256 amount, address indexed actor
    );
    event EarlyAborted(uint64 indexed nonce, address indexed pool, uint160 sqrtPriceX96);
    event CycleExecuted(
        uint64 indexed nonce,
        address indexed profitToken,
        uint256 initialAmount,
        uint256 netProfit,
        address indexed actor
    );

    // Immutable custody destinations use SCREAMING_SNAKE per Solidity convention.
    // slither-disable-next-line naming-convention
    address public immutable TREASURY;
    // slither-disable-next-line naming-convention
    address public immutable REVENUE_SINK;

    // Slot-packed control state keeps early-abort precheck SLOAD/SSTORE overhead minimal.
    address public guardian;
    bool public paused;
    uint64 public lastNonce;
    uint8 private _lockState;

    address private _expectedCallbackPool;
    bool private _expectedZeroForOne;
    address private _expectedPayToken;
    uint256 private _expectedPayAmount;

    mapping(address operator => bool allowed) public isOperator;
    mapping(address pool => PoolKind kind) public approvedPools;
    mapping(address token => bool allowed) public approvedTokens;

    modifier onlyOwnerOrOperator() {
        _checkOwnerOrOperator();
        _;
    }

    modifier nonReentrant() {
        if (_lockState != _UNLOCKED) revert ReentrantCall();
        // Transient reentrancy mutex is not a governance access-control change.
        // forge-lint: disable-next-line(missing-events-access-control)
        _lockState = _LOCKED;
        _;
        _lockState = _UNLOCKED;
    }

    constructor(address initialOwner_, address guardian_, address treasury_, address revenueSink_)
        Ownable(initialOwner_)
    {
        if (
            initialOwner_ == address(0) || guardian_ == address(0) || treasury_ == address(0)
                || revenueSink_ == address(0)
        ) revert ZeroAddress();
        TREASURY = treasury_;
        REVENUE_SINK = revenueSink_;
        guardian = guardian_;
    }

    /// @notice Rotate the emergency guardian address.
    function setGuardian(address newGuardian) external onlyOwner {
        if (newGuardian == address(0)) revert ZeroAddress();
        address oldGuardian = guardian;
        if (newGuardian == oldGuardian) revert NoStateChange();
        guardian = newGuardian;
        emit GuardianSet(oldGuardian, newGuardian);
    }

    /// @notice Grant or revoke operator submission and profit-sweep authority.
    function setOperator(address operator, bool allowed) external onlyOwner {
        if (operator == address(0)) revert ZeroAddress();
        if (isOperator[operator] == allowed) revert NoStateChange();
        // Event `OperatorSet` is emitted immediately below.
        // forge-lint: disable-next-line(missing-events-access-control)
        isOperator[operator] = allowed;
        emit OperatorSet(operator, allowed, msg.sender);
    }

    /// @notice Allowlist or revoke an AMM pool and pin its adapter kind.
    function setApprovedPool(address pool, PoolKind kind) external onlyOwner {
        if (pool == address(0)) revert ZeroAddress();
        if (approvedPools[pool] == kind) revert NoStateChange();
        approvedPools[pool] = kind;
        emit ApprovedPoolSet(pool, kind, msg.sender);
    }

    /// @notice Allowlist or revoke an ERC-20 token for cycle routing.
    function setApprovedToken(address token, bool allowed) external onlyOwner {
        if (token == address(0)) revert ZeroAddress();
        if (approvedTokens[token] == allowed) revert NoStateChange();
        approvedTokens[token] = allowed;
        emit ApprovedTokenSet(token, allowed, msg.sender);
    }

    /// @notice Owner-only pause/unpause toggle.
    function setPaused(bool paused_) external onlyOwner {
        if (paused == paused_) revert NoStateChange();
        paused = paused_;
        emit PausedSet(paused_, msg.sender);
    }

    /// @notice Guardian-only one-way emergency halt; guardian cannot unpause or move funds.
    function emergencyPause() external {
        if (msg.sender != guardian) revert UnauthorizedGuardian(msg.sender);
        if (paused) revert NoStateChange();
        paused = true;
        emit PausedSet(true, msg.sender);
    }

    /// @notice Sweep realized profit exclusively to the immutable `REVENUE_SINK`.
    function sweepProfit(IERC20 token, uint256 amount) external nonReentrant onlyOwnerOrOperator {
        if (paused) revert ExecutorPaused();
        if (address(token) == address(0)) revert ZeroAddress();
        if (amount == 0) revert ZeroAmount();
        emit ProfitSwept(address(token), REVENUE_SINK, amount, msg.sender);
        token.safeTransfer(REVENUE_SINK, amount);
    }

    /// @notice Owner-only emergency rescue sending tokens exclusively to immutable `TREASURY`.
    function emergencyRescue(IERC20 token, uint256 amount) external nonReentrant onlyOwner {
        if (address(token) == address(0)) revert ZeroAddress();
        if (amount == 0) revert ZeroAmount();
        emit EmergencyRescued(address(token), TREASURY, amount, msg.sender);
        token.safeTransfer(TREASURY, amount);
    }

    /// @notice Execute a 2-to-3-hop atomic arbitrage cycle with optional cheap slot0 precheck.
    /// @dev Consumes `params.nonce` prior to external precheck/swap calls so stale frames cannot
    ///      be replayed after an `EarlyAborted` outcome.
    function executeCycle(CycleParams calldata params)
        external
        onlyOwnerOrOperator
        returns (bool executed, uint256 netProfit)
    {
        if (_lockState != _UNLOCKED) revert ReentrantCall();
        if (paused) revert ExecutorPaused();
        if (params.nonce <= lastNonce) revert StaleNonce(params.nonce, lastNonce);
        // Short-lived bundle expiry deadline against bounded block timestamps.
        // forge-lint: disable-start(block-timestamp)
        // slither-disable-start timestamp
        if (block.timestamp > params.deadline) revert DeadlinePassed(params.deadline);
        // slither-disable-end timestamp
        // forge-lint: disable-end(block-timestamp)

        _validateCycle(params);

        if (params.guard.pool != address(0)) {
            if (approvedPools[params.guard.pool] != PoolKind.UNISWAP_V3) {
                revert InvalidGuardPool(params.guard.pool);
            }
            if (params.guard.minSqrtPriceX96 > params.guard.maxSqrtPriceX96) {
                revert InvalidGuardBounds(
                    params.guard.minSqrtPriceX96, params.guard.maxSqrtPriceX96
                );
            }
        }

        // Strict CEI & replay defense: consume nonce before any external call.
        lastNonce = params.nonce;

        if (params.guard.pool != address(0)) {
            uint160 sqrtPriceX96 = IV3PoolState(params.guard.pool).slot0();
            if (
                sqrtPriceX96 < params.guard.minSqrtPriceX96
                    || sqrtPriceX96 > params.guard.maxSqrtPriceX96
            ) {
                emit EarlyAborted(params.nonce, params.guard.pool, sqrtPriceX96);
                return (false, 0);
            }
        }

        return _executeSwaps(params);
    }

    function _executeSwaps(CycleParams calldata params)
        private
        nonReentrant
        returns (bool executed, uint256 netProfit)
    {
        uint256 balBefore = IERC20(params.profitToken).balanceOf(address(this));
        if (balBefore < params.initialAmount) {
            revert InsufficientBalance(params.profitToken, balBefore, params.initialAmount);
        }

        // `nonReentrant` blocks reentrancy across every swap leg, and `_expectedCallbackPool`
        // prevents spoofed or duplicate callbacks; observing balance deltas is required to measure
        // actual swap proceeds across fee-on-transfer and partial-fill tokens.
        // slither-disable-start reentrancy-balance
        // slither-disable-start reentrancy-benign
        uint256 currentAmount = _executeLeg(params.legs[0], params.initialAmount);
        currentAmount = _executeLeg(params.legs[1], currentAmount);
        if (params.legs.length == 3) {
            _executeLeg(params.legs[2], currentAmount);
        }

        uint256 balAfter = IERC20(params.profitToken).balanceOf(address(this));
        if (balAfter < balBefore || balAfter - balBefore < params.minNetProfit) {
            revert InsufficientNetProfit(balBefore, balAfter, params.minNetProfit);
        }
        // slither-disable-end reentrancy-benign
        // slither-disable-end reentrancy-balance

        netProfit = balAfter - balBefore;
        // `nonReentrant` guards the full swap sequence; `netProfit` depends on actual proceeds.
        // forge-lint: disable-start(reentrancy-events)
        emit CycleExecuted(
            params.nonce, params.profitToken, params.initialAmount, netProfit, msg.sender
        );
        // forge-lint: disable-end(reentrancy-events)
        return (true, netProfit);
    }

    /// @notice Uniswap V3 and Aerodrome Slipstream swap callback, strictly authenticated to the active leg.
    function uniswapV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata)
        external
    {
        if (
            _lockState != _LOCKED || msg.sender == address(0) || msg.sender != _expectedCallbackPool
        ) {
            revert InvalidCallbackCaller(msg.sender);
        }
        bool expectedZeroForOne = _expectedZeroForOne;
        if (expectedZeroForOne) {
            if (amount0Delta <= 0 || amount1Delta > 0) {
                revert InvalidSwapDelta(amount0Delta, amount1Delta);
            }
        } else {
            if (amount1Delta <= 0 || amount0Delta > 0) {
                revert InvalidSwapDelta(amount0Delta, amount1Delta);
            }
        }

        // Positivity is guaranteed by the directional delta checks above.
        // forge-lint: disable-next-line(unsafe-typecast)
        uint256 amountOwed = expectedZeroForOne ? uint256(amount0Delta) : uint256(amount1Delta);
        uint256 maxAllowed = _expectedPayAmount;
        if (amountOwed == 0 || amountOwed > maxAllowed) {
            revert ExcessiveCallbackPayment(amountOwed, maxAllowed);
        }

        address payToken = _expectedPayToken;
        // Ephemeral single-leg callback gate cleared strictly before token transfer.
        // forge-lint: disable-next-line(missing-events-access-control)
        _expectedCallbackPool = address(0);
        _expectedPayToken = address(0);
        _expectedPayAmount = 0;
        _expectedZeroForOne = false;

        IERC20(payToken).safeTransfer(msg.sender, amountOwed);
    }

    function _checkOwnerOrOperator() private view {
        if (!isOperator[msg.sender] && msg.sender != owner()) {
            revert UnauthorizedOperator(msg.sender);
        }
    }

    function _validateCycle(CycleParams calldata params) private view {
        uint256 legCount = params.legs.length;
        if (legCount < 2 || legCount > 3) revert InvalidLegCount(legCount);
        if (params.initialAmount == 0) revert ZeroAmount();
        if (!approvedTokens[params.profitToken]) revert UnapprovedToken(params.profitToken);
        if (
            params.legs[0].tokenIn != params.profitToken
                || params.legs[legCount - 1].tokenOut != params.profitToken
        ) revert InvalidCyclePath();

        // Contiguous cycle invariants mean `profitToken` already covers legs[0].tokenIn and
        // legs[last].tokenOut, and each leg's `tokenOut` covers the next leg's `tokenIn`.
        _validateLegPoolAndOut(params.legs[0]);
        if (params.legs[0].tokenOut != params.legs[1].tokenIn) revert InvalidCyclePath();
        if (legCount == 2) {
            _validateLegPoolOnly(params.legs[1]);
        } else {
            _validateLegPoolAndOut(params.legs[1]);
            if (params.legs[1].tokenOut != params.legs[2].tokenIn) revert InvalidCyclePath();
            _validateLegPoolOnly(params.legs[2]);
        }
    }

    function _validateLegPoolAndOut(SwapLeg calldata leg) private view {
        _validateLegPoolOnly(leg);
        if (!approvedTokens[leg.tokenOut]) revert UnapprovedToken(leg.tokenOut);
    }

    function _validateLegPoolOnly(SwapLeg calldata leg) private view {
        if (leg.tokenIn == leg.tokenOut) revert InvalidCyclePath();
        PoolKind kind = approvedPools[leg.pool];
        if (kind == PoolKind.NONE) revert UnapprovedPool(leg.pool);
        if (kind == PoolKind.UNISWAP_V2 && leg.v2FeeBps >= _BPS_DENOMINATOR) {
            revert InvalidV2FeeBps(leg.v2FeeBps);
        }
    }

    function _executeLeg(SwapLeg calldata leg, uint256 currentAmount)
        private
        returns (uint256 receivedAmount)
    {
        if (approvedPools[leg.pool] == PoolKind.UNISWAP_V2) {
            return _executeV2Leg(leg, currentAmount);
        }
        return _executeV3Leg(leg, currentAmount);
    }

    function _executeV2Leg(SwapLeg calldata leg, uint256 currentAmount)
        private
        returns (uint256 receivedAmount)
    {
        IERC20(leg.tokenIn).safeTransfer(leg.pool, currentAmount);
        // `blockTimestampLast` is intentionally unused for instantaneous constant-product sizing.
        // forge-lint: disable-start(unused-return)
        // slither-disable-start unused-return
        (uint112 reserve0, uint112 reserve1,) = IV2Pair(leg.pool).getReserves();
        // slither-disable-end unused-return
        // forge-lint: disable-end(unused-return)
        (uint256 reserveIn, uint256 reserveOut) = leg.zeroForOne
            ? (uint256(reserve0), uint256(reserve1))
            : (uint256(reserve1), uint256(reserve0));
        if (reserveIn == 0 || reserveOut == 0) revert InsufficientLiquidity(leg.pool);

        uint256 amountInWithFee = currentAmount * (_BPS_DENOMINATOR - uint256(leg.v2FeeBps));
        uint256 numerator = amountInWithFee * reserveOut;
        uint256 denominator = (reserveIn * _BPS_DENOMINATOR) + amountInWithFee;
        uint256 amountOut = numerator / denominator;
        if (amountOut < 1 || amountOut >= reserveOut) revert InsufficientOutputAmount(leg.pool);

        // `nonReentrant` prevents any reentrant call from mutating balances mid-swap.
        // slither-disable-start reentrancy-balance
        uint256 balOutBefore = IERC20(leg.tokenOut).balanceOf(address(this));
        (uint256 amount0Out, uint256 amount1Out) =
            leg.zeroForOne ? (uint256(0), amountOut) : (amountOut, uint256(0));
        IV2Pair(leg.pool).swap(amount0Out, amount1Out, address(this), "");

        uint256 balOutAfter = IERC20(leg.tokenOut).balanceOf(address(this));
        if (balOutAfter <= balOutBefore) revert ZeroOutputReceived(leg.pool, leg.tokenOut);
        // slither-disable-end reentrancy-balance
        return balOutAfter - balOutBefore;
    }

    function _executeV3Leg(SwapLeg calldata leg, uint256 currentAmount)
        private
        returns (uint256 receivedAmount)
    {
        if (currentAmount > _MAX_INT256) revert AmountOverflow(currentAmount);

        // `nonReentrant` and single-use `_expectedCallbackPool` guard this swap and its callback.
        // slither-disable-start reentrancy-balance
        // slither-disable-start reentrancy-benign
        uint256 balOutBefore = IERC20(leg.tokenOut).balanceOf(address(this));
        // Ephemeral single-leg callback gate armed strictly around this pool swap call.
        // forge-lint: disable-next-line(missing-events-access-control)
        _expectedCallbackPool = leg.pool;
        _expectedZeroForOne = leg.zeroForOne;
        _expectedPayToken = leg.tokenIn;
        _expectedPayAmount = currentAmount;

        // Authoritative received output is measured from contract token balance delta below.
        // forge-lint: disable-start(unused-return)
        // slither-disable-start unused-return
        IV3Pool(leg.pool)
            .swap(
                address(this),
                leg.zeroForOne,
                // Explicitly bounded <= _MAX_INT256 above.
                // forge-lint: disable-next-line(unsafe-typecast)
                int256(currentAmount),
                leg.sqrtPriceLimitX96,
                abi.encode(leg.tokenIn)
            );
        // slither-disable-end unused-return
        // forge-lint: disable-end(unused-return)

        // forge-lint: disable-next-line(missing-events-access-control)
        _expectedCallbackPool = address(0);
        _expectedZeroForOne = false;
        _expectedPayToken = address(0);
        _expectedPayAmount = 0;

        uint256 balOutAfter = IERC20(leg.tokenOut).balanceOf(address(this));
        if (balOutAfter <= balOutBefore) revert ZeroOutputReceived(leg.pool, leg.tokenOut);
        // slither-disable-end reentrancy-benign
        // slither-disable-end reentrancy-balance
        return balOutAfter - balOutBefore;
    }
}
