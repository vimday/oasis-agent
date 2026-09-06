// SPDX-License-Identifier: BUSL-1.1
pragma solidity 0.8.26;

import { Ownable } from "@openzeppelin/contracts/access/Ownable.sol";
import { Ownable2Step } from "@openzeppelin/contracts/access/Ownable2Step.sol";
import { IHooks } from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import { IPoolManager } from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import { Hooks } from "@uniswap/v4-core/src/libraries/Hooks.sol";
import { LPFeeLibrary } from "@uniswap/v4-core/src/libraries/LPFeeLibrary.sol";
import { PoolId, PoolIdLibrary } from "@uniswap/v4-core/src/types/PoolId.sol";
import { PoolKey } from "@uniswap/v4-core/src/types/PoolKey.sol";
import { ModifyLiquidityParams, SwapParams } from "@uniswap/v4-core/src/types/PoolOperation.sol";
import { BalanceDelta } from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {
    BeforeSwapDelta,
    BeforeSwapDeltaLibrary
} from "@uniswap/v4-core/src/types/BeforeSwapDelta.sol";

import { IOasisPositionManager } from "./interfaces/IOasisPositionManager.sol";

/// @notice Minimal Phase-1 dynamic-fee hook: public swaps, permissioned LP additions, free removal.
/// @dev Governance is expected to be a timelock-controlled multisig. This contract holds no tokens
///      and deliberately exposes no arbitrary-call, upgrade, or withdrawal surface.
contract OasisDynamicFeeHook is IHooks, Ownable2Step {
    using PoolIdLibrary for PoolKey;

    /// @dev Governance safety ceiling. A lower cap limits both configuration mistakes and
    ///      malicious-governance blast radius; 100% fees can also make exact-output swaps revert.
    uint24 public constant MAX_CONFIGURED_FEE = 100_000;
    uint160 public constant REQUIRED_HOOK_FLAGS =
        Hooks.BEFORE_INITIALIZE_FLAG | Hooks.BEFORE_ADD_LIQUIDITY_FLAG | Hooks.BEFORE_SWAP_FLAG;
    uint48 public constant QUOTE_VALIDITY_SECONDS = 5;

    struct FeeConfig {
        uint24 baseFee;
        uint24 mediumFee;
        uint24 highFee;
        uint128 mediumAmount0Threshold;
        uint128 highAmount0Threshold;
        uint128 mediumAmount1Threshold;
        uint128 highAmount1Threshold;
        uint64 ruleVersion;
        bool configured;
    }

    error CallerNotPoolManager(address caller);
    error UnauthorizedInitializer(address sender);
    error WrongPositionManager(address sender);
    error InvalidPoolHook(address hook);
    error PoolMustUseDynamicFee(uint24 fee);
    error PoolNotConfigured(bytes32 poolId);
    error PoolBindingImmutable(bytes32 boundPoolId, bytes32 requestedPoolId);
    error InvalidFeeConfiguration();
    error LiquidityAdditionPaused();
    error InvalidPositionSalt();
    error PositionNotWhitelisted(uint256 tokenId, address owner);
    error PositionPoolMismatch(uint256 tokenId);
    error PositionRangeMismatch(uint256 tokenId);
    error UnauthorizedGuardian(address caller);
    error ZeroAddress();
    error ForbiddenWhitelistAccount(address account);
    error NoStateChange();
    error HookNotImplemented();

    event WhitelistSet(address indexed account, bool allowed, address indexed actor);
    event GuardianSet(address indexed oldGuardian, address indexed newGuardian);
    event RiskPauseSet(bool paused, address indexed actor);
    event FeeConfigSet(bytes32 indexed poolId, uint64 indexed ruleVersion, FeeConfig config);

    IPoolManager public immutable POOL_MANAGER;
    IOasisPositionManager public immutable POSITION_MANAGER;
    address public immutable INITIALIZER;

    address public guardian;
    bool public riskPaused;
    bytes32 public boundPoolId;
    mapping(address account => bool allowed) public isWhitelisted;
    mapping(bytes32 poolId => FeeConfig config) private _feeConfigs;

    constructor(
        IPoolManager poolManager_,
        IOasisPositionManager positionManager_,
        address initializer_,
        address initialOwner_,
        address guardian_
    ) Ownable(initialOwner_) {
        if (
            address(poolManager_) == address(0) || address(positionManager_) == address(0)
                || initializer_ == address(0) || initialOwner_ == address(0)
                || guardian_ == address(0)
        ) revert ZeroAddress();
        POOL_MANAGER = poolManager_;
        POSITION_MANAGER = positionManager_;
        INITIALIZER = initializer_;
        guardian = guardian_;
        Hooks.validateHookPermissions(IHooks(address(this)), getHookPermissions());
    }

    modifier onlyPoolManager() {
        _checkPoolManager();
        _;
    }

    function _checkPoolManager() private view {
        if (msg.sender != address(POOL_MANAGER)) revert CallerNotPoolManager(msg.sender);
    }

    function getHookPermissions() public pure returns (Hooks.Permissions memory) {
        return Hooks.Permissions({
            beforeInitialize: true,
            afterInitialize: false,
            beforeAddLiquidity: true,
            afterAddLiquidity: false,
            beforeRemoveLiquidity: false,
            afterRemoveLiquidity: false,
            beforeSwap: true,
            afterSwap: false,
            beforeDonate: false,
            afterDonate: false,
            beforeSwapReturnDelta: false,
            afterSwapReturnDelta: false,
            afterAddLiquidityReturnDelta: false,
            afterRemoveLiquidityReturnDelta: false
        });
    }

    function feeConfig(PoolKey calldata key) external view returns (FeeConfig memory) {
        _validatePoolKey(key);
        bytes32 poolId = PoolId.unwrap(key.toId());
        _requireBoundPool(poolId);
        return _feeConfigs[poolId];
    }

    function setWhitelist(address account, bool allowed) external onlyOwner {
        _setWhitelist(account, allowed);
    }

    /// @notice The guardian may only reduce authority; it cannot add an LP.
    function guardianRevoke(address account) external {
        if (msg.sender != guardian) revert UnauthorizedGuardian(msg.sender);
        _setWhitelist(account, false);
    }

    function setGuardian(address newGuardian) external onlyOwner {
        if (newGuardian == address(0)) revert ZeroAddress();
        address oldGuardian = guardian;
        if (newGuardian == oldGuardian) revert NoStateChange();
        guardian = newGuardian;
        emit GuardianSet(oldGuardian, newGuardian);
    }

    /// @notice Governance may pause or unpause after review.
    function setRiskPaused(bool paused) external onlyOwner {
        _setRiskPaused(paused);
    }

    /// @notice The guardian may immediately stop additions and force base-fee fallback.
    function emergencyPause() external {
        if (msg.sender != guardian) revert UnauthorizedGuardian(msg.sender);
        _setRiskPaused(true);
    }

    function configurePool(PoolKey calldata key, FeeConfig calldata config) external onlyOwner {
        _validatePoolKey(key);
        if (
            config.baseFee > config.mediumFee || config.mediumFee > config.highFee
                || config.highFee > MAX_CONFIGURED_FEE || config.mediumAmount0Threshold == 0
                || config.mediumAmount0Threshold >= config.highAmount0Threshold
                || config.mediumAmount1Threshold == 0
                || config.mediumAmount1Threshold >= config.highAmount1Threshold
                || config.ruleVersion == 0 || !config.configured
        ) revert InvalidFeeConfiguration();
        bytes32 poolId = PoolId.unwrap(key.toId());
        if (boundPoolId == bytes32(0)) {
            boundPoolId = poolId;
        } else {
            _requireBoundPool(poolId);
        }
        FeeConfig memory previous = _feeConfigs[poolId];
        if (previous.configured && config.ruleVersion <= previous.ruleVersion) {
            revert InvalidFeeConfiguration();
        }
        _feeConfigs[poolId] = config;
        emit FeeConfigSet(poolId, config.ruleVersion, config);
    }

    function previewFee(PoolKey calldata key, SwapParams calldata params)
        public
        view
        returns (uint24 feePips, uint64 ruleVersion, bytes32 stateHash, uint256 validUntilTimestamp)
    {
        _validatePoolKey(key);
        bytes32 poolId = PoolId.unwrap(key.toId());
        _requireBoundPool(poolId);
        FeeConfig memory config = _feeConfigs[poolId];
        if (!config.configured) revert PoolNotConfigured(poolId);
        uint256 magnitude = _absolute(params.amountSpecified);
        // v4 denominates amountSpecified in the input asset for exact-input swaps and the output
        // asset for exact-output swaps. Thresholds therefore must be explicit per token; comparing
        // raw units across currencies with different decimals is economically invalid.
        bool exactInput = params.amountSpecified < 0;
        bool specifiedCurrency0 = params.zeroForOne == exactInput;
        uint128 mediumThreshold =
            specifiedCurrency0 ? config.mediumAmount0Threshold : config.mediumAmount1Threshold;
        uint128 highThreshold =
            specifiedCurrency0 ? config.highAmount0Threshold : config.highAmount1Threshold;
        feePips = riskPaused
            ? config.baseFee
            : magnitude >= highThreshold
                ? config.highFee
                : magnitude >= mediumThreshold ? config.mediumFee : config.baseFee;
        ruleVersion = config.ruleVersion;
        stateHash = keccak256(abi.encode(block.chainid, address(this), poolId, config, riskPaused));
        validUntilTimestamp = block.timestamp + QUOTE_VALIDITY_SECONDS;
    }

    function beforeInitialize(address sender, PoolKey calldata key, uint160)
        external
        view
        onlyPoolManager
        returns (bytes4)
    {
        if (sender != INITIALIZER) revert UnauthorizedInitializer(sender);
        _validatePoolKey(key);
        bytes32 poolId = PoolId.unwrap(key.toId());
        _requireBoundPool(poolId);
        if (!_feeConfigs[poolId].configured) revert PoolNotConfigured(poolId);
        return IHooks.beforeInitialize.selector;
    }

    function beforeAddLiquidity(
        address sender,
        PoolKey calldata key,
        ModifyLiquidityParams calldata params,
        bytes calldata
    ) external view onlyPoolManager returns (bytes4) {
        if (riskPaused) revert LiquidityAdditionPaused();
        _validatePoolKey(key);
        bytes32 poolId = PoolId.unwrap(key.toId());
        _requireBoundPool(poolId);
        if (!_feeConfigs[poolId].configured) revert PoolNotConfigured(poolId);
        if (sender != address(POSITION_MANAGER)) revert WrongPositionManager(sender);
        if (params.liquidityDelta <= 0) revert InvalidFeeConfiguration();
        uint256 tokenId = uint256(params.salt);
        if (tokenId == 0) revert InvalidPositionSalt();
        address positionOwner = POSITION_MANAGER.ownerOf(tokenId);
        if (!isWhitelisted[positionOwner]) revert PositionNotWhitelisted(tokenId, positionOwner);

        (PoolKey memory recordedKey, uint256 info) =
            POSITION_MANAGER.getPoolAndPositionInfo(tokenId);
        if (PoolId.unwrap(recordedKey.toId()) != PoolId.unwrap(key.toId())) {
            revert PositionPoolMismatch(tokenId);
        }
        int24 recordedLower = _packedInt24(info, 8);
        int24 recordedUpper = _packedInt24(info, 32);
        if (recordedLower != params.tickLower || recordedUpper != params.tickUpper) {
            revert PositionRangeMismatch(tokenId);
        }
        return IHooks.beforeAddLiquidity.selector;
    }

    function beforeSwap(address, PoolKey calldata key, SwapParams calldata params, bytes calldata)
        external
        view
        onlyPoolManager
        returns (bytes4, BeforeSwapDelta, uint24)
    {
        (uint24 feePips,,,) = previewFee(key, params);
        return (
            IHooks.beforeSwap.selector,
            BeforeSwapDeltaLibrary.ZERO_DELTA,
            feePips | LPFeeLibrary.OVERRIDE_FEE_FLAG
        );
    }

    function afterInitialize(address, PoolKey calldata, uint160, int24)
        external
        pure
        returns (bytes4)
    {
        revert HookNotImplemented();
    }

    function afterAddLiquidity(
        address,
        PoolKey calldata,
        ModifyLiquidityParams calldata,
        BalanceDelta,
        BalanceDelta,
        bytes calldata
    ) external pure returns (bytes4, BalanceDelta) {
        revert HookNotImplemented();
    }

    function beforeRemoveLiquidity(
        address,
        PoolKey calldata,
        ModifyLiquidityParams calldata,
        bytes calldata
    ) external pure returns (bytes4) {
        revert HookNotImplemented();
    }

    function afterRemoveLiquidity(
        address,
        PoolKey calldata,
        ModifyLiquidityParams calldata,
        BalanceDelta,
        BalanceDelta,
        bytes calldata
    ) external pure returns (bytes4, BalanceDelta) {
        revert HookNotImplemented();
    }

    function afterSwap(address, PoolKey calldata, SwapParams calldata, BalanceDelta, bytes calldata)
        external
        pure
        returns (bytes4, int128)
    {
        revert HookNotImplemented();
    }

    function beforeDonate(address, PoolKey calldata, uint256, uint256, bytes calldata)
        external
        pure
        returns (bytes4)
    {
        revert HookNotImplemented();
    }

    function afterDonate(address, PoolKey calldata, uint256, uint256, bytes calldata)
        external
        pure
        returns (bytes4)
    {
        revert HookNotImplemented();
    }

    function _setWhitelist(address account, bool allowed) private {
        if (account == address(0)) revert ZeroAddress();
        if (
            account == address(this) || account == address(POOL_MANAGER)
                || account == address(POSITION_MANAGER)
        ) revert ForbiddenWhitelistAccount(account);
        if (isWhitelisted[account] == allowed) revert NoStateChange();
        isWhitelisted[account] = allowed;
        emit WhitelistSet(account, allowed, msg.sender);
    }

    function _setRiskPaused(bool paused) private {
        if (riskPaused == paused) revert NoStateChange();
        riskPaused = paused;
        emit RiskPauseSet(paused, msg.sender);
    }

    function _validatePoolKey(PoolKey calldata key) private view {
        if (address(key.hooks) != address(this)) revert InvalidPoolHook(address(key.hooks));
        if (key.fee != LPFeeLibrary.DYNAMIC_FEE_FLAG) revert PoolMustUseDynamicFee(key.fee);
    }

    function _requireBoundPool(bytes32 poolId) private view {
        if (poolId != boundPoolId) revert PoolBindingImmutable(boundPoolId, poolId);
    }

    function _packedInt24(uint256 value, uint256 shift) private pure returns (int24 result) {
        assembly ("memory-safe") {
            result := signextend(2, shr(shift, value))
        }
    }

    function _absolute(int256 value) private pure returns (uint256 result) {
        assembly ("memory-safe") {
            switch slt(value, 0)
            case 0 { result := value }
            default { result := add(not(value), 1) }
        }
    }
}
