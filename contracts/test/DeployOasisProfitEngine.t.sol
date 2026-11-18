// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { Test } from "forge-std/Test.sol";
import { Ownable } from "@openzeppelin/contracts/access/Ownable.sol";

import { DeployOasisProfitEngine } from "../script/DeployOasisProfitEngine.s.sol";
import { OasisAtomicExecutor } from "../src/OasisAtomicExecutor.sol";
import { OasisIntentVault } from "../src/OasisIntentVault.sol";

contract DeployOasisProfitEngineTest is Test {
    address private constant DEPLOYER = address(0x1001);
    address private constant OWNER = address(0x1002);
    address private constant GUARDIAN = address(0x1003);
    address private constant OPERATOR = address(0x1004);
    address private constant QUOTE_SIGNER = address(0x1005);
    address private constant TREASURY = address(0x1006);
    address private constant REVENUE_SINK = address(0x1007);

    bytes32 private constant EXECUTOR_SALT = keccak256("oasis.profit.executor.salt.v1");
    bytes32 private constant VAULT_SALT = keccak256("oasis.profit.vault.salt.v1");

    DeployOasisProfitEngine private script;

    function setUp() public {
        script = new DeployOasisProfitEngine();
    }

    function test_deployProfitEngineDeterministicCreate2AndRoleGetters() public {
        DeployOasisProfitEngine.DeployParams memory params = _defaultParams();

        address expectedExecutor = _expectedExecutorAddress(params);
        address expectedVault = _expectedVaultAddress(params);

        DeployOasisProfitEngine.DeployedProfitEngine memory deployed =
            script.deployProfitEngine(params);

        assertEq(address(deployed.executor), expectedExecutor);
        assertEq(address(deployed.vault), expectedVault);
        assertTrue(address(deployed.executor) != address(deployed.vault));
        assertGt(address(deployed.executor).code.length, 0);
        assertGt(address(deployed.vault).code.length, 0);

        assertTrue(deployed.executorCodeHash != bytes32(0));
        assertTrue(deployed.vaultCodeHash != bytes32(0));
        assertEq(deployed.executorCodeHash, address(deployed.executor).codehash);
        assertEq(deployed.vaultCodeHash, address(deployed.vault).codehash);
        assertTrue(deployed.executorCodeHash != deployed.vaultCodeHash);

        assertEq(deployed.executor.owner(), OWNER);
        assertEq(deployed.executor.guardian(), GUARDIAN);
        assertEq(deployed.executor.TREASURY(), TREASURY);
        assertEq(deployed.executor.REVENUE_SINK(), REVENUE_SINK);
        assertFalse(deployed.executor.paused());
        assertEq(deployed.executor.lastNonce(), 0);
        assertFalse(deployed.executor.isOperator(OPERATOR));
        assertEq(deployed.executor.pendingOwner(), address(0));

        assertEq(deployed.vault.owner(), OWNER);
        assertEq(deployed.vault.guardian(), GUARDIAN);
        assertEq(deployed.vault.TREASURY(), TREASURY);
        assertEq(deployed.vault.REVENUE_SINK(), REVENUE_SINK);
        assertFalse(deployed.vault.paused());
        assertEq(deployed.vault.signerEpoch(), 0);
        assertEq(deployed.vault.fillerOperator(), address(0));
        assertEq(deployed.vault.quoteSigner(), address(0));
        assertEq(deployed.vault.pendingOwner(), address(0));

        // Deployer has zero post-deploy governance power; only owner multisig may wire operators.
        vm.prank(DEPLOYER);
        vm.expectRevert(
            abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, DEPLOYER)
        );
        deployed.executor.setOperator(OPERATOR, true);

        vm.prank(DEPLOYER);
        vm.expectRevert(
            abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, DEPLOYER)
        );
        deployed.vault.setFillerOperator(OPERATOR);

        vm.startPrank(OWNER);
        deployed.executor.setOperator(OPERATOR, true);
        deployed.vault.setFillerOperator(OPERATOR);
        deployed.vault.rotateQuoteSigner(QUOTE_SIGNER);
        vm.stopPrank();

        assertTrue(deployed.executor.isOperator(OPERATOR));
        assertEq(deployed.vault.fillerOperator(), OPERATOR);
        assertEq(deployed.vault.quoteSigner(), QUOTE_SIGNER);
        assertEq(deployed.vault.signerEpoch(), 1);
    }

    function test_deployProfitEngineDeterministicAcrossScriptInstancesAndSalts() public {
        DeployOasisProfitEngine.DeployParams memory params = _defaultParams();
        DeployOasisProfitEngine.DeployedProfitEngine memory first =
            script.deployProfitEngine(params);

        params.executorSalt = bytes32(uint256(77));
        params.vaultSalt = bytes32(uint256(88));
        DeployOasisProfitEngine freshScript = new DeployOasisProfitEngine();
        DeployOasisProfitEngine.DeployedProfitEngine memory shifted =
            freshScript.deployProfitEngine(params);

        assertEq(address(shifted.executor), _expectedExecutorAddress(params));
        assertEq(address(shifted.vault), _expectedVaultAddress(params));
        assertTrue(address(shifted.executor) != address(first.executor));
        assertTrue(address(shifted.vault) != address(first.vault));
        assertEq(shifted.executorCodeHash, first.executorCodeHash);
        assertEq(shifted.vaultCodeHash, first.vaultCodeHash);
    }

    function test_runReadsEnvVarsAndDeploysDeterministically() public {
        vm.setEnv("OASIS_CHAIN_ID", vm.toString(block.chainid));
        vm.setEnv("OASIS_DEPLOYER", vm.toString(DEPLOYER));
        vm.setEnv("OASIS_PROFIT_OWNER", vm.toString(OWNER));
        vm.setEnv("OASIS_PROFIT_GUARDIAN", vm.toString(GUARDIAN));
        vm.setEnv("OASIS_PROFIT_OPERATOR", vm.toString(OPERATOR));
        vm.setEnv("OASIS_QUOTE_SIGNER", vm.toString(QUOTE_SIGNER));
        vm.setEnv("OASIS_TREASURY", vm.toString(TREASURY));
        vm.setEnv("OASIS_REVENUE_SINK", vm.toString(REVENUE_SINK));
        vm.setEnv("OASIS_EXECUTOR_SALT", vm.toString(bytes32(uint256(101))));
        vm.setEnv("OASIS_VAULT_SALT", vm.toString(bytes32(uint256(202))));

        DeployOasisProfitEngine.DeployParams memory expectedParams = _defaultParams();
        expectedParams.executorSalt = bytes32(uint256(101));
        expectedParams.vaultSalt = bytes32(uint256(202));

        DeployOasisProfitEngine.DeployedProfitEngine memory deployed = script.run();

        assertEq(address(deployed.executor), _expectedExecutorAddress(expectedParams));
        assertEq(address(deployed.vault), _expectedVaultAddress(expectedParams));
        assertGt(address(deployed.executor).code.length, 0);
        assertGt(address(deployed.vault).code.length, 0);
        assertEq(deployed.executor.owner(), OWNER);
        assertEq(deployed.vault.owner(), OWNER);
        assertEq(deployed.executor.TREASURY(), TREASURY);
        assertEq(deployed.vault.REVENUE_SINK(), REVENUE_SINK);
    }

    function test_revertsOnWrongChain() public {
        DeployOasisProfitEngine.DeployParams memory params = _defaultParams();
        params.expectedChainId = block.chainid + 1;

        vm.expectRevert(
            abi.encodeWithSelector(
                DeployOasisProfitEngine.WrongChain.selector, block.chainid, block.chainid + 1
            )
        );
        script.deployProfitEngine(params);

        params.expectedChainId = 0;
        vm.expectRevert(
            abi.encodeWithSelector(
                DeployOasisProfitEngine.WrongChain.selector, block.chainid, uint256(0)
            )
        );
        script.deployProfitEngine(params);
    }

    function test_revertsOnZeroAddressForEveryRole() public {
        for (uint256 roleIdx = 0; roleIdx < 7; ++roleIdx) {
            DeployOasisProfitEngine.DeployParams memory params = _defaultParams();
            address[7] memory roles = _rolesArray(params);
            roles[roleIdx] = address(0);
            params = _applyRoles(params, roles);

            vm.expectRevert(DeployOasisProfitEngine.ZeroAddress.selector);
            script.deployProfitEngine(params);
        }
    }

    function test_zeroAddressTakesPrecedenceOverRolesNotSeparated() public {
        DeployOasisProfitEngine.DeployParams memory params = _defaultParams();
        params.deployer = OWNER;
        params.revenueSink = address(0);

        vm.expectRevert(DeployOasisProfitEngine.ZeroAddress.selector);
        script.deployProfitEngine(params);
    }

    function test_revertsOnEveryPairwiseRolesNotSeparatedCollision() public {
        for (uint256 i = 0; i < 7; ++i) {
            for (uint256 j = i + 1; j < 7; ++j) {
                DeployOasisProfitEngine.DeployParams memory params = _defaultParams();
                address[7] memory roles = _rolesArray(params);
                roles[j] = roles[i];
                params = _applyRoles(params, roles);

                vm.expectRevert(DeployOasisProfitEngine.RolesNotSeparated.selector);
                script.deployProfitEngine(params);
            }
        }
    }

    function test_revertsOnDuplicateCreate2Deployment() public {
        DeployOasisProfitEngine.DeployParams memory params = _defaultParams();
        script.deployProfitEngine(params);

        vm.expectRevert();
        script.deployProfitEngine(params);
    }

    function _defaultParams()
        private
        view
        returns (DeployOasisProfitEngine.DeployParams memory params)
    {
        params = DeployOasisProfitEngine.DeployParams({
            expectedChainId: block.chainid,
            deployer: DEPLOYER,
            owner: OWNER,
            guardian: GUARDIAN,
            operator: OPERATOR,
            quoteSigner: QUOTE_SIGNER,
            treasury: TREASURY,
            revenueSink: REVENUE_SINK,
            executorSalt: EXECUTOR_SALT,
            vaultSalt: VAULT_SALT
        });
    }

    function _rolesArray(DeployOasisProfitEngine.DeployParams memory params)
        private
        pure
        returns (address[7] memory roles)
    {
        roles[0] = params.deployer;
        roles[1] = params.owner;
        roles[2] = params.guardian;
        roles[3] = params.operator;
        roles[4] = params.quoteSigner;
        roles[5] = params.treasury;
        roles[6] = params.revenueSink;
    }

    function _applyRoles(
        DeployOasisProfitEngine.DeployParams memory params,
        address[7] memory roles
    ) private pure returns (DeployOasisProfitEngine.DeployParams memory updated) {
        updated = params;
        updated.deployer = roles[0];
        updated.owner = roles[1];
        updated.guardian = roles[2];
        updated.operator = roles[3];
        updated.quoteSigner = roles[4];
        updated.treasury = roles[5];
        updated.revenueSink = roles[6];
    }

    function _expectedExecutorAddress(DeployOasisProfitEngine.DeployParams memory params)
        private
        pure
        returns (address)
    {
        bytes memory initCode = abi.encodePacked(
            type(OasisAtomicExecutor).creationCode,
            abi.encode(params.owner, params.guardian, params.treasury, params.revenueSink)
        );
        return vm.computeCreate2Address(params.executorSalt, keccak256(initCode));
    }

    function _expectedVaultAddress(DeployOasisProfitEngine.DeployParams memory params)
        private
        pure
        returns (address)
    {
        bytes memory initCode = abi.encodePacked(
            type(OasisIntentVault).creationCode,
            abi.encode(params.owner, params.guardian, params.treasury, params.revenueSink)
        );
        return vm.computeCreate2Address(params.vaultSalt, keccak256(initCode));
    }
}
