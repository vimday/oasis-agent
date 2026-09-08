// SPDX-License-Identifier: BUSL-1.1
pragma solidity 0.8.26;

import { Script } from "forge-std/Script.sol";
import { IERC20 } from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import { SafeERC20 } from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import { SafeCast } from "@openzeppelin/contracts/utils/math/SafeCast.sol";
import { IPoolManager } from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import { IHooks } from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import { LPFeeLibrary } from "@uniswap/v4-core/src/libraries/LPFeeLibrary.sol";
import { TickMath } from "@uniswap/v4-core/src/libraries/TickMath.sol";
import { Currency } from "@uniswap/v4-core/src/types/Currency.sol";
import { PoolId, PoolIdLibrary } from "@uniswap/v4-core/src/types/PoolId.sol";
import { PoolKey } from "@uniswap/v4-core/src/types/PoolKey.sol";
import { PositionManager } from "@uniswap/v4-periphery/src/PositionManager.sol";
import { IStateView } from "@uniswap/v4-periphery/src/interfaces/IStateView.sol";
import { IAllowanceTransfer } from "permit2/src/interfaces/IAllowanceTransfer.sol";

import { OasisDynamicFeeHook } from "../src/OasisDynamicFeeHook.sol";
import { OasisPositionExecutor } from "../src/OasisPositionExecutor.sol";

/// @notice Creates the first bounded ERC-20/ERC-20 LP canary with an external signer.
/// @dev The script never reads a private key. It refuses existing token/Permit2 allowances,
///      grants only the configured maxima with a short expiry, mints the position, revokes
///      both allowance layers, then grants this NFT alone to the bounded Oasis executor.
contract MintOasisCanary is Script {
    using PoolIdLibrary for PoolKey;
    using SafeERC20 for IERC20;
    using SafeCast for int256;
    using SafeCast for uint256;

    error WrongChain(uint256 actual, uint256 expected);
    error InvalidConfiguration();
    error CodeHashMismatch();
    error BindingMismatch();
    error PoolUnavailable();
    error UnsafeExistingAllowance();
    error InsufficientBalance();
    error UnexpectedBalanceChange();
    error PostconditionFailed();

    struct CanaryConfig {
        uint256 chainId;
        address lpOwner;
        address governor;
        address guardian;
        IPoolManager poolManager;
        PositionManager positionManager;
        IStateView stateView;
        IAllowanceTransfer permit2;
        OasisDynamicFeeHook hook;
        OasisPositionExecutor executor;
        IERC20 token0;
        IERC20 token1;
        int24 tickSpacing;
        int24 tickLower;
        int24 tickUpper;
        uint256 liquidity;
        uint128 amount0Max;
        uint128 amount1Max;
        uint48 permitExpiry;
        uint256 deadline;
        bytes32 poolManagerHash;
        bytes32 positionManagerHash;
        bytes32 stateViewHash;
        bytes32 permit2Hash;
        bytes32 hookHash;
        bytes32 executorHash;
        bytes32 token0Hash;
        bytes32 token1Hash;
    }

    function run() external returns (uint256 tokenId, uint256 spent0, uint256 spent1) {
        CanaryConfig memory config = _loadConfig();
        PoolKey memory key = _validate(config);
        tokenId = config.positionManager.nextTokenId();
        uint256 balance0Before = config.token0.balanceOf(config.lpOwner);
        uint256 balance1Before = config.token1.balanceOf(config.lpOwner);
        if (balance0Before < config.amount0Max || balance1Before < config.amount1Max) {
            revert InsufficientBalance();
        }
        if (
            config.token0.allowance(config.lpOwner, address(config.permit2)) != 0
                || config.token1.allowance(config.lpOwner, address(config.permit2)) != 0
        ) revert UnsafeExistingAllowance();
        (uint160 permitAmount0,,) = config.permit2
            .allowance(config.lpOwner, address(config.token0), address(config.positionManager));
        (uint160 permitAmount1,,) = config.permit2
            .allowance(config.lpOwner, address(config.token1), address(config.positionManager));
        if (permitAmount0 != 0 || permitAmount1 != 0) revert UnsafeExistingAllowance();

        vm.startBroadcast(config.lpOwner);
        config.token0.forceApprove(address(config.permit2), config.amount0Max);
        config.token1.forceApprove(address(config.permit2), config.amount1Max);
        config.permit2
            .approve(
                address(config.token0),
                address(config.positionManager),
                uint160(config.amount0Max),
                config.permitExpiry
            );
        config.permit2
            .approve(
                address(config.token1),
                address(config.positionManager),
                uint160(config.amount1Max),
                config.permitExpiry
            );
        bytes[] memory params = new bytes[](3);
        params[0] = abi.encode(
            key,
            config.tickLower,
            config.tickUpper,
            config.liquidity,
            config.amount0Max,
            config.amount1Max,
            config.lpOwner,
            bytes("")
        );
        params[1] = abi.encode(key.currency0);
        params[2] = abi.encode(key.currency1);
        config.positionManager.modifyLiquidities(abi.encode(hex"021212", params), config.deadline);
        config.permit2.approve(address(config.token0), address(config.positionManager), 0, 0);
        config.permit2.approve(address(config.token1), address(config.positionManager), 0, 0);
        config.token0.forceApprove(address(config.permit2), 0);
        config.token1.forceApprove(address(config.permit2), 0);
        config.positionManager.approve(address(config.executor), tokenId);
        vm.stopBroadcast();

        uint256 balance0After = config.token0.balanceOf(config.lpOwner);
        uint256 balance1After = config.token1.balanceOf(config.lpOwner);
        if (balance0After > balance0Before || balance1After > balance1Before) {
            revert UnexpectedBalanceChange();
        }
        spent0 = balance0Before - balance0After;
        spent1 = balance1Before - balance1After;
        _verifyPostconditions(config, tokenId);
    }

    function _validate(CanaryConfig memory config) private view returns (PoolKey memory key) {
        if (block.chainid != config.chainId) revert WrongChain(block.chainid, config.chainId);
        if (
            config.lpOwner == address(0) || config.governor == address(0)
                || config.guardian == address(0) || address(config.poolManager) == address(0)
                || address(config.positionManager) == address(0)
                || address(config.stateView) == address(0) || address(config.permit2) == address(0)
                || address(config.hook) == address(0) || address(config.executor) == address(0)
                || address(config.token0) == address(0)
                || address(config.token0) >= address(config.token1) || config.tickSpacing <= 0
                || config.governor == config.guardian || config.governor == config.lpOwner
                || config.guardian == config.lpOwner || config.liquidity == 0
                || config.amount0Max == 0 || config.amount1Max == 0
                || config.tickLower >= config.tickUpper || config.tickLower < TickMath.MIN_TICK
                || config.tickUpper > TickMath.MAX_TICK
                || config.tickLower % config.tickSpacing != 0
                || config.tickUpper % config.tickSpacing != 0
                // A short explicit deadline bounds a canary transaction. Small validator timestamp
                // movement cannot bypass the capital caps, pool binding, or owner signature.
                // forge-lint: disable-next-line(block-timestamp)
                || config.deadline < block.timestamp || config.permitExpiry < config.deadline
        ) {
            revert InvalidConfiguration();
        }
        if (
            address(config.poolManager).codehash != config.poolManagerHash
                || address(config.positionManager).codehash != config.positionManagerHash
                || address(config.stateView).codehash != config.stateViewHash
                || address(config.permit2).codehash != config.permit2Hash
                || address(config.hook).codehash != config.hookHash
                || address(config.executor).codehash != config.executorHash
                || address(config.token0).codehash != config.token0Hash
                || address(config.token1).codehash != config.token1Hash
        ) revert CodeHashMismatch();
        if (
            address(config.positionManager.poolManager()) != address(config.poolManager)
                || address(config.positionManager.permit2()) != address(config.permit2)
                || address(config.stateView.poolManager()) != address(config.poolManager)
                || address(config.hook.POOL_MANAGER()) != address(config.poolManager)
                || address(config.hook.POSITION_MANAGER()) != address(config.positionManager)
                || config.hook.owner() != config.governor
                || config.hook.guardian() != config.guardian || config.hook.riskPaused()
                || address(config.executor.POOL_MANAGER()) != address(config.poolManager)
                || address(config.executor.POSITION_MANAGER()) != address(config.positionManager)
                || config.executor.owner() != config.lpOwner
                || config.executor.guardian() != config.guardian || config.executor.paused()
                || config.executor.MAX_LIQUIDITY() < config.liquidity
        ) revert BindingMismatch();

        key = PoolKey({
            currency0: Currency.wrap(address(config.token0)),
            currency1: Currency.wrap(address(config.token1)),
            fee: LPFeeLibrary.DYNAMIC_FEE_FLAG,
            tickSpacing: config.tickSpacing,
            hooks: IHooks(address(config.hook))
        });
        PoolId poolId = key.toId();
        (uint160 sqrtPriceX96, int24 tick,,) = config.stateView.getSlot0(poolId);
        OasisDynamicFeeHook.FeeConfig memory fee = config.hook.feeConfig(key);
        if (
            sqrtPriceX96 == 0 || tick < config.tickLower || tick >= config.tickUpper
                || config.hook.boundPoolId() != PoolId.unwrap(poolId) || !fee.configured
                || !config.hook.isWhitelisted(config.lpOwner)
        ) revert PoolUnavailable();
    }

    function _verifyPostconditions(CanaryConfig memory config, uint256 tokenId) private view {
        (uint160 amount0, uint48 expiry0,) = config.permit2
            .allowance(config.lpOwner, address(config.token0), address(config.positionManager));
        (uint160 amount1, uint48 expiry1,) = config.permit2
            .allowance(config.lpOwner, address(config.token1), address(config.positionManager));
        if (
            config.positionManager.ownerOf(tokenId) != config.lpOwner
                || config.positionManager.getPositionLiquidity(tokenId) != config.liquidity
                || config.positionManager.getApproved(tokenId) != address(config.executor)
                || amount0 != 0 || amount1 != 0 || expiry0 != 0 || expiry1 != 0
                || config.token0.allowance(config.lpOwner, address(config.permit2)) != 0
                || config.token1.allowance(config.lpOwner, address(config.permit2)) != 0
        ) revert PostconditionFailed();
    }

    function _loadConfig() private view returns (CanaryConfig memory config) {
        config = CanaryConfig({
            chainId: vm.envUint("OASIS_CHAIN_ID"),
            lpOwner: vm.envAddress("OASIS_LP_OWNER"),
            governor: vm.envAddress("OASIS_GOVERNOR"),
            guardian: vm.envAddress("OASIS_GUARDIAN"),
            poolManager: IPoolManager(vm.envAddress("OASIS_POOL_MANAGER")),
            positionManager: PositionManager(payable(vm.envAddress("OASIS_POSITION_MANAGER"))),
            stateView: IStateView(vm.envAddress("OASIS_STATE_VIEW")),
            permit2: IAllowanceTransfer(vm.envAddress("OASIS_PERMIT2")),
            hook: OasisDynamicFeeHook(vm.envAddress("OASIS_HOOK")),
            executor: OasisPositionExecutor(vm.envAddress("OASIS_EXECUTOR")),
            token0: IERC20(vm.envAddress("OASIS_TOKEN0")),
            token1: IERC20(vm.envAddress("OASIS_TOKEN1")),
            tickSpacing: int256(vm.envInt("OASIS_TICK_SPACING")).toInt24(),
            tickLower: int256(vm.envInt("OASIS_TICK_LOWER")).toInt24(),
            tickUpper: int256(vm.envInt("OASIS_TICK_UPPER")).toInt24(),
            liquidity: vm.envUint("OASIS_LIQUIDITY"),
            amount0Max: vm.envUint("OASIS_AMOUNT0_MAX").toUint128(),
            amount1Max: vm.envUint("OASIS_AMOUNT1_MAX").toUint128(),
            permitExpiry: vm.envUint("OASIS_PERMIT_EXPIRY").toUint48(),
            deadline: vm.envUint("OASIS_DEADLINE"),
            poolManagerHash: vm.envBytes32("OASIS_POOL_MANAGER_HASH"),
            positionManagerHash: vm.envBytes32("OASIS_POSITION_MANAGER_HASH"),
            stateViewHash: vm.envBytes32("OASIS_STATE_VIEW_HASH"),
            permit2Hash: vm.envBytes32("OASIS_PERMIT2_HASH"),
            hookHash: vm.envBytes32("OASIS_HOOK_HASH"),
            executorHash: vm.envBytes32("OASIS_EXECUTOR_HASH"),
            token0Hash: vm.envBytes32("OASIS_TOKEN0_HASH"),
            token1Hash: vm.envBytes32("OASIS_TOKEN1_HASH")
        });
    }
}
