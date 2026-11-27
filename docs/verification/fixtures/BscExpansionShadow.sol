// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

interface ExpansionToken {
    function deposit() external payable;
    function balanceOf(address account) external view returns (uint256);
    function transfer(address to, uint256 amount) external returns (bool);
}

interface ExpansionV2 {
    function getReserves() external view returns (uint112, uint112, uint32);
    function swap(uint256 amount0, uint256 amount1, address recipient, bytes calldata data) external;
}

interface ExpansionV3 {
    function swap(address recipient, bool zeroForOne, int256 amount, uint160 limit, bytes calldata data)
        external returns (int256, int256);
}

interface ExpansionFactoryV2 {
    function getPair(address asset0, address asset1) external view returns (address);
}

interface ExpansionFactoryV3 {
    function getPool(address asset0, address asset1, uint24 fee) external view returns (address);
}

/// @notice Fixed two-pool, no-auth shadow probe for a read-only account code override.
contract BscExpansionShadow {
    address private constant CALLER = address(0xd35c);
    address private constant PROBE = address(0xd35d);
    ExpansionToken private constant WBNB = ExpansionToken(0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c);
    ExpansionToken private constant USDT = ExpansionToken(0x55d398326f99059fF775485246999027B3197955);
    ExpansionFactoryV2 private constant F2 = ExpansionFactoryV2(0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73);
    ExpansionFactoryV3 private constant F3 = ExpansionFactoryV3(0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865);
    address private constant P2 = 0x16b9a82891338f9bA80E2D6970FddA79D1eb0daE;
    address private constant P3 = 0x172fcD41E0913e95784454622d1c3724f546f849;
    uint256 private constant PRINCIPAL = 100_000_000_000_000;
    uint160 private constant MAX_LIMIT = 1461446703485210103287273052203988822378723970341;

    function observe() external payable returns (uint256 beforeWbnb, uint256 afterWbnb, uint256 bodyGas) {
        uint256 gasBefore = gasleft();
        require(block.chainid == 56 && address(this) == PROBE && msg.sender == CALLER && msg.value == PRINCIPAL);
        require(F2.getPair(address(USDT), address(WBNB)) == P2);
        require(F3.getPool(address(USDT), address(WBNB), 100) == P3);
        beforeWbnb = WBNB.balanceOf(address(this));
        uint256 beforeUsdt = USDT.balanceOf(address(this));
        WBNB.deposit{value: PRINCIPAL}();
        (int256 receivedDelta, int256 paidDelta) =
            ExpansionV3(P3).swap(address(this), false, int256(PRINCIPAL), MAX_LIMIT, "");
        require(receivedDelta < 0 && paidDelta == int256(PRINCIPAL));
        uint256 received = uint256(-receivedDelta);
        require(USDT.balanceOf(address(this)) == beforeUsdt + received);
        (uint256 reserveIn, uint256 reserveOut,) = ExpansionV2(P2).getReserves();
        uint256 lessFee = received * 9975;
        uint256 output = lessFee * reserveOut / (reserveIn * 10_000 + lessFee);
        require(output > 0);
        pay(USDT, P2, received);
        ExpansionV2(P2).swap(0, output, address(this), "");
        afterWbnb = WBNB.balanceOf(address(this));
        require(afterWbnb == beforeWbnb + output && USDT.balanceOf(address(this)) == beforeUsdt);
        bodyGas = gasBefore - gasleft();
    }

    function pancakeV3SwapCallback(int256 delta0, int256 delta1, bytes calldata data) external {
        require(address(this) == PROBE && msg.sender == P3 && data.length == 0);
        require(delta0 < 0 && delta1 == int256(PRINCIPAL));
        pay(WBNB, P3, PRINCIPAL);
    }

    function pay(ExpansionToken currency, address recipient, uint256 amount) private {
        uint256 beforePool = currency.balanceOf(recipient);
        uint256 beforeProbe = currency.balanceOf(address(this));
        (bool success, bytes memory result) =
            address(currency).call(abi.encodeCall(ExpansionToken.transfer, (recipient, amount)));
        require(success && (result.length == 0 || abi.decode(result, (bool))));
        require(currency.balanceOf(recipient) == beforePool + amount);
        require(currency.balanceOf(address(this)) + amount == beforeProbe);
    }
}
