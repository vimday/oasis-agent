"""Independent ABI and lifecycle tests for launch venue adapters."""

from dataclasses import replace
from unittest.mock import Mock

import pytest
from eth_abi import decode, encode
from eth_utils import keccak

from oasis_agent.sniping.discovery import (
    AERO_CREATED,
    AERO_MINT,
    V4_INITIALIZED,
    V4_MODIFIED,
    VenueScanner,
    decode_created,
    decode_liquidity,
    target_topic,
)
from oasis_agent.sniping.event_wakeup import EventWakeup, alchemy_websocket_url
from oasis_agent.sniping.journal import ExecutionJournal
from oasis_agent.sniping.rpc import DualValue, ProviderDisagreement, _selector
from oasis_agent.sniping.venues import (
    AERO_FACTORY,
    AERO_ROUTER,
    AERO_USDC_WETH,
    KEY_ABI,
    PATH_ABI,
    SLIPSTREAM,
    SLIPSTREAM_USDC_WETH,
    USDC,
    V4_MANAGER,
    V4_QUOTER,
    V4_ROUTER,
    V4_USDC_WETH,
    WETH,
    ZERO,
    VenuePool,
    encode_slipstream_path,
    quote,
    swap_call,
    usdc_quote,
    usdc_swap_call,
)

TARGET = "0xb095274743941e953c746f9c228da9c18bb6ec29"
WALLET = "0x" + "9" * 40
POOL = "0x" + "8" * 40
BLOCK_HASH = "0x" + "a" * 64
TX_HASH = "0x" + "b" * 64


def v4_pool(other=ZERO, hooks=ZERO):
    currencies = sorted([other, TARGET])
    key = (*currencies, 3000, 60, hooks)
    pool_id = "0x" + keccak(encode([KEY_ABI], [key])).hex()
    return VenuePool(
        "uniswap_v4", V4_MANAGER, pool_id, *currencies, fee=3000, tick_spacing=60, hooks=hooks
    )


def aero_pool():
    return VenuePool("aerodrome", AERO_FACTORY, POOL, WETH, TARGET)


def creation(pool):
    if pool.venue == "uniswap_v4":
        topics = [
            V4_INITIALIZED,
            pool.pool,
            target_topic(pool.currency0),
            target_topic(pool.currency1),
        ]
        data = encode(
            ["uint24", "int24", "address", "uint160", "int24"],
            [pool.fee, pool.tick_spacing, pool.hooks, 2**96, 0],
        )
    else:
        topics = [AERO_CREATED, target_topic(WETH), target_topic(TARGET), "0x" + "0" * 64]
        data = encode(["address", "uint256"], [POOL, 1])
    return {
        "address": pool.factory,
        "topics": topics,
        "data": "0x" + data.hex(),
        "blockNumber": "0xa",
        "blockHash": BLOCK_HASH,
        "transactionHash": TX_HASH,
        "logIndex": "0x0",
        "removed": False,
    }


@pytest.mark.parametrize("buy", [True, False])
def test_aerodrome_unsigned_calls_preserve_minimum_route_and_deadline(buy):
    pool = aero_pool()
    tx = swap_call(pool, TARGET, WALLET, 100, 90, 1000, buy=buy)
    types = ["uint256", "(address,address,bool,address)[]", "address", "uint256"]
    if not buy:
        types.insert(0, "uint256")
    args = decode(types, bytes.fromhex(tx["data"][10:]))
    assert args[-1] == 1000 and args[-2] == WALLET
    assert args[-3][0] == (
        (WETH, TARGET, False, AERO_FACTORY) if buy else (TARGET, WETH, False, AERO_FACTORY)
    )
    assert args[-4] == 90
    assert int(tx["value"], 16) == (100 if buy else 0)


@pytest.mark.parametrize("factory", list(SLIPSTREAM))
@pytest.mark.parametrize("buy", [True, False])
def test_slipstream_uses_tick_spacing_and_correct_factory_router(factory, buy):
    pool = VenuePool("slipstream", factory, POOL, WETH, TARGET, tick_spacing=200)
    tx = swap_call(pool, TARGET, WALLET, 100, 90, 1000, buy=buy)
    assert tx["to"] == SLIPSTREAM[factory][1]
    calls = decode(["bytes[]"], bytes.fromhex(tx["data"][10:]))[0]
    assert len(calls) == 2
    params = decode(
        ["(address,address,int24,address,uint256,uint256,uint256,uint160)"], calls[0][4:]
    )[0]
    assert params[2] == 200 and params[4:7] == (1000, 100, 90)
    expected = _selector("refundETH()" if buy else "unwrapWETH9(uint256,address)")
    assert "0x" + calls[1][:4].hex() == expected


@pytest.mark.parametrize("other", [ZERO, WETH])
@pytest.mark.parametrize("buy", [True, False])
def test_v4_unsigned_exact_input_actions_settle_and_take_correct_currencies(other, buy):
    pool = v4_pool(other)
    tx = swap_call(pool, TARGET, WALLET, 100, 90, 1000, buy=buy)
    assert tx["to"] == V4_ROUTER
    commands, inputs, deadline = decode(
        ["bytes", "bytes[]", "uint256"], bytes.fromhex(tx["data"][10:])
    )
    assert deadline == 1000
    index = list(commands).index(0x10)
    actions, parameters = decode(["bytes", "bytes[]"], inputs[index])
    assert actions == bytes([0x06, 0x0B, 0x0E])
    args = decode([f"({KEY_ABI},bool,uint128,uint128,bytes)"], parameters[0])[0]
    assert args[0] == pool.key_tuple() and args[2:4] == (100, 90)
    settlement = decode(["address", "uint256", "bool"], parameters[1])
    assert settlement == (other if buy else TARGET, 0, not buy)
    take = decode(["address", "address", "uint256"], parameters[2])
    assert take[0] == (TARGET if buy else other)
    assert take[1] == (pool.router if not buy and other == WETH else WALLET)
    assert commands[-1] == 4  # sweep any unused input
    if buy and other == WETH:
        assert commands[0] == 0x0B
    if not buy and other == WETH:
        assert 0x0C in commands


@pytest.mark.parametrize("amount", [0, -1, True, 1.5, 2**128])
def test_v4_amount_bounds(amount):
    with pytest.raises(ValueError):
        swap_call(v4_pool(), TARGET, WALLET, amount, 1, 1000, buy=True)


def test_hook_discovery_is_allowed_but_unsigned_execution_is_blocked():
    pool = v4_pool(hooks="0x" + "1" * 40)
    assert decode_created(creation(pool), TARGET) == pool
    with pytest.raises(ValueError, match="hook"):
        swap_call(pool, TARGET, WALLET, 100, 90, 1000, buy=True)


def test_hooked_pool_is_never_tradeable_or_directly_routable():
    hook = "0x" + "1" * 40
    hooked_weth = v4_pool(other=WETH, hooks=hook)
    hooked_usdc = v4_pool(other=USDC, hooks=hook)
    assert hooked_weth.hooked and hooked_usdc.hooked
    assert not hooked_weth.tradeable(TARGET)
    assert not hooked_weth.direct_route_enabled(TARGET)
    assert not hooked_usdc.tradeable(TARGET, USDC)
    assert not hooked_usdc.direct_route_enabled(TARGET)
    # A hookless v4 pool on the same pairs remains tradeable.
    assert v4_pool(other=WETH).tradeable(TARGET)
    assert v4_pool(other=WETH).direct_route_enabled(TARGET)


def test_direct_route_predicate_accepts_only_weth_or_native_v4():
    usdc = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    assert aero_pool().direct_route_enabled(TARGET)
    assert not VenuePool("aerodrome", AERO_FACTORY, POOL, usdc, TARGET).direct_route_enabled(TARGET)
    assert v4_pool(WETH).direct_route_enabled(TARGET)
    assert v4_pool(ZERO).direct_route_enabled(TARGET)
    assert not v4_pool(usdc).direct_route_enabled(TARGET)
    assert not aero_pool().direct_route_enabled(WALLET)


def test_pool_id_mismatch_is_rejected():
    with pytest.raises(ValueError, match="PoolId"):
        replace(v4_pool(), pool="0x" + "f" * 64)


@pytest.mark.parametrize("pool", [aero_pool(), v4_pool()])
def test_creation_decodes_and_rejects_removed_or_spoofed_factory(pool):
    log = creation(pool)
    assert decode_created(log, TARGET) == pool
    with pytest.raises(ValueError):
        decode_created(log | {"removed": True}, TARGET)
    with pytest.raises(ValueError):
        decode_created(log | {"address": WALLET}, TARGET)


@pytest.mark.parametrize(
    "delta,kind", [(100, "liquidity_added"), (-100, "liquidity_removed"), (0, "position_update")]
)
def test_v4_liquidity_delta_signed_direction(delta, kind):
    pool = v4_pool()
    log = creation(pool) | {
        "topics": [V4_MODIFIED, pool.pool, target_topic(WALLET)],
        "data": "0x"
        + encode(["int24", "int24", "int256", "bytes32"], [-60, 60, delta, bytes(32)]).hex(),
    }
    assert decode_liquidity(log, {pool.pool: pool}, TARGET, set()).kind == kind


def test_quote_uses_signed_slipstream_tick_abi():
    pool = VenuePool("slipstream", next(iter(SLIPSTREAM)), POOL, WETH, TARGET, tick_spacing=200)
    rpc = Mock()
    raw = "0x" + encode(["uint256", "uint160", "uint32", "uint256"], [99, 2**96, 0, 100000]).hex()
    rpc.dual_eth_call.return_value = DualValue(raw, raw)
    assert quote(rpc, pool, TARGET, 100, buy=True) == 99
    assert rpc.dual_eth_call.call_args.args[1].startswith(
        _selector("quoteExactInputSingle((address,address,uint256,int24,uint160))")
    )


def test_creation_then_liquidity_event_same_block_is_not_lost_and_restart_dedups(tmp_path):
    pool = aero_pool()
    create = creation(pool)
    mint = create | {
        "address": POOL,
        "topics": [AERO_MINT, target_topic(WALLET)],
        "data": "0x" + encode(["uint256", "uint256"], [1, 2]).hex(),
        "logIndex": "0x1",
    }
    rpc = Mock()
    header = {"number": "0xa", "hash": BLOCK_HASH}

    def dual(method, params):
        if method == "eth_getBlockByNumber":
            return DualValue(header, header)
        query = params[0]
        logs = []
        if query["address"] == [AERO_FACTORY, *SLIPSTREAM] and query["topics"][2] == target_topic(
            TARGET
        ):
            logs = [create]
        if query["address"] == [POOL]:
            logs = [mint]
        return DualValue(logs, logs)

    rpc.dual.side_effect = dual
    with ExecutionJournal(tmp_path / "j.db", {"test": True}) as journal:
        scanner = VenueScanner(rpc, TARGET, 10, journal)
        signals = scanner.poll(10)
        assert [s.kind for s in signals] == ["created", "liquidity_added"]
        again = VenueScanner(rpc, TARGET, 10, journal)
        assert again.pools == scanner.pools and again.poll(10) == []
        rpc.dual.return_value = DualValue(
            {"number": "0xa", "hash": TX_HASH}, {"number": "0xa", "hash": TX_HASH}
        )
        rpc.dual.side_effect = None
        with pytest.raises(ProviderDisagreement, match="reorg"):
            again.poll(11)


def test_disagreement_never_advances_discovery_cursor(tmp_path):
    rpc = Mock()
    rpc.dual.side_effect = [
        DualValue({"number": "0xa", "hash": BLOCK_HASH}, {"number": "0xa", "hash": BLOCK_HASH}),
        DualValue([], [creation(aero_pool())]),
    ]
    with ExecutionJournal(tmp_path / "j.db", {"test": True}) as journal:
        scanner = VenueScanner(rpc, TARGET, 10, journal)
        with pytest.raises(ProviderDisagreement):
            scanner.poll(10)
        assert scanner.cursor == 9 and scanner.pools == {} and journal.read() == []


def aero_usdc_pool():
    return VenuePool("aerodrome", AERO_FACTORY, POOL, USDC, TARGET)


def v4_usdc_pool():
    currencies = sorted([USDC, TARGET])
    key = (*currencies, 500, 10, ZERO)
    pool_id = "0x" + keccak(encode([KEY_ABI], [key])).hex()
    return VenuePool("uniswap_v4", V4_MANAGER, pool_id, *currencies, fee=500, tick_spacing=10)


def slipstream_usdc_pool(factory):
    return VenuePool("slipstream", factory, POOL, USDC, TARGET, tick_spacing=200)


def test_usdc_hop_shape_and_factory_coverage():
    assert aero_usdc_pool().usdc_hop() == AERO_USDC_WETH
    assert v4_usdc_pool().usdc_hop() == V4_USDC_WETH
    for factory, hop in SLIPSTREAM_USDC_WETH.items():
        assert slipstream_usdc_pool(factory).usdc_hop() == hop
    unhopped = next(f for f in SLIPSTREAM if f not in SLIPSTREAM_USDC_WETH)
    assert slipstream_usdc_pool(unhopped).usdc_hop() is None


def test_tradeable_requires_verified_usdc_hop():
    assert aero_usdc_pool().tradeable(TARGET, USDC)
    assert v4_usdc_pool().tradeable(TARGET, USDC)
    hopped = next(iter(SLIPSTREAM_USDC_WETH))
    assert slipstream_usdc_pool(hopped).tradeable(TARGET, USDC)
    unhopped = next(f for f in SLIPSTREAM if f not in SLIPSTREAM_USDC_WETH)
    assert not slipstream_usdc_pool(unhopped).tradeable(TARGET, USDC)
    # Without a USDC hint a USDC-paired pool is not directly executable.
    assert not aero_usdc_pool().tradeable(TARGET)


@pytest.mark.parametrize("buy", [True, False])
def test_usdc_quote_aerodrome_two_hop(buy):
    pool = aero_usdc_pool()
    rpc = Mock()
    raw = "0x" + encode(["uint256[]"], [[10**18, 1234, 999]]).hex()
    rpc.dual_eth_call.return_value = DualValue(raw, raw)
    assert usdc_quote(rpc, pool, TARGET, 10**18, buy=buy) == 999
    assert rpc.dual_eth_call.call_args.args[0] == AERO_ROUTER
    assert rpc.dual_eth_call.call_args.args[1].startswith(
        _selector("getAmountsOut(uint256,(address,address,bool,address)[])")
    )


def test_usdc_quote_slipstream_packed_path():
    factory = next(iter(SLIPSTREAM_USDC_WETH))
    pool = slipstream_usdc_pool(factory)
    rpc = Mock()
    raw = (
        "0x" + encode(["uint256", "uint160[]", "uint32[]", "uint256"], [888, [], [], 100000]).hex()
    )
    rpc.dual_eth_call.return_value = DualValue(raw, raw)
    assert usdc_quote(rpc, pool, TARGET, 10**18, buy=True) == 888
    assert rpc.dual_eth_call.call_args.args[0] == SLIPSTREAM[factory][0]
    assert rpc.dual_eth_call.call_args.args[1].startswith(
        _selector("quoteExactInput(bytes,uint256)")
    )


@pytest.mark.parametrize("buy", [True, False])
def test_usdc_quote_v4_multihop(buy):
    pool = v4_usdc_pool()
    rpc = Mock()
    raw = "0x" + encode(["uint256", "uint256"], [777, 300000]).hex()
    rpc.dual_eth_call.return_value = DualValue(raw, raw)
    assert usdc_quote(rpc, pool, TARGET, 10**18, buy=buy) == 777
    assert rpc.dual_eth_call.call_args.args[0] == V4_QUOTER
    assert rpc.dual_eth_call.call_args.args[1].startswith(
        _selector(f"quoteExactInput((address,{PATH_ABI}[],uint128))")
    )


@pytest.mark.parametrize("buy", [True, False])
def test_usdc_swap_call_aerodrome_two_hop(buy):
    pool = aero_usdc_pool()
    tx = usdc_swap_call(pool, TARGET, WALLET, 100, 90, 1000, buy=buy)
    assert tx["to"] == AERO_ROUTER
    assert int(tx["value"], 16) == (100 if buy else 0)
    types = ["uint256", "(address,address,bool,address)[]", "address", "uint256"]
    if not buy:
        types.insert(0, "uint256")
    args = decode(types, bytes.fromhex(tx["data"][10:]))
    routes = args[-3]
    assert len(routes) == 2
    assert routes[0] == (
        (WETH, USDC, False, AERO_FACTORY) if buy else (TARGET, USDC, False, AERO_FACTORY)
    )
    assert routes[1] == (
        (USDC, TARGET, False, AERO_FACTORY) if buy else (USDC, WETH, False, AERO_FACTORY)
    )
    assert args[-1] == 1000 and args[-2] == WALLET and args[-4] == 90


@pytest.mark.parametrize("buy", [True, False])
def test_usdc_swap_call_slipstream_two_hop_multicall(buy):
    factory = next(iter(SLIPSTREAM_USDC_WETH))
    pool = slipstream_usdc_pool(factory)
    hop_spacing = SLIPSTREAM_USDC_WETH[factory][1]
    tx = usdc_swap_call(pool, TARGET, WALLET, 100, 90, 1000, buy=buy)
    assert tx["to"] == SLIPSTREAM[factory][1]
    assert int(tx["value"], 16) == (100 if buy else 0)
    calls = decode(["bytes[]"], bytes.fromhex(tx["data"][10:]))[0]
    assert len(calls) == 2
    params = decode(["(bytes,address,uint256,uint256,uint256)"], calls[0][4:])[0]
    expected = (
        encode_slipstream_path((WETH, USDC, TARGET), (hop_spacing, pool.tick_spacing))
        if buy
        else encode_slipstream_path((TARGET, USDC, WETH), (pool.tick_spacing, hop_spacing))
    )
    assert params[0] == expected


@pytest.mark.parametrize("buy", [True, False])
def test_usdc_swap_call_v4_two_hop(buy):
    pool = v4_usdc_pool()
    tx = usdc_swap_call(pool, TARGET, WALLET, 100, 90, 1000, buy=buy)
    assert tx["to"] == V4_ROUTER
    assert int(tx["value"], 16) == (100 if buy else 0)
    commands, inputs, deadline = decode(
        ["bytes", "bytes[]", "uint256"], bytes.fromhex(tx["data"][10:])
    )
    assert deadline == 1000
    index = list(commands).index(0x10)
    _, parameters = decode(["bytes", "bytes[]"], inputs[index])
    swap_params = decode([f"(address,{PATH_ABI}[],uint128,uint128)"], parameters[0])[0]
    assert swap_params[0] == (WETH if buy else TARGET)
    assert len(swap_params[1]) == 2
    assert swap_params[1][0][0] == USDC
    assert swap_params[1][1][0] == (TARGET if buy else WETH)
    assert swap_params[2:4] == (100, 90)


def test_websocket_hint_only_wakes_known_subscription_and_never_orders():
    listener = EventWakeup("wss://example.invalid", TARGET, AERO_FACTORY)
    listener._notification(
        {"method": "eth_subscription", "params": {"subscription": "wrong", "result": {}}}, "right"
    )
    assert not listener.wait(0)
    listener._notification(
        {
            "method": "eth_subscription",
            "params": {"subscription": "right", "result": {"removed": True}},
        },
        "right",
    )
    assert listener.wait(0) and listener.notifications == 1


def test_websocket_hint_url_conversion_is_alchemy_only():
    assert (
        alchemy_websocket_url("https://base-mainnet.g.alchemy.com/v2/key")
        == "wss://base-mainnet.g.alchemy.com/v2/key"
    )
    # Free public endpoints (mainnet.base.org, Tenderly gateway) have no
    # WebSocket hint stream; polling covers them, so conversion returns None
    # instead of crashing bootstrap.
    assert alchemy_websocket_url("https://mainnet.base.org") is None
    assert alchemy_websocket_url("https://base.gateway.tenderly.co") is None
