// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { SafeCast } from "@openzeppelin/contracts/utils/math/SafeCast.sol";

interface IProbeToken {
    function balanceOf(address owner) external view returns (uint256);
    function transfer(address to, uint256 amount) external returns (bool);
}

// Metric pool-contracts ddf8ae0cdb2ad0041c3ff3aebc200cf488d5b0d3.
// swap returns int128, but metricOmmSwapCallback receives int256.
interface IProbeMetricPool {
    function swap(
        address recipient,
        bool zeroForOne,
        int128 amount,
        uint128 limit,
        bytes calldata callbackData,
        bytes calldata extensionData
    ) external returns (int128 amount0, int128 amount1);
}

// Uniswap v3-core 5ea68a77cb541c6b3812df783be71ba8c081e25f IUniswapV3PoolActions.swap ABI.
interface IProbeV3Pool {
    function swap(
        address recipient,
        bool zeroForOne,
        int256 amount,
        uint160 limit,
        bytes calldata data
    ) external returns (int256 amount0, int256 amount1);
}

/// @notice Local research only: every probe reverts, including a fully settled profitable cycle.
/// @dev No approvals, transferFrom, arbitrary calls or signing; probe() cannot succeed.
///      First-pool output funds the second swap inside the first callback, then repays the first.
///      Runtime/config/proxy and source independence still require external evidence.
contract MetricV3RevertingProbe {
    using SafeCast for uint256;
    using SafeCast for int256;

    struct Config {
        address observer;
        address metric;
        address v3;
        address assetA;
        address assetB;
        bool metricToken0IsA;
        bytes32[4] codeHashes; // Metric, V3, assetA, assetB
        uint256 chainId;
        uint256 blockNumber;
        uint256 timestamp;
    }

    struct Request {
        bool metricFirst;
        bool startWithA;
        uint128 amountIn;
        uint128 minimumProfit;
        uint128 metricPriceLimitX64;
        uint160 v3SqrtPriceLimitX96;
        bytes extensionData;
    }

    error Rejected(uint8 reason);
    error ExternalFailure(uint8 phase);
    error ProbeResult(
        bytes32 requestHash,
        uint256 amountIn,
        uint256 intermediate,
        uint256 output,
        uint256 grossProfit,
        uint256 innerGasUsed
    );

    address public immutable observer;
    address public immutable metric;
    address public immutable v3;
    address public immutable assetA;
    address public immutable assetB;
    bool public immutable metricToken0IsA;
    bytes32 public immutable metricCode;
    bytes32 public immutable v3Code;
    bytes32 public immutable assetACode;
    bytes32 public immutable assetBCode;
    uint256 public immutable expectedChain;
    uint256 public immutable expectedBlock;
    uint256 public immutable expectedTimestamp;

    // All temporary state is reverted. The probe must be deployed locally or injected with empty
    // storage at an independently proven empty address, never over an existing contract.
    uint8 private phase; // 0 idle, 1 first callback, 2 second callback, 3 callbacks closed / settlement
    bool private firstIsMetric;
    bool private startA;
    uint128 private principal;
    uint128 private metricLimit;
    uint160 private v3Limit;
    uint256 private intermediate;
    uint256 private output;
    bytes32 private requestHash;
    bytes private extension;

    constructor(Config memory c) {
        if (
            c.observer == address(0) || c.metric == address(0) || c.v3 == address(0)
                || c.assetA == address(0) || c.assetA >= c.assetB || c.chainId == 0
                || c.timestamp == 0 || c.blockNumber == 0
        ) revert Rejected(1);
        address[5] memory addresses = [c.observer, c.metric, c.v3, c.assetA, c.assetB];
        bool duplicate = false;
        for (uint256 i; i < addresses.length; ++i) {
            for (uint256 j = i + 1; j < addresses.length; ++j) {
                if (addresses[i] == addresses[j]) duplicate = true;
            }
        }
        if (duplicate) revert Rejected(1);
        observer = c.observer;
        metric = c.metric;
        v3 = c.v3;
        assetA = c.assetA;
        assetB = c.assetB;
        metricToken0IsA = c.metricToken0IsA;
        metricCode = c.codeHashes[0];
        v3Code = c.codeHashes[1];
        assetACode = c.codeHashes[2];
        assetBCode = c.codeHashes[3];
        expectedChain = c.chainId;
        expectedBlock = c.blockNumber;
        expectedTimestamp = c.timestamp;
        _identity();
    }

    // These are reverted per-call parameters, not persistent admin configuration. An event would
    // also be reverted. The timestamp equality binds exact historical context, not randomness.
    // slither-disable-next-line events-maths,timestamp
    function probe(Request calldata r) external {
        if (msg.sender != observer || phase != 0) revert Rejected(2);
        if (
            block.chainid != expectedChain || block.number != expectedBlock
                // Exact historical EVM context pin, not randomness or a deadline assumption.
                // forge-lint: disable-next-line(block-timestamp)
                || block.timestamp != expectedTimestamp
        ) {
            revert Rejected(3);
        }
        if (r.amountIn == 0 || r.amountIn > (1 << 127) - 1 || r.extensionData.length > 1024) {
            revert Rejected(4);
        }
        _identity();
        if (
            _balance(assetA, address(this)) != 0 || _balance(assetB, address(this)) != 0
                || address(this).balance > 0
        ) revert Rejected(5);
        uint256 initialGas = gasleft();
        firstIsMetric = r.metricFirst;
        startA = r.startWithA;
        principal = r.amountIn;
        metricLimit = r.metricPriceLimitX64;
        v3Limit = r.v3SqrtPriceLimitX96;
        extension = r.extensionData;
        requestHash = keccak256(
            abi.encode(address(this), observer, expectedChain, expectedBlock, expectedTimestamp, r)
        );
        phase = 1;
        (int256 d0, int256 d1) = _swap(firstIsMetric, startA, principal, 1);
        if (phase != 3) revert Rejected(6);
        _returns(firstIsMetric, startA, d0, d1, principal, intermediate);
        _identity();
        address start = startA ? assetA : assetB;
        address middle = startA ? assetB : assetA;
        uint256 profit = _balance(start, address(this));
        if (
            _balance(middle, address(this)) != 0 || address(this).balance > 0 || output < principal
                || profit != output - principal || profit < r.minimumProfit
        ) {
            revert Rejected(7);
        }
        revert ProbeResult(
            requestHash, principal, intermediate, output, profit, initialGas - gasleft()
        );
    }

    function metricOmmSwapCallback(int256 d0, int256 d1, bytes calldata data) external {
        if (msg.sender != metric) revert Rejected(8);
        _callback(true, d0, d1, data);
    }

    function uniswapV3SwapCallback(int256 d0, int256 d1, bytes calldata data) external {
        if (msg.sender != v3) revert Rejected(8);
        _callback(false, d0, d1, data);
    }

    function _callback(bool isMetric, int256 d0, int256 d1, bytes calldata data) private {
        uint8 step = phase;
        if (
            (step != 1 && step != 2) || isMetric != (step == 1 ? firstIsMetric : !firstIsMetric)
                || data.length != 64 || keccak256(data) != keccak256(abi.encode(requestHash, step))
        ) {
            revert Rejected(9);
        }
        bool inputA = step == 1 ? startA : !startA;
        (uint256 amountIn, uint256 amountOut) = _deltas(isMetric, inputA, d0, d1);
        address start = startA ? assetA : assetB;
        address middle = startA ? assetB : assetA;
        if (step == 1) {
            if (
                amountIn != principal || _balance(middle, address(this)) != amountOut
                    || _balance(start, address(this)) != 0
            ) revert Rejected(10);
            intermediate = amountOut;
            phase = 2;
            (int256 s0, int256 s1) = _swap(!firstIsMetric, !startA, amountOut, 2);
            if (phase != 3) revert Rejected(6);
            _returns(!firstIsMetric, !startA, s0, s1, intermediate, output);
            if (
                _balance(start, address(this)) != output || output < principal
                    || _balance(middle, address(this)) != 0
            ) revert Rejected(11);
            _pay(start, msg.sender, principal);
        } else {
            if (
                amountIn != intermediate || _balance(start, address(this)) != amountOut
                    || _balance(middle, address(this)) != intermediate
            ) revert Rejected(12);
            output = amountOut;
            phase = 3;
            _pay(middle, msg.sender, intermediate);
        }
    }

    function _swap(bool isMetric, bool inputA, uint256 amount, uint8 step)
        private
        returns (int256 d0, int256 d1)
    {
        bool zeroForOne = isMetric ? inputA == metricToken0IsA : inputA;
        bytes memory data = abi.encode(requestHash, step);
        // _deltas restricts every intermediate to positive int128; principal is checked at entry.
        if (isMetric) {
            try IProbeMetricPool(metric)
                .swap(
                    address(this),
                    zeroForOne,
                    amount.toInt256().toInt128(),
                    metricLimit,
                    data,
                    extension
                ) returns (
                int128 a, int128 b
            ) {
                return (a, b);
            } catch {
                revert ExternalFailure(step);
            }
        } else {
            try IProbeV3Pool(v3)
                .swap(address(this), zeroForOne, amount.toInt256(), v3Limit, data) returns (
                int256 a, int256 b
            ) {
                return (a, b);
            } catch {
                revert ExternalFailure(step);
            }
        }
    }

    function _deltas(bool isMetric, bool inputA, int256 d0, int256 d1)
        private
        view
        returns (uint256 amountIn, uint256 amountOut)
    {
        bool zeroForOne = isMetric ? inputA == metricToken0IsA : inputA;
        (int256 incoming, int256 outgoing) = zeroForOne ? (d0, d1) : (d1, d0);
        int256 maximum = type(int128).max;
        if (incoming <= 0 || incoming > maximum || outgoing >= 0 || outgoing < -maximum) {
            revert Rejected(13);
        }
        return (incoming.toUint256(), (-outgoing).toUint256());
    }

    function _returns(
        bool isMetric,
        bool inputA,
        int256 d0,
        int256 d1,
        uint256 paid,
        uint256 received
    ) private view {
        (uint256 i, uint256 o) = _deltas(isMetric, inputA, d0, d1);
        if (i != paid || o != received) revert Rejected(14);
    }

    function _balance(address token, address account) private view returns (uint256) {
        try IProbeToken(token).balanceOf(account) returns (uint256 balance) {
            return balance;
        } catch {
            revert ExternalFailure(phase);
        }
    }

    // phase=3 closes both authenticated callbacks and probe() before transfers. Pre/post balance
    // comparison is intentional settlement reconciliation, not a cached spendable balance.
    // slither-disable-next-line reentrancy-balance
    function _pay(address token, address pool, uint256 amount) private {
        if (phase != 3) revert Rejected(9);
        uint256 beforePool = _balance(token, pool);
        uint256 beforeProbe = _balance(token, address(this));
        try IProbeToken(token).transfer(pool, amount) returns (bool ok) {
            if (!ok) revert Rejected(15);
        } catch {
            revert ExternalFailure(phase);
        }
        if (
            beforeProbe < amount || _balance(token, address(this)) != beforeProbe - amount
                || _balance(token, pool) < beforePool
                || _balance(token, pool) - beforePool != amount
        ) {
            revert Rejected(16);
        }
    }

    function _identity() private view {
        if (
            metric.code.length == 0 || v3.code.length == 0 || assetA.code.length == 0
                || assetB.code.length == 0 || metric.codehash != metricCode || v3.codehash != v3Code
                || assetA.codehash != assetACode || assetB.codehash != assetBCode
        ) revert Rejected(17);
    }
}
