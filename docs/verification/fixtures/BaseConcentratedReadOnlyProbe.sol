// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

interface BaseCLReadPool {
    function slot0() external view returns (uint160 sqrtPriceX96);
    function swap(address recipient, bool zeroForOne, int256 amount, uint160 limit, bytes calldata data)
        external
        returns (int256 amount0, int256 amount1);
}

interface BaseCLReadFactory {
    function getPool(address token0, address token1, uint24 fee) external view returns (address);
}

interface BaseCLReadSlipFactory {
    function getPool(address token0, address token1, int24 spacing) external view returns (address);
}

interface BaseCLReadFees {
    function getL1FeeUpperBound(uint256 unsignedSize) external view returns (uint256);
    function getOperatorFee(uint256 gasLimit) external view returns (uint256);
}

interface BaseCLReadClock {
    function timestampMs() external view returns (uint256);
}

/// @dev A fixed-catalog, always-reverting callback for unsigned Base observations.
contract BaseConcentratedReadOnlyProbe {
    address private constant PROBE = address(uint160(0xa259));
    address private constant WETH = address(bytes20(hex"4200000000000000000000000000000000000006"));
    address private constant USDC = address(bytes20(hex"833589fcd6edb6e08f4c7c32d4f71b54bda02913"));
    address private constant CBBTC = address(bytes20(hex"cbb7c0000ab88b473b1f5afd9ef808440eed33bf"));
    address private constant AERO = address(bytes20(hex"940181a94a35a4569e4529a3cdfb74e38fd98631"));
    address private constant UNI = address(bytes20(hex"33128a8fc17869897dce68ed026d694621f6fdfd"));
    address private constant SLIP = address(bytes20(hex"5e7bb104d84c7cb9b682aac2f3d509f5f406809a"));
    address private constant SLIP_V2 = address(bytes20(hex"f8f2eb4940cfe7d13603dddd87f123820fc061ef"));
    BaseCLReadFees private constant FEES = BaseCLReadFees(address(uint160(0x420000000000000000000000000000000000000F)));
    BaseCLReadClock private constant CLOCK =
        BaseCLReadClock(address(uint160(0x4200000000000000000000000000000000000030)));
    uint160 private constant MIN = 4295128740;
    uint160 private constant MAX = 1461446703485210103287273052203988822378723970341;

    error Outcome(int256 delta0, int256 delta1);
    error InvalidObservation();

    struct Key {
        address factory;
        uint24 parameter;
    }

    struct Leg {
        uint8 status;
        uint256 consumed;
        uint256 output;
        uint256 spentGas;
        bytes4 selector;
        uint256 reasonLength;
    }

    struct Route {
        Leg first;
        Leg second;
    }

    struct Batch {
        uint256 number;
        uint256 timestampMs;
        uint256 l1Wei;
        uint256 operatorWei;
        address other;
        address[] pools;
        bytes32[] codeHashes;
        Route[] routes;
    }

    function market(uint8 cohort) private view returns (address other, address[] memory pools) {
        Key[] memory keys;
        if (cohort == 0) {
            other = USDC;
            keys = new Key[](6);
            keys[0] = Key(UNI, 100);
            keys[1] = Key(SLIP, 100);
            keys[2] = Key(UNI, 500);
            keys[3] = Key(SLIP, 1);
            keys[4] = Key(SLIP_V2, 1);
            keys[5] = Key(UNI, 3000);
        } else if (cohort == 1) {
            other = CBBTC;
            keys = new Key[](4);
            keys[0] = Key(UNI, 500);
            keys[1] = Key(UNI, 3000);
            keys[2] = Key(SLIP, 1);
            keys[3] = Key(SLIP, 100);
        } else if (cohort == 2) {
            other = AERO;
            keys = new Key[](3);
            keys[0] = Key(UNI, 3000);
            keys[1] = Key(UNI, 10000);
            keys[2] = Key(SLIP, 200);
        } else {
            revert InvalidObservation();
        }
        pools = new address[](keys.length);
        for (uint256 i = 0; i < keys.length; ++i) {
            Key memory k = keys[i];
            pools[i] = k.factory == UNI
                ? BaseCLReadFactory(k.factory).getPool(WETH, other, k.parameter)
                : BaseCLReadSlipFactory(k.factory).getPool(WETH, other, int24(k.parameter));
            if (pools[i].code.length == 0) revert InvalidObservation();
        }
    }

    function quote(uint8 cohort, uint256 amount, uint256 unsignedSize, uint16 band)
        external
        returns (Batch memory result)
    {
        if (
            block.chainid != 8453 || address(this) != PROBE || amount == 0 || amount > 1 ether || unsignedSize > 512
                || band == 0 || band > 1000
        ) revert InvalidObservation();
        (result.other, result.pools) = market(cohort);
        result.number = block.number;
        result.timestampMs = CLOCK.timestampMs();
        result.l1Wei = FEES.getL1FeeUpperBound(unsignedSize);
        result.operatorWei = FEES.getOperatorFee(550_000);
        result.codeHashes = new bytes32[](result.pools.length);
        result.routes = new Route[](result.pools.length * (result.pools.length - 1));
        uint256 next = 0;
        for (uint256 i = 0; i < result.pools.length; ++i) {
            address left = result.pools[i];
            result.codeHashes[i] = left.codehash;
            Leg memory first = leg(left, true, amount, band, 440_000);
            for (uint256 j = 0; j < result.pools.length; ++j) {
                if (i == j) continue;
                Leg memory second;
                if (first.status == 1) second = leg(result.pools[j], false, first.output, band, 440_000);
                result.routes[next++] = Route(first, second);
            }
        }
    }

    function diagnose(uint8 index, bool zeroForOne, uint256 amount, uint16 band, uint256 gasLimit)
        external
        returns (Leg memory)
    {
        if (
            block.chainid != 8453 || address(this) != PROBE || amount == 0 || amount > 1 ether || band > 1000
                || gasLimit < 100_000 || gasLimit > 2_000_000
        ) revert InvalidObservation();
        (, address[] memory pools) = market(0);
        return leg(pools[index], zeroForOne, amount, band, gasLimit);
    }

    function leg(address pool, bool zeroForOne, uint256 amount, uint16 band, uint256 gasLimit)
        private
        returns (Leg memory result)
    {
        uint160 sqrtLimit;
        if (band == 0) {
            sqrtLimit = zeroForOne ? MIN : MAX;
        } else {
            uint256 mid = BaseCLReadPool(pool).slot0();
            uint256 calculated = mid * (zeroForOne ? 10_000 - band : 10_000 + band) / 10_000;
            sqrtLimit = calculated < MIN ? MIN : calculated > MAX ? MAX : uint160(calculated);
        }
        assembly ("memory-safe") { tstore(0, pool) }
        uint256 gasBefore = gasleft();
        try BaseCLReadPool(pool).swap{gas: gasLimit}(address(this), zeroForOne, int256(amount), sqrtLimit, "") {
            revert InvalidObservation();
        } catch (bytes memory reason) {
            result.spentGas = gasBefore - gasleft();
            result.reasonLength = reason.length;
            result.selector = bytes4(reason);
            if (reason.length == 68 && result.selector == Outcome.selector) {
                int256 delta0;
                int256 delta1;
                assembly ("memory-safe") {
                    delta0 := mload(add(reason, 36))
                    delta1 := mload(add(reason, 68))
                }
                int256 input = zeroForOne ? delta0 : delta1;
                int256 output = zeroForOne ? delta1 : delta0;
                if (input < 0 || uint256(input) > amount || output > 0) revert InvalidObservation();
                result.consumed = uint256(input);
                result.output = uint256(-output);
                result.status = result.consumed == amount && result.output != 0 ? 1 : 2;
            } else {
                result.status = reason.length == 0 ? 3 : 4;
            }
        }
        assembly ("memory-safe") { tstore(0, 0) }
    }

    function uniswapV3SwapCallback(int256 delta0, int256 delta1, bytes calldata data) external view {
        address pool;
        assembly ("memory-safe") { pool := tload(0) }
        if (msg.sender != pool || data.length != 0) revert InvalidObservation();
        revert Outcome(delta0, delta1);
    }
}
