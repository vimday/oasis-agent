from dataclasses import replace

import pytest

from oasis_agent.arbitrage.cycle_protocols import calldata, topic
from oasis_agent.evidence.public_receipts import ObservedReceipt
from oasis_agent.evidence.swap_flow import TRANSFER
from oasis_agent.evidence.weth_batch_cash import replay_weth_self_calls
from oasis_agent.evidence.weth_principal import WETH
from oasis_agent.models.chain import BlockRef, LogRecord

OWNER, POOL, SENDER = ("0x" + byte * 20 for byte in ("11", "22", "33"))
HASH, TX = ("0x" + byte * 32 for byte in ("aa", "bb"))


def frame(sender, target, method="CALL", data="0x", output="0x", **fields):
    return {
        "from": sender,
        "to": target,
        "type": method,
        "input": data,
        "value": "0x0",
        "output": output,
        **fields,
    }


def word(n):
    return "0x" + n.to_bytes(32).hex()


def read_balance(n, who=OWNER):
    return frame(
        OWNER, WETH, "STATICCALL", calldata("balanceOf(address)", ("address",), (who,)), word(n)
    )


def log(topics, amount):
    return {"address": WETH, "topics": topics, "data": word(amount), "position": "0x0"}


def sample(closing=27):
    me, pool = word(int(OWNER, 16)), word(int(POOL, 16))
    inbound = log([TRANSFER, pool, me], 13)
    wrap = log([topic("Deposit(address,uint256)"), me], 6)
    unwrap = log([topic("Withdrawal(address,uint256)"), me], 2)
    ignored = log([TRANSFER, me, pool], 9)
    exchange = frame(OWNER, POOL, calls=[frame(POOL, WETH, logs=[inbound])])
    failed = frame(
        OWNER, POOL, error="execution reverted", calls=[frame(POOL, WETH, logs=[ignored])]
    )
    calls = [
        read_balance(10),
        exchange,
        failed,
        frame(OWNER, WETH, logs=[wrap]),
        frame(OWNER, WETH, logs=[unwrap]),
        read_balance(closing),
    ]
    branch = frame(OWNER, OWNER, gasUsed="0x300", calls=calls)
    other = frame(OWNER, OWNER, gasUsed="0x41", calls=[])
    revert = frame(
        OWNER,
        OWNER,
        gasUsed="0x52",
        error="execution reverted",
        calls=[frame(OWNER, WETH, logs=[ignored])],
    )
    tree = frame(SENDER, OWNER, calls=[branch, other, revert], gasUsed="0x99ff")
    logs = tuple(
        LogRecord(WETH, tuple(row["topics"]), row["data"], 100, TX, i)
        for i, row in enumerate([inbound, wrap, unwrap], 44)
    )
    rec = ObservedReceipt(BlockRef(1, 100, HASH), TX, 1, SENDER, OWNER, 1, 0x99FF, 5, logs)
    return rec, tree


def test_cash_reconciles_weth_wraps_and_excludes_reverted_subtrees():
    first, missing, reverted = replay_weth_self_calls(*sample())
    assert (first.root_child, first.status, first.frame_gas) == (0, "reconciled", 768)
    assert (first.before_weth_wei, first.after_weth_wei, first.weth_delta_wei) == (10, 27, 17)
    assert first.cash_logs == (44, 45, 46)
    assert (missing.status, missing.weth_delta_wei) == ("missing_boundary_reads", None)
    assert (reverted.status, reverted.weth_delta_wei) == ("reverted", 0)


def test_fails_closed_on_balance_disagreement_or_non_mainnet_weth():
    with pytest.raises(ValueError, match="balance change"):
        replay_weth_self_calls(*sample(28))
    r, t = sample()
    with pytest.raises(ValueError, match="Ethereum executor"):
        replay_weth_self_calls(replace(r, block=BlockRef(56, 100, HASH)), t)


def test_unrelated_balance_query_does_not_witness_executor_cash():
    r, t = sample()
    t["calls"][0]["calls"][0] = read_balance(10, POOL)
    first, *_ = replay_weth_self_calls(r, t)
    assert first.status == "missing_boundary_reads" and first.weth_delta_wei is None


def test_successful_child_has_no_committed_cash_after_root_revert():
    r, t = sample()
    t["error"] = "execution reverted"
    results = replay_weth_self_calls(replace(r, status=0, logs=()), t)
    assert [(c.status, c.weth_delta_wei) for c in results] == [
        ("rolled_back_by_root", 0),
        ("rolled_back_by_root", 0),
        ("reverted", 0),
    ]
