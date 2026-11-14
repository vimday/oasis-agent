// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.26;

import { Test } from "forge-std/Test.sol";
import { MetricV3RevertingProbe } from "../src/research/MetricV3RevertingProbe.sol";

interface IProbeCallbacks {
    function metricOmmSwapCallback(int256 d0, int256 d1, bytes calldata data) external;
    function uniswapV3SwapCallback(int256 d0, int256 d1, bytes calldata data) external;
}

contract ProbeTestToken {
    mapping(address => uint256) private balances;
    uint8 public failure;
    uint8 public immutable decimals;
    address private attackSender;
    address private attackTarget;
    bytes private attackData;

    constructor(uint8 d) {
        decimals = d;
    }

    function mint(address to, uint256 value) external {
        balances[to] += value;
    }

    function fail(uint8 mode) external {
        failure = mode;
    }

    function attack(address sender, address target, bytes calldata data) external {
        attackSender = sender;
        attackTarget = target;
        attackData = data;
    }

    function balanceOf(address account) external view returns (uint256) {
        if (failure == 3) revert MetricV3RevertingProbe.ProbeResult(bytes32(0), 1, 2, 3, 4, 5);
        return balances[account];
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        if (failure == 1) return false;
        if (failure == 4) revert MetricV3RevertingProbe.ProbeResult(bytes32(0), 1, 2, 3, 4, 5);
        if (msg.sender == attackSender && attackTarget != address(0)) {
            (bool ok,) = attackTarget.call(attackData);
            require(ok, "REENTRY_REJECTED");
        }
        balances[msg.sender] -= amount;
        balances[to] += amount - (failure == 2 ? 1 : 0);
        return true;
    }
}

/// @dev Synthetic transfer/callback model only; not Metric bin math or real V3 tick behavior.
contract ProbeTestPool {
    ProbeTestToken public immutable token0;
    ProbeTestToken public immutable token1;
    bool public immutable isMetric;
    uint8 public failure;
    uint256 public numerator = 1;
    uint256 public denominator = 1;
    address private callbackTarget;
    bytes private lastData;
    int256 private last0;
    int256 private last1;

    constructor(ProbeTestToken a, ProbeTestToken b, bool metric) {
        token0 = a;
        token1 = b;
        isMetric = metric;
    }

    function fail(uint8 mode) external {
        failure = mode;
    }

    function rate(uint256 n, uint256 d) external {
        numerator = n;
        denominator = d;
    }

    function attemptCallback() external {
        if (isMetric) {
            IProbeCallbacks(callbackTarget).metricOmmSwapCallback(last0, last1, lastData);
        } else {
            IProbeCallbacks(callbackTarget).uniswapV3SwapCallback(last0, last1, lastData);
        }
    }

    function swap(
        address to,
        bool direction,
        int128 amount,
        uint128 limit,
        bytes calldata data,
        bytes calldata extension
    ) external returns (int128, int128) {
        require(isMetric && limit == 123 && keccak256(extension) == keccak256(hex"abcd"));
        (int256 a, int256 b) = _swap(to, direction, uint256(int256(amount)), data);
        require(a >= type(int128).min && a <= type(int128).max);
        require(b >= type(int128).min && b <= type(int128).max);
        return (int128(a), int128(b));
    }

    function swap(address to, bool direction, int256 amount, uint160 limit, bytes calldata data)
        external
        returns (int256, int256)
    {
        require(!isMetric && limit == 456);
        return _swap(to, direction, uint256(amount), data);
    }

    function _swap(address to, bool direction, uint256 input, bytes calldata data)
        private
        returns (int256 d0, int256 d1)
    {
        if (failure == 9) {
            revert MetricV3RevertingProbe.ProbeResult(bytes32(0), 1, 2, 3, 4, 5);
        }
        ProbeTestToken incoming = direction ? token0 : token1;
        ProbeTestToken outgoing = direction ? token1 : token0;
        uint256 consumed = input - (failure == 1 ? 1 : 0);
        uint256 out = consumed * numerator / denominator;
        uint256 beforeBalance = incoming.balanceOf(address(this));
        require(outgoing.transfer(to, out - (failure == 6 ? 1 : 0)));
        (d0, d1) = direction ? (int256(consumed), -int256(out)) : (-int256(out), int256(consumed));
        if (failure == 2) return (d0, d1);
        bytes memory callbackData = failure == 4 ? abi.encode(bytes32(0), uint8(1)) : data;
        if (failure == 10) d0 = type(int256).min;
        if (failure == 11) (d0, d1) = (int256(1), int256(1));
        if (failure == 12) {
            if (direction) d0 += 1;
            else d1 += 1;
        }
        callbackTarget = msg.sender;
        lastData = callbackData;
        last0 = d0;
        last1 = d1;
        if (failure == 7) {
            MetricV3RevertingProbe.Request memory r;
            MetricV3RevertingProbe(msg.sender).probe(r);
        }
        if (isMetric != (failure == 8)) {
            IProbeCallbacks(msg.sender).metricOmmSwapCallback(d0, d1, callbackData);
            if (failure == 3) {
                IProbeCallbacks(msg.sender).metricOmmSwapCallback(d0, d1, callbackData);
            }
        } else {
            IProbeCallbacks(msg.sender).uniswapV3SwapCallback(d0, d1, callbackData);
            if (failure == 3) {
                IProbeCallbacks(msg.sender).uniswapV3SwapCallback(d0, d1, callbackData);
            }
        }
        require(incoming.balanceOf(address(this)) == beforeBalance + consumed);
        if (failure == 5) d0 += 1;
    }
}

contract MetricV3RevertingProbeTest is Test {
    ProbeTestToken private a;
    ProbeTestToken private b;
    ProbeTestPool private metric;
    ProbeTestPool private v3;
    MetricV3RevertingProbe private probe;
    uint256 private constant STOCK = 10 ** 36;

    function setUp() public {
        vm.chainId(1234567);
        vm.roll(1234);
        vm.warp(1_800_000_000);
        ProbeTestToken x = new ProbeTestToken(6);
        ProbeTestToken y = new ProbeTestToken(18);
        (a, b) = address(x) < address(y) ? (x, y) : (y, x);
        _deploy(false); // Metric's ordering differs from V3's canonical address order.
    }

    function _deploy(bool metricToken0IsA) private {
        metric = new ProbeTestPool(metricToken0IsA ? a : b, metricToken0IsA ? b : a, true);
        v3 = new ProbeTestPool(a, b, false);
        v3.rate(101, 100);
        a.mint(address(metric), STOCK);
        b.mint(address(metric), STOCK);
        a.mint(address(v3), STOCK);
        b.mint(address(v3), STOCK);
        probe = new MetricV3RevertingProbe(_config(metricToken0IsA));
    }

    function _config(bool metricToken0IsA)
        private
        view
        returns (MetricV3RevertingProbe.Config memory c)
    {
        c.observer = address(this);
        c.metric = address(metric);
        c.v3 = address(v3);
        c.assetA = address(a);
        c.assetB = address(b);
        c.metricToken0IsA = metricToken0IsA;
        c.codeHashes = [
            address(metric).codehash, address(v3).codehash, address(a).codehash, address(b).codehash
        ];
        c.chainId = block.chainid;
        c.blockNumber = block.number;
        c.timestamp = block.timestamp;
    }

    function _request(bool metricFirst, bool startA, uint128 amount)
        private
        pure
        returns (MetricV3RevertingProbe.Request memory r)
    {
        r.metricFirst = metricFirst;
        r.startWithA = startA;
        r.amountIn = amount;
        r.minimumProfit = 1;
        r.metricPriceLimitX64 = 123;
        r.v3SqrtPriceLimitX96 = 456;
        r.extensionData = hex"abcd";
    }

    function _invoke(MetricV3RevertingProbe.Request memory r)
        private
        returns (bytes memory result)
    {
        (bool ok, bytes memory data) = address(probe).call(abi.encodeCall(probe.probe, (r)));
        assertFalse(ok, "probe must never settle state");
        return data;
    }

    function _unchanged() private view {
        assertEq(a.balanceOf(address(probe)), 0);
        assertEq(b.balanceOf(address(probe)), 0);
        assertEq(a.balanceOf(address(metric)), STOCK);
        assertEq(b.balanceOf(address(metric)), STOCK);
        assertEq(a.balanceOf(address(v3)), STOCK);
        assertEq(b.balanceOf(address(v3)), STOCK);
    }

    /// @dev Emit actual local EVM bytes for differential Python ABI/build verification, no file I/O.
    function testExportProbeCodecVectors() public {
        for (uint256 i; i < 4; ++i) {
            bool metricFirst = i < 2;
            bool startA = i % 2 == 0;
            MetricV3RevertingProbe.Request memory r = _request(metricFirst, startA, 10_000);
            bytes memory result = _invoke(r);
            assertEq(bytes4(result), MetricV3RevertingProbe.ProbeResult.selector);
            _unchanged();
            string memory key = "probe-vector";
            vm.serializeUint(key, "chain_id", block.chainid);
            vm.serializeUint(key, "block_number", block.number);
            vm.serializeUint(key, "block_timestamp", block.timestamp);
            vm.serializeAddress(key, "probe", address(probe));
            vm.serializeAddress(key, "observer", address(this));
            vm.serializeAddress(key, "metric", address(metric));
            vm.serializeAddress(key, "v3", address(v3));
            vm.serializeAddress(key, "asset_a", address(a));
            vm.serializeAddress(key, "asset_b", address(b));
            vm.serializeUint(key, "decimals_a", a.decimals());
            vm.serializeUint(key, "decimals_b", b.decimals());
            vm.serializeBytes32(key, "metric_runtime_hash", address(metric).codehash);
            vm.serializeBytes32(key, "v3_runtime_hash", address(v3).codehash);
            vm.serializeBytes32(key, "asset_a_runtime_hash", address(a).codehash);
            vm.serializeBytes32(key, "asset_b_runtime_hash", address(b).codehash);
            vm.serializeBool(key, "metric_first", metricFirst);
            vm.serializeBool(key, "start_with_a", startA);
            vm.serializeBytes(key, "runtime", address(probe).code);
            vm.serializeBytes(key, "calldata", abi.encodeCall(probe.probe, (r)));
            vm.serializeBytes32(
                key,
                "request_hash",
                keccak256(
                    abi.encode(
                        address(probe),
                        address(this),
                        block.chainid,
                        block.number,
                        block.timestamp,
                        r
                    )
                )
            );
            emit log_named_string("PROBE_VECTOR", vm.serializeBytes(key, "revert_data", result));
        }
    }

    /// @dev Full nonzero storage of these fresh synthetic constructors, not historical witnesses.
    function testExportLocalStateImage() public {
        bytes32[] memory tokenSlots = new bytes32[](2);
        tokenSlots[0] = keccak256(abi.encode(address(metric), uint256(0)));
        tokenSlots[1] = keccak256(abi.encode(address(v3), uint256(0)));
        for (uint256 i; i < tokenSlots.length; ++i) {
            assertEq(uint256(vm.load(address(a), tokenSlots[i])), STOCK);
            assertEq(uint256(vm.load(address(b), tokenSlots[i])), STOCK);
        }
        bytes32[] memory poolSlots = new bytes32[](2);
        poolSlots[0] = bytes32(uint256(1));
        poolSlots[1] = bytes32(uint256(2));
        assertEq(uint256(vm.load(address(metric), poolSlots[0])), metric.numerator());
        assertEq(uint256(vm.load(address(metric), poolSlots[1])), metric.denominator());
        assertEq(uint256(vm.load(address(v3), poolSlots[0])), v3.numerator());
        assertEq(uint256(vm.load(address(v3), poolSlots[1])), v3.denominator());
        _exportAccount(address(a), tokenSlots);
        _exportAccount(address(b), tokenSlots);
        _exportAccount(address(metric), poolSlots);
        _exportAccount(address(v3), poolSlots);
    }

    function _exportAccount(address account, bytes32[] memory slots) private {
        bytes32[] memory values = new bytes32[](slots.length);
        for (uint256 i; i < slots.length; ++i) {
            values[i] = vm.load(account, slots[i]);
        }
        string memory key = "account-image";
        vm.serializeAddress(key, "address", account);
        vm.serializeUint(key, "nonce", vm.getNonce(account));
        vm.serializeUint(key, "balance", account.balance);
        vm.serializeBytes(key, "code", account.code);
        vm.serializeBytes32(key, "slots", slots);
        emit log_named_string("PROBE_ACCOUNT", vm.serializeBytes32(key, "values", values));
    }

    function _success(bool metricFirst, bool startA, uint128 amount) private {
        MetricV3RevertingProbe.Request memory r = _request(metricFirst, startA, amount);
        bytes memory raw = _invoke(r);
        assertEq(bytes4(raw), MetricV3RevertingProbe.ProbeResult.selector);
        // Remove selector before the fixed six-word ABI tuple.
        bytes memory fields = new bytes(raw.length - 4);
        for (uint256 i; i < fields.length; ++i) {
            fields[i] = raw[i + 4];
        }
        (
            bytes32 requestHash,
            uint256 input,
            uint256 mid,
            uint256 out,
            uint256 profit,
            uint256 gasUsed
        ) = abi.decode(fields, (bytes32, uint256, uint256, uint256, uint256, uint256));
        assertEq(
            requestHash,
            keccak256(
                abi.encode(
                    address(probe), address(this), block.chainid, block.number, block.timestamp, r
                )
            )
        );
        assertEq(input, amount);
        assertEq(mid, metricFirst ? amount : uint256(amount) * 101 / 100);
        assertEq(out, uint256(amount) * 101 / 100);
        assertEq(profit, out - input);
        assertGt(gasUsed, 0);
        _unchanged();
    }

    function test_both_orders_and_both_directions_repay_without_seed_capital() public {
        for (uint8 i; i < 4; ++i) {
            _success(i & 1 == 1, i & 2 == 2, 1000);
        }
        _deploy(true);
        for (uint8 i; i < 4; ++i) {
            _success(i & 1 == 1, i & 2 == 2, 1000);
        }
    }

    function testFuzz_revert_conserves_all_balances(uint128 amount, bool metricFirst, bool startA)
        public
    {
        amount = uint128(bound(amount, 100, 10 ** 18));
        _success(metricFirst, startA, amount);
        // Stateful sequence: the first forced revert must restore the callback phase and all
        // balances before the same instance accepts the opposite route at a different size.
        _success(!metricFirst, !startA, amount + 100);
    }

    function test_partial_fill_missing_duplicate_corrupt_and_wrong_callbacks_fail() public {
        for (uint8 mode = 1; mode <= 12; ++mode) {
            metric.fail(mode);
            assertTrue(
                bytes4(_invoke(_request(true, true, 1000)))
                    != MetricV3RevertingProbe.ProbeResult.selector
            );
            metric.fail(0);
            v3.fail(mode);
            assertTrue(
                bytes4(_invoke(_request(true, true, 1000)))
                    != MetricV3RevertingProbe.ProbeResult.selector
            );
            v3.fail(0);
            _unchanged();
        }
    }

    function test_tokens_cannot_spoof_success_or_tax_payments() public {
        for (uint8 mode = 1; mode <= 4; ++mode) {
            a.fail(mode);
            assertTrue(
                bytes4(_invoke(_request(true, true, 1000)))
                    != MetricV3RevertingProbe.ProbeResult.selector
            );
            a.fail(0);
            b.fail(mode);
            assertTrue(
                bytes4(_invoke(_request(false, false, 1000)))
                    != MetricV3RevertingProbe.ProbeResult.selector
            );
            b.fail(0);
            _unchanged();
        }
    }

    function reenterProbe() external {
        probe.probe(_request(true, true, 1000));
    }

    function test_payment_phase_rejects_authenticated_pool_and_observer_reentry() public {
        a.attack(address(probe), address(metric), abi.encodeCall(metric.attemptCallback, ()));
        assertEq(
            bytes4(_invoke(_request(true, true, 1000))),
            MetricV3RevertingProbe.ExternalFailure.selector
        );
        a.attack(address(0), address(0), hex"");
        b.attack(address(probe), address(v3), abi.encodeCall(v3.attemptCallback, ()));
        assertEq(
            bytes4(_invoke(_request(true, true, 1000))),
            MetricV3RevertingProbe.ExternalFailure.selector
        );
        b.attack(address(0), address(0), hex"");
        a.attack(address(probe), address(this), abi.encodeCall(this.reenterProbe, ()));
        assertEq(
            bytes4(_invoke(_request(true, true, 1000))),
            MetricV3RevertingProbe.ExternalFailure.selector
        );
        a.attack(address(0), address(0), hex"");
        _unchanged();
    }

    function test_exact_timestamp_chain_and_native_inventory_are_required() public {
        vm.chainId(1234568);
        assertEq(
            bytes4(_invoke(_request(true, true, 1000))), MetricV3RevertingProbe.Rejected.selector
        );
        vm.chainId(1234567);
        vm.warp(1_800_000_001);
        assertEq(
            bytes4(_invoke(_request(true, true, 1000))), MetricV3RevertingProbe.Rejected.selector
        );
        vm.warp(1_800_000_000);
        vm.deal(address(probe), 1);
        assertEq(
            bytes4(_invoke(_request(true, true, 1000))), MetricV3RevertingProbe.Rejected.selector
        );
    }

    function test_direct_callbacks_wrong_caller_context_and_code_changes_fail() public {
        vm.expectRevert(abi.encodeWithSelector(MetricV3RevertingProbe.Rejected.selector, uint8(8)));
        probe.metricOmmSwapCallback(1, -1, hex"");
        vm.prank(address(metric));
        vm.expectRevert(abi.encodeWithSelector(MetricV3RevertingProbe.Rejected.selector, uint8(9)));
        probe.metricOmmSwapCallback(1, -1, hex"");
        vm.prank(address(0x123));
        vm.expectRevert(abi.encodeWithSelector(MetricV3RevertingProbe.Rejected.selector, uint8(2)));
        probe.probe(_request(true, true, 1000));
        vm.roll(1235);
        assertEq(
            bytes4(_invoke(_request(true, true, 1000))), MetricV3RevertingProbe.Rejected.selector
        );
        vm.roll(1234);
        vm.etch(address(metric), hex"00");
        assertEq(
            bytes4(_invoke(_request(true, true, 1000))), MetricV3RevertingProbe.Rejected.selector
        );
    }

    function test_zero_excess_input_bound_minimum_profit_and_existing_inventory_fail() public {
        assertEq(bytes4(_invoke(_request(true, true, 0))), MetricV3RevertingProbe.Rejected.selector);
        assertEq(
            bytes4(_invoke(_request(true, true, type(uint128).max))),
            MetricV3RevertingProbe.Rejected.selector
        );
        MetricV3RevertingProbe.Request memory r = _request(true, true, 1000);
        r.minimumProfit = 11;
        assertEq(bytes4(_invoke(r)), MetricV3RevertingProbe.Rejected.selector);
        a.mint(address(probe), 1);
        assertEq(
            bytes4(_invoke(_request(true, true, 1000))), MetricV3RevertingProbe.Rejected.selector
        );
    }

    function test_second_leg_loss_cannot_use_a_prefunded_inventory() public {
        v3.rate(99, 100);
        assertEq(
            bytes4(_invoke(_request(true, true, 1000))),
            MetricV3RevertingProbe.ExternalFailure.selector
        );
        _unchanged();
    }

    function test_opposite_token_orders_and_decimal_scales_reconcile_in_base_units() public {
        uint128 unitA = uint128(10 ** a.decimals()); // Fixture decimals are exactly 6 or 18.
        uint256 unitB = 10 ** b.decimals();
        metric.rate(unitB, unitA);
        v3.rate(101 * uint256(unitA), 100 * unitB);
        bytes memory raw = _invoke(_request(true, true, unitA));
        assertEq(bytes4(raw), MetricV3RevertingProbe.ProbeResult.selector);
        bytes memory fields = new bytes(raw.length - 4);
        for (uint256 i; i < fields.length; ++i) {
            fields[i] = raw[i + 4];
        }
        (, uint256 input, uint256 mid, uint256 out, uint256 profit,) =
            abi.decode(fields, (bytes32, uint256, uint256, uint256, uint256, uint256));
        assertEq(input, unitA);
        assertEq(mid, unitB);
        assertEq(out, uint256(unitA) * 101 / 100);
        assertEq(profit, unitA / 100);
        _unchanged();
    }
}
