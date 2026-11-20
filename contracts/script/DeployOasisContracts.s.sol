// SPDX-License-Identifier: BUSL-1.1
pragma solidity 0.8.26;

import { Script } from "forge-std/Script.sol";
import { Hooks } from "@uniswap/v4-core/src/libraries/Hooks.sol";
import { IPoolManager } from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import { PositionManager } from "@uniswap/v4-periphery/src/PositionManager.sol";

import { OasisDynamicFeeHook } from "../src/OasisDynamicFeeHook.sol";
import { OasisHookFactory } from "../src/OasisHookFactory.sol";
import { OasisPositionExecutor } from "../src/OasisPositionExecutor.sol";
import { IOasisPositionManager } from "../src/interfaces/IOasisPositionManager.sol";

/// @notice Software-EOA, encrypted keystore (`--account`), and external-signer compatible
///         deployment for the immutable Phase-1 contracts.
/// @dev This script deliberately does not read a private key. Use Foundry encrypted software
///      keystores (`cast wallet new` / `--account`), external wallets, or optional multisigs with
///      Two-Zone Cold-vs-Hot separation (`deployer`, `governor`, and `guardian` in the Cold Admin
///      zone, distinct from `lpOwner` in the Hot Canary zone). By default all four roles must be
///      distinct; set `OASIS_ALLOW_SOLO_ROLE_COLLAPSE=true` to allow the documented solo-operator
///      profile where `deployer`/`guardian` default to `governor`.
///      Governance config and pool initialization are separate transactions owned by the configured
///      governor.
contract DeployOasisContracts is Script {
    error WrongChain(uint256 actual, uint256 expected);
    error ZeroAddress();
    error RolesNotSeparated();
    error PositionManagerMismatch();
    error CoreCodeHashMismatch();
    error DeploymentVerificationFailed();
    error SaltNotFound();

    uint256 private constant MAX_SALT_ATTEMPTS = 200_000;

    function run()
        external
        returns (OasisHookFactory factory, OasisDynamicFeeHook hook, OasisPositionExecutor executor)
    {
        uint256 expectedChainId = vm.envUint("OASIS_CHAIN_ID");
        IPoolManager poolManager = IPoolManager(vm.envAddress("OASIS_POOL_MANAGER"));
        PositionManager positionManager =
            PositionManager(payable(vm.envAddress("OASIS_POSITION_MANAGER")));
        address governor = vm.envAddress("OASIS_GOVERNOR");
        address deployer = vm.envOr("OASIS_ALLOW_SOLO_ROLE_COLLAPSE", false)
            ? _envAddressOr("OASIS_DEPLOYER", governor)
            : vm.envAddress("OASIS_DEPLOYER");
        address guardian = vm.envOr("OASIS_ALLOW_SOLO_ROLE_COLLAPSE", false)
            ? _envAddressOr("OASIS_GUARDIAN", governor)
            : vm.envAddress("OASIS_GUARDIAN");
        address lpOwner = vm.envAddress("OASIS_LP_OWNER");
        uint256 maxLiquidity = vm.envUint("OASIS_MAX_LIQUIDITY");
        bytes32 expectedPoolManagerHash = vm.envBytes32("OASIS_POOL_MANAGER_HASH");
        bytes32 expectedPositionManagerHash = vm.envBytes32("OASIS_POSITION_MANAGER_HASH");

        if (block.chainid != expectedChainId) revert WrongChain(block.chainid, expectedChainId);
        if (
            deployer == address(0) || address(poolManager) == address(0)
                || address(positionManager) == address(0) || governor == address(0)
                || guardian == address(0) || lpOwner == address(0) || maxLiquidity == 0
        ) revert ZeroAddress();
        if (allowSoloRoleCollapse()) {
            if (lpOwner == governor || lpOwner == guardian || lpOwner == deployer) {
                revert RolesNotSeparated();
            }
        } else if (
            governor == guardian || governor == lpOwner || guardian == lpOwner
                || deployer == governor || deployer == guardian || deployer == lpOwner
        ) {
            revert RolesNotSeparated();
        }
        if (address(positionManager.poolManager()) != address(poolManager)) {
            revert PositionManagerMismatch();
        }
        if (
            address(poolManager).codehash != expectedPoolManagerHash
                || address(positionManager).codehash != expectedPositionManagerHash
        ) revert CoreCodeHashMismatch();

        vm.startBroadcast(deployer);
        factory = new OasisHookFactory();
        bytes32 codeHash = factory.initCodeHash(
            poolManager,
            IOasisPositionManager(address(positionManager)),
            governor,
            governor,
            guardian
        );
        bytes32 salt = _mineSalt(factory, codeHash);
        hook = factory.deploy(
            salt,
            poolManager,
            IOasisPositionManager(address(positionManager)),
            governor,
            governor,
            guardian
        );
        executor = new OasisPositionExecutor(
            poolManager, positionManager, lpOwner, guardian, maxLiquidity
        );
        vm.stopBroadcast();

        if (
            address(factory).code.length == 0 || address(hook).code.length == 0
                || address(executor).code.length == 0 || hook.owner() != governor
                || hook.guardian() != guardian
                || address(hook.POOL_MANAGER()) != address(poolManager)
                || address(hook.POSITION_MANAGER()) != address(positionManager)
                || hook.INITIALIZER() != governor || executor.owner() != lpOwner
                || executor.guardian() != guardian
                || address(executor.POOL_MANAGER()) != address(poolManager)
                || address(executor.POSITION_MANAGER()) != address(positionManager)
                || executor.MAX_LIQUIDITY() != maxLiquidity
                || uint160(address(hook)) & Hooks.ALL_HOOK_MASK != factory.REQUIRED_HOOK_FLAGS()
        ) revert DeploymentVerificationFailed();
    }

    function _mineSalt(OasisHookFactory factory, bytes32 codeHash)
        private
        view
        returns (bytes32 salt)
    {
        uint160 requiredFlags = factory.REQUIRED_HOOK_FLAGS();
        for (uint256 candidate = 0; candidate < MAX_SALT_ATTEMPTS; ++candidate) {
            salt = bytes32(candidate);
            if (
                uint160(
                            uint256(
                                keccak256(
                                    abi.encodePacked(bytes1(0xff), address(factory), salt, codeHash)
                                )
                            )
                        ) & Hooks.ALL_HOOK_MASK == requiredFlags
            ) {
                return salt;
            }
        }
        revert SaltNotFound();
    }

    function _envAddressOr(string memory name, address fallbackAddress)
        private
        view
        returns (address)
    {
        string memory raw = vm.envOr(name, string(""));
        if (bytes(raw).length == 0) {
            return fallbackAddress;
        }
        return vm.parseAddress(raw);
    }

    function allowSoloRoleCollapse() private view returns (bool) {
        return vm.envOr("OASIS_ALLOW_SOLO_ROLE_COLLAPSE", false);
    }
}
