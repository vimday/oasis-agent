"""Full raw snapshot/event/payment replay; fixtures do not establish live PnL."""

import json
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.ingestion import persist_and_verify_block_range
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.cash_window import (
    CashWindowRequest,
    replay_cash_window,
    require_cash_window_successor,
)
from oasis_agent.farming.owner_payments import persist_owner_payment
from oasis_agent.farming.position_capture import persist_position_snapshot_evidence
from oasis_agent.models.common import canonical_json
from scripts.verify_cash_window import verify
from tests.rpc_evidence_helpers import range_exchanges
from tests.unit.test_owner_payments import fixture
from tests.unit.test_position_capture import snapshot_capture
from tests.unit.test_position_dataset import CORE, scenario


@pytest.fixture
def retained(tmp_path):
    store = EvidenceStore(tmp_path / "cash.db")
    try:

        def snapshots(closing):
            capture = snapshot_capture(closing=closing)
            return tuple(
                persist_position_snapshot_evidence(store, replace(capture, source=source))
                for source in ("rpc-a", "rpc-b")
            )

        def events(dataset):
            refs = []
            for source in ("rpc-a", "rpc-b"):
                peer = replace(dataset, source=source)
                refs.append(
                    persist_and_verify_block_range(
                        store, peer, range_exchanges(peer), research_single_provider=True
                    ).evidence
                )
            return tuple(refs)

        _, _, core, nft = scenario()
        payment = fixture()
        payments = tuple(
            persist_owner_payment(
                store, replace(payment, source=source, core=replace(payment.core, source=source))
            ).settlement_evidence
            for source in ("rpc-a", "rpc-b")
        )
        yield (
            store,
            CashWindowRequest(
                CORE, snapshots(False), snapshots(True), events(core), events(nft), (payments,)
            ),
        )
    finally:
        store.close()


def test_full_window_reconstructs_cash_and_action_time_owner(retained, tmp_path):
    store, request = retained
    result = replay_cash_window(store, request)
    assert result.ledger.settlement_complete
    assert (result.ledger.cumulative_owner_amount0, result.ledger.cumulative_owner_amount1) == (
        -100,
        -200,
    )
    assert result.cash_by_owner[result.ledger.opening.nft_owner].amount0 == -100
    assert result.ledger.closing.nft_owner not in result.cash_by_owner
    assert result.payments[0].owner == result.ledger.opening.nft_owner
    assert result.payments[0].owner != result.ledger.closing.nft_owner
    assert json.loads(result.payload)["net_pnl"] is None
    assert json.loads(result.payload)["execution_eligible"] is False
    path = tmp_path / "request.json"
    path.write_bytes(canonical_json(asdict(request)))
    output = tmp_path / "report.json"
    verify(path, tmp_path / "cash.db", output)
    assert output.read_bytes() == result.payload + b"\n"
    with pytest.raises(ValueError, match="new output"):
        verify(path, tmp_path / "cash.db", output)


@pytest.mark.parametrize("case", ["missing", "extra", "same_source", "cohort", "wrong_block"])
def test_incomplete_or_misbound_evidence_rejected(retained, case):
    store, request = retained
    if case == "missing":
        request = replace(request, payments=())
    elif case == "extra":
        request = replace(request, payments=request.payments * 2)
    elif case == "same_source":
        request = replace(request, opening=(request.opening[0], request.opening[0]))
    elif case == "cohort":
        request = replace(
            request, closing=(request.closing[0], replace(request.closing[1], source="c"))
        )
    else:
        request = replace(request, closing=request.opening)
    with pytest.raises(ValueError):
        replay_cash_window(store, request)


def test_forged_cash_interpretation_cannot_change_window_balance(retained):
    store, request = retained
    ref = request.payments[0][1]
    record = store.get_record(ref)
    value = json.loads(record.payload)
    value["amounts"]["amount0"] = 999999
    forged = store.put(
        ref.kind,
        canonical_json(value),
        ref.block,
        ref.source,
        request=record.request_payload,
        raw_response=record.raw_response_payload,
        interpretation={"forged": True},
    )
    with pytest.raises(ValueError, match="interpretation"):
        replay_cash_window(store, replace(request, payments=((request.payments[0][0], forged),)))


def test_window_successor_rejects_duplicate_and_gap(retained):
    store, request = retained
    previous = replay_cash_window(store, request)
    with pytest.raises(ValueError, match="gap"):
        require_cash_window_successor(previous, previous)
    # This exercises the pure boundary comparison, not a second raw replay claim.
    following = replace(previous, ledger=replace(previous.ledger, opening=previous.ledger.closing))
    require_cash_window_successor(previous, following)
    following = replace(following, request=replace(request, pool_manager="0x" + "fe" * 20))
    with pytest.raises(ValueError, match="PoolManager"):
        require_cash_window_successor(previous, following)


def test_forged_snapshot_interpretation_is_replayed_from_wire(retained):
    store, request = retained
    ref = request.closing[1]
    record = store.get_record(ref)
    value = json.loads(record.payload)
    value["snapshot"]["liquidity"] += 1
    forged = store.put(
        ref.kind,
        canonical_json(value),
        ref.block,
        ref.source,
        request=record.request_payload,
        raw_response=record.raw_response_payload,
        interpretation={"forged": True},
    )
    with pytest.raises(ValueError, match="interpretation"):
        replay_cash_window(store, replace(request, closing=(request.closing[0], forged)))


def test_cash_totals_do_not_net_different_owners(retained):
    store, request = retained
    window = replay_cash_window(store, request)
    first = window.payments[0]
    other_owner = window.ledger.closing.nft_owner
    other = replace(first, owner=other_owner)
    # Pure aggregation test; this constructed object is not a raw witness claim.
    combined = replace(window, payments=(first, other, first))
    assert combined.cash_by_owner[first.owner].amount0 == -200
    assert combined.cash_by_owner[other_owner].amount0 == -100
    wrong_units = replace(
        first, amounts=replace(first.amounts, token0=replace(first.amounts.token0, decimals=6))
    )
    with pytest.raises(ValueError, match="decimals"):
        _ = replace(window, payments=(first, wrong_units)).cash_by_owner
