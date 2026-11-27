// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

interface BaseProbeToken {
    function balanceOf(address holder) external view returns (uint256);
    function transfer(address recipient, uint256 amount) external returns (bool);
}

interface BaseProbeWeth is BaseProbeToken {
    function deposit() external payable;
}

interface BaseProbePool {
    function swap(address recipient, bool zeroForOne, int256 amount, uint160 limit, bytes calldata data)
        external
        returns (int256 amount0, int256 amount1);
}

/// @dev An ephemeral two-pool fixture for an unsigned Base simulation.
contract BaseMixedWholeProbe {
    address private constant HARNESS = address(uint160(0xa257));
    address private constant ACTOR = address(uint160(0xdead));
    address private constant WETH = 0x4200000000000000000000000000000000000006;
    address private constant USDC = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913;
    address private constant UNI = address(bytes20(hex"b4cb800910b228ed3d0834cf79d697127bbb00e5"));
    address private constant AERO = address(bytes20(hex"b2cc224c1c9fee385f8ad6a55b4d94e92359dc59"));
    uint160 private constant MIN_LIMIT = 4295128740;
    uint160 private constant MAX_LIMIT = 1461446703485210103287273052203988822378723970341;

    error InvalidFixture();
    error UnsettledLeg();
    error MinimumOutput(uint256 actual, uint256 required);

    function probe(bool usdcFirst, uint256 principal, uint256 floor)
        external
        payable
        returns (uint256 intermediate, uint256 output)
    {
        if (
            block.chainid != 8453 || address(this) != HARNESS || msg.sender != ACTOR || principal == 0
                || principal > uint256(type(int256).max) || floor == 0 || msg.value != (usdcFirst ? 0 : principal)
        ) revert InvalidFixture();
        address firstToken = usdcFirst ? USDC : WETH;
        address secondToken = usdcFirst ? WETH : USDC;
        if (!usdcFirst) BaseProbeWeth(WETH).deposit{value: principal}();
        if (
            BaseProbeToken(firstToken).balanceOf(HARNESS) != principal
                || BaseProbeToken(secondToken).balanceOf(HARNESS) != 0
        ) revert InvalidFixture();

        intermediate = _swap(usdcFirst ? AERO : UNI, firstToken, principal);
        output = _swap(usdcFirst ? UNI : AERO, secondToken, intermediate);
        if (output < floor) revert MinimumOutput(output, floor);
        if (
            BaseProbeToken(firstToken).balanceOf(HARNESS) != output
                || BaseProbeToken(secondToken).balanceOf(HARNESS) != 0
        ) revert UnsettledLeg();
        uint256 before = BaseProbeToken(firstToken).balanceOf(ACTOR);
        if (
            !BaseProbeToken(firstToken).transfer(ACTOR, output)
                || BaseProbeToken(firstToken).balanceOf(ACTOR) != before + output
                || BaseProbeToken(firstToken).balanceOf(HARNESS) != 0
        ) revert UnsettledLeg();
    }

    function _swap(address pool, address token, uint256 amount) private returns (uint256) {
        assembly ("memory-safe") {
            tstore(0, pool)
            tstore(1, amount)
        }
        bool zeroForOne = token == WETH;
        (int256 d0, int256 d1) =
            BaseProbePool(pool).swap(HARNESS, zeroForOne, int256(amount), zeroForOne ? MIN_LIMIT : MAX_LIMIT, "");
        uint256 pending;
        assembly ("memory-safe") {
            pending := tload(1)
            tstore(0, 0)
        }
        (int256 input, int256 output) = zeroForOne ? (d0, d1) : (d1, d0);
        if (pending != 0 || input != int256(amount) || output >= 0) revert UnsettledLeg();
        return uint256(-output);
    }

    function uniswapV3SwapCallback(int256 d0, int256 d1, bytes calldata data) external {
        address pool;
        uint256 amount;
        assembly ("memory-safe") {
            pool := tload(0)
            amount := tload(1)
            tstore(1, 0)
        }
        if (
            msg.sender != pool || (pool != UNI && pool != AERO) || amount == 0 || data.length != 0
                || (pool == UNI ? (d0 != int256(amount) || d1 >= 0) : (d1 != int256(amount) || d0 >= 0))
        ) revert UnsettledLeg();
        if (!BaseProbeToken(pool == UNI ? WETH : USDC).transfer(pool, amount)) revert UnsettledLeg();
    }
}
