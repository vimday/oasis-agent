// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

interface ShadowToken {
    function balanceOf(address owner) external view returns (uint256);
    function transfer(address target, uint256 value) external returns (bool);
    function deposit() external payable;
}

interface ShadowFactory {
    function getPair(address tokenA, address tokenB) external view returns (address);
}

interface ShadowPair {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function getReserves() external view returns (uint112, uint112, uint32);
    function swap(uint256 out0, uint256 out1, address recipient, bytes calldata data) external;
}

/// @notice Runtime used as an eth_call account override for one pinned experiment.
contract EthV2WideShadowProbe {
    address private constant UNI = 0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f;
    address private constant SUSHI = 0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac;
    ShadowToken private constant WETH = ShadowToken(0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2);
    address private constant CALLER = address(0xd35c);
    address private constant PROBE = address(0xd35d);

    function observe(address[] calldata assets, address[] calldata pools, uint256 sushiMask)
        external
        payable
        returns (uint256 beforeWeth, uint256 afterWeth, uint256 bodyGas)
    {
        uint256 beforeGas = gasleft();
        require(address(this) == PROBE && msg.sender == CALLER && block.chainid == 1);
        require(pools.length >= 2 && pools.length <= 6 && assets.length == pools.length + 1);
        require(assets[0] == address(WETH) && assets[assets.length - 1] == address(WETH));
        require(msg.value > 0 && msg.value <= 10 ether && sushiMask >> pools.length == 0);
        beforeWeth = WETH.balanceOf(address(this));
        WETH.deposit{value: msg.value}();
        uint256 amount = msg.value;
        for (uint256 i; i < pools.length; ++i) {
            for (uint256 j; j < i; ++j) {
                require(pools[j] != pools[i]);
            }
            address factory = (sushiMask & (1 << i)) == 0 ? UNI : SUSHI;
            amount = hop(factory, ShadowPair(pools[i]), assets[i], assets[i + 1], amount);
        }
        afterWeth = WETH.balanceOf(address(this));
        require(afterWeth == beforeWeth + amount);
        bodyGas = beforeGas - gasleft();
    }

    function hop(address factory, ShadowPair pool, address tokenIn, address tokenOut, uint256 amount)
        private
        returns (uint256)
    {
        require(ShadowFactory(factory).getPair(tokenIn, tokenOut) == address(pool));
        address token0 = pool.token0();
        bool forward = tokenIn == token0;
        require(forward ? tokenOut == pool.token1() : tokenIn == pool.token1() && tokenOut == token0);
        (uint256 reserve0, uint256 reserve1,) = pool.getReserves();
        (uint256 reserveIn, uint256 reserveOut) = forward ? (reserve0, reserve1) : (reserve1, reserve0);
        uint256 amountWithFee = amount * 997;
        uint256 out = (amountWithFee * reserveOut) / (reserveIn * 1000 + amountWithFee);
        require(out > 0);
        uint256 beforeIn = ShadowToken(tokenIn).balanceOf(address(pool));
        uint256 beforeOut = ShadowToken(tokenOut).balanceOf(address(this));
        (bool ok, bytes memory result) = tokenIn.call(abi.encodeCall(ShadowToken.transfer, (address(pool), amount)));
        require(ok && (result.length == 0 || abi.decode(result, (bool))));
        require(ShadowToken(tokenIn).balanceOf(address(pool)) == beforeIn + amount);
        pool.swap(forward ? 0 : out, forward ? out : 0, address(this), "");
        require(ShadowToken(tokenOut).balanceOf(address(this)) == beforeOut + out);
        return out;
    }
}
