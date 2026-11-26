"""Synthetic call envelopes using the principal amount from the retained short case."""

from dataclasses import replace

import pytest
from eth_abi.abi import encode

from oasis_agent.arbitrage.cycle_protocols import calldata
from oasis_agent.evidence.public_receipts import ObservedReceipt
from oasis_agent.evidence.swap_flow import TRANSFER
from oasis_agent.evidence.weth_principal import MANAGER, WETH, replay_weth_principal
from oasis_agent.models.chain import LogRecord
from oasis_agent.models.common import BlockRef

BORROWER = "0x96cb0b6dd66cbd3201681b9f9588b7107084f9ea"
SENDER = "0x1d042c727d3f0b228a7438378e531773d1d8e353"
OTHER_ASSET = "0x" + "22" * 20
OTHER_BORROWER = "0x" + "33" * 20
AMOUNT = 14_902_510_626_322_879_634
BASELINE = 2_348_104_655_577_240_218_564
BLOCK = BlockRef(1, 26_406_447, "0x" + "ee" * 32)
TX = "0x60dbcb1ba3dce2621d98150d176b453e91f6d1c7b59e987ba0d3a309aded324a"


def word(value):
    return f"0x{value:064x}"


def call(
    sender,
    recipient,
    signature=None,
    types=(),
    values=(),
    *,
    out="0x",
    children=(),
    logs=(),
    kind="CALL",
):
    return {
        "type": kind,
        "value": "0x0",
        "from": sender,
        "to": recipient,
        "input": calldata(signature, types, values) if signature else "0x",
        "output": out,
        "calls": list(children),
        "logs": list(logs),
    }


def movement(sender, recipient, amount):
    return call(
        sender,
        WETH,
        "transfer(address,uint256)",
        ("address", "uint256"),
        (recipient, amount),
        out=word(1),
        logs=[
            {
                "address": WETH,
                "topics": [TRANSFER, word(int(sender, 16)), word(int(recipient, 16))],
                "data": word(amount),
                "position": "0x0",
            }
        ],
    )


def balance(amount):
    return call(
        MANAGER,
        WETH,
        "balanceOf(address)",
        ("address",),
        (MANAGER,),
        out=word(amount),
        kind="STATICCALL",
    )


def take(amount=AMOUNT, borrower=BORROWER):
    return call(
        BORROWER,
        MANAGER,
        "take(address,address,uint256)",
        ("address", "address", "uint256"),
        (WETH, borrower, amount),
        children=[movement(MANAGER, borrower, amount)],
    )


def repayment(amount=AMOUNT, *, asset=WETH, delta=None):
    sync = call(
        BORROWER,
        MANAGER,
        "sync(address)",
        ("address",),
        (asset,),
        children=[balance(BASELINE)],
    )
    pay = movement(BORROWER, MANAGER, amount)
    settle = call(
        BORROWER,
        MANAGER,
        "settle()",
        out=word(amount),
        children=[balance(BASELINE + (amount if delta is None else delta))],
    )
    return sync, pay, settle


def unlock(children=(), *, payload=b"frozen plan", callback_payload=None):
    output = "0x" + encode(("bytes",), (b"",)).hex()
    callback = call(
        MANAGER,
        BORROWER,
        "unlockCallback(bytes)",
        ("bytes",),
        (payload if callback_payload is None else callback_payload,),
        children=children,
        out=output,
    )
    return call(
        BORROWER,
        MANAGER,
        "unlock(bytes)",
        ("bytes",),
        (payload,),
        children=[callback],
        out=output,
    )


def bound_receipt(*children):
    trace = call(SENDER, BORROWER, children=children)
    trace["gasUsed"] = "0x7530"

    def committed_logs(frame):
        if frame.get("error") is None:
            yield from frame["logs"]
            for child in frame["calls"]:
                yield from committed_logs(child)

    logs = tuple(
        LogRecord(row["address"], tuple(row["topics"]), row["data"], BLOCK.number, TX, i)
        for i, row in enumerate(committed_logs(trace))
    )
    return ObservedReceipt(BLOCK, TX, 2, SENDER, BORROWER, 1, 30_000, 1, logs), trace


def test_retained_amount_and_other_weth_movements_keep_distinct_proof() -> None:
    group = unlock((take(), *repayment(8), take(5), *repayment()))
    receipt, root = bound_receipt(group)
    result = replay_weth_principal(receipt, root)
    (row,) = result.unlocks
    assert row.reason is None and row.proof is not None
    assert row.proof.amount_wei == AMOUNT
    assert (row.proof.take_log, row.proof.repayment_log) == (0, 3)
    assert row.other_outbound_logs == (2,) and row.other_inbound_logs == (1,)
    assert row.proof.balance_at_settle_wei - row.proof.balance_at_sync_wei == AMOUNT


def test_equal_amounts_in_disjoint_unlocks_use_each_log_once() -> None:
    receipt, root = bound_receipt(unlock((take(), *repayment())), unlock((take(), *repayment())))
    results = replay_weth_principal(receipt, root).unlocks
    proofs = tuple(row.proof for row in results if row.proof)
    assert len(proofs) == 2
    assert [(p.take_log, p.repayment_log) for p in proofs] == [(0, 1), (2, 3)]


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("precursor", "no_initial_borrower_weth_take"),
        ("wrong_actor", "no_initial_borrower_weth_take"),
        ("other_currency", "missing_unique_weth_sync_settle"),
        ("balance", "weth_reserve_change_does_not_match_return"),
        ("same_amount", "missing_or_ambiguous_equal_weth_transfer"),
        ("stray_transfer", "non_unique_weth_movement_in_settlement_window"),
        ("callback", "invalid_unlock_callback"),
    ],
)
def test_uncertain_attribution_is_exposed(fault: str, reason: str) -> None:
    sync, pay, settle = repayment(
        asset=OTHER_ASSET if fault == "other_currency" else WETH,
        delta=AMOUNT - 1 if fault == "balance" else None,
    )
    children = [
        take(borrower=OTHER_BORROWER if fault == "wrong_actor" else BORROWER),
        sync,
        pay,
        settle,
    ]
    if fault == "precursor":
        children.insert(
            0, call(BORROWER, MANAGER, "clear(address,uint256)", ("address", "uint256"), (WETH, 0))
        )
    elif fault == "same_amount":
        children.append(pay)
    elif fault == "stray_transfer":
        children.insert(3, movement(BORROWER, MANAGER, 1))
    group = unlock(children, callback_payload=b"different plan" if fault == "callback" else None)
    result = replay_weth_principal(*bound_receipt(group))
    (row,) = result.unlocks
    assert row.proof is None and row.reason == reason


def test_nested_success_is_rejected_and_reverted_nested_evidence_is_ignored() -> None:
    inner = unlock((take(), *repayment()))
    outer = unlock((take(), inner, *repayment()))
    nested = replay_weth_principal(*bound_receipt(outer))
    assert [row.reason for row in nested.unlocks] == ["nested_committed_unlock"] * 2
    assert all(row.proof is None for row in nested.unlocks)

    inner["error"] = "execution reverted"
    reverted = replay_weth_principal(*bound_receipt(outer))
    successful, failed = reverted.unlocks
    assert successful.proof is not None and successful.proof.amount_wei == AMOUNT
    assert failed.reason == "uncommitted_unlock" and failed.proof is None


def test_refuses_mismatched_receipt_or_non_ethereum() -> None:
    receipt, root = bound_receipt(unlock((take(), *repayment())))
    other_log = replace(receipt.logs[-1], data=word(AMOUNT - 1))
    with pytest.raises(ValueError, match="differ from receipt"):
        replay_weth_principal(replace(receipt, logs=(*receipt.logs[:-1], other_log)), root)
    with pytest.raises(ValueError, match="only canonical Ethereum"):
        replay_weth_principal(replace(receipt, block=replace(BLOCK, chain_id=8453)), root)
