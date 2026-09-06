// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { Test } from "forge-std/Test.sol";

/// @notice Diagnostic probe for fork-head behavior on a pinned remote fork.
/// Logs the requested height and the fork-head block number foundry reports,
/// which for L2 nodes exposing l1BlockNumber differs from the L2 height, and
/// the blockhash window boundaries so L2 blockhash semantics can be recorded
/// as evidence rather than assumed.
contract ProbeForkTest is Test {
    function test_probe() public {
        uint256 height = vm.envUint("OASIS_FORK_BLOCK");
        uint256 forkId = vm.createSelectFork(vm.envString("OASIS_FORK_RPC"), height);
        // The probe's entire purpose is to emit diagnostics after the fork call.
        // forge-lint: disable-start(reentrancy-events)
        emit log_string("=== PROBE ===");
        emit log_uint(vm.activeFork());
        emit log_uint(forkId);
        emit log_uint(height);
        emit log_uint(block.number);
        emit log_uint(vm.getBlockNumber());
        emit log_uint(block.chainid);
        emit log_uint(vm.envOr("OASIS_FORK_L1_BLOCK", uint256(0)));
        emit log_uint(block.timestamp);
        emit log_uint(vm.envOr("OASIS_FORK_TIMESTAMP", uint256(0)));
        emit log_bytes32(blockhash(vm.getBlockNumber() > 0 ? vm.getBlockNumber() - 1 : 0));
        emit log_bytes32(blockhash(vm.getBlockNumber() - 2));
        emit log_bytes32(blockhash(vm.getBlockNumber() - 256));
        emit log_bytes32(blockhash(vm.getBlockNumber() - 257));
        emit log_bytes32(blockhash(height > 0 ? height - 1 : 0));
        emit log_bytes32(blockhash(vm.getBlockNumber()));
        emit log_bytes32(vm.envOr("OASIS_FORK_PARENT_HASH", bytes32(0)));
        emit log_string("=== END ===");
        // forge-lint: disable-end(reentrancy-events)
    }
}
