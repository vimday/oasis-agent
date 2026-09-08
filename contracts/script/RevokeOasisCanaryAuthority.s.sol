// SPDX-License-Identifier: BUSL-1.1
pragma solidity 0.8.26;

import { Script } from "forge-std/Script.sol";
import { IERC20 } from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import { SafeERC20 } from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import { PositionManager } from "@uniswap/v4-periphery/src/PositionManager.sol";
import { IAllowanceTransfer } from "permit2/src/interfaces/IAllowanceTransfer.sol";

/// @notice Emergency cleanup for partial canary setup or operator-requested authority removal.
/// @dev Revokes both ERC-20/Permit2 allowance layers. When tokenId is nonzero it also revokes
///      the single-token PositionManager approval. This script never reads a private key.
contract RevokeOasisCanaryAuthority is Script {
    using SafeERC20 for IERC20;

    error WrongChain(uint256 actual, uint256 expected);
    error InvalidConfiguration();
    error CodeHashMismatch();
    error PositionNotOwned();
    error PostconditionFailed();

    struct RevokeConfig {
        uint256 chainId;
        address lpOwner;
        PositionManager positionManager;
        IAllowanceTransfer permit2;
        IERC20 token0;
        IERC20 token1;
        uint256 tokenId;
        bytes32 positionManagerHash;
        bytes32 permit2Hash;
    }

    function run() external {
        RevokeConfig memory config = _loadConfig();
        if (block.chainid != config.chainId) revert WrongChain(block.chainid, config.chainId);
        if (
            config.lpOwner == address(0) || address(config.positionManager) == address(0)
                || address(config.permit2) == address(0) || address(config.token0) == address(0)
                || address(config.token0) >= address(config.token1)
        ) revert InvalidConfiguration();
        if (
            address(config.positionManager).codehash != config.positionManagerHash
                || address(config.permit2).codehash != config.permit2Hash
        ) revert CodeHashMismatch();
        if (address(config.positionManager.permit2()) != address(config.permit2)) {
            revert InvalidConfiguration();
        }
        if (config.tokenId != 0 && config.positionManager.ownerOf(config.tokenId) != config.lpOwner)
        {
            revert PositionNotOwned();
        }
        (,, uint48 nonce0Before) = config.permit2
            .allowance(config.lpOwner, address(config.token0), address(config.positionManager));
        (,, uint48 nonce1Before) = config.permit2
            .allowance(config.lpOwner, address(config.token1), address(config.positionManager));

        vm.startBroadcast(config.lpOwner);
        config.permit2.approve(address(config.token0), address(config.positionManager), 0, 0);
        config.permit2.approve(address(config.token1), address(config.positionManager), 0, 0);
        config.token0.forceApprove(address(config.permit2), 0);
        config.token1.forceApprove(address(config.permit2), 0);
        if (config.tokenId != 0) config.positionManager.approve(address(0), config.tokenId);
        vm.stopBroadcast();

        (uint160 amount0, uint48 expiry0, uint48 nonce0After) = config.permit2
            .allowance(config.lpOwner, address(config.token0), address(config.positionManager));
        (uint160 amount1, uint48 expiry1, uint48 nonce1After) = config.permit2
            .allowance(config.lpOwner, address(config.token1), address(config.positionManager));
        if (
            amount0 != 0 || amount1 != 0 || expiry0 != 0 || expiry1 != 0
                || nonce0After != nonce0Before || nonce1After != nonce1Before
                || config.token0.allowance(config.lpOwner, address(config.permit2)) != 0
                || config.token1.allowance(config.lpOwner, address(config.permit2)) != 0
                || (config.tokenId != 0
                    && config.positionManager.getApproved(config.tokenId) != address(0))
        ) revert PostconditionFailed();
    }

    function _loadConfig() private view returns (RevokeConfig memory config) {
        config = RevokeConfig({
            chainId: vm.envUint("OASIS_CHAIN_ID"),
            lpOwner: vm.envAddress("OASIS_LP_OWNER"),
            positionManager: PositionManager(payable(vm.envAddress("OASIS_POSITION_MANAGER"))),
            permit2: IAllowanceTransfer(vm.envAddress("OASIS_PERMIT2")),
            token0: IERC20(vm.envAddress("OASIS_TOKEN0")),
            token1: IERC20(vm.envAddress("OASIS_TOKEN1")),
            tokenId: vm.envUint("OASIS_TOKEN_ID"),
            positionManagerHash: vm.envBytes32("OASIS_POSITION_MANAGER_HASH"),
            permit2Hash: vm.envBytes32("OASIS_PERMIT2_HASH")
        });
    }
}
