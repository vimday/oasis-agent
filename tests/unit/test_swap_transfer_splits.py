import pytest
from eth_abi import encode

from oasis_agent.arbitrage.cycle_protocols import calldata, topic
from oasis_agent.evidence.public_receipts import ObservedReceipt
from oasis_agent.evidence.swap_flow import TRANSFER
from oasis_agent.evidence.swap_transfer_splits import replay_with_transfer_splits
from oasis_agent.models.chain import BlockRef, LogRecord

A, B, C, POOL1, POOL2, HOLDER, SENDER, IMPLEMENTATION, OTHER = (
    "0x" + f"{i:02x}" * 20 for i in range(1, 10)
)
TX, BLOCK = ("0x" + v * 32 for v in ("31", "32"))


def word(n):
    return "0x" + n.to_bytes(32).hex()


def frame(sender, target, data, *, out="0x", kind="CALL", logs=(), calls=()):
    return {
        "from": sender,
        "to": target,
        "input": data,
        "output": out,
        "type": kind,
        "value": "0x0",
        "logs": list(logs),
        "calls": list(calls),
    }


def event(token, sig, a, b, data, position=0):
    return {
        "address": token,
        "topics": [sig, word(int(a, 16)), word(int(b, 16))],
        "data": data,
        "position": hex(position),
    }


def transfer(token, sender, recipient, amount):
    return frame(
        sender,
        token,
        calldata("transfer(address,uint256)", ("address", "uint256"), (recipient, amount)),
        out=word(1),
        logs=[event(token, TRANSFER, sender, recipient, word(amount))],
    )


def balance(amount):
    return frame(
        HOLDER,
        B,
        calldata("balanceOf(address)", ("address",), (HOLDER,)),
        out=word(amount),
        kind="STATICCALL",
    )


def reserves(pool, malformed=False):
    return frame(
        HOLDER,
        pool,
        "0x0902f1ac",
        kind="STATICCALL",
        out="0x" + encode(["uint112", "uint112", "uint32"], [500, 500, 1]).hex()
        if not malformed
        else "0x",
    )


def swap(pool, in_token, out_token, supplied, bought, fault=None):
    values = (supplied, 0, 0, bought)
    log = event(
        pool,
        topic("Swap(address,uint256,uint256,uint256,uint256,address)"),
        HOLDER,
        HOLDER,
        "0x" + encode(["uint256"] * 4, values).hex(),
        3,
    )
    balances = [500 + supplied, 500 - bought]
    sync = {
        "address": pool,
        "topics": [topic("Sync(uint112,uint112)")],
        "position": "0x3",
        "data": "0x"
        + encode(
            ["uint112", "uint112"],
            [
                balances[0] + int(fault == "sync"),
                balances[1],
            ],
        ).hex(),
    }
    actual_input = OTHER if fault == "unrelated_asset" else in_token
    checks = [
        frame(
            pool,
            token,
            calldata("balanceOf(address)", ("address",), (pool,)),
            kind="STATICCALL",
            out=word(value),
        )
        for token, value in zip((actual_input, out_token), balances, strict=True)
    ]
    return frame(
        HOLDER,
        pool,
        calldata(
            "swap(uint256,uint256,address,bytes)",
            ("uint256", "uint256", "address", "bytes"),
            (0, bought, HOLDER, b""),
        ),
        logs=[sync, log],
        calls=[transfer(out_token, pool, HOLDER, bought), *checks],
    )


def sample(fault):
    logs = [
        event(B, TRANSFER, HOLDER, B if fault != "other_recipient" else OTHER, word(1)),
        event(B, TRANSFER, HOLDER, POOL2, word(99)),
    ]
    call_data = calldata(
        "transfer(address,uint256)",
        ("address", "uint256"),
        (POOL2, 101 if fault == "gross_arg" else 100),
    )
    out = word(0 if fault == "false" else 1)
    impl = frame(B, IMPLEMENTATION, call_data, out=out, kind="DELEGATECALL", logs=logs)
    split = frame(HOLDER, B, call_data, out=out, calls=[impl])
    moves = [
        balance(3 if fault == "inventory" else 0),
        transfer(A, HOLDER, POOL1, 25),
        reserves(POOL1),
        swap(POOL1, A, B, 25, 100),
        balance(99 if fault == "intermediate" else 100),
        split,
        reserves(POOL2, fault == "malformed_reserves"),
        swap(POOL2, B, C, 99, 24, fault),
        balance(1 if fault == "residual" else 0),
    ]
    if fault == "extra_movement":
        moves.insert(5, transfer(B, HOLDER, OTHER, 4))
    if fault == "malformed":
        impl["logs"].append({"address": B, "topics": [TRANSFER], "position": "0x0", "data": "0x"})
    root = frame(SENDER, HOLDER, "0x", calls=moves)
    root["gasUsed"] = "0x1111"
    ordered = []

    def visit(node):
        for pos in range(len(node["calls"]) + 1):
            ordered.extend(log for log in node["logs"] if int(log["position"], 16) == pos)
            if pos < len(node["calls"]):
                visit(node["calls"][pos])

    visit(root)
    rc = ObservedReceipt(
        BlockRef(56, 100, BLOCK),
        TX,
        0,
        SENDER,
        HOLDER,
        1,
        0x1111,
        7,
        tuple(
            LogRecord(log["address"], tuple(log["topics"]), log["data"], 100, TX, idx)
            for idx, log in enumerate(ordered)
        ),
    )
    return rc, root


def test_balance_checked_split_joins_only_the_net_credit_from_a_proxy_token():
    strict, supplemented = replay_with_transfer_splits(*sample(None))
    assert strict.profile.links == 0
    assert supplemented.profile_with_splits.witnessed_depth == 2
    (e,) = supplemented.splits
    assert (e.gross, e.credited, e.retained_by_token) == (100, 99, 1)
    assert (e.source_transfer_log, e.token_credit_log, e.pool_credit_log) == (1, 4, 5)
    assert e.balance_reads == ((0,), (4,), (8,))
    assert e.reserve_reads == ((2,), (6,))
    assert e.sync_logs == (2, 7)


@pytest.mark.parametrize(
    "fault",
    [
        "other_recipient",
        "gross_arg",
        "false",
        "inventory",
        "intermediate",
        "residual",
        "extra_movement",
        "malformed",
        "unrelated_asset",
        "malformed_reserves",
        "sync",
    ],
)
def test_ambiguous_inexact_or_unwitnessed_debit_stays_unknown(fault):
    strict, supplemented = replay_with_transfer_splits(*sample(fault))
    assert not supplemented.splits
    assert supplemented.profile_with_splits == strict.profile
