// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import { IERC20 } from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import { IV2Pair } from "../OasisAtomicExecutor.sol";

interface IBatchShadowPair is IV2Pair {
    function token0() external view returns (address);
    function token1() external view returns (address);
}

interface IBatchShadowFactory {
    function getPair(address tokenA, address tokenB) external view returns (address);
}

interface IBatchShadowWeth is IERC20 {
    function deposit() external payable;
}

/// @notice Ethereum V2 ordered-batch probe for an eth_call account code override.
/// @dev The constructor refuses deployment. The successful return only describes simulated state.
contract EthV2OrderedBatchProbe {
    struct Route {
        address[] assets;
        address[] pools;
        uint256 sushiMask;
        uint256 amountIn;
    }

    struct HopBalances {
        uint256 reserveIn;
        uint256 reserveOut;
        uint256 probeIn;
        uint256 probeOut;
    }

    address private constant UNI_FACTORY = 0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f;
    address private constant SUSHI_FACTORY = 0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac;
    address private constant WETH = 0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2;
    address private constant CALLER = address(0xd35c);
    address private constant PROBE = address(0xd35d);
    uint256 private constant MAX_CAPITAL = 10 ether;
    uint256 private constant MAX_ROUTES = 8;
    uint256 private constant MAX_LEGS = 24;

    error DeploymentDisabled();
    error InvalidShadowContext();
    error InvalidRoute();
    error UnregisteredPair();
    error InsufficientCapital();
    error InsufficientDepth();
    error UnexpectedBalance();
    error TransferFailed();

    constructor() {
        revert DeploymentDisabled();
    }

    // Each shadow batch has hard route/leg bounds. Per-hop external calls observe the
    // actual shared state. Exact balance checks and a whole-batch revert reject ambiguity.
    // slither-disable-start calls-loop
    // forge-lint: disable-start(calls-loop)
    // forge-lint: disable-start(require-revert-in-loop)
    // forge-lint: disable-start(incorrect-strict-equality)
    function observe(Route[] calldata routes)
        external
        payable
        returns (
            address[] memory assets,
            uint256[] memory startBalances,
            uint256[] memory endBalances,
            uint256[][] memory routeAmounts,
            uint256 functionBodyGas
        )
    {
        uint256 startGas = gasleft();
        if (
            block.chainid != 1 || address(this) != PROBE || msg.sender != CALLER || msg.value == 0
                || msg.value > MAX_CAPITAL || address(this).balance != msg.value
        ) revert InvalidShadowContext();

        assets = _validateAndListAssets(routes);
        for (uint256 i; i < assets.length; ++i) {
            if (IERC20(assets[i]).balanceOf(address(this)) != 0) revert UnexpectedBalance();
        }
        IBatchShadowWeth(WETH).deposit{ value: msg.value }();
        startBalances = _balances(assets);
        if (startBalances[0] != msg.value) revert UnexpectedBalance();
        for (uint256 i = 1; i < assets.length; ++i) {
            if (startBalances[i] != 0) revert UnexpectedBalance();
        }

        routeAmounts = new uint256[][](routes.length);
        uint256 bankroll = msg.value;
        for (uint256 i; i < routes.length; ++i) {
            Route calldata route = routes[i];
            if (route.amountIn > bankroll) revert InsufficientCapital();
            uint256[] memory amounts = new uint256[](route.assets.length);
            amounts[0] = route.amountIn;
            for (uint256 j; j < route.pools.length; ++j) {
                address factory = (route.sushiMask & (1 << j)) == 0 ? UNI_FACTORY : SUSHI_FACTORY;
                amounts[j + 1] = _hop(
                    factory, route.pools[j], route.assets[j], route.assets[j + 1], amounts[j]
                );
            }
            bankroll = bankroll - route.amountIn + amounts[amounts.length - 1];
            if (IERC20(WETH).balanceOf(address(this)) != bankroll) revert UnexpectedBalance();
            routeAmounts[i] = amounts;
        }

        endBalances = _balances(assets);
        if (endBalances[0] != bankroll || address(this).balance != 0) revert UnexpectedBalance();
        for (uint256 i = 1; i < assets.length; ++i) {
            if (endBalances[i] != startBalances[i]) revert UnexpectedBalance();
        }
        functionBodyGas = startGas - gasleft();
    }

    function _validateAndListAssets(Route[] calldata routes)
        private
        pure
        returns (address[] memory assets)
    {
        if (routes.length == 0 || routes.length > MAX_ROUTES) revert InvalidRoute();
        address[MAX_LEGS + 1] memory seen;
        seen[0] = WETH;
        uint256 assetCount = 1;
        uint256 totalLegs = 0;
        for (uint256 i; i < routes.length; ++i) {
            Route calldata route = routes[i];
            uint256 legs = route.pools.length;
            totalLegs += legs;
            if (
                legs < 2 || legs > 6 || totalLegs > MAX_LEGS || route.assets.length != legs + 1
                    || route.assets[0] != WETH || route.assets[legs] != WETH
                    || route.sushiMask >> legs != 0 || route.amountIn == 0
                    || route.amountIn > MAX_CAPITAL
            ) revert InvalidRoute();
            for (uint256 j; j < legs; ++j) {
                address next = route.assets[j + 1];
                if (next == route.assets[j] || !_assetAllowed(next)) revert InvalidRoute();
                for (uint256 k; k < j; ++k) {
                    if (route.pools[k] == route.pools[j]) revert InvalidRoute();
                }
                uint256 index = 0;
                while (index < assetCount && next != seen[index]) ++index;
                if (index == assetCount) seen[assetCount++] = next;
            }
        }
        assets = new address[](assetCount);
        for (uint256 i; i < assetCount; ++i) assets[i] = seen[i];
    }

    function _balances(address[] memory assets) private view returns (uint256[] memory values) {
        values = new uint256[](assets.length);
        for (uint256 i; i < assets.length; ++i) {
            values[i] = IERC20(assets[i]).balanceOf(address(this));
        }
    }

    function _hop(
        address factory,
        address pairAddress,
        address tokenIn,
        address tokenOut,
        uint256 amount
    ) private returns (uint256 amountOut) {
        if (
            pairAddress == address(0)
                || IBatchShadowFactory(factory).getPair(tokenIn, tokenOut) != pairAddress
        ) revert UnregisteredPair();
        IBatchShadowPair pair = IBatchShadowPair(pairAddress);
        // All fields are assigned from checked reserves and balances before any read.
        // slither-disable-next-line uninitialized-local
        HopBalances memory initial;
        bool forward;
        (forward, initial.reserveIn, initial.reserveOut) = _pairReserves(pair, tokenIn, tokenOut);
        if (
            initial.reserveIn == 0 || initial.reserveOut == 0
                || amount > type(uint112).max - initial.reserveIn
        ) {
            revert InsufficientDepth();
        }
        uint256 effectiveInput = amount * 997;
        amountOut =
            effectiveInput * initial.reserveOut / (initial.reserveIn * 1000 + effectiveInput);
        if (amountOut == 0) revert InsufficientDepth();

        if (
            IERC20(tokenIn).balanceOf(pairAddress) != initial.reserveIn
                || IERC20(tokenOut).balanceOf(pairAddress) != initial.reserveOut
        ) revert UnexpectedBalance();
        initial.probeIn = IERC20(tokenIn).balanceOf(address(this));
        initial.probeOut = IERC20(tokenOut).balanceOf(address(this));
        // The token, recipient and selector are all fixed by the validated V2 hop.
        (bool success, bytes memory data) =
            tokenIn.call(abi.encodeCall(IERC20.transfer, (pairAddress, amount)));
        if (!success || (data.length != 0 && (data.length != 32 || !abi.decode(data, (bool))))) {
            revert TransferFailed();
        }
        if (
            IERC20(tokenIn).balanceOf(pairAddress) != initial.reserveIn + amount
                || IERC20(tokenIn).balanceOf(address(this)) + amount != initial.probeIn
        ) revert UnexpectedBalance();
        pair.swap(forward ? 0 : amountOut, forward ? amountOut : 0, address(this), "");
        if (
            IERC20(tokenIn).balanceOf(pairAddress) != initial.reserveIn + amount
                || IERC20(tokenOut).balanceOf(pairAddress) != initial.reserveOut - amountOut
                || IERC20(tokenIn).balanceOf(address(this)) + amount != initial.probeIn
                || IERC20(tokenOut).balanceOf(address(this)) != initial.probeOut + amountOut
        ) revert UnexpectedBalance();
    }

    function _pairReserves(IBatchShadowPair pair, address tokenIn, address tokenOut)
        private
        view
        returns (bool forward, uint256 reserveIn, uint256 reserveOut)
    {
        address token0 = pair.token0();
        address token1 = pair.token1();
        forward = tokenIn == token0;
        if (forward ? tokenOut != token1 : tokenIn != token1 || tokenOut != token0) {
            revert UnregisteredPair();
        }
        // The timestamp has no role in a single pinned reserve snapshot.
        // forge-lint: disable-start(unused-return)
        // slither-disable-next-line unused-return
        (uint256 reserve0, uint256 reserve1,) = pair.getReserves();
        // forge-lint: disable-end(unused-return)
        (reserveIn, reserveOut) = forward ? (reserve0, reserve1) : (reserve1, reserve0);
    }
    // forge-lint: disable-end(incorrect-strict-equality)
    // forge-lint: disable-end(require-revert-in-loop)
    // forge-lint: disable-end(calls-loop)
    // slither-disable-end calls-loop

    function _assetAllowed(address asset) private pure returns (bool) {
        return asset == WETH || asset == 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48
            || asset == 0xdAC17F958D2ee523a2206206994597C13D831ec7
            || asset == 0x6B175474E89094C44Da98b954EedeAC495271d0F
            || asset == 0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599
            || asset == 0x514910771AF9Ca656af840dff83E8264EcF986CA
            || asset == 0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984
            || asset == 0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9
            || asset == 0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE
            || asset == 0x6982508145454Ce325dDbE47a25d4ec3d2311933
            || asset == 0x5A98FcBEA516Cf06857215779Fd812CA3beF1B32
            || asset == 0xD533a949740bb3306d119CC777fa900bA034cd52
            || asset == 0xC18360217D8F7Ab5e7c516566761Ea12Ce7F9D72
            || asset == 0xc00e94Cb662C3520282E6f5717214004A7f26888;
    }
}
