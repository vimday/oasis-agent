// SPDX-License-Identifier: BUSL-1.1
pragma solidity 0.8.26;

import { Script } from "forge-std/Script.sol";

import { OasisAtomicExecutor } from "../src/OasisAtomicExecutor.sol";
import { OasisIntentVault } from "../src/OasisIntentVault.sol";

/// @notice Hardware/external-signer compatible deterministic CREATE2 deployment for the
///         Oasis profit-engine contracts (`OasisAtomicExecutor` and `OasisIntentVault`).
/// @dev This script intentionally never reads a private key. Use Foundry's hardware-wallet,
///      remote signer, or an unlocked external deployment account. Governance wiring such as
///      `setOperator`, `setFillerOperator`, and `rotateQuoteSigner` is intentionally executed
///      by the configured `owner` multisig after deployment verification.
contract DeployOasisProfitEngine is Script {
    struct DeployParams {
        uint256 expectedChainId;
        address deployer;
        address owner;
        address guardian;
        address operator;
        address quoteSigner;
        address treasury;
        address revenueSink;
        bytes32 executorSalt;
        bytes32 vaultSalt;
    }

    struct DeployedProfitEngine {
        OasisAtomicExecutor executor;
        OasisIntentVault vault;
        bytes32 executorCodeHash;
        bytes32 vaultCodeHash;
    }

    error WrongChain(uint256 actual, uint256 expected);
    error ZeroAddress();
    error RolesNotSeparated();
    error DeploymentVerificationFailed();

    uint256 private constant _ROLE_COUNT = 7;

    /// @notice Read deployment parameters from environment variables and deploy deterministically.
    function run() external returns (DeployedProfitEngine memory deployed) {
        DeployParams memory params = _loadEnvParams();
        return deployProfitEngine(params);
    }

    /// @notice Validate custody separation, deploy executor and vault via CREATE2, and verify.
    function deployProfitEngine(DeployParams memory params)
        public
        returns (DeployedProfitEngine memory deployed)
    {
        _validateParams(params);

        vm.startBroadcast(params.deployer);
        OasisAtomicExecutor executor = new OasisAtomicExecutor{ salt: params.executorSalt }(
            params.owner, params.guardian, params.treasury, params.revenueSink
        );
        OasisIntentVault vault = new OasisIntentVault{ salt: params.vaultSalt }(
            params.owner, params.guardian, params.treasury, params.revenueSink
        );
        vm.stopBroadcast();

        (bytes32 executorCodeHash, bytes32 vaultCodeHash) =
            _verifyDeployment(params, executor, vault);

        deployed = DeployedProfitEngine({
            executor: executor,
            vault: vault,
            executorCodeHash: executorCodeHash,
            vaultCodeHash: vaultCodeHash
        });
    }

    function _loadEnvParams() private view returns (DeployParams memory params) {
        params = DeployParams({
            expectedChainId: vm.envUint("OASIS_CHAIN_ID"),
            deployer: vm.envAddress("OASIS_DEPLOYER"),
            owner: vm.envAddress("OASIS_PROFIT_OWNER"),
            guardian: vm.envAddress("OASIS_PROFIT_GUARDIAN"),
            operator: vm.envAddress("OASIS_PROFIT_OPERATOR"),
            quoteSigner: vm.envAddress("OASIS_QUOTE_SIGNER"),
            treasury: vm.envAddress("OASIS_TREASURY"),
            revenueSink: vm.envAddress("OASIS_REVENUE_SINK"),
            executorSalt: vm.envBytes32("OASIS_EXECUTOR_SALT"),
            vaultSalt: vm.envBytes32("OASIS_VAULT_SALT")
        });
    }

    function _validateParams(DeployParams memory params) private view {
        if (block.chainid != params.expectedChainId) {
            revert WrongChain(block.chainid, params.expectedChainId);
        }

        address[_ROLE_COUNT] memory roles = [
            params.deployer,
            params.owner,
            params.guardian,
            params.operator,
            params.quoteSigner,
            params.treasury,
            params.revenueSink
        ];

        for (uint256 i = 0; i < _ROLE_COUNT; ++i) {
            if (roles[i] == address(0)) revert ZeroAddress();
        }
        for (uint256 i = 0; i < _ROLE_COUNT; ++i) {
            for (uint256 j = i + 1; j < _ROLE_COUNT; ++j) {
                if (roles[i] == roles[j]) revert RolesNotSeparated();
            }
        }
    }

    function _verifyDeployment(
        DeployParams memory params,
        OasisAtomicExecutor executor,
        OasisIntentVault vault
    ) private view returns (bytes32 executorCodeHash, bytes32 vaultCodeHash) {
        if (
            address(executor) == address(0) || address(vault) == address(0)
                || address(executor) == address(vault)
        ) {
            revert DeploymentVerificationFailed();
        }
        if (address(executor).code.length == 0 || address(vault).code.length == 0) {
            revert DeploymentVerificationFailed();
        }

        executorCodeHash = address(executor).codehash;
        vaultCodeHash = address(vault).codehash;
        if (executorCodeHash == bytes32(0) || vaultCodeHash == bytes32(0)) {
            revert DeploymentVerificationFailed();
        }

        _verifyExecutorState(params, executor);
        _verifyVaultState(params, vault);
    }

    function _verifyExecutorState(DeployParams memory params, OasisAtomicExecutor executor)
        private
        view
    {
        if (
            executor.owner() != params.owner || executor.guardian() != params.guardian
                || executor.TREASURY() != params.treasury
                || executor.REVENUE_SINK() != params.revenueSink || executor.paused()
                || executor.lastNonce() != 0 || executor.isOperator(params.operator)
                || executor.pendingOwner() != address(0)
        ) {
            revert DeploymentVerificationFailed();
        }
    }

    function _verifyVaultState(DeployParams memory params, OasisIntentVault vault) private view {
        if (
            vault.owner() != params.owner || vault.guardian() != params.guardian
                || vault.TREASURY() != params.treasury || vault.REVENUE_SINK() != params.revenueSink
                || vault.paused() || vault.signerEpoch() != 0
                || vault.fillerOperator() != address(0) || vault.quoteSigner() != address(0)
                || vault.pendingOwner() != address(0)
        ) {
            revert DeploymentVerificationFailed();
        }
    }
}
