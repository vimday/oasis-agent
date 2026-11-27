// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @dev Constructor output is consumed only by a historical or shadow eth_call.
contract BaseV4StaticStateRead {
    constructor(bytes memory aggregate) {
        require(block.chainid == 8453);
        require(keccak256(aggregate) == 0xf5596037673bfeaa35f94a5dd0a7dbc8a926ebd8c841a8bdc7fecc99f1be190d);
        address manager = address(bytes20(hex"498581ff718922c3f8e6a244956af099b2652b2b"));
        address multicall = address(bytes20(hex"ca11bde05977b3631167028862be2a173976ca11"));
        (bool ok, bytes memory state) = multicall.staticcall(aggregate);
        require(ok);
        bytes memory result = abi.encode(manager.codehash, state);
        assembly ("memory-safe") {
            return(add(result, 32), mload(result))
        }
    }
}
