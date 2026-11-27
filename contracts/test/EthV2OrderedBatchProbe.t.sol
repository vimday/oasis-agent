// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { Test } from "forge-std/Test.sol";
import { EthV2OrderedBatchProbe } from "../src/research/EthV2OrderedBatchProbe.sol";
import { MockAtomicERC20, MockV2Pair } from "./OasisAtomicExecutor.t.sol";

contract MockBatchWeth is MockAtomicERC20 {
    constructor() MockAtomicERC20("WETH", "WETH") { }

    function deposit() external payable {
        this.mint(msg.sender, msg.value);
    }
}

contract MockBatchFactory {
    address private _pair;

    function setPair(address pair) external {
        _pair = pair;
    }

    function getPair(address, address) external view returns (address) {
        return _pair;
    }
}

contract EthV2OrderedBatchProbeTest is Test {
    address private constant WETH = 0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2;
    address private constant USDC = 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48;
    address private constant UNI_FACTORY = 0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f;
    address private constant SUSHI_FACTORY = 0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac;
    address private constant CALLER = address(0xd35c);
    address private constant PROBE = address(0xd35d);
    MockV2Pair private _uni;
    MockV2Pair private _sushi;

    function setUp() external {
        vm.chainId(1);
        MockBatchWeth weth = new MockBatchWeth();
        MockAtomicERC20 usdc = new MockAtomicERC20("USDC", "USDC");
        MockBatchFactory factory = new MockBatchFactory();
        vm.etch(WETH, address(weth).code);
        vm.etch(USDC, address(usdc).code);
        vm.etch(UNI_FACTORY, address(factory).code);
        vm.etch(SUSHI_FACTORY, address(factory).code);
        vm.etch(PROBE, vm.getDeployedCode("EthV2OrderedBatchProbe.sol:EthV2OrderedBatchProbe"));
        vm.deal(CALLER, 10 ether);

        _uni = new MockV2Pair(USDC, WETH, 30);
        _sushi = new MockV2Pair(USDC, WETH, 30);
        _fund(_uni, 1_025 ether, 1_000 ether);
        _fund(_sushi, 1_000 ether, 1_010 ether);
        MockBatchFactory(UNI_FACTORY).setPair(address(_uni));
        MockBatchFactory(SUSHI_FACTORY).setPair(address(_sushi));
    }

    function testSharedStateAndOneFundingWithALosingSecondRoute() external {
        EthV2OrderedBatchProbe.Route[] memory orders = new EthV2OrderedBatchProbe.Route[](2);
        orders[0] = _cycle();
        orders[1] = _cycle();
        vm.prank(CALLER);
        (
            address[] memory assets,
            uint256[] memory starts,
            uint256[] memory ends,
            uint256[][] memory amounts,
            uint256 bodyGas
        ) = EthV2OrderedBatchProbe(PROBE).observe{ value: 6 ether }(orders);

        assertEq(assets.length, 2);
        assertEq(assets[0], WETH);
        assertEq(assets[1], USDC);
        assertEq(starts[0], 6 ether);
        assertEq(starts[1], 0);
        assertEq(ends[0], 6_086_986_528_776_960_848);
        assertEq(ends[1], 0);
        assertEq(amounts[0][0], 5 ether);
        assertEq(amounts[0][1], 5_084_279_864_873_605_078);
        assertEq(amounts[0][2], 5_093_896_198_039_943_761);
        assertEq(amounts[1][0], 5 ether);
        assertEq(amounts[1][1], 5_034_015_222_873_216_016);
        assertEq(amounts[1][2], 4_993_090_330_737_017_087);
        assertTrue(bodyGas > 0);
        assertEq(MockAtomicERC20(WETH).balanceOf(PROBE), ends[0]);
        assertEq(MockAtomicERC20(USDC).balanceOf(PROBE), 0);
    }

    // These calls assert a revert so return data must be absent.
    // forge-lint: disable-start(unused-return)
    function testTaxRollsBackAllFundingAndPoolBalanceChanges() external {
        MockAtomicERC20(USDC).setSenderFeeBps(address(_uni), 100);
        vm.expectRevert(EthV2OrderedBatchProbe.UnexpectedBalance.selector);
        vm.prank(CALLER);
        EthV2OrderedBatchProbe(PROBE).observe{ value: 6 ether }(_orders());
        assertEq(MockAtomicERC20(WETH).balanceOf(PROBE), 0);
        assertEq(MockAtomicERC20(WETH).balanceOf(address(_uni)), 1_000 ether);
        assertEq(MockAtomicERC20(USDC).balanceOf(address(_uni)), 1_025 ether);
        assertEq(CALLER.balance, 10 ether);
    }

    function testInsufficientCombinedBankrollRollsBackEarlierRoute() external {
        EthV2OrderedBatchProbe.Route[] memory orders = new EthV2OrderedBatchProbe.Route[](2);
        orders[0] = _cycle();
        orders[1] = _cycle();
        orders[1].amountIn = 7 ether;
        vm.expectRevert(EthV2OrderedBatchProbe.InsufficientCapital.selector);
        vm.prank(CALLER);
        EthV2OrderedBatchProbe(PROBE).observe{ value: 6 ether }(orders);
        assertEq(MockAtomicERC20(WETH).balanceOf(address(_uni)), 1_000 ether);
        assertEq(MockAtomicERC20(WETH).balanceOf(PROBE), 0);
    }

    function testRejectsUnregisteredPairAndAsset() external {
        EthV2OrderedBatchProbe.Route[] memory orders = _orders();
        orders[0].sushiMask = 1;
        vm.expectRevert(EthV2OrderedBatchProbe.UnregisteredPair.selector);
        vm.prank(CALLER);
        EthV2OrderedBatchProbe(PROBE).observe{ value: 6 ether }(orders);

        orders[0].assets[1] = address(0x1234);
        vm.expectRevert(EthV2OrderedBatchProbe.InvalidRoute.selector);
        vm.prank(CALLER);
        EthV2OrderedBatchProbe(PROBE).observe{ value: 6 ether }(orders);
    }

    function testDeploymentAndWrongChainAreRejected() external {
        vm.expectRevert(EthV2OrderedBatchProbe.DeploymentDisabled.selector);
        new EthV2OrderedBatchProbe();
        vm.chainId(31337);
        vm.expectRevert(EthV2OrderedBatchProbe.InvalidShadowContext.selector);
        vm.prank(CALLER);
        EthV2OrderedBatchProbe(PROBE).observe{ value: 6 ether }(_orders());
    }
    // forge-lint: disable-end(unused-return)

    function _orders() private view returns (EthV2OrderedBatchProbe.Route[] memory orders) {
        orders = new EthV2OrderedBatchProbe.Route[](1);
        orders[0] = _cycle();
    }

    function _cycle() private view returns (EthV2OrderedBatchProbe.Route memory route) {
        route.assets = new address[](3);
        route.assets[0] = WETH;
        route.assets[1] = USDC;
        route.assets[2] = WETH;
        route.pools = new address[](2);
        route.pools[0] = address(_uni);
        route.pools[1] = address(_sushi);
        route.sushiMask = 2;
        route.amountIn = 5 ether;
    }

    function _fund(MockV2Pair pair, uint256 usdc, uint256 weth) private {
        MockAtomicERC20(USDC).mint(address(pair), usdc);
        MockAtomicERC20(WETH).mint(address(pair), weth);
        pair.syncReserves();
    }
}
