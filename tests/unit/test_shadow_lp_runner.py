"""Shadow LP decision journaling regressions; never live profitability."""

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.lp_state_machine import TickRange
from oasis_agent.execution.range_controller import (
    LpActionType,
    MarginalValueInputs,
    RebalanceGateInput,
)
from oasis_agent.farming.strategy import Band, BandTableFeeStrategy
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import BlockRef
from oasis_agent.models.finance import PoolState
from oasis_agent.models.v4 import SwapDirection
from oasis_agent.risk.lp_policy import CanaryRiskLimits
from oasis_agent.shadow.journal import ShadowJournal
from oasis_agent.shadow.lp_runner import LpShadowRunner


def evidence(tmp_path, block):
    return EvidenceStore(tmp_path / "evidence.db").put(
        "shadow_lp_header",
        b"fixture",
        block.ref,
        "fixture",
        request={"block": block.ref.number},
        raw_response={"hash": block.ref.block_hash},
        interpretation={"fixture": True},
    )


def band_strategy(**overrides):
    base = {
        "min_fee": 100,
        "max_fee": 10_000,
        "base_fee": 500,
        "volatility_bands": (Band(100, 100), Band(1_000, 400)),
        "impact_bands0": (Band(1_000_000, 200), Band(10_000_000, 800)),
        "impact_bands1": (Band(1_000, 200), Band(10_000, 800)),
        "toxicity_bands": (Band(100, 100), Band(500, 300)),
        "inventory_bands": (Band(100, 100), Band(500, 500)),
    }
    base.update(overrides)
    return BandTableFeeStrategy(**base)  # type: ignore[arg-type]


def pool_state(**kwargs):
    values = {
        "volatility": Decimal("0.01"),
        "inventory_imbalance": Decimal("0.02"),
        "adverse_selection": Decimal("0.01"),
        "rebalance_incentive": Decimal("0.005"),
        "observed_at_timestamp": 100,
        "oracle_timestamp": 95,
        "block": blocks()[0].ref,
    }
    values.update(kwargs)
    return PoolState(**values)


def marginal_inputs():
    return MarginalValueInputs(
        expected_incremental_fees=Decimal(1000),
        expected_incremental_lvr=Decimal(100),
        inventory_risk_charge=Decimal(50),
        swap_rebalance_gas_mev_cost=Decimal(100),
        failure_and_model_uncertainty_buffer=Decimal(25),
    )


def passing_gate():
    return RebalanceGateInput(
        price_left_buffer=True,
        dwell_met=True,
        marginal_value=Decimal(750),
        hysteresis_threshold=Decimal(10),
        volatility=Decimal("0.01"),
        max_volatility=Decimal("0.1"),
        toxicity=Decimal("0.01"),
        max_toxicity=Decimal("0.1"),
        reference_fresh=True,
        route_fresh=True,
        nonce_fresh=True,
        balances_fresh=True,
        approvals_fresh=True,
        gas_reserve_fresh=True,
        post_action_limits_ok=True,
        pin_simulated_ok=True,
        cooldown_met=True,
        daily_limit_ok=True,
    )


def canary_limits():
    return CanaryRiskLimits(
        approved_experiment_loss=Decimal(1000),
        portfolio_nav=Decimal(1000000),
        exit_depth_two_sided=Decimal(2000000),
        reserve_requirement=Decimal(200000),
    )


def blocks():
    b1 = BlockSnapshot(BlockRef(1, 10, "0x" + "11" * 32, 100), "0x" + "00" * 32)
    b2 = BlockSnapshot(BlockRef(1, 11, "0x" + "22" * 32, 101), b1.ref.block_hash)
    return b1, b2


def runner(tmp_path, **kwargs):
    journal = ShadowJournal(tmp_path / "shadow.db", chain_id=1, max_age_seconds=10)
    return LpShadowRunner(journal, band_strategy(), **kwargs)


def recenter_candidates():
    return {
        LpActionType.RECENTER: (
            marginal_inputs(),
            TickRange(-60, 60, 60),
        )
    }


def test_hold_decision_records_fee_quote(tmp_path):
    b, _ = blocks()
    r = runner(tmp_path)
    decision = r.decide(
        header=b,
        evidence=evidence(tmp_path, b),
        state=pool_state(),
        direction=SwapDirection.ZERO_FOR_ONE,
        amount_specified=100_000,
        exact_input=True,
        now=100,
    )
    assert decision.decision == "hold"
    assert decision.health == "HOLD"
    assert decision.fee_quote is not None
    assert 100 <= decision.fee_quote.effective_fee <= 10_000
    assert decision.range_action is None
    assert decision.reasons == ()
    record = r.journal.decision_records()[-1]
    assert record["execution_eligible"] is False
    assert record["strategy_qualification"] == "unresolved"
    assert record["fee_quote"]["rule_version"] == "band-table-v1"
    assert record["decision"] == "hold"
    r.journal.close()


def test_recenter_proposal_canary_and_gate_recorded(tmp_path):
    b, _ = blocks()
    r = runner(tmp_path, min_benefit=Decimal(0), canary_limits=canary_limits())
    decision = r.decide(
        header=b,
        evidence=evidence(tmp_path, b),
        state=pool_state(),
        direction=SwapDirection.ONE_FOR_ZERO,
        amount_specified=50_000,
        exact_input=False,
        now=100,
        candidates=recenter_candidates(),
        gate_input=passing_gate(),
    )
    assert decision.decision == "recenter"
    assert decision.range_action is not None
    assert decision.range_action.target_range == TickRange(-60, 60, 60)
    assert decision.rebalance_allowed is True
    assert decision.rebalance_reasons == ()
    assert decision.canary_cap is not None
    assert decision.canary_cap.cap == Decimal(200)
    record = r.journal.decision_records()[-1]
    assert record["range_action"]["target_range"]["tick_lower"] == -60
    assert record["canary_cap"]["cap"] == "200"
    assert record["decision"] == "recenter"
    r.journal.close()


def test_gate_denial_suppresses_proposal_without_erasing_it(tmp_path):
    b, _ = blocks()
    gate = replace(passing_gate(), volatility=Decimal("0.5"))
    r = runner(tmp_path, min_benefit=Decimal(0))
    decision = r.decide(
        header=b,
        evidence=evidence(tmp_path, b),
        state=pool_state(),
        direction=SwapDirection.ZERO_FOR_ONE,
        amount_specified=100_000,
        exact_input=True,
        now=100,
        candidates=recenter_candidates(),
        gate_input=gate,
    )
    assert decision.decision == "hold"
    assert decision.rebalance_allowed is False
    assert any("volatility" in reason for reason in decision.rebalance_reasons)
    assert decision.range_action is not None  # proposal retained in the audit trail
    r.journal.close()


def test_stale_block_halts_and_never_evaluates(tmp_path):
    b, _ = blocks()
    r = runner(tmp_path)
    decision = r.decide(
        header=b,
        evidence=evidence(tmp_path, b),
        state=pool_state(),
        direction=SwapDirection.ZERO_FOR_ONE,
        amount_specified=100_000,
        exact_input=True,
        now=150,
        candidates=recenter_candidates(),
    )
    assert decision.decision == "halt"
    assert decision.health == "HALT"
    assert "stale_or_future_block" in decision.reasons
    assert decision.fee_quote is None
    assert decision.range_action is None
    record = r.journal.decision_records()[-1]
    assert record["execution_eligible"] is False
    r.journal.close()


def test_halt_latches_future_decisions(tmp_path):
    b1, b2 = blocks()
    path = tmp_path / "shadow.db"
    journal = ShadowJournal(path, chain_id=1, max_age_seconds=10)
    r = LpShadowRunner(journal, band_strategy())
    assert (
        r.decide(
            header=b1,
            evidence=evidence(tmp_path, b1),
            state=pool_state(),
            direction=SwapDirection.ZERO_FOR_ONE,
            amount_specified=100_000,
            exact_input=True,
            now=150,
        ).decision
        == "halt"
    )
    # Healthy inputs cannot resume a latched HALT.
    decision = r.decide(
        header=b2,
        evidence=evidence(tmp_path, b2),
        state=pool_state(),
        direction=SwapDirection.ZERO_FOR_ONE,
        amount_specified=100_000,
        exact_input=True,
        now=101,
    )
    assert decision.decision == "halt"
    assert "halt_latched" in decision.reasons
    r.journal.close()


def test_strategy_failure_halts_without_retaining_error_text(tmp_path):
    b, _ = blocks()
    r = runner(tmp_path)
    stale = pool_state(oracle_timestamp=0)  # 100s old, beyond max_oracle_age_seconds
    decision = r.decide(
        header=b,
        evidence=evidence(tmp_path, b),
        state=stale,
        direction=SwapDirection.ZERO_FOR_ONE,
        amount_specified=100_000,
        exact_input=True,
        now=100,
    )
    assert decision.decision == "halt"
    assert decision.health == "HOLD"
    assert decision.reasons == ("strategy_evaluation_failed",)
    payload = json.dumps(r.journal.decision_records()[-1])
    assert "oracle state is stale" not in payload
    assert "strategy_evaluation_failed" in payload
    r.journal.close()


def test_decisions_chain_and_detect_tampering(tmp_path):
    b1, b2 = blocks()
    path = tmp_path / "shadow.db"
    journal = ShadowJournal(path, chain_id=1, max_age_seconds=10)
    r = LpShadowRunner(journal, band_strategy())
    for b in (b1, b2):
        r.decide(
            header=b,
            evidence=evidence(tmp_path, b),
            state=pool_state(observed_at_timestamp=b.ref.timestamp, block=b.ref),
            direction=SwapDirection.ZERO_FOR_ONE,
            amount_specified=100_000,
            exact_input=True,
            now=100 if b.ref.number == 10 else 101,
        )
    records = journal.decision_records()
    assert len(records) == 2
    assert records[0]["previous"] == "0" * 64
    assert records[1]["previous"] == records[0]["digest"]
    health_digests = [
        row[0] for row in journal.db.execute("SELECT digest FROM observations ORDER BY seq")
    ]
    assert [record["health_digest"] for record in records] == health_digests
    # Every decision references the health observation that preceded it.
    observations = journal._last()
    assert observations["digest"] == records[1]["health_digest"]
    journal.db.execute("UPDATE decisions SET digest='broken' WHERE seq=1")
    journal.db.commit()
    with pytest.raises(ValueError, match="corrupted"):
        journal.decision_records()
    with pytest.raises(ValueError, match="corrupted"):
        journal.record_decision({"schema": "oasis.shadow-lp-decision.v1", "decision": "halt"})
    journal.close()


def test_unpinned_evidence_rejected(tmp_path):
    b, _ = blocks()
    r = runner(tmp_path)
    ev = replace(evidence(tmp_path, b), raw_response_hash="")
    with pytest.raises(ValueError, match="raw block-pinned evidence"):
        r.decide(
            header=b,
            evidence=ev,
            state=pool_state(),
            direction=SwapDirection.ZERO_FOR_ONE,
            amount_specified=100_000,
            exact_input=True,
            now=100,
        )
    r.journal.close()


def test_record_decision_validates_schema_and_chain_position(tmp_path):
    path = tmp_path / "shadow.db"
    journal = ShadowJournal(path, chain_id=1, max_age_seconds=10)
    with pytest.raises(ValueError, match="schema"):
        journal.record_decision({"decision": "halt"})
    with pytest.raises(ValueError, match="journal-owned"):
        journal.record_decision(
            {"schema": "oasis.shadow-lp-decision.v1", "seq": 9, "previous": "0" * 64}
        )
    with pytest.raises(ValueError, match="observation-only"):
        journal.record_decision({"schema": "oasis.shadow-lp-decision.v1", "decision": "halt"})
    assert journal.decision_records() == ()
    journal.close()


def test_missing_gate_cannot_select_recenter(tmp_path):
    b, _ = blocks()
    r = runner(tmp_path)
    result = r.decide(
        header=b,
        evidence=evidence(tmp_path, b),
        state=pool_state(observed_at_timestamp=100, oracle_timestamp=95),
        direction=SwapDirection.ZERO_FOR_ONE,
        amount_specified=100,
        exact_input=True,
        now=100,
        candidates=recenter_candidates(),
    )
    assert result.decision == "hold"
    assert result.rebalance_allowed is False
    assert result.rebalance_reasons == ("rebalance_gate_missing",)
    r.journal.close()


@pytest.mark.parametrize("timestamp", [99, 101, 1000])
def test_state_time_must_match_header(tmp_path, timestamp):
    b, _ = blocks()
    r = runner(tmp_path)
    result = r.decide(
        header=b,
        evidence=evidence(tmp_path, b),
        state=pool_state(observed_at_timestamp=timestamp, oracle_timestamp=timestamp),
        direction=SwapDirection.ZERO_FOR_ONE,
        amount_specified=100,
        exact_input=True,
        now=100,
    )
    assert result.decision == "halt"
    assert result.reasons == ("state_snapshot_mismatch",)
    r.journal.close()


def test_expired_quote_cannot_be_a_healthy_decision(tmp_path):
    b, _ = blocks()
    r = runner(tmp_path)
    result = r.decide(
        header=b,
        evidence=evidence(tmp_path, b),
        state=pool_state(observed_at_timestamp=100, oracle_timestamp=95),
        direction=SwapDirection.ZERO_FOR_ONE,
        amount_specified=100,
        exact_input=True,
        now=110,
    )
    assert result.decision == "halt"
    assert result.reasons == ("fee_quote_expired",)
    r.journal.close()


def test_decision_halt_survives_restart(tmp_path):
    b1, b2 = blocks()
    r = runner(tmp_path)
    result = r.decide(
        header=b1,
        evidence=evidence(tmp_path, b1),
        state=pool_state(oracle_timestamp=0),
        direction=SwapDirection.ZERO_FOR_ONE,
        amount_specified=100,
        exact_input=True,
        now=100,
    )
    assert result.decision == "halt"
    r.journal.close()
    r = runner(tmp_path)
    result = r.decide(
        header=b2,
        evidence=evidence(tmp_path, b2),
        state=pool_state(observed_at_timestamp=101, block=b2.ref),
        direction=SwapDirection.ZERO_FOR_ONE,
        amount_specified=100,
        exact_input=True,
        now=101,
    )
    assert result.decision == "halt"
    assert "decision_halt_latched" in result.reasons
    r.journal.close()


def test_decimal_serialization_preserves_precision_and_signed_zero():
    from decimal import localcontext

    from oasis_agent.shadow.lp_runner import _decimal_str

    value = Decimal("123456789012345678901234567890.123456789000")
    with localcontext() as context:
        context.prec = 6
        assert _decimal_str(value) == "123456789012345678901234567890.123456789"
        assert _decimal_str(Decimal("-0.000")) == "0"


def test_journal_rejects_forged_health_link_or_execution_eligibility(tmp_path):
    b, _ = blocks()
    r = runner(tmp_path)
    r.decide(
        header=b,
        evidence=evidence(tmp_path, b),
        state=pool_state(),
        direction=SwapDirection.ZERO_FOR_ONE,
        amount_specified=100,
        exact_input=True,
        now=100,
    )
    payload = {
        key: value
        for key, value in r.journal.decision_records()[0].items()
        if key not in ("seq", "previous", "digest")
    }
    for changes in (
        {"health_digest": "f" * 64},
        {"execution_eligible": True},
        {"block": None},
        {"strategy_qualification": "qualified"},
    ):
        with pytest.raises(ValueError):
            r.journal.record_decision(payload | changes)
    assert len(r.journal.decision_records()) == 1
    r.journal.close()


def test_unpinned_state_cannot_use_header_evidence_as_its_own_provenance(tmp_path):
    b, _ = blocks()
    r = runner(tmp_path)
    result = r.decide(
        header=b,
        evidence=evidence(tmp_path, b),
        state=pool_state(block=None),
        direction=SwapDirection.ZERO_FOR_ONE,
        amount_specified=100,
        exact_input=True,
        now=100,
    )
    assert result.decision == "halt"
    assert result.reasons == ("state_snapshot_mismatch",)
    r.journal.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"chain_id": 2},
        {"number": 11},
        {"block_hash": "0x" + "33" * 32},
    ],
)
def test_state_block_identity_cannot_be_substituted(tmp_path, changes):
    b, _ = blocks()
    r = runner(tmp_path)
    result = r.decide(
        header=b,
        evidence=evidence(tmp_path, b),
        state=pool_state(block=replace(b.ref, **changes)),
        direction=SwapDirection.ZERO_FOR_ONE,
        amount_specified=100,
        exact_input=True,
        now=100,
    )
    assert result.decision == "halt"
    assert result.reasons == ("state_snapshot_mismatch",)
    r.journal.close()
