"""Wallet index rows remain claims even when balance arithmetic closes."""

from dataclasses import replace

import pytest

from oasis_agent.arbitrage import aave_wallet as wallet
from tests.unit.test_aave_cashflows import transfer
from tests.unit.test_aave_history import ACCOUNT, ACTOR, RESERVE, receipt
from tests.unit.test_public_receipts import BLOCK, TX


def indexed(kind="erc20", **changes):
    row = {
        "blockNumber": str(BLOCK.number),
        "blockHash": BLOCK.block_hash,
        "transactionIndex": "2",
        "hash": TX,
        "from": ACTOR,
        "to": ACCOUNT,
        "value": "5",
        "contractAddress": RESERVE,
        "tokenDecimal": "6",
        "isError": "0",
        "txreceipt_status": "1",
        "type": "call",
        "traceId": "2",
        "gasUsed": "7",
        "gasPrice": "3",
        **changes,
    }
    return wallet.index_rows(
        kind,
        {"status": "1", "result": [row]},
        ACCOUNT,
        BLOCK.number,
        BLOCK.number,
        (RESERVE, 6) if kind == "erc20" else None,
    )[0]


def test_log_multiset_preserves_equal_sized_transfers_and_missing_receipts():
    transfers = (indexed(), indexed())
    observation = receipt(
        transfer(RESERVE, ACTOR, ACCOUNT, 5, 0), transfer(RESERVE, ACTOR, ACCOUNT, 5, 1)
    )
    start, end = {None: 0, RESERVE: 0}, {None: 0, RESERVE: 10}
    report = wallet.reconcile_wallet(ACCOUNT, start, end, transfers, (observation,))
    assert report["assets"][1]["balance_minus_index_raw"] == 0
    assert report["assets"][1]["receipt_matched_transfer_rows"] == 2
    assert not report["complete_wallet_history_verified"] and report["net_profit"] is None
    partial = wallet.reconcile_wallet(ACCOUNT, start, end, transfers, ())
    assert partial["transactions_without_receipts"] == [TX]
    assert partial["assets"][1]["receipt_matched_transfer_rows"] == 0
    missing = wallet.reconcile_wallet(ACCOUNT, start, end, transfers[:1], (observation,))
    assert missing["disagreements"][0]["reason"] == "erc20_claim_multiset_mismatch"


def test_failed_outer_value_does_not_move_but_costs_gas_and_trace_duplicate_fails():
    external = indexed(
        "external", **{"from": ACCOUNT, "to": ACTOR, "isError": "1", "txreceipt_status": "0"}
    )
    reverted = replace(
        receipt(), sender=ACCOUNT, recipient=ACTOR, status=0, gas_used=7, effective_gas_price=3
    )
    output = wallet.reconcile_wallet(ACCOUNT, {None: 25}, {None: 4}, (external,), (reverted,))
    assert output["assets"][0]["indexed_net_raw"] == -21
    assert output["gas_exact_matches"] == 1
    trace = indexed("internal")
    with pytest.raises(ValueError, match="duplicate"):
        wallet.reconcile_wallet(ACCOUNT, {None: 25}, {None: 25}, (trace, trace), ())


@pytest.mark.parametrize("bad", [{"tokenDecimal": "18"}, {"value": "+5"}, {"blockNumber": "0100"}])
def test_index_uses_strict_units_and_positions(bad):
    with pytest.raises(ValueError):
        indexed(**bad)
