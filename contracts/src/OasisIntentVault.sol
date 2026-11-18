// SPDX-License-Identifier: BUSL-1.1
pragma solidity 0.8.26;

import { Ownable } from "@openzeppelin/contracts/access/Ownable.sol";
import { Ownable2Step } from "@openzeppelin/contracts/access/Ownable2Step.sol";
import { IERC1271 } from "@openzeppelin/contracts/interfaces/IERC1271.sol";
import { IERC20 } from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import { SafeERC20 } from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

/// @notice Minimal Uniswap V3 / Aerodrome Slipstream state interface for slot0 prechecks.
/// @dev Declaring only the leading `sqrtPriceX96` word decodes cleanly on both 7-word
///      Uniswap V3 `slot0()` and 6-word Aerodrome Slipstream `slot0()` implementations.
interface IV3PoolState {
    function slot0() external view returns (uint160 sqrtPriceX96);
}

/// @notice Canonical UniswapX order metadata included in `ResolvedOrder`.
struct OrderInfo {
    address reactor;
    address swapper;
    uint256 nonce;
    uint256 deadline;
    address additionalValidationContract;
    bytes additionalValidationData;
}

/// @notice Canonical UniswapX resolved input leg.
struct InputToken {
    IERC20 token;
    uint256 amount;
    uint256 maxAmount;
}

/// @notice Canonical UniswapX resolved output leg.
struct OutputToken {
    address token;
    uint256 amount;
    address recipient;
}

/// @notice Canonical UniswapX resolved order passed to `IReactorCallback.reactorCallback`.
struct ResolvedOrder {
    OrderInfo info;
    InputToken input;
    OutputToken[] outputs;
    bytes sig;
    bytes32 hash;
}

/// @notice Canonical UniswapX signed order payload passed to reactor execution entrypoints.
struct SignedOrder {
    bytes order;
    bytes sig;
}

/// @notice Canonical UniswapX reactor execution interface with callback support.
interface IReactor {
    function executeWithCallback(SignedOrder calldata order, bytes calldata callbackData)
        external
        payable;

    function executeBatchWithCallback(SignedOrder[] calldata orders, bytes calldata callbackData)
        external
        payable;
}

/// @notice Canonical UniswapX filler callback interface invoked by an active reactor.
interface IReactorCallback {
    function reactorCallback(ResolvedOrder[] calldata resolvedOrders, bytes calldata callbackData)
        external;
}

/// @notice Bounded ERC-1271 quote-signing vault and UniswapX `IReactorCallback` filler.
/// @dev Dual-plane custody model matching `OasisAtomicExecutor`:
///      - Sweeps (`sweepERC20` / `sweepNative`) route exclusively to immutable `TREASURY` or
///        immutable `REVENUE_SINK`; no arbitrary withdrawal recipient exists.
///      - `quoteSigner` has zero on-chain privilege; ERC-1271 signatures are domain-separated to
///        `(QUOTE_DOMAIN_TYPEHASH, block.chainid, address(this), signerEpoch, deadline, hash)` and
///        can be invalidated immediately via `pause()`, `rotateQuoteSigner()`, `disableQuoteSigner()`,
///        or `revokeDigest()`.
///      - `fillerOperator` can trigger bounded reactor fills (`executeReactorFill`) against
///        allowlisted reactors, routers, router selectors, and pools, subject to an optional
///        `<65k` gas `slot0` precheck and strict post-fill `minNetProfit` enforcement.
contract OasisIntentVault is Ownable2Step, IERC1271, IReactorCallback {
    using SafeERC20 for IERC20;

    /// @notice Optional slot0 price-band precheck executed prior to reactor execution.
    struct PrecheckSqrtPrice {
        address pool;
        uint160 minSqrtPriceX96;
        uint160 maxSqrtPriceX96;
    }

    /// @notice Bounded hedge/swap leg executed inside `reactorCallback`.
    struct SwapStep {
        address router;
        address pool;
        address tokenIn;
        address tokenOut;
        uint256 amountIn;
        uint256 minOut;
        bytes data;
    }

    /// @notice Operator plan committing to reactor, profit token, precheck, and swap steps.
    struct ReactorFillPlan {
        address reactor;
        address profitToken;
        uint256 minNetProfit;
        PrecheckSqrtPrice precheck;
        SwapStep[] steps;
    }

    bytes32 public constant QUOTE_DOMAIN_TYPEHASH = keccak256(
        "OasisIntentQuote(uint256 chainId,address vault,uint256 signerEpoch,uint256 deadline,bytes32 orderDigest)"
    );

    bytes4 private constant _ERC1271_MAGIC_VALUE = IERC1271.isValidSignature.selector;
    bytes4 private constant _ERC1271_INVALID = 0xffffffff;
    // Upper bound on secp256k1 low-s malleability (secp256k1n / 2).
    uint256 private constant _SECP256K1N_HALF =
        0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0;
    uint256 private constant _ENCODED_SIG_LENGTH = 192;
    uint256 private constant _MAX_SWAP_STEPS = 3;
    uint256 private constant _MAX_OUTPUT_TOKENS = 4;
    uint256 private constant _MAX_STEP_CALLDATA_BYTES = 4096;
    uint8 private constant _UNLOCKED = 0;
    uint8 private constant _LOCKED = 1;

    error ZeroAddress();
    error ZeroAmount();
    error ZeroSelector();
    error NoStateChange();
    error VaultPaused();
    error ReentrantCall();
    error UnauthorizedOperator(address caller);
    error UnauthorizedPauser(address caller);
    error UnauthorizedRevoker(address caller);
    error InvalidCustodyRecipient(address recipient);
    error InsufficientNativeBalance(uint256 available, uint256 required);
    error NativeTransferFailed(address recipient, uint256 amount);
    error UnapprovedReactor(address reactor);
    error UnapprovedRouter(address router);
    error UnapprovedRouterSelector(bytes4 selector);
    error UnapprovedPool(address pool);
    error InvalidPrecheckBounds(uint160 minSqrtPriceX96, uint160 maxSqrtPriceX96);
    error InvalidStepCount(uint256 count);
    error InvalidSwapStep();
    error InvalidReactorCalldata();
    error InvalidReactorSelector(bytes4 selector);
    error UnauthorizedReactorCallback(address caller);
    error InvalidCallbackData();
    error EmptyResolvedOrders();
    error TooManyOutputTokens(uint256 count);
    error ReactorCallbackNotInvoked();
    error ReactorCallFailed();
    error RouterSwapFailed();
    error InsufficientStepOutput(
        address pool, address tokenOut, uint256 balBefore, uint256 balAfter, uint256 minOut
    );
    error InsufficientNetProfit(uint256 balBefore, uint256 balAfter, uint256 minNetProfit);

    event PausedSet(bool paused, address indexed actor);
    event GuardianSet(address indexed oldGuardian, address indexed newGuardian);
    event FillerOperatorSet(
        address indexed oldOperator, address indexed newOperator, address indexed actor
    );
    event QuoteSignerRotated(
        address indexed oldSigner,
        address indexed newSigner,
        uint256 indexed signerEpoch,
        address actor
    );
    event DigestRevoked(bytes32 indexed digest, address indexed actor);
    event ReactorAllowedSet(address indexed reactor, bool allowed, address indexed actor);
    event RouterAllowedSet(address indexed router, bool allowed, address indexed actor);
    event RouterSelectorAllowedSet(bytes4 indexed selector, bool allowed, address indexed actor);
    event PoolAllowedSet(address indexed pool, bool allowed, address indexed actor);
    event ERC20Swept(
        address indexed token, address indexed recipient, uint256 amount, address indexed actor
    );
    event NativeSwept(address indexed recipient, uint256 amount, address indexed actor);
    event EarlyAborted(address indexed reactor, address indexed pool, uint160 sqrtPriceX96);
    event ReactorFillExecuted(
        address indexed reactor,
        address indexed profitToken,
        uint256 netProfit,
        address indexed actor
    );

    // Immutable custody destinations use SCREAMING_SNAKE per Solidity convention.
    // slither-disable-next-line naming-convention
    address public immutable TREASURY;
    // slither-disable-next-line naming-convention
    address public immutable REVENUE_SINK;

    address public fillerOperator;
    address public quoteSigner;
    address public guardian;
    bool public paused;
    uint8 private _lockState;
    uint256 public signerEpoch;

    address private _activeReactor;
    bytes32 private _pendingStepsHash;
    address private _pendingOutputToken0;
    address private _pendingOutputToken1;
    address private _pendingOutputToken2;
    address private _pendingOutputToken3;

    mapping(bytes32 digest => bool revoked) public revokedDigests;
    mapping(address reactor => bool allowed) public allowedReactors;
    mapping(address router => bool allowed) public allowedRouters;
    mapping(bytes4 selector => bool allowed) public allowedRouterSelectors;
    mapping(address pool => bool allowed) public allowedPools;

    modifier onlyFillerOperator() {
        _checkFillerOperator();
        _;
    }

    modifier onlyOwnerOrOperator() {
        _checkOwnerOrOperator();
        _;
    }

    modifier whenNotPaused() {
        _checkNotPaused();
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

    /// @notice Allow receiving native ETH for native refunds or custody sweeps.
    receive() external payable { }

    /// @notice Pause ERC-1271 quote validation, reactor fills, and operator sweeps.
    function pause() external {
        if (msg.sender != guardian && msg.sender != owner()) {
            revert UnauthorizedPauser(msg.sender);
        }
        if (paused) revert NoStateChange();
        paused = true;
        emit PausedSet(true, msg.sender);
    }

    /// @notice Owner-only unpause after incident resolution.
    function unpause() external onlyOwner {
        if (!paused) revert NoStateChange();
        paused = false;
        emit PausedSet(false, msg.sender);
    }

    /// @notice Owner-only rotation of the emergency guardian address.
    function setGuardian(address newGuardian) external onlyOwner {
        if (newGuardian == address(0)) revert ZeroAddress();
        address oldGuardian = guardian;
        if (newGuardian == oldGuardian) revert NoStateChange();
        guardian = newGuardian;
        emit GuardianSet(oldGuardian, newGuardian);
    }

    /// @notice Owner-only assignment of the hot filler operator address.
    function setFillerOperator(address newFillerOperator) external onlyOwner {
        if (newFillerOperator == address(0)) revert ZeroAddress();
        address oldOperator = fillerOperator;
        if (newFillerOperator == oldOperator) revert NoStateChange();
        fillerOperator = newFillerOperator;
        emit FillerOperatorSet(oldOperator, newFillerOperator, msg.sender);
    }

    /// @notice Owner-only quote signer rotation; increments `signerEpoch` to invalidate prior quotes.
    function rotateQuoteSigner(address newQuoteSigner) external onlyOwner {
        if (newQuoteSigner == address(0)) revert ZeroAddress();
        address oldSigner = quoteSigner;
        if (newQuoteSigner == oldSigner) revert NoStateChange();
        quoteSigner = newQuoteSigner;
        uint256 nextEpoch = signerEpoch + 1;
        signerEpoch = nextEpoch;
        emit QuoteSignerRotated(oldSigner, newQuoteSigner, nextEpoch, msg.sender);
    }

    /// @notice Owner-only immediate disablement of quote signing without changing pause state.
    function disableQuoteSigner() external onlyOwner {
        address oldSigner = quoteSigner;
        if (oldSigner == address(0)) revert NoStateChange();
        quoteSigner = address(0);
        uint256 nextEpoch = signerEpoch + 1;
        signerEpoch = nextEpoch;
        emit QuoteSignerRotated(oldSigner, address(0), nextEpoch, msg.sender);
    }

    /// @notice Allowlist or revoke a UniswapX reactor address.
    function setReactorAllowed(address reactor, bool allowed) external onlyOwner {
        if (reactor == address(0)) revert ZeroAddress();
        if (allowedReactors[reactor] == allowed) revert NoStateChange();
        // Event `ReactorAllowedSet` is emitted immediately below.
        // forge-lint: disable-next-line(missing-events-access-control)
        allowedReactors[reactor] = allowed;
        emit ReactorAllowedSet(reactor, allowed, msg.sender);
    }

    /// @notice Allowlist or revoke an execution swap router address.
    function setRouterAllowed(address router, bool allowed) external onlyOwner {
        if (router == address(0)) revert ZeroAddress();
        if (allowedRouters[router] == allowed) revert NoStateChange();
        // Event `RouterAllowedSet` is emitted immediately below.
        // forge-lint: disable-next-line(missing-events-access-control)
        allowedRouters[router] = allowed;
        emit RouterAllowedSet(router, allowed, msg.sender);
    }

    /// @notice Allowlist or revoke a router function selector permitted in `SwapStep.data`.
    function setRouterSelectorAllowed(bytes4 selector, bool allowed) external onlyOwner {
        if (selector == bytes4(0)) revert ZeroSelector();
        if (
            allowed
                && (selector == IERC20.approve.selector
                    || selector == IERC20.transfer.selector
                    || selector == IERC20.transferFrom.selector)
        ) {
            revert UnapprovedRouterSelector(selector);
        }
        if (allowedRouterSelectors[selector] == allowed) revert NoStateChange();
        // Event `RouterSelectorAllowedSet` is emitted immediately below.
        // forge-lint: disable-next-line(missing-events-access-control)
        allowedRouterSelectors[selector] = allowed;
        emit RouterSelectorAllowedSet(selector, allowed, msg.sender);
    }

    /// @notice Allowlist or revoke a pool address for swap routing or `slot0` prechecks.
    function setPoolAllowed(address pool, bool allowed) external onlyOwner {
        if (pool == address(0)) revert ZeroAddress();
        if (allowedPools[pool] == allowed) revert NoStateChange();
        // Event `PoolAllowedSet` is emitted immediately below.
        // forge-lint: disable-next-line(missing-events-access-control)
        allowedPools[pool] = allowed;
        emit PoolAllowedSet(pool, allowed, msg.sender);
    }

    /// @notice Permanently revoke an ERC-1271 order digest so previously signed quotes fail closed.
    function revokeDigest(bytes32 digest) external {
        if (msg.sender != guardian && msg.sender != fillerOperator && msg.sender != owner()) {
            revert UnauthorizedRevoker(msg.sender);
        }
        if (revokedDigests[digest]) revert NoStateChange();
        revokedDigests[digest] = true;
        emit DigestRevoked(digest, msg.sender);
    }

    /// @notice Sweep ERC-20 funds exclusively to immutable `TREASURY` or immutable `REVENUE_SINK`.
    function sweepERC20(address token, address recipient, uint256 amount)
        external
        nonReentrant
        onlyOwnerOrOperator
    {
        if (msg.sender != owner() && paused) revert VaultPaused();
        if (token == address(0) || recipient == address(0)) revert ZeroAddress();
        if (recipient != TREASURY && recipient != REVENUE_SINK) {
            revert InvalidCustodyRecipient(recipient);
        }
        if (amount == 0) revert ZeroAmount();
        address target = recipient == TREASURY ? TREASURY : REVENUE_SINK;
        emit ERC20Swept(token, target, amount, msg.sender);
        IERC20(token).safeTransfer(target, amount);
    }

    /// @notice Sweep native ETH exclusively to immutable `TREASURY` or immutable `REVENUE_SINK`.
    function sweepNative(address recipient, uint256 amount)
        external
        nonReentrant
        onlyOwnerOrOperator
    {
        if (msg.sender != owner() && paused) revert VaultPaused();
        if (recipient == address(0)) revert ZeroAddress();
        if (recipient != TREASURY && recipient != REVENUE_SINK) {
            revert InvalidCustodyRecipient(recipient);
        }
        if (amount == 0) revert ZeroAmount();
        uint256 available = address(this).balance;
        if (available < amount) revert InsufficientNativeBalance(available, amount);
        address target = recipient == TREASURY ? TREASURY : REVENUE_SINK;
        emit NativeSwept(target, amount, msg.sender);
        // `nonReentrant` locks `_lockState = _LOCKED` prior to transfer, and `target` is restricted
        // to immutable `TREASURY` or `REVENUE_SINK`.
        // forge-lint: disable-next-line(reentrancy-eth)
        (bool ok,) = payable(target).call{ value: amount }("");
        if (!ok) revert NativeTransferFailed(target, amount);
    }

    /// @notice Compute the domain-separated digest expected from `quoteSigner`.
    function computeQuoteDigest(bytes32 hash, uint256 deadline) public view returns (bytes32) {
        return keccak256(
            abi.encode(
                QUOTE_DOMAIN_TYPEHASH, block.chainid, address(this), signerEpoch, deadline, hash
            )
        );
    }

    /// @notice ERC-1271 signature verification bound to chain, vault, epoch, deadline, and digest.
    /// @dev `signature` must be canonical `abi.encode(uint256 deadline, bytes rawSig)` with
    ///      `rawSig.length == 65`. Short, malformed, expired, revoked, or malleable signatures
    ///      return `0xffffffff` cleanly without reverting.
    function isValidSignature(bytes32 hash, bytes calldata signature)
        external
        view
        override
        returns (bytes4)
    {
        address signer = quoteSigner;
        if (paused || signer == address(0) || revokedDigests[hash]) {
            return _ERC1271_INVALID;
        }
        if (signature.length != _ENCODED_SIG_LENGTH) {
            return _ERC1271_INVALID;
        }

        uint256 deadline = uint256(bytes32(signature[0:32]));
        uint256 rawOffset = uint256(bytes32(signature[32:64]));
        uint256 rawLen = uint256(bytes32(signature[64:96]));
        if (rawOffset != 64 || rawLen != 65 || bytes31(signature[161:192]) != bytes31(0)) {
            return _ERC1271_INVALID;
        }

        // Short-lived quote expiry deadline against bounded block timestamps.
        // forge-lint: disable-start(block-timestamp)
        // slither-disable-start timestamp
        if (block.timestamp > deadline) {
            return _ERC1271_INVALID;
        }
        // slither-disable-end timestamp
        // forge-lint: disable-end(block-timestamp)

        bytes32 r = bytes32(signature[96:128]);
        bytes32 s = bytes32(signature[128:160]);
        uint8 v = uint8(signature[160]);
        if (v != 27 && v != 28) {
            return _ERC1271_INVALID;
        }
        if (uint256(s) > _SECP256K1N_HALF) {
            return _ERC1271_INVALID;
        }

        bytes32 digest = computeQuoteDigest(hash, deadline);
        address recovered = ecrecover(digest, v, r, s);
        if (recovered == address(0) || recovered != signer) {
            return _ERC1271_INVALID;
        }
        return _ERC1271_MAGIC_VALUE;
    }

    /// @notice Execute a bounded UniswapX reactor fill with optional `<65k` gas `slot0` precheck.
    function executeReactorFill(ReactorFillPlan calldata plan, bytes calldata reactorCalldata)
        external
        onlyFillerOperator
        whenNotPaused
        returns (bool executed, uint256 netProfit)
    {
        if (_lockState != _UNLOCKED) revert ReentrantCall();
        _validateFillPlan(plan, reactorCalldata);

        if (plan.precheck.pool != address(0)) {
            uint160 sqrtPriceX96 = IV3PoolState(plan.precheck.pool).slot0();
            if (
                sqrtPriceX96 < plan.precheck.minSqrtPriceX96
                    || sqrtPriceX96 > plan.precheck.maxSqrtPriceX96
            ) {
                emit EarlyAborted(plan.reactor, plan.precheck.pool, sqrtPriceX96);
                return (false, 0);
            }
        }

        return _executeFillWithLock(plan, reactorCalldata);
    }

    /// @notice UniswapX `IReactorCallback` invoked solely by the active allowlisted reactor.
    function reactorCallback(ResolvedOrder[] calldata resolvedOrders, bytes calldata callbackData)
        external
        override
    {
        if (paused) revert VaultPaused();
        address reactor = _activeReactor;
        if (
            _lockState != _LOCKED || reactor == address(0) || msg.sender != reactor
                || !allowedReactors[msg.sender]
        ) {
            revert UnauthorizedReactorCallback(msg.sender);
        }
        if (keccak256(callbackData) != _pendingStepsHash) {
            revert InvalidCallbackData();
        }
        if (resolvedOrders.length == 0) {
            revert EmptyResolvedOrders();
        }

        // Strict CEI: clear ephemeral callback authorization prior to any router or token calls.
        _activeReactor = address(0);
        _pendingStepsHash = bytes32(0);

        SwapStep[] memory steps = abi.decode(callbackData, (SwapStep[]));
        _executeSwapSteps(steps);
        _approveReactorOutputs(reactor, resolvedOrders);
    }

    function _executeFillWithLock(ReactorFillPlan calldata plan, bytes calldata reactorCalldata)
        private
        nonReentrant
        returns (bool executed, uint256 netProfit)
    {
        // `nonReentrant` blocks reentrancy across reactor execution; balance delta is required
        // to enforce exact post-fill `minNetProfit`.
        // slither-disable-start reentrancy-balance
        // slither-disable-start reentrancy-benign
        uint256 balBefore = IERC20(plan.profitToken).balanceOf(address(this));

        _activeReactor = plan.reactor;
        _pendingStepsHash = keccak256(abi.encode(plan.steps));

        (bool ok, bytes memory ret) = plan.reactor.call(reactorCalldata);
        if (!ok) {
            _revertWithData(ret, ReactorCallFailed.selector);
        }
        if (_activeReactor != address(0)) {
            revert ReactorCallbackNotInvoked();
        }

        _clearPendingOutputApprovals(plan.reactor);

        uint256 balAfter = IERC20(plan.profitToken).balanceOf(address(this));
        if (balAfter < balBefore || balAfter - balBefore < plan.minNetProfit) {
            revert InsufficientNetProfit(balBefore, balAfter, plan.minNetProfit);
        }
        // slither-disable-end reentrancy-benign
        // slither-disable-end reentrancy-balance

        netProfit = balAfter - balBefore;
        // `nonReentrant` guards the full reactor fill; `netProfit` depends on actual proceeds.
        // forge-lint: disable-start(reentrancy-events)
        emit ReactorFillExecuted(plan.reactor, plan.profitToken, netProfit, msg.sender);
        // forge-lint: disable-end(reentrancy-events)
        return (true, netProfit);
    }

    function _validateFillPlan(ReactorFillPlan calldata plan, bytes calldata reactorCalldata)
        private
        view
    {
        if (plan.reactor == address(0) || plan.profitToken == address(0)) revert ZeroAddress();
        if (!allowedReactors[plan.reactor]) revert UnapprovedReactor(plan.reactor);

        if (reactorCalldata.length < 4) revert InvalidReactorCalldata();
        bytes4 reactorSelector = bytes4(reactorCalldata[:4]);
        if (
            reactorSelector != IReactor.executeWithCallback.selector
                && reactorSelector != IReactor.executeBatchWithCallback.selector
        ) {
            revert InvalidReactorSelector(reactorSelector);
        }

        if (plan.precheck.pool != address(0)) {
            if (!allowedPools[plan.precheck.pool]) revert UnapprovedPool(plan.precheck.pool);
            if (plan.precheck.minSqrtPriceX96 > plan.precheck.maxSqrtPriceX96) {
                revert InvalidPrecheckBounds(
                    plan.precheck.minSqrtPriceX96, plan.precheck.maxSqrtPriceX96
                );
            }
        }

        uint256 stepCount = plan.steps.length;
        if (stepCount > _MAX_SWAP_STEPS) revert InvalidStepCount(stepCount);
        if (stepCount >= 1) _validateSwapStep(plan.steps[0]);
        if (stepCount >= 2) _validateSwapStep(plan.steps[1]);
        if (stepCount == 3) _validateSwapStep(plan.steps[2]);
    }

    function _validateSwapStep(SwapStep calldata step) private view {
        if (
            step.router == address(0) || step.pool == address(0) || step.tokenIn == address(0)
                || step.tokenOut == address(0)
        ) {
            revert ZeroAddress();
        }
        if (!allowedRouters[step.router]) revert UnapprovedRouter(step.router);
        if (!allowedPools[step.pool]) revert UnapprovedPool(step.pool);
        if (step.tokenIn == step.tokenOut || step.amountIn == 0) revert InvalidSwapStep();
        if (step.data.length < 4 || step.data.length > _MAX_STEP_CALLDATA_BYTES) {
            revert InvalidSwapStep();
        }
        bytes4 selector = bytes4(step.data[:4]);
        if (!allowedRouterSelectors[selector]) revert UnapprovedRouterSelector(selector);
    }

    function _executeSwapSteps(SwapStep[] memory steps) private {
        uint256 stepCount = steps.length;
        if (stepCount > _MAX_SWAP_STEPS) revert InvalidStepCount(stepCount);
        if (stepCount >= 1) _executeSingleSwapStep(steps[0]);
        if (stepCount >= 2) _executeSingleSwapStep(steps[1]);
        if (stepCount == 3) _executeSingleSwapStep(steps[2]);
    }

    function _executeSingleSwapStep(SwapStep memory step) private {
        if (
            step.router == address(0) || step.pool == address(0) || step.tokenIn == address(0)
                || step.tokenOut == address(0)
        ) {
            revert ZeroAddress();
        }
        if (!allowedRouters[step.router]) revert UnapprovedRouter(step.router);
        if (!allowedPools[step.pool]) revert UnapprovedPool(step.pool);
        if (
            step.tokenIn == step.tokenOut || step.amountIn == 0 || step.data.length < 4
                || step.data.length > _MAX_STEP_CALLDATA_BYTES
        ) {
            revert InvalidSwapStep();
        }
        bytes4 selector;
        bytes memory stepData = step.data;
        assembly ("memory-safe") {
            selector := mload(add(stepData, 32))
        }
        if (!allowedRouterSelectors[selector]) revert UnapprovedRouterSelector(selector);

        // `nonReentrant` and single-use `_activeReactor` guard this router call and delta check.
        // slither-disable-start reentrancy-balance
        // slither-disable-start reentrancy-benign
        uint256 outBefore = IERC20(step.tokenOut).balanceOf(address(this));
        IERC20(step.tokenIn).forceApprove(step.router, step.amountIn);
        (bool swapOk, bytes memory swapRet) = step.router.call(stepData);
        if (!swapOk) {
            _revertWithData(swapRet, RouterSwapFailed.selector);
        }
        IERC20(step.tokenIn).forceApprove(step.router, 0);
        uint256 outAfter = IERC20(step.tokenOut).balanceOf(address(this));
        if (outAfter < outBefore || outAfter - outBefore < step.minOut) {
            revert InsufficientStepOutput(
                step.pool, step.tokenOut, outBefore, outAfter, step.minOut
            );
        }
        // slither-disable-end reentrancy-benign
        // slither-disable-end reentrancy-balance
    }

    struct _OutputAccumulator {
        address[_MAX_OUTPUT_TOKENS] tokens;
        uint256[_MAX_OUTPUT_TOKENS] totals;
        uint256 count;
        bool hasZeroAddress;
        uint256 overflowCount;
    }

    function _approveReactorOutputs(address reactor, ResolvedOrder[] calldata resolvedOrders)
        private
    {
        _OutputAccumulator memory acc = _OutputAccumulator({
            tokens: [address(0), address(0), address(0), address(0)],
            totals: [uint256(0), uint256(0), uint256(0), uint256(0)],
            count: 0,
            hasZeroAddress: false,
            overflowCount: 0
        });
        uint256 orderCount = resolvedOrders.length;
        for (uint256 i = 0; i < orderCount; ++i) {
            OutputToken[] calldata outputs = resolvedOrders[i].outputs;
            uint256 outLen = outputs.length;
            for (uint256 j = 0; j < outLen; ++j) {
                OutputToken calldata output = outputs[j];
                if (output.token == address(0) || output.recipient == address(0)) {
                    acc.hasZeroAddress = true;
                    break;
                }
                if (output.amount == 0) continue;
                _accumulateOutputToken(acc, output.token, output.amount);
                if (acc.overflowCount > 0) break;
            }
            if (acc.hasZeroAddress || acc.overflowCount > 0) break;
        }

        if (acc.hasZeroAddress) revert ZeroAddress();
        if (acc.overflowCount > 0) revert TooManyOutputTokens(acc.overflowCount);
        if (acc.count == 0) return;
        if (acc.count >= 1) {
            _pendingOutputToken0 = acc.tokens[0];
            IERC20(acc.tokens[0]).forceApprove(reactor, acc.totals[0]);
        }
        if (acc.count >= 2) {
            _pendingOutputToken1 = acc.tokens[1];
            IERC20(acc.tokens[1]).forceApprove(reactor, acc.totals[1]);
        }
        if (acc.count >= 3) {
            _pendingOutputToken2 = acc.tokens[2];
            IERC20(acc.tokens[2]).forceApprove(reactor, acc.totals[2]);
        }
        if (acc.count == 4) {
            _pendingOutputToken3 = acc.tokens[3];
            IERC20(acc.tokens[3]).forceApprove(reactor, acc.totals[3]);
        }
    }

    function _accumulateOutputToken(_OutputAccumulator memory acc, address token, uint256 amount)
        private
        pure
    {
        uint256 count = acc.count;
        if (count >= 1 && acc.tokens[0] == token) {
            acc.totals[0] += amount;
            return;
        }
        if (count >= 2 && acc.tokens[1] == token) {
            acc.totals[1] += amount;
            return;
        }
        if (count >= 3 && acc.tokens[2] == token) {
            acc.totals[2] += amount;
            return;
        }
        if (count == 4 && acc.tokens[3] == token) {
            acc.totals[3] += amount;
            return;
        }
        if (count >= _MAX_OUTPUT_TOKENS) {
            acc.overflowCount = count + 1;
            return;
        }
        acc.tokens[count] = token;
        acc.totals[count] = amount;
        acc.count = count + 1;
    }

    function _clearPendingOutputApprovals(address reactor) private {
        _clearSinglePendingApproval(_pendingOutputToken0, reactor);
        _clearSinglePendingApproval(_pendingOutputToken1, reactor);
        _clearSinglePendingApproval(_pendingOutputToken2, reactor);
        _clearSinglePendingApproval(_pendingOutputToken3, reactor);
        _pendingOutputToken0 = address(0);
        _pendingOutputToken1 = address(0);
        _pendingOutputToken2 = address(0);
        _pendingOutputToken3 = address(0);
    }

    function _clearSinglePendingApproval(address token, address reactor) private {
        if (token != address(0) && IERC20(token).allowance(address(this), reactor) > 0) {
            IERC20(token).forceApprove(reactor, 0);
        }
    }

    function _revertWithData(bytes memory ret, bytes4 fallbackSelector) private pure {
        if (ret.length > 0) {
            assembly ("memory-safe") {
                revert(add(ret, 32), mload(ret))
            }
        }
        assembly ("memory-safe") {
            mstore(0x00, fallbackSelector)
            revert(0x00, 0x04)
        }
    }

    function _checkFillerOperator() private view {
        if (fillerOperator == address(0) || msg.sender != fillerOperator) {
            revert UnauthorizedOperator(msg.sender);
        }
    }

    function _checkOwnerOrOperator() private view {
        if (msg.sender != owner() && (fillerOperator == address(0) || msg.sender != fillerOperator))
        {
            revert UnauthorizedOperator(msg.sender);
        }
    }

    function _checkNotPaused() private view {
        if (paused) revert VaultPaused();
    }
}
