// SPDX-License-Identifier: BUSL-1.1
pragma solidity 0.8.26;

import { Script } from "forge-std/Script.sol";

import { OasisAtomicExecutor } from "../src/OasisAtomicExecutor.sol";
import { OasisIntentVault } from "../src/OasisIntentVault.sol";

/// @notice Two-Zone Software EOA compatible deterministic CREATE2 deployment for the Oasis
///         profit-engine contracts (`OasisAtomicExecutor` and `OasisIntentVault`).
/// @dev This script intentionally never reads a private key. Use Foundry encrypted software
///      keystores (`cast wallet new` / `--account`), external wallets, remote signers, or optional
///      multisigs. Enforces Two-Zone Cold-vs-Hot Plane Separation between the Cold Admin Plane
///      (`deployer`, `owner`, `guardian`, `treasury`, `revenueSink`) and the Hot Canary Plane
///      (`operator`, `quoteSigner`). A solo operator may share one Cold Admin EOA across all Cold
///      roles and one Hot Canary EOA across Hot roles; when `deployer == owner` and
///      `OASIS_WIRE_OPERATORS_ON_DEPLOY=true`, `deployAndWireSoloOperator` also executes post-deploy
///      operator and quote-signer wiring atomically.
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
    error DeployerMustBeOwnerToWire();
    error DeploymentVerificationFailed();

    uint256 private constant _COLD_ROLE_COUNT = 5;
    uint256 private constant _HOT_ROLE_COUNT = 2;

    /// @notice Read deployment parameters from environment variables and deploy deterministically.
    function run() external returns (DeployedProfitEngine memory deployed) {
        DeployParams memory params = _loadEnvParams();
        bool wireSolo = vm.envOr("OASIS_WIRE_OPERATORS_ON_DEPLOY", false);
        if (wireSolo) {
            return deployAndWireSoloOperator(params);
        }
        return deployProfitEngine(params);
    }

    /// @notice Validate Two-Zone custody separation, deploy executor and vault via CREATE2, and verify.
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

    /// @notice Deploy via CREATE2 and atomically wire Hot operators when `deployer == owner`.
    function deployAndWireSoloOperator(DeployParams memory params)
        public
        returns (DeployedProfitEngine memory deployed)
    {
        deployed = deployProfitEngine(params);
        if (params.deployer != params.owner) revert DeployerMustBeOwnerToWire();

        vm.startBroadcast(params.owner);
        deployed.executor.setOperator(params.operator, true);
        deployed.vault.setFillerOperator(params.operator);
        deployed.vault.rotateQuoteSigner(params.quoteSigner);
        vm.stopBroadcast();

        _verifyWiredState(params, deployed.executor, deployed.vault);
    }

    function _loadEnvParams() private view returns (DeployParams memory params) {
        uint256 expectedChainId = vm.envUint("OASIS_CHAIN_ID");
        address owner = vm.envAddress("OASIS_PROFIT_OWNER");
        address operator = vm.envAddress("OASIS_PROFIT_OPERATOR");
        params = DeployParams({
            expectedChainId: expectedChainId,
            deployer: _envAddressOr("OASIS_DEPLOYER", owner),
            owner: owner,
            guardian: _envAddressOr("OASIS_PROFIT_GUARDIAN", owner),
            operator: operator,
            quoteSigner: _envAddressOr("OASIS_QUOTE_SIGNER", operator),
            treasury: _envAddressOr("OASIS_TREASURY", owner),
            revenueSink: _envAddressOr("OASIS_REVENUE_SINK", owner),
            executorSalt: vm.envBytes32("OASIS_EXECUTOR_SALT"),
            vaultSalt: vm.envBytes32("OASIS_VAULT_SALT")
        });
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

    function _validateParams(DeployParams memory params) private view {
        if (block.chainid != params.expectedChainId) {
            revert WrongChain(block.chainid, params.expectedChainId);
        }

        address[_COLD_ROLE_COUNT] memory cold =
            [params.deployer, params.owner, params.guardian, params.treasury, params.revenueSink];
        address[_HOT_ROLE_COUNT] memory hot = [params.operator, params.quoteSigner];

        for (uint256 i = 0; i < _COLD_ROLE_COUNT; ++i) {
            if (cold[i] == address(0)) revert ZeroAddress();
        }
        for (uint256 i = 0; i < _HOT_ROLE_COUNT; ++i) {
            if (hot[i] == address(0)) revert ZeroAddress();
        }
        for (uint256 h = 0; h < _HOT_ROLE_COUNT; ++h) {
            for (uint256 c = 0; c < _COLD_ROLE_COUNT; ++c) {
                if (hot[h] == cold[c]) revert RolesNotSeparated();
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

    function _verifyWiredState(
        DeployParams memory params,
        OasisAtomicExecutor executor,
        OasisIntentVault vault
    ) private view {
        if (
            !executor.isOperator(params.operator) || executor.owner() != params.owner
                || vault.fillerOperator() != params.operator
                || vault.quoteSigner() != params.quoteSigner || vault.signerEpoch() != 1
        ) {
            revert DeploymentVerificationFailed();
        }
    }
}
