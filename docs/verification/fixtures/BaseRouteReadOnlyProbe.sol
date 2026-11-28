// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// Read-only route probe for eth_call with a state override only. Each hop calls the real
/// pool `swap` and reverts from the callback, so no state ever changes and no token moves.
/// The callback reverts with the exact pool deltas; the next hop's input is the prior output.
interface IClPool {
    function swap(address recipient, bool zeroForOne, int256 amount, uint160 limit, bytes calldata data)
        external
        returns (int256, int256);
    function token0() external view returns (address);
}

contract BaseRouteReadOnlyProbe {
    error Outcome(int256 delta0, int256 delta1);
    error Probe();

    uint160 private constant MIN = 4295128740;
    uint160 private constant MAX = 1461446703485210103287273052203988822378723970341;

    struct Hop {
        address pool;
        address tokenIn;
    }

    function quote(Hop[] calldata hops, uint256 amountIn)
        external
        returns (uint256[] memory outputs, uint256[] memory gasUsed)
    {
        if (block.chainid != 8453 || hops.length == 0 || hops.length > 8 || amountIn == 0) revert Probe();
        outputs = new uint256[](hops.length);
        gasUsed = new uint256[](hops.length);
        uint256 amount = amountIn;
        for (uint256 i; i < hops.length; ++i) {
            bool zeroForOne = IClPool(hops[i].pool).token0() == hops[i].tokenIn;
            assembly ("memory-safe") {
                tstore(0, calldataload(add(hops.offset, mul(i, 0x40))))
            }
            uint256 before = gasleft();
            try IClPool(hops[i].pool).swap(address(this), zeroForOne, int256(amount), zeroForOne ? MIN : MAX, "") {
                revert Probe();
            } catch (bytes memory reason) {
                gasUsed[i] = before - gasleft();
                if (reason.length != 68 || bytes4(reason) != Outcome.selector) revert Probe();
                int256 d0;
                int256 d1;
                assembly ("memory-safe") {
                    d0 := mload(add(reason, 36))
                    d1 := mload(add(reason, 68))
                }
                int256 input = zeroForOne ? d0 : d1;
                int256 output = zeroForOne ? d1 : d0;
                if (input <= 0 || uint256(input) != amount || output >= 0) revert Probe();
                amount = uint256(-output);
                outputs[i] = amount;
            }
        }
        assembly ("memory-safe") { tstore(0, 0) }
    }

    function uniswapV3SwapCallback(int256 delta0, int256 delta1, bytes calldata) external view {
        address pool;
        assembly ("memory-safe") { pool := tload(0) }
        if (msg.sender != pool) revert Probe();
        revert Outcome(delta0, delta1);
    }
}
