// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { MockAtomicERC20, MockV2Pair } from "./OasisAtomicExecutor.t.sol";

/// @dev Local simulation fixture. Pools and tokens are disposable test contracts.
contract V2OrderedRouteFixture {
    MockAtomicERC20 private immutable _anchor;
    address payable private immutable _beneficiary;

    constructor(MockAtomicERC20 anchor, address payable beneficiary) {
        _anchor = anchor;
        _beneficiary = beneficiary;
    }

    function execute(MockV2Pair[][] calldata routes, uint256 amountIn)
        external
        payable
        returns (uint256 gross)
    {
        uint256 initial = _anchor.balanceOf(address(this));
        for (uint256 i; i < routes.length; ++i) {
            uint256 beforeRoute = _anchor.balanceOf(address(this));
            uint256 amount = amountIn;
            MockAtomicERC20 token = _anchor;
            for (uint256 j; j < routes[i].length; ++j) {
                (token, amount) = _swap(routes[i][j], token, amount);
            }
            require(token == _anchor && _anchor.balanceOf(address(this)) >= beforeRoute, "LOSS");
        }
        gross = _anchor.balanceOf(address(this)) - initial;
        (bool paid,) = _beneficiary.call{ value: msg.value }("");
        require(paid, "PAYMENT");
    }

    function shiftReserve(MockV2Pair pair, uint256 amount) external {
        _anchor.mint(address(pair), amount);
        pair.syncReserves();
    }

    function _swap(MockV2Pair pair, MockAtomicERC20 token, uint256 amount)
        private
        returns (MockAtomicERC20 output, uint256 received)
    {
        bool forward = address(token) == pair.token0();
        require(forward || address(token) == pair.token1(), "PATH");
        output = MockAtomicERC20(forward ? pair.token1() : pair.token0());
        (uint112 reserve0, uint112 reserve1,) = pair.getReserves();
        (uint256 reserveIn, uint256 reserveOut) =
            forward ? (reserve0, reserve1) : (reserve1, reserve0);
        uint256 effectiveInput = amount * (10_000 - pair.feeBps());
        uint256 out = (effectiveInput * reserveOut) / (reserveIn * 10_000 + effectiveInput);
        uint256 beforeSwap = output.balanceOf(address(this));
        require(token.transfer(address(pair), amount), "INPUT");
        pair.swap(forward ? 0 : out, forward ? out : 0, address(this), "");
        received = output.balanceOf(address(this)) - beforeSwap;
    }
}
