// SPDX-License-Identifier: BUSL-1.1
pragma solidity 0.8.26;

import { Ownable } from "@openzeppelin/contracts/access/Ownable.sol";
import { Ownable2Step } from "@openzeppelin/contracts/access/Ownable2Step.sol";
import { IPoolManager } from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import { TickMath } from "@uniswap/v4-core/src/libraries/TickMath.sol";
import { PoolId, PoolIdLibrary } from "@uniswap/v4-core/src/types/PoolId.sol";
import { PoolKey } from "@uniswap/v4-core/src/types/PoolKey.sol";

import { PositionManager } from "@uniswap/v4-periphery/src/PositionManager.sol";
import { Actions } from "@uniswap/v4-periphery/src/libraries/Actions.sol";
import { ActionConstants } from "@uniswap/v4-periphery/src/libraries/ActionConstants.sol";

/// @notice Bounded atomic rebalance executor for the Phase-1 v4 LP system (blueprint 4.3/6.5).
/// @dev This is not a general wallet or arbitrary-call proxy. It performs exactly two
///      flows from explicit owner-signed intents, atomically within one pinned
///      PositionManager `modifyLiquidities` call:
///
///      - RECENTER: burn the old position (collecting principal and fees), mint a new
///        range from the proceeds, and take any surplus directly to the owner;
///      - REMOVE: burn the old position and take every token to the owner.
///
///      A RECENTER can never consume more than the removed proceeds: the bundle ends
///      with `TAKE(OPEN_DELTA)` for both currencies, and the pinned PositionManager
///      reverts the whole transaction if a net delta is negative. The pinned periphery
///      does not support swap actions inside `modifyLiquidities`, so any intent with a
///      nonzero bounded swap input is rejected instead of being executed as a naked leg.
///
///      The contract holds no tokens between transactions and has no receive surface;
///      proceeds always leave through the atomic TAKE to the owner. The LP owner must
///      approve this contract on the PositionManager NFT before execution. Only the
///      owner may submit intents; a guardian may only pause. Nonces are strictly
///      increasing so a retry can never create a second economic rebalance.
contract OasisPositionExecutor is Ownable2Step {
    using PoolIdLibrary for PoolKey;

    /// @notice Atomic flow selector. RECENTER burns and re-mints; REMOVE unwinds fully.
    enum Mode {
        RECENTER,
        REMOVE
    }

    /// @notice Explicit owner-signed intent. Every field is validated before the bundle
    ///      is built; no field is free-form calldata passed to an arbitrary target.
    struct Intent {
        uint64 nonce;
        uint64 deadline;
        Mode mode;
        PoolKey poolKey;
        uint256 oldTokenId;
        int24 tickLower;
        int24 tickUpper;
        uint256 liquidity;
        uint128 amount0Min;
        uint128 amount1Min;
        uint128 amount0Max;
        uint128 amount1Max;
        uint256 maxSwapInput;
        bytes hookData;
    }

    error StaleNonce(uint64 nonce, uint64 lastNonce);
    error DeadlinePassed(uint64 deadline);
    error PoolBindingImmutable(bytes32 boundPoolId, bytes32 requestedPoolId);
    error SwapLegUnsupported(uint256 maxSwapInput);
    error NoExistingPosition();
    error NotOwnerPosition(uint256 tokenId);
    error PositionPoolMismatch(uint256 tokenId);
    error InvalidRange(int24 tickLower, int24 tickUpper);
    error InvalidLiquidity(uint256 liquidity);
    error LiquidityAboveMax(uint256 liquidity, uint256 maxLiquidity);
    error UnauthorizedGuardian(address caller);
    error MismatchedPoolManager();
    error ZeroAddress();
    error NoStateChange();
    error ExecutorPaused();

    event IntentExecuted(
        uint64 indexed nonce,
        bytes32 indexed intentHash,
        Mode indexed mode,
        uint256 oldTokenId,
        uint256 newTokenId,
        int24 tickLower,
        int24 tickUpper,
        uint256 liquidity,
        address actor
    );
    event PausedSet(bool paused, address indexed actor);
    event GuardianSet(address indexed oldGuardian, address indexed newGuardian);

    IPoolManager public immutable POOL_MANAGER;
    PositionManager public immutable POSITION_MANAGER;
    uint256 public immutable MAX_LIQUIDITY;

    address public guardian;
    bool public paused;
    bytes32 public boundPoolId;
    uint64 public lastNonce;
    bytes32[] public history;
    mapping(uint64 nonce => bytes32 intentHash) public executedIntents;

    constructor(
        IPoolManager poolManager_,
        PositionManager positionManager_,
        address initialOwner_,
        address guardian_,
        uint256 maxLiquidity_
    ) Ownable(initialOwner_) {
        if (
            address(poolManager_) == address(0) || address(positionManager_) == address(0)
                || initialOwner_ == address(0) || guardian_ == address(0) || maxLiquidity_ == 0
        ) revert ZeroAddress();
        if (address(positionManager_.poolManager()) != address(poolManager_)) {
            revert MismatchedPoolManager();
        }
        POOL_MANAGER = poolManager_;
        POSITION_MANAGER = positionManager_;
        MAX_LIQUIDITY = maxLiquidity_;
        guardian = guardian_;
    }

    function setPaused(bool paused_) external onlyOwner {
        if (paused == paused_) revert NoStateChange();
        paused = paused_;
        emit PausedSet(paused_, msg.sender);
    }

    /// @notice The guardian may only halt; it cannot resume or tighten limits itself.
    function emergencyPause() external {
        if (msg.sender != guardian) revert UnauthorizedGuardian(msg.sender);
        if (paused) revert NoStateChange();
        paused = true;
        emit PausedSet(true, msg.sender);
    }

    function setGuardian(address newGuardian) external onlyOwner {
        if (newGuardian == address(0)) revert ZeroAddress();
        address oldGuardian = guardian;
        if (newGuardian == oldGuardian) revert NoStateChange();
        guardian = newGuardian;
        emit GuardianSet(oldGuardian, newGuardian);
    }

    function intentHash(Intent calldata intent) public view returns (bytes32) {
        return keccak256(abi.encode(block.chainid, address(this), intent));
    }

    function execute(Intent calldata intent) external onlyOwner {
        if (paused) revert ExecutorPaused();
        if (intent.nonce <= lastNonce) revert StaleNonce(intent.nonce, lastNonce);
        // The canonical v4-periphery deadline check (PositionManager.checkDeadline); a 60-second
        // rebalance deadline window is immune to the bounded timestamp manipulation validators can
        // apply, and the whole intent is owner-signed, so this is expiry enforcement, not an oracle.
        // slither-disable-next-line timestamp
        // forge-lint: disable-next-line(block-timestamp)
        if (block.timestamp > intent.deadline) revert DeadlinePassed(intent.deadline);

        bytes32 poolId = PoolId.unwrap(intent.poolKey.toId());
        if (boundPoolId == bytes32(0)) {
            // The pool is bound on the first successful execution and immutable after.
            boundPoolId = poolId;
        } else if (poolId != boundPoolId) {
            revert PoolBindingImmutable(boundPoolId, poolId);
        }

        // The pinned PositionManager bundles liquidity and settlement actions only; a
        // swap leg is not representable, so a nonzero intent is rejected rather than
        // executed as an unbounded naked leg (blueprint 6.5).
        if (intent.maxSwapInput != 0) revert SwapLegUnsupported(intent.maxSwapInput);

        if (intent.oldTokenId == 0) revert NoExistingPosition();
        if (POSITION_MANAGER.ownerOf(intent.oldTokenId) != owner()) {
            revert NotOwnerPosition(intent.oldTokenId);
        }
        // The second return value (position info) is deliberately discarded: the burn action's
        // min-out slippage checks are the authoritative protection for the removed amounts, the
        // pool identity is validated via the returned PoolKey above, and the position's recorded
        // tick range is irrelevant to both flows (RECENTER mints the signed new range; REMOVE
        // always burns the full position).
        // slither-disable-next-line unused-return
        // forge-lint: disable-next-line(unused-return)
        (PoolKey memory recordedKey,) = POSITION_MANAGER.getPoolAndPositionInfo(intent.oldTokenId);
        if (PoolId.unwrap(recordedKey.toId()) != poolId) {
            revert PositionPoolMismatch(intent.oldTokenId);
        }

        if (intent.mode == Mode.RECENTER) {
            _validateRange(intent);
        } else if (intent.liquidity != 0) {
            revert InvalidLiquidity(intent.liquidity);
        }

        uint256 newTokenId = intent.mode == Mode.RECENTER ? POSITION_MANAGER.nextTokenId() : 0;
        (bytes memory actions, bytes[] memory params) = _buildBundle(intent);

        // Effects and the event are recorded before the pinned call. A revert rolls back both
        // the effects and the event, so the audit trail is only visible on success; emitting
        // first also keeps the log from being reordered by a reentrant callback.
        bytes32 hash = intentHash(intent);
        executedIntents[intent.nonce] = hash;
        history.push(hash);
        lastNonce = intent.nonce;

        // The emit precedes the only state-changing external call (modifyLiquidities); the pinned
        // PositionManager never calls back into this contract, and a revert rolls the event back
        // with the effects, so no log can be reordered or fabricated by a reentrant callback.
        // forge-lint: disable-start(reentrancy-events)
        emit IntentExecuted(
            intent.nonce,
            hash,
            intent.mode,
            intent.oldTokenId,
            newTokenId,
            intent.tickLower,
            intent.tickUpper,
            intent.liquidity,
            msg.sender
        );
        // forge-lint: disable-end(reentrancy-events)

        POSITION_MANAGER.modifyLiquidities(abi.encode(actions, params), intent.deadline);
    }

    function _buildBundle(Intent calldata intent)
        private
        view
        returns (bytes memory actions, bytes[] memory params)
    {
        address recipient = owner();
        if (intent.mode == Mode.REMOVE) {
            actions = abi.encodePacked(
                _action(Actions.BURN_POSITION), _action(Actions.TAKE), _action(Actions.TAKE)
            );
            params = new bytes[](3);
            params[0] = abi.encode(
                intent.oldTokenId, intent.amount0Min, intent.amount1Min, intent.hookData
            );
        } else {
            actions = abi.encodePacked(
                _action(Actions.BURN_POSITION),
                _action(Actions.MINT_POSITION),
                _action(Actions.TAKE),
                _action(Actions.TAKE)
            );
            params = new bytes[](4);
            params[0] = abi.encode(
                intent.oldTokenId, intent.amount0Min, intent.amount1Min, intent.hookData
            );
            params[1] = abi.encode(
                intent.poolKey,
                intent.tickLower,
                intent.tickUpper,
                intent.liquidity,
                intent.amount0Max,
                intent.amount1Max,
                recipient,
                intent.hookData
            );
        }
        // OPEN_DELTA takes the full remaining positive delta for each currency; a
        // negative net delta reverts inside the pinned PositionManager, so the whole
        // bundle fails closed rather than carrying a naked leg.
        params[params.length - 2] =
            abi.encode(intent.poolKey.currency0, recipient, ActionConstants.OPEN_DELTA);
        params[params.length - 1] =
            abi.encode(intent.poolKey.currency1, recipient, ActionConstants.OPEN_DELTA);
    }

    function _validateRange(Intent calldata intent) private view {
        if (intent.liquidity == 0) revert InvalidLiquidity(intent.liquidity);
        if (intent.liquidity > MAX_LIQUIDITY) {
            revert LiquidityAboveMax(intent.liquidity, MAX_LIQUIDITY);
        }
        int24 spacing = intent.poolKey.tickSpacing;
        if (
            intent.tickLower >= intent.tickUpper || intent.tickLower < TickMath.MIN_TICK
                || intent.tickUpper > TickMath.MAX_TICK || intent.tickLower % spacing != 0
                || intent.tickUpper % spacing != 0
        ) revert InvalidRange(intent.tickLower, intent.tickUpper);
    }

    function _action(uint256 action) private pure returns (bytes1) {
        require(action <= type(uint8).max, "ACTION_OVERFLOW");
        // The explicit bound above proves this narrowing conversion is lossless.
        // forge-lint: disable-next-line(unsafe-typecast)
        return bytes1(uint8(action));
    }
}
