"""Focused receipt/call matching using two conversions from the retained ETH nine-pool case."""

from dataclasses import replace

import pytest
from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.arbitrage.cycle_protocols import calldata, topic
from oasis_agent.evidence.public_receipts import ObservedReceipt
from oasis_agent.evidence.swap_flow import BANCOR_CONVERSION_TOPIC, TRANSFER, profile_swap_receipt
from oasis_agent.evidence.swap_flow_trace import replay_trace_funding
from oasis_agent.models.chain import LogRecord
from oasis_agent.models.common import BlockRef

BANCOR = "0x2f9ec37d6ccfff1cab21733bdadede11c823ccb0"
ACTOR = "0x81463b0f960f247f704377661ec81c1fd65b5128"
SEND = "0xe75ed6f453c602bd696ce27af11565edc9b46b0d"
POOL1 = "0x9a2d4f269a9b5d2a9a7b0635f505bdab03f2c928"
POOL2 = "0xf8d9b7691e7721a8697e1d72725375a7abfdf670"
ANCHOR1 = "0xdcdff3a2cd440c78854e06c7ef67347ff89d0cab"
ANCHOR2 = "0x13fac856d064ace14cd4e253cd7b35850c44d07d"
FINAL_POOL = "0xf55c33d94150d93c2cfb833bcca30be388b14964"
A = "0x69af81e73a73b40adf4f3d4223cd9b1ece623074"
B = "0x1f573d6fb3f13d689ff844b4ce37794d79a7ff1c"
C = "0xa1faa113cbe53436df28ff0aee54275c13b40975"
X, Y, Z = 10_212_160_666_165_313_536, 12_765_394_420_910_310_408, 8_356_418_047_912_439_167_969
HEADER = "0x444e4bfe668be89a654254c9c050ee694092fb0067e1438648bdcb2cdbeebfbe"
TX = "0xce5a52338f6a5544607d880b50d75edc9c894bbe201fc05f74e1625952449998"


def word(value: int) -> str:
    return f"0x{value:064x}"


def event(target, sig, indexed, types, values, position=0):
    topics = [sig, *(word(int(item, 16)) for item in indexed)]
    return {
        "address": target,
        "topics": topics,
        "data": "0x" + encode(types, values).hex(),
        "position": hex(position),
    }


def call(sender, to, data, *, out="0x", children=(), logs=()):
    return dict(
        type="CALL",
        value="0x0",
        **{"from": sender},
        to=to,
        input=data,
        output=out,
        calls=list(children),
        logs=list(logs),
    )


def movement(token, operator, sender, to, amount):
    return call(
        operator,
        token,
        calldata(
            "transferFrom(address,address,uint256)",
            ("address", "address", "uint256"),
            (sender, to, amount),
        ),
        logs=[event(token, TRANSFER, (sender, to), ("uint256",), (amount,))],
    )


def network(token_in, token_out, bought, sold, pool, anchor, beneficiary, *, faulty=None):
    ingress = movement(token_in, BANCOR, ACTOR, pool, sold)
    if faulty == "duplicate":
        ingress["logs"] *= 2
    egress = movement(token_out, pool, pool, beneficiary, bought)
    swap = event(
        pool,
        BANCOR_CONVERSION_TOPIC,
        (token_in, token_out, ACTOR),
        ("uint256", "uint256", "int256"),
        (sold, bought, 9),
        1,
    )
    converter = call(
        BANCOR,
        pool,
        calldata(
            "convert(address,address,uint256,address,address)",
            ("address", "address", "uint256", "address", "address"),
            (token_in, token_out, sold, ACTOR, beneficiary),
        ),
        out=word(bought - 1 if faulty == "return" else bought),
        children=[egress],
        logs=[swap],
    )
    return call(
        ACTOR,
        BANCOR,
        calldata(
            "convertByPath(address[],uint256,uint256,address,address,uint256)",
            ("address[]", "uint256", "uint256", "address", "address", "uint256"),
            ((token_in, anchor, token_out), sold, bought, beneficiary, "0x" + "00" * 20, 0),
        ),
        out=word(bought),
        children=[ingress, converter],
    )


def case(faulty=None):
    first = network(A, B, Y, X, POOL1, ANCHOR1, ACTOR)
    second = network(B, C, Z, Y, POOL2, ANCHOR2, FINAL_POOL, faulty=faulty)
    other = [movement(B, ACTOR, ACTOR, "0x" + "01" * 20, 1)] if faulty == "intervening" else []
    root = call(SEND, ACTOR, "0x", children=[first, *other, second])
    root["gasUsed"] = "0x20000"
    logs = []
    for node in root["calls"]:
        if node["to"] != BANCOR:
            logs.extend(node["logs"])
        else:
            ingress, conversion = node["calls"]
            logs.extend(ingress["logs"])
            logs.extend(conversion["calls"][0]["logs"])
            logs.extend(conversion["logs"])
    receipt = ObservedReceipt(
        BlockRef(1, 26_477_242, HEADER),
        TX,
        81,
        SEND,
        ACTOR,
        1,
        0x20000,
        9,
        tuple(
            LogRecord(log["address"], tuple(log["topics"]), log["data"], 26_477_242, TX, pos)
            for pos, log in enumerate(logs)
        ),
    )
    return receipt, root


def test_bancor_conversion_legs_bind_real_integer_scales_and_trace_transfers():
    receipt, trace = case()
    assert [event.shape for event in profile_swap_receipt(receipt).events] == [
        "bancor_conversion",
        "bancor_conversion",
    ]
    proof = replay_trace_funding(receipt, trace)
    assert not proof.unresolved_events
    assert [(p.leg.amount_in, p.leg.amount_out) for p in proof.resolved_swaps] == [(X, Y), (Y, Z)]
    assert [p.call_path for p in proof.resolved_swaps] == [(0, 1), (1, 1)]
    assert proof.profile.longest_witnessed_path == (2, 5)
    assert [(p.amount, p.token, p.evidence_id) for p in proof.links] == [(Y, B, "logs:1,3")]


@pytest.mark.parametrize("kind", ["duplicate", "return"])
def test_bancor_ambiguity_and_conflicting_returns_stay_unresolved(kind):
    receipt, trace = case(kind)
    proof = replay_trace_funding(receipt, trace)
    assert len(proof.resolved_swaps) == 1
    assert len(proof.unresolved_events) == 1 and not proof.links


def test_intervening_balance_movement_prevents_attribution_to_same_recipient():
    receipt, trace = case("intervening")
    proof = replay_trace_funding(receipt, trace)
    assert len(proof.resolved_swaps) == 2 and not proof.links


def test_unmatched_trace_log_rejects_all_funding():
    receipt, trace = case()
    first = replace(receipt.logs[0], data=word(1))
    with pytest.raises(ValueError, match="differ from receipt"):
        replay_trace_funding(replace(receipt, logs=(first, *receipt.logs[1:])), trace)


def test_v3_supports_only_omitted_trailing_bytes_padding():
    sig = topic("Swap(address,address,int256,int256,uint160,uint128,int24)")
    data = calldata(
        "swap(address,bool,int256,uint160,bytes)",
        ("address", "bool", "int256", "uint160", "bytes"),
        (ACTOR, True, 500, 42, b"\x12"),
    )
    inc = movement(A, POOL1, ACTOR, POOL1, 500)
    out = movement(B, POOL1, POOL1, ACTOR, 700)
    swap = event(
        POOL1,
        sig,
        (ACTOR, ACTOR),
        ("int256", "int256", "uint160", "uint128", "int24"),
        (500, -700, 50, 60, 1),
        2,
    )
    root = call(
        SEND,
        ACTOR,
        "0x",
        children=[
            call(
                ACTOR,
                POOL1,
                data[:-62],
                out="0x" + encode(("int256", "int256"), (500, -700)).hex(),
                children=[inc, out],
                logs=[swap],
            )
        ],
    )
    receipt = replay_receipt(root, [*inc["logs"], *out["logs"], swap])
    resolved = replay_trace_funding(receipt, root)
    assert resolved.resolved_swaps[0].leg.token_in == A
    assert resolved.resolved_swaps[0].leg.token_out == B
    root["calls"][0]["input"] = data[:-64]  # Drops the actual bytes argument, not just padding.
    rejected = replay_trace_funding(receipt, root)
    assert not rejected.resolved_swaps and rejected.unresolved_events[0][0] == 2


MANAGER = "0x000000000004444c5dc75cb358380d2e3de08a90"
V4_EVENT = "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"
V4_KEY = ("address", "address", "uint24", "int24", "address")
KEY_ARG = "(" + ",".join(V4_KEY) + ")"


def replay_receipt(root, logs):
    base, _ = case()
    root["gasUsed"] = hex(base.gas_used)
    return replace(
        base,
        logs=tuple(
            LogRecord(log["address"], tuple(log["topics"]), log["data"], base.block.number, TX, i)
            for i, log in enumerate(logs)
        ),
    )


def v4_swap(token, output, amount_in, amount_out, hook):
    key = (*sorted((token, output)), 3000, 60, hook)
    forward = key[0] == token
    d0, d1 = (-amount_in, amount_out) if forward else (amount_out, -amount_in)
    pool_id = "0x" + keccak(encode(V4_KEY, key)).hex()
    raw = calldata(
        "swap((address,address,uint24,int24,address),(bool,int256,uint160),bytes)",
        (KEY_ARG, "(bool,int256,uint160)", "bytes"),
        (key, (forward, -amount_in, 42), b""),
    )
    swap = {
        "address": MANAGER,
        "topics": [topic(V4_EVENT), pool_id, word(int(ACTOR, 16))],
        "data": "0x"
        + encode(
            ("int128", "int128", "uint160", "uint128", "int24", "uint24"),
            (d0, d1, 43, 400, 1, 3000),
        ).hex(),
        "position": "0x0",
    }
    mask = (1 << 128) - 1
    packed = ((d0 & mask) << 128) | (d1 & mask)
    return call(ACTOR, MANAGER, raw, out=word(packed), logs=[swap]), swap


@pytest.mark.parametrize("fault", ["none", "hook", "callback_payload"])
def test_exact_v4_unlocked_net_links_require_matching_hookless_callback(fault):
    zero = "0x" + "00" * 20
    first, a = v4_swap(A, B, 500, 700, zero if fault != "hook" else ACTOR)
    second, b = v4_swap(B, C, 700, 900, zero)
    unlock_data = calldata("unlock(bytes)", ("bytes",), (b"\x53",))[:-62]
    callback_data = calldata(
        "unlockCallback(bytes)", ("bytes",), (b"\x51" if fault == "callback_payload" else b"\x53",)
    )
    callback = call(MANAGER, ACTOR, callback_data, children=[first, second])
    unlock = call(ACTOR, MANAGER, unlock_data, children=[callback])
    root = call(SEND, ACTOR, "0x", children=[unlock])
    receipt = replay_receipt(root, [a, b])
    proof = replay_trace_funding(receipt, root)
    assert len(proof.resolved_swaps) == 2
    if fault == "none":
        assert proof.profile.longest_witnessed_path == (0, 1)
        assert proof.links[0].evidence_id == "unlock:0,1"
    else:
        assert not proof.links


@pytest.mark.parametrize(
    "fault", ["none", "wrong_owner", "wrong_middle", "missing_payment", "extra_converter"]
)
def test_bancor_two_converter_route_binds_both_anchors_and_intermediate_payment(fault):
    one = network(A, B, Y, X, POOL1, ANCHOR1, POOL2)
    two = network(B, C, Z, Y if fault != "wrong_middle" else Y + 1, POOL2, ANCHOR2, FINAL_POOL)
    ingress, first = one["calls"]
    second = two["calls"][1]
    if fault == "missing_payment":
        first["calls"][0]["logs"] = []
    lookups = [
        call(BANCOR, a, topic("owner()")[:10], out=word(int(p, 16)))
        for a, p in ((ANCHOR1, POOL1), (ANCHOR2, POOL1 if fault == "wrong_owner" else POOL2))
    ]
    parent = call(
        ACTOR,
        BANCOR,
        calldata(
            "convertByPath(address[],uint256,uint256,address,address,uint256)",
            ("address[]", "uint256", "uint256", "address", "address", "uint256"),
            ((A, ANCHOR1, B, ANCHOR2, C), X, Z, FINAL_POOL, "0x" + "00" * 20, 0),
        ),
        out=word(Z),
        children=[*lookups, ingress, first, second],
    )
    if fault == "extra_converter":
        parent["calls"].append(dict(second, logs=[], calls=[]))
    root = call(SEND, ACTOR, "0x", children=[parent])
    logs = [
        *ingress["logs"],
        *first["calls"][0]["logs"],
        *first["logs"],
        *second["calls"][0]["logs"],
        *second["logs"],
    ]
    result = replay_trace_funding(replay_receipt(root, logs), root)
    if fault == "none":
        assert len(result.resolved_swaps) == 2 and not result.unresolved_events
        assert [(link.amount, link.token, link.evidence_id) for link in result.links] == [
            (Y, B, "logs:1")
        ]
    else:
        assert not result.resolved_swaps and len(result.unresolved_events) == 2


@pytest.mark.parametrize(
    ("suffix", "resolved"),
    [
        (b"", True),
        (bytes(32), True),
        (bytes(31), False),
        (bytes(31) + b"\x01", False),
        (bytes(64), False),
    ],
)
def test_v2_empty_callback_single_zero_word_is_replay_only(suffix, resolved):
    types = ("uint256", "uint256", "address", "bytes")
    data = calldata("swap(uint256,uint256,address,bytes)", types, (0, 700, ACTOR, b""))
    inc = movement(A, ACTOR, ACTOR, POOL1, 500)
    out = movement(B, POOL1, POOL1, ACTOR, 700)
    swap = event(
        POOL1,
        topic("Swap(address,uint256,uint256,uint256,uint256,address)"),
        (ACTOR, ACTOR),
        ("uint256",) * 4,
        (500, 0, 0, 700),
        1,
    )
    root = call(
        SEND,
        ACTOR,
        "0x",
        children=[inc, call(ACTOR, POOL1, data + suffix.hex(), children=[out], logs=[swap])],
    )
    result = replay_trace_funding(replay_receipt(root, [*inc["logs"], *out["logs"], swap]), root)
    assert bool(result.resolved_swaps) is resolved


@pytest.mark.parametrize(
    "mode",
    [
        "legacy",
        "explicit",
        "wrong_receiver",
        "wrong_return",
        "missing_return",
        "minimum",
        "ambiguous",
    ],
)
def test_curve_exchange_binds_signed_ids_minimum_transfers_and_optional_return(mode):
    explicit = mode != "legacy"
    args = ("int128", "int128", "uint256", "uint256") + (("address",) if explicit else ())
    recipient = FINAL_POOL if explicit else ACTOR
    signature = "exchange(" + ",".join(args) + ")"
    data = calldata(
        signature, args, (0, 1, X, Z + int(mode == "minimum")) + ((recipient,) if explicit else ())
    )
    paid = movement(A, POOL1, ACTOR, POOL1, X)
    sent = movement(B, POOL1, POOL1, ACTOR if mode == "wrong_receiver" else recipient, Z)
    if mode == "ambiguous":
        paid["logs"] *= 2
    swap = event(
        POOL1,
        topic("TokenExchange(address,int128,uint256,int128,uint256)"),
        (ACTOR,),
        ("int128", "uint256", "int128", "uint256"),
        (0, X, 1, Z),
        2,
    )
    frame = call(
        ACTOR,
        POOL1,
        data,
        out=word(Z + int(mode == "wrong_return"))
        if explicit and mode != "missing_return"
        else "0x",
        children=[paid, sent],
        logs=[swap],
    )
    root = call(SEND, ACTOR, "0x", children=[frame])
    result = replay_trace_funding(replay_receipt(root, [*paid["logs"], *sent["logs"], swap]), root)
    assert bool(result.resolved_swaps) == (mode in {"legacy", "explicit"})
    if result.resolved_swaps:
        assert result.resolved_swaps[0].leg.amount_out == Z


@pytest.mark.parametrize("fault", ["none", "extra_movement", "wrong_payer", "delta_hook"])
def test_three_transfer_v4_funding_stops_at_intervening_route_inventory(fault):
    previous = network(A, B, Y, X, POOL1, ANCHOR1, ACTOR)
    previous_payment, conversion = previous["calls"]
    prepay = movement(B, ACTOR, ACTOR, POOL2, Y)
    extra = [movement(B, ACTOR, POOL2, FINAL_POOL, 1)] if fault == "extra_movement" else []
    deposit = movement(B, POOL2, FINAL_POOL if fault == "wrong_payer" else POOL2, MANAGER, Y)
    swap, ev = v4_swap(B, C, Y, Z, ACTOR if fault == "delta_hook" else "0x" + "00" * 20)
    swap["from"] = POOL2
    ev["topics"][2] = word(int(POOL2, 16))
    cb = call(
        MANAGER,
        POOL2,
        calldata("unlockCallback(bytes)", ("bytes",), (b"x",)),
        children=[deposit, swap],
    )
    unlock = call(POOL2, MANAGER, calldata("unlock(bytes)", ("bytes",), (b"x",)), children=[cb])
    # The router is called by the executor before it opens the manager callback.
    router = call(ACTOR, POOL2, "0x", children=[unlock])
    root = call(SEND, ACTOR, "0x", children=[previous, prepay, *extra, router])
    logs = [
        *previous_payment["logs"],
        *conversion["calls"][0]["logs"],
        *conversion["logs"],
        *prepay["logs"],
        *(log for frame in extra for log in frame["logs"]),
        *deposit["logs"],
        ev,
    ]
    result = replay_trace_funding(replay_receipt(root, logs), root)
    assert len(result.resolved_swaps) == 2
    assert [link.evidence_id for link in result.links] == (
        ["logs:1,3,4"] if fault == "none" else []
    )
