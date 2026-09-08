// SPDX-License-Identifier: BUSL-1.1
pragma solidity 0.8.26;

import { Script } from "forge-std/Script.sol";
import { SafeCast } from "@openzeppelin/contracts/utils/math/SafeCast.sol";
import { IHooks } from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import { IPoolManager } from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import { LPFeeLibrary } from "@uniswap/v4-core/src/libraries/LPFeeLibrary.sol";
import { PoolId, PoolIdLibrary } from "@uniswap/v4-core/src/types/PoolId.sol";
import { Currency } from "@uniswap/v4-core/src/types/Currency.sol";
import { PoolKey } from "@uniswap/v4-core/src/types/PoolKey.sol";
import { PositionManager } from "@uniswap/v4-periphery/src/PositionManager.sol";

import { OasisDynamicFeeHook } from "../src/OasisDynamicFeeHook.sol";

/// @notice Configure, whitelist, and initialize one immutable Oasis pool.
/// @dev This script reads no private key. The configured governor must be the external signer.
contract ConfigureOasisPool is Script {
    using PoolIdLibrary for PoolKey;
    using SafeCast for int256;
    using SafeCast for uint256;

    error WrongChain(uint256 actual, uint256 expected);
    error InvalidConfiguration();
    error HookBindingMismatch();
    error CodeHashMismatch();
    error PostconditionFailed();

    struct DeploymentConfig {
        uint256 chainId;
        address governor;
        address guardian;
        address lpOwner;
        IPoolManager poolManager;
        PositionManager positionManager;
        OasisDynamicFeeHook hook;
        address token0;
        address token1;
        int24 tickSpacing;
        uint160 initialSqrtPriceX96;
        bytes32 poolManagerHash;
        bytes32 positionManagerHash;
        bytes32 hookHash;
        bytes32 token0Hash;
        bytes32 token1Hash;
    }

    function run() external returns (bytes32 poolId) {
        DeploymentConfig memory deployment = _loadDeploymentConfig();

        if (block.chainid != deployment.chainId) {
            revert WrongChain(block.chainid, deployment.chainId);
        }
        if (
            deployment.governor == address(0) || deployment.guardian == address(0)
                || deployment.lpOwner == address(0) || address(deployment.poolManager) == address(0)
                || address(deployment.positionManager) == address(0)
                || address(deployment.hook) == address(0) || deployment.token0 >= deployment.token1
                || deployment.tickSpacing <= 0 || deployment.initialSqrtPriceX96 == 0
                || deployment.governor == deployment.guardian
                || deployment.governor == deployment.lpOwner
                || deployment.guardian == deployment.lpOwner
        ) revert InvalidConfiguration();
        if (
            deployment.hook.owner() != deployment.governor
                || deployment.hook.guardian() != deployment.guardian
                || deployment.hook.INITIALIZER() != deployment.governor
                || address(deployment.hook.POOL_MANAGER()) != address(deployment.poolManager)
                || address(deployment.hook.POSITION_MANAGER())
                    != address(deployment.positionManager)
                || address(deployment.positionManager.poolManager())
                    != address(deployment.poolManager)
        ) revert HookBindingMismatch();
        if (
            address(deployment.poolManager).codehash != deployment.poolManagerHash
                || address(deployment.positionManager).codehash != deployment.positionManagerHash
                || address(deployment.hook).codehash != deployment.hookHash
                || (deployment.token0 == address(0)
                        ? deployment.token0Hash != bytes32(0)
                        : deployment.token0.codehash != deployment.token0Hash)
                || deployment.token1.codehash != deployment.token1Hash
        ) revert CodeHashMismatch();

        PoolKey memory key = PoolKey({
            currency0: Currency.wrap(deployment.token0),
            currency1: Currency.wrap(deployment.token1),
            fee: LPFeeLibrary.DYNAMIC_FEE_FLAG,
            tickSpacing: deployment.tickSpacing,
            hooks: IHooks(address(deployment.hook))
        });
        OasisDynamicFeeHook.FeeConfig memory fee = _loadFeeConfig();

        vm.startBroadcast(deployment.governor);
        deployment.hook.configurePool(key, fee);
        deployment.hook.setWhitelist(deployment.lpOwner, true);
        // Initialization returns the derived tick; StateView verification is
        // performed by the post-deployment identity checklist at a pinned block.
        // forge-lint: disable-next-line(unused-return)
        deployment.poolManager.initialize(key, deployment.initialSqrtPriceX96);
        vm.stopBroadcast();

        poolId = PoolId.unwrap(key.toId());
        OasisDynamicFeeHook.FeeConfig memory observed = deployment.hook.feeConfig(key);
        if (
            deployment.hook.boundPoolId() != poolId
                || !deployment.hook.isWhitelisted(deployment.lpOwner)
                || keccak256(abi.encode(observed)) != keccak256(abi.encode(fee))
        ) revert PostconditionFailed();
    }

    function _loadDeploymentConfig() private view returns (DeploymentConfig memory deployment) {
        deployment = DeploymentConfig({
            chainId: vm.envUint("OASIS_CHAIN_ID"),
            governor: vm.envAddress("OASIS_GOVERNOR"),
            guardian: vm.envAddress("OASIS_GUARDIAN"),
            lpOwner: vm.envAddress("OASIS_LP_OWNER"),
            poolManager: IPoolManager(vm.envAddress("OASIS_POOL_MANAGER")),
            positionManager: PositionManager(payable(vm.envAddress("OASIS_POSITION_MANAGER"))),
            hook: OasisDynamicFeeHook(vm.envAddress("OASIS_HOOK")),
            token0: vm.envAddress("OASIS_TOKEN0"),
            token1: vm.envAddress("OASIS_TOKEN1"),
            tickSpacing: vm.envInt("OASIS_TICK_SPACING").toInt24(),
            initialSqrtPriceX96: vm.envUint("OASIS_INITIAL_SQRT_PRICE_X96").toUint160(),
            poolManagerHash: vm.envBytes32("OASIS_POOL_MANAGER_HASH"),
            positionManagerHash: vm.envBytes32("OASIS_POSITION_MANAGER_HASH"),
            hookHash: vm.envBytes32("OASIS_HOOK_HASH"),
            token0Hash: vm.envBytes32("OASIS_TOKEN0_HASH"),
            token1Hash: vm.envBytes32("OASIS_TOKEN1_HASH")
        });
    }

    function _loadFeeConfig() private view returns (OasisDynamicFeeHook.FeeConfig memory fee) {
        fee = OasisDynamicFeeHook.FeeConfig({
            baseFee: vm.envUint("OASIS_BASE_FEE").toUint24(),
            mediumFee: vm.envUint("OASIS_MEDIUM_FEE").toUint24(),
            highFee: vm.envUint("OASIS_HIGH_FEE").toUint24(),
            mediumAmount0Threshold: vm.envUint("OASIS_MEDIUM_AMOUNT0_THRESHOLD").toUint128(),
            highAmount0Threshold: vm.envUint("OASIS_HIGH_AMOUNT0_THRESHOLD").toUint128(),
            mediumAmount1Threshold: vm.envUint("OASIS_MEDIUM_AMOUNT1_THRESHOLD").toUint128(),
            highAmount1Threshold: vm.envUint("OASIS_HIGH_AMOUNT1_THRESHOLD").toUint128(),
            ruleVersion: vm.envUint("OASIS_RULE_VERSION").toUint64(),
            configured: true
        });
    }
}
