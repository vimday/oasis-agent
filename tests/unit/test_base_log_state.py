"""Log-driven Base roster state: exact advance, block ordering and fail-closed conflicts."""

import pytest
from eth_abi.abi import encode

from oasis_agent.evidence.log_bloom import log_emitter_mask
from oasis_agent.fastpath.base_log_state import (
    BaseLogState,
    ClDepth,
    LogStateError,
    StreamHead,
    StreamLog,
    apply_pool_log,
    cl_interval_node,
    decode_stream_head,
    decode_stream_log,
    depth_node,
    depth_window,
    spacing_interval,
)
from oasis_agent.fastpath.pool_graph import PoolKind, PoolNode
from oasis_agent.fastpath.state_sync import (
    AERO_SYNC_TOPIC,
    V3_BURN_TOPIC,
    V3_MINT_TOPIC,
    V3_SWAP_TOPIC,
)
from oasis_agent.uniswap_v4.liquidity_math import sqrt_price_at_tick

CL = "0x" + "11" * 20
V2 = "0x" + "22" * 20
OTHER = "0x" + "33" * 20
T0 = "0x" + "0a" * 20
T1 = "0x" + "0b" * 20
SENDER = "0x" + "00" * 12 + "44" * 20


def _h(n: int) -> str:
    return f"0x{n:064x}"


def _cl_node(tick: int = 5, liquidity: int = 10**18) -> PoolNode:
    base = PoolNode(8453, CL, PoolKind.SLIPSTREAM, T0, T1, 18, 6, 500)
    return cl_interval_node(
        base,
        sqrt_price_x96=sqrt_price_at_tick(tick),
        tick=tick,
        liquidity=liquidity,
        spacing=10,
        height=100,
    )


def _v2_node() -> PoolNode:
    return PoolNode(8453, V2, PoolKind.V2, T0, T1, 18, 6, 3000, 100, 0, 10**21, 2 * 10**12)


def _state() -> BaseLogState:
    return BaseLogState(100, _h(100), (_cl_node(), _v2_node()), {CL: 10, V2: None})


def _head(n: int, emitters: tuple[str, ...] = (), parent: int | None = None) -> StreamHead:
    bloom = 0
    for emitter in emitters:
        bloom |= log_emitter_mask(emitter)
    return StreamHead(n, _h(n), _h(n - 1 if parent is None else parent), n * 200, 1, bloom, n)


def _close(state: BaseLogState, height: int):
    """Feed the heads that confirm ``height`` (CONFIRM_LAG = 2) and return its report."""
    assert state.on_head(_head(height + 1)) is None
    return state.on_head(_head(height + 2))


def _swap(n: int, index: int, tick: int, liquidity: int, *, block_hash: str | None = None):
    data = encode(
        ["int256", "int256", "uint160", "uint128", "int24"],
        [10, -10, sqrt_price_at_tick(tick) + 7, liquidity, tick],
    )
    return StreamLog(n, block_hash or _h(n), 0, index, CL, (V3_SWAP_TOPIC, SENDER, SENDER), data, n)


def _sync(n: int, index: int, r0: int, r1: int) -> StreamLog:
    data = encode(["uint256", "uint256"], [r0, r1])
    return StreamLog(n, _h(n), 0, index, V2, (AERO_SYNC_TOPIC,), data, n)


def test_last_log_in_block_is_exact_post_block_state() -> None:
    state = _state()
    assert state.on_log(_swap(101, 1, 3, 9 * 10**17))
    assert state.on_log(_swap(101, 4, 12, 7 * 10**17))
    assert state.on_log(_sync(101, 5, 5, 6))
    assert state.on_head(_head(101, (CL, V2))) is None
    nodes, changed = state.view(101)
    assert changed == (CL, V2)
    assert nodes[CL].tick == 12 and nodes[CL].liquidity == 7 * 10**17
    assert [(t.tick, t.liquidity_net) for t in nodes[CL].ticks] == [(10, 7e17), (20, -7e17)]
    assert (nodes[V2].reserve0, nodes[V2].reserve1) == (5, 6)
    report = _close(state, 101)
    assert report is not None and report.height == 101 and report.bloom_only == ()
    assert state.confirmed(101)[1] == nodes


def test_log_before_its_head_is_buffered_and_counted() -> None:
    state = _state()
    state.on_log(_swap(101, 0, 6, 10**18))
    state.on_head(_head(101, (CL,)))
    report = _close(state, 101)
    assert report is not None and report.logs_before_head == 1


def test_bloom_flag_without_log_is_reported_not_fatal() -> None:
    state = _state()
    state.on_head(_head(101, (V2,)))
    assert state.pending(101) == (V2,)
    report = _close(state, 101)
    assert report is not None and report.bloom_only == (V2,) and report.changed_pools == ()


def test_log_without_bloom_bits_fails_closed() -> None:
    state = _state()
    state.on_log(_swap(101, 0, 6, 10**18))
    state.on_head(_head(101))
    state.on_head(_head(102))
    with pytest.raises(LogStateError, match="Bloom"):
        state.on_head(_head(103))


@pytest.mark.parametrize(
    "event",
    [
        "late_log",
        "gap",
        "parent",
        "hash_conflict",
        "index_reuse",
        "duplicate_head",
    ],
)
def test_ordering_violations_fail_closed(event: str) -> None:
    state = _state()
    state.on_head(_head(101, (CL,)))
    with pytest.raises(LogStateError):
        if event == "late_log":
            _close(state, 101)
            state.on_log(_swap(101, 0, 6, 10**18))
        elif event == "gap":
            state.on_head(_head(103))
        elif event == "parent":
            state.on_head(_head(102, parent=99))
        elif event == "hash_conflict":
            state.on_log(_swap(101, 0, 6, 10**18, block_hash=_h(9999)))
        elif event == "index_reuse":
            state.on_log(_swap(101, 3, 6, 10**18))
            state.on_log(_sync(101, 3, 1, 1))
        else:
            state.on_head(StreamHead(101, _h(5555), _h(100), 1, 1, 0, 1))


def test_untracked_and_pre_seed_logs_are_ignored() -> None:
    state = _state()
    other = StreamLog(101, _h(101), 0, 0, OTHER, (AERO_SYNC_TOPIC,), b"\0" * 64, 1)
    assert not state.on_log(other)
    assert not state.on_log(_swap(100, 0, 6, 10**18))


def test_mint_and_burn_change_liquidity_only_when_range_covers_tick() -> None:
    node = _cl_node(tick=5, liquidity=1000)

    def position(topic: str, lower: int, upper: int, amount: int) -> StreamLog:
        if topic == V3_MINT_TOPIC:
            data = encode(
                ["address", "uint128", "uint256", "uint256"], ["0x" + "44" * 20, amount, 1, 1]
            )
        else:
            data = encode(["uint128", "uint256", "uint256"], [amount, 1, 1])
        topics = (topic, SENDER, _h(lower % (1 << 256)), _h(upper % (1 << 256)))
        return StreamLog(1, _h(1), 0, 0, CL, topics, data, 1)

    minted = apply_pool_log(node, 10, position(V3_MINT_TOPIC, -20, 30, 500))
    assert minted is not None and minted.liquidity == 1500
    assert apply_pool_log(node, 10, position(V3_MINT_TOPIC, 10, 30, 500)) is None
    burned = apply_pool_log(node, 10, position(V3_BURN_TOPIC, 0, 10, 400))
    assert burned is not None and burned.liquidity == 600
    with pytest.raises(LogStateError, match="aligned"):
        apply_pool_log(node, 10, position(V3_MINT_TOPIC, -5, 30, 1))
    with pytest.raises(LogStateError, match="uint128"):
        apply_pool_log(node, 10, position(V3_BURN_TOPIC, 0, 10, 1001))


def test_boundary_price_and_negative_tick_intervals() -> None:
    assert spacing_interval(-1, 10) == (-10, 0)
    assert spacing_interval(-10, 10) == (-10, 0)
    assert spacing_interval(9, 10) == (0, 10)
    node = _cl_node()
    on_boundary = cl_interval_node(
        node, sqrt_price_x96=sqrt_price_at_tick(10), tick=9, liquidity=1, spacing=10, height=1
    )
    assert on_boundary.ticks[0].tick == 0 and on_boundary.ticks[1].tick == 10
    with pytest.raises(LogStateError, match="outside its tick"):
        cl_interval_node(
            node, sqrt_price_x96=sqrt_price_at_tick(11), tick=9, liquidity=1, spacing=10, height=1
        )


def test_swap_rejects_non_canonical_tick_word() -> None:
    data = bytearray(encode(["int256", "int256", "uint160", "uint128", "int24"], [0, 0, 1, 1, 0]))
    data[128] = 0x01
    log = StreamLog(1, _h(1), 0, 0, CL, (V3_SWAP_TOPIC, SENDER, SENDER), bytes(data), 1)
    with pytest.raises(LogStateError, match="canonical"):
        apply_pool_log(_cl_node(), 10, log)


def test_reseed_rejects_pool_with_buffered_logs() -> None:
    state = _state()
    state.on_log(_swap(101, 0, 6, 10**18))
    with pytest.raises(LogStateError, match="buffered"):
        state.reseed_pool(100, _cl_node(tick=7))
    state.reseed_pool(100, _v2_node())


def test_decoders_reject_removed_and_bad_heads() -> None:
    log = {
        "address": CL,
        "topics": [V3_SWAP_TOPIC, SENDER, SENDER],
        "data": "0x" + "00" * 160,
        "blockNumber": "0x65",
        "blockHash": _h(101),
        "transactionIndex": "0x0",
        "logIndex": "0x2",
        "removed": False,
    }
    assert decode_stream_log(log, 1).log_index == 2
    with pytest.raises(LogStateError, match="removed"):
        decode_stream_log({**log, "removed": True}, 1)
    head = {
        "number": "0x65",
        "hash": _h(101),
        "parentHash": _h(100),
        "timestamp": "0x2",
        "timestampMs": "0x7d0",
        "baseFeePerGas": "0x1",
        "logsBloom": "0x" + "00" * 256,
    }
    assert decode_stream_head(head, 1).timestamp_ms == 2000
    with pytest.raises(LogStateError, match="timestampMs"):
        decode_stream_head({**head, "timestampMs": "0xbb8"}, 1)
    with pytest.raises(LogStateError, match="leading zero"):
        decode_stream_head({**head, "number": "0x065"}, 1)


def _depth_state(tick: int = 5, liquidity: int = 1000) -> tuple[BaseLogState, ClDepth]:
    depth = ClDepth(10, -2560, 2560, {-20: 300, 20: -300, 40: 700, 60: -700})
    node = depth_node(
        _cl_node(tick, liquidity),
        sqrt_price_x96=sqrt_price_at_tick(tick),
        tick=tick,
        liquidity=liquidity,
        depth=depth,
        height=100,
    )
    state = BaseLogState(100, _h(100), (node, _v2_node()), {CL: 10, V2: None}, depths={CL: depth})
    return state, depth


def test_depth_node_carries_window_ticks_and_draining_edges() -> None:
    _, depth = _depth_state()
    node = depth_node(
        _cl_node(),
        sqrt_price_x96=sqrt_price_at_tick(5),
        tick=5,
        liquidity=1000,
        depth=depth,
        height=1,
    )
    # Floor liquidity = 1000 - 300; inner nets; the upper edge drains what remains.
    assert [(t.tick, t.liquidity_net) for t in node.ticks] == [
        (-2560, 700),
        (-20, 300),
        (20, -300),
        (40, 700),
        (60, -700),
        (2560, -700),
    ]
    assert sum(t.liquidity_net for t in node.ticks) == 0
    assert depth_window(5, 10, 1) == (-2560, 5120, (-1, 0, 1))


def test_depth_swap_across_ticks_is_cross_checked_by_logged_liquidity() -> None:
    state, _ = _depth_state()
    state.on_log(_swap(101, 0, 45, 1000 - 300 + 700))
    state.on_head(_head(101, (CL,)))
    nodes, _ = state.view(101)
    assert nodes[CL].tick == 45 and len(nodes[CL].ticks) == 6
    _close(state, 101)
    assert state.depth_pools == (CL,) and state.downgrades == []


def test_depth_downgrades_on_disagreement_or_window_exit() -> None:
    state, _ = _depth_state()
    state.on_log(_swap(101, 0, 45, 999))
    state.on_head(_head(101, (CL,)))
    _close(state, 101)
    assert state.downgrades == [(101, CL, "swap_liquidity_disagrees_with_ticks")]
    assert state.depth_pools == ()
    nodes = state.confirmed(101)[1]
    assert len(nodes[CL].ticks) == 2  # rebuilt on the conservative interval model

    state, _ = _depth_state()
    state.on_log(_swap(101, 0, 2600, 1))
    state.on_head(_head(101, (CL,)))
    _close(state, 101)
    assert state.downgrades[0][2] == "price_left_window"


def test_depth_mint_outside_active_interval_updates_nets_only() -> None:
    state, _ = _depth_state()
    data = encode(["address", "uint128", "uint256", "uint256"], ["0x" + "44" * 20, 50, 1, 1])
    topics = (V3_MINT_TOPIC, SENDER, _h(100), _h(200))
    state.on_log(StreamLog(101, _h(101), 0, 0, CL, topics, data, 1))
    state.on_head(_head(101, (CL,)))
    nodes, _ = state.view(101)
    assert nodes[CL].liquidity == 1000
    assert (100, 50) in [(t.tick, t.liquidity_net) for t in nodes[CL].ticks]
    assert (200, -50) in [(t.tick, t.liquidity_net) for t in nodes[CL].ticks]


def test_out_of_order_delivery_is_applied_in_log_index_order() -> None:
    state = _state()
    state.on_log(_swap(101, 7, 12, 7 * 10**17))
    state.on_log(_swap(101, 2, 3, 9 * 10**17))
    state.on_head(_head(101, (CL,)))
    nodes, _ = state.view(101)
    assert nodes[CL].tick == 12  # log index 7 is last in consensus order
    report = _close(state, 101)
    assert report is not None and report.logs_out_of_order == 1


def test_log_after_child_head_is_still_applied_before_confirmation() -> None:
    state = _state()
    state.on_head(_head(101, (CL,)))
    state.on_head(_head(102))
    state.on_log(_swap(101, 0, 12, 7 * 10**17))  # observed live: up to 25 ms after head N+1
    nodes, changed = state.view(102)
    assert changed == (CL,) and nodes[CL].tick == 12
    report = state.on_head(_head(103))
    assert report is not None and report.height == 101 and report.changed_pools == (CL,)


def test_position_ending_on_window_edge_leaves_the_sentinel_untouched() -> None:
    state, _ = _depth_state()
    data = encode(["address", "uint128", "uint256", "uint256"], ["0x" + "44" * 20, 50, 1, 1])
    topics = (V3_MINT_TOPIC, SENDER, _h((-2560) % (1 << 256)), _h(40))
    state.on_log(StreamLog(101, _h(101), 0, 0, CL, topics, data, 1))
    state.on_head(_head(101, (CL,)))
    nodes, _ = state.view(101)
    nets = {t.tick: t.liquidity_net for t in nodes[CL].ticks}
    assert nodes[CL].liquidity == 1050 and nets[40] == 650
    assert sum(nets.values()) == 0  # the edges still drain exactly what is active there
    with pytest.raises(LogStateError, match="strictly inside"):
        ClDepth(10, -2560, 2560, {2560: 1})
