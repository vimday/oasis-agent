// SPDX-License-Identifier: BUSL-1.1
pragma solidity 0.8.26;

import { IPoolManager } from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import { Hooks } from "@uniswap/v4-core/src/libraries/Hooks.sol";

import { OasisDynamicFeeHook } from "./OasisDynamicFeeHook.sol";
import { IOasisPositionManager } from "./interfaces/IOasisPositionManager.sol";

/// @notice Stateless CREATE2 deployer that fails before deployment when hook flags are wrong.
contract OasisHookFactory {
    uint160 public constant REQUIRED_HOOK_FLAGS =
        Hooks.BEFORE_INITIALIZE_FLAG | Hooks.BEFORE_ADD_LIQUIDITY_FLAG | Hooks.BEFORE_SWAP_FLAG;

    error IncorrectHookAddressFlags(address predicted, uint160 actual, uint160 required);
    error DeploymentAddressMismatch(address predicted, address deployed);

    event HookDeployed(
        address indexed hook,
        bytes32 indexed salt,
        bytes32 indexed initCodeHash,
        bytes32 runtimeCodeHash
    );

    function initCodeHash(
        IPoolManager poolManager,
        IOasisPositionManager positionManager,
        address initializer,
        address initialOwner,
        address guardian
    ) public pure returns (bytes32) {
        // Computes the CREATE2 init code hash from the hook creation bytecode; the
        // "many digits" finding is a keccak digest comparison, not a numeric literal.
        // slither-disable-next-line too-many-digits
        return keccak256(
            bytes.concat(
                type(OasisDynamicFeeHook).creationCode,
                abi.encode(poolManager, positionManager, initializer, initialOwner, guardian)
            )
        );
    }

    function computeAddress(bytes32 salt, bytes32 codeHash) public view returns (address) {
        return address(
            uint160(uint256(keccak256(abi.encodePacked(bytes1(0xff), this, salt, codeHash))))
        );
    }

    function deploy(
        bytes32 salt,
        IPoolManager poolManager,
        IOasisPositionManager positionManager,
        address initializer,
        address initialOwner,
        address guardian
    ) external returns (OasisDynamicFeeHook hook) {
        bytes32 codeHash = initCodeHash(
            poolManager, positionManager, initializer, initialOwner, guardian
        );
        address predicted = computeAddress(salt, codeHash);
        uint160 actualFlags = uint160(predicted) & Hooks.ALL_HOOK_MASK;
        if (actualFlags != REQUIRED_HOOK_FLAGS) {
            revert IncorrectHookAddressFlags(predicted, actualFlags, REQUIRED_HOOK_FLAGS);
        }
        hook = new OasisDynamicFeeHook{ salt: salt }(
            poolManager, positionManager, initializer, initialOwner, guardian
        );
        if (address(hook) != predicted) revert DeploymentAddressMismatch(predicted, address(hook));
        // The constructor has no untrusted callback path; emit-after-create is intentional.
        // forge-lint: disable-next-line(reentrancy-events)
        emit HookDeployed(address(hook), salt, codeHash, address(hook).codehash);
    }
}
