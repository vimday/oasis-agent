// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import { PoolKey } from "@uniswap/v4-core/src/types/PoolKey.sol";

/// @dev Minimal read-only interface used by the liquidity-add authorization hook.
interface IOasisPositionManager {
    function ownerOf(uint256 tokenId) external view returns (address owner);

    function getPoolAndPositionInfo(uint256 tokenId)
        external
        view
        returns (PoolKey memory poolKey, uint256 packedPositionInfo);
}
