from dataclasses import replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.accounting_replay import ValuedLedgerEntry, replay_position_accounting
from oasis_agent.farming.full_cost import (
    CostCategory,
    CostDisclosure,
    CostTreatment,
    reconcile_full_cost,
)
from oasis_agent.farming.position_ledger import build_position_token_ledger
from oasis_agent.farming.position_ownership import replay_position_ownership
from tests.unit import test_accounting_replay as accounting_fixture


def fixture(tmp_path):
    f = accounting_fixture.PositionAccountingReplayTests()
    f.setUp()
    entry = f.entry(True)
    a, b = f.snapshot(f.b0, 100), f.snapshot(f.b2, 120)
    ledger = build_position_token_ledger(
        a,
        b,
        (entry,),
        canonical_blocks=f.canonical_blocks,
        ownership_replay=replay_position_ownership(a, b, ()),
    )
    replay = replay_position_accounting(
        ledger, f.valuation(f.b0), f.valuation(f.b2), (ValuedLedgerEntry(entry, f.valuation(f.b1)),)
    )
    store = EvidenceStore(tmp_path / "costs.db")
    costs = tuple(
        CostDisclosure(
            c,
            CostTreatment.NOT_APPLICABLE,
            f.key.currency1,
            18,
            0,
            (
                store.put(
                    "synthetic_cost",
                    c.value.encode(),
                    f.b1,
                    "fixture",
                    request={"category": c.value},
                    raw_response={"amount": 0},
                    interpretation={"fixture": True},
                ),
            ),
            "synthetic explicit zero",
        )
        for c in CostCategory
    )
    return replay, costs, f.canonical_blocks


def test_missing_cost_never_turns_into_zero(tmp_path):
    replay, costs, blocks = fixture(tmp_path)
    for i in range(len(costs)):
        result = reconcile_full_cost(
            replay, costs[:i] + costs[i + 1 :], quote_decimals=18, canonical_blocks=blocks
        )
        assert result.net_pnl is None
        assert result.missing == (costs[i].category.value,)


def test_extra_costs_debit_exactly_once_and_embedded_losses_are_diagnostic(tmp_path):
    replay, costs, blocks = fixture(tmp_path)
    for amount in (0, 1, 10**30):
        changed = tuple(
            replace(c, treatment=CostTreatment.EXTRA, amount=amount)
            if c.category is CostCategory.GAS
            else replace(c, treatment=CostTreatment.EMBEDDED, amount=999)
            if c.category is CostCategory.IL
            else c
            for c in costs
        )
        result = reconcile_full_cost(replay, changed, quote_decimals=18, canonical_blocks=blocks)
        assert result.net_pnl == replay.net_pnl1 - amount
        assert not result.execution_eligible


def test_cost_double_count_and_currency_mismatch_rejected(tmp_path):
    replay, costs, blocks = fixture(tmp_path)
    with pytest.raises(ValueError):
        reconcile_full_cost(replay, (*costs, costs[0]), quote_decimals=18, canonical_blocks=blocks)
    with pytest.raises(ValueError):
        reconcile_full_cost(replay, costs, quote_decimals=6, canonical_blocks=blocks)
    with pytest.raises(ValueError):
        replace(costs[-3], category=CostCategory.IL, treatment=CostTreatment.EXTRA)
    with pytest.raises(ValueError):
        reconcile_full_cost(
            replace(replay, external_cost1=1), costs, quote_decimals=18, canonical_blocks=blocks
        )


def test_wrong_block_hash_and_reused_expense_evidence_fail(tmp_path):
    replay, costs, blocks = fixture(tmp_path)
    wrong = replace(
        costs[0].evidence[0], block=replace(costs[0].evidence[0].block, block_hash="0x" + "99" * 32)
    )
    with pytest.raises(ValueError, match="provenance"):
        reconcile_full_cost(
            replay,
            (replace(costs[0], evidence=(wrong,)), *costs[1:]),
            quote_decimals=18,
            canonical_blocks=blocks,
        )
    first = replace(costs[0], treatment=CostTreatment.EXTRA, amount=1)
    second = replace(costs[1], treatment=CostTreatment.EXTRA, amount=1, evidence=first.evidence)
    with pytest.raises(ValueError, match="more than once"):
        reconcile_full_cost(
            replay, (first, second, *costs[2:]), quote_decimals=18, canonical_blocks=blocks
        )
