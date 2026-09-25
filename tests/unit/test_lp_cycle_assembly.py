"""The declared LP cycle producer: mechanism bound to evidence, economics declared.

``run_lp_shadow.py`` consumes ``LpCycle`` records and ``prepare_lp_simulation.py``
turns a recenter/remove decision into simulated calldata, so an ``LpCycle`` producer
is on the live path. This module tests that producer without asserting any trading
hypothesis: every candidate term is an operator declaration, while the header, the
market window, inventory and the reference price are re-read from retained
two-provider evidence, and volatility/toxicity/marginal value are derived.
"""

import json
import sqlite3
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.lp_cycle_assembly import (
    CYCLE_DECLARATION_SCHEMA,
    CYCLE_INPUT_KIND,
    DECLARATION_KIND,
    DECLARED_GATE_NAMES,
    DERIVED_GATE_NAMES,
    HEADER_KIND,
    CandidateDeclaration,
    LpCycleDeclaration,
    build_lp_cycle,
    parse_lp_cycle_declaration,
)
from oasis_agent.execution.lp_state_machine import TickRange
from oasis_agent.execution.range_controller import LpActionType
from oasis_agent.models.common import BlockRef, canonical_json, sha256_hex
from oasis_agent.models.position_scope import LpPositionScope
from oasis_agent.models.v4 import SwapDirection
from oasis_agent.shadow.features import AGREED_INTEGER_SCHEMA, AgreedInteger
from oasis_agent.shadow.journal import ShadowJournal
from oasis_agent.shadow.service import LpShadowService, serializable
from tests.unit.test_live_shadow_producer import CHAIN, MANAGER, blocks
from tests.unit.test_lp_features import Q96, agreed
from tests.unit.test_market_window import POOL_ID, build, inputs
from tests.unit.test_shadow_lp_runner import band_strategy

RETRIEVED_AT = "2026-09-13T00:00:00+00:00"
HEADERS = blocks(100, 3)
PREVIOUS_BLOCK = HEADERS[1].ref
DECISION_BLOCK = HEADERS[2].ref
NOW = 1103
#: Derived from the window fixture: two adjacent relative price moves of 0.75 and 3
#: average 1.875; one toxic markout of 2.6 and one benign 0 average 1.3; inventory
#: skew is (3000 - 1000) / 4000 = 0.5 with the reference price equal to the final
#: pool price, so oracle deviation is zero.
DERIVED_VOLATILITY = Decimal("1.875")
DERIVED_TOXICITY = Decimal("1.3")
#: 1000 - 100 - 50 - 100 - 25 declared terms.
DECLARED_MARGINAL_VALUE = Decimal(725)


def candidate(**overrides: object) -> CandidateDeclaration:
    values: dict[str, object] = {
        "action_type": LpActionType.RECENTER,
        "target_range": TickRange(-60, 60, 60),
        "expected_incremental_fees": Decimal(1000),
        "expected_incremental_lvr": Decimal(100),
        "inventory_risk_charge": Decimal(50),
        "swap_rebalance_gas_mev_cost": Decimal(100),
        "failure_and_model_uncertainty_buffer": Decimal(25),
    }
    values.update(overrides)
    return CandidateDeclaration(**values)  # type: ignore[arg-type]


def declaration(**overrides: object) -> LpCycleDeclaration:
    values: dict[str, object] = {
        "chain_id": CHAIN,
        "pool_id": POOL_ID,
        "block_number": DECISION_BLOCK.number,
        "block_hash": DECISION_BLOCK.block_hash,
        "direction": SwapDirection.ZERO_FOR_ONE,
        "amount_specified": 1000,
        "exact_input": True,
        "benign_flow": False,
        "min_benefit": Decimal(600),
        "hysteresis_threshold": Decimal(100),
        "max_volatility": Decimal(2),
        "max_toxicity": Decimal(2),
        "candidates": (candidate(),),
        **dict.fromkeys(DECLARED_GATE_NAMES, True),
    }
    values.update(overrides)
    return LpCycleDeclaration(**values)  # type: ignore[arg-type]


def retain_agreed(
    store: EvidenceStore,
    name: str,
    value: int,
    block: BlockRef = DECISION_BLOCK,
    sources: tuple[str, ...] = ("a", "b"),
) -> AgreedInteger:
    """Retain one two-provider agreed integer whose payload binds its own value."""
    payload = canonical_json(
        {
            "schema": AGREED_INTEGER_SCHEMA,
            "name": name,
            "value": value,
            "block": asdict(block),
        }
    )
    interpretation = {"schema": "oasis.fixture-account-integer.v1", "name": name, "value": value}
    references = tuple(
        store.put(
            "derived_account_integer",
            payload,
            block,
            source,
            request={"method": "fixture", "name": name},
            raw_response={"value": value},
            interpretation=interpretation,
            retrieved_at=RETRIEVED_AT,
        )
        for source in sources
    )
    return AgreedInteger(name, value, block, references)


def account_inputs(store: EvidenceStore, **overrides: object) -> dict[str, AgreedInteger]:
    values: dict[str, AgreedInteger] = {
        "inventory0": retain_agreed(store, "inventory0", 3000),
        "inventory1": retain_agreed(store, "inventory1", 1000),
        "reference_price_x192": retain_agreed(store, "reference_price_x192", Q96**2),
    }
    values.update(overrides)  # type: ignore[arg-type]
    return values


def market(tmp_path: Path) -> tuple[EvidenceStore, tuple[object, object], object]:
    store = EvidenceStore(tmp_path / "evidence.db")
    # ``final_swap=False`` leaves no pending markout, so the window can compose.
    refs = inputs(store, final_swap=False)
    return store, refs, build(store, refs)


def assemble(
    store: EvidenceStore,
    refs: tuple[object, object],
    window: object,
    cycle_declaration: LpCycleDeclaration | None = None,
    *,
    position_scope: LpPositionScope | None = None,
    **account_overrides: object,
):
    return build_lp_cycle(
        store,
        declaration() if cycle_declaration is None else cycle_declaration,
        window=window,
        slot0_references=refs[1],
        position_scope=position_scope,
        retrieved_at=RETRIEVED_AT,
        **account_inputs(store, **account_overrides),
    )


def consumer(
    tmp_path: Path, store: EvidenceStore, *, min_benefit: Decimal = Decimal(600)
) -> tuple[ShadowJournal, LpShadowService]:
    journal = ShadowJournal(tmp_path / "shadow.db", chain_id=CHAIN, max_age_seconds=3600)
    service = LpShadowService(
        store, journal, band_strategy(), min_benefit=min_benefit, canary_limits=None
    )
    return journal, service


def evidence_rows(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0])
    finally:
        connection.close()


def as_object(value: LpCycleDeclaration) -> dict[str, object]:
    return json.loads(value.payload)


def test_assembled_cycle_is_consumed_into_a_recenter_decision(tmp_path):
    store, refs, window = market(tmp_path)
    try:
        assembled = assemble(store, refs, window)
        assert assembled.action.action_type is LpActionType.RECENTER
        assert assembled.action.lower_confidence_bound == DECLARED_MARGINAL_VALUE
        journal, service = consumer(tmp_path, store)
        try:
            first = service.consume_once(assembled.cycle, now=NOW)
            assert first.record["decision"] == "recenter"
            assert first.record["rebalance_allowed"] is True
            assert first.record["rebalance_reasons"] == []
            assert first.record["execution_eligible"] is False
            assert first.duplicate is False and first.halted is False
            replay = service.consume_once(assembled.cycle, now=NOW)
            assert replay.duplicate is True
            assert len(journal.decision_records()) == 1
        finally:
            journal.close()
    finally:
        store.close()


def test_declaration_round_trips_and_keeps_metadata_out_of_its_payload(tmp_path):
    store, refs, window = market(tmp_path)
    declared = declaration()
    try:
        assembled = assemble(store, refs, window, declared)
        record = store.get_record(assembled.declaration_evidence)
        assert assembled.declaration_evidence.kind == DECLARATION_KIND
        assert assembled.declaration_evidence.content_hash == declared.digest
        assert record.payload == declared.payload
        assert parse_lp_cycle_declaration(record.payload) == declared
        body = json.loads(record.payload)
        assert body["schema"] == CYCLE_DECLARATION_SCHEMA
        assert "execution_eligible" not in body
        assert "declared_economics" not in body
        interpretation = json.loads(record.interpretation_payload)
        assert interpretation["execution_eligible"] is False
        assert interpretation["declared_economics"] == "operator_supplied_not_estimated"
        assert interpretation["declared_gate_conditions"] == list(DECLARED_GATE_NAMES)
    finally:
        store.close()


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ("float", TypeError),
        ("unknown_key", ValueError),
        ("missing_schema", ValueError),
        ("wrong_schema", ValueError),
        ("bool_amount", ValueError),
        ("numeric_decimal", ValueError),
        ("hold_candidate", ValueError),
        ("negative_term", ValueError),
        ("range_extra_field", ValueError),
        ("candidate_missing_term", ValueError),
        ("integer_gate", ValueError),
    ],
)
def test_malformed_declarations_fail_closed(change, error):
    data = as_object(declaration())
    if change == "float":
        data["min_benefit"] = 0.5
    elif change == "unknown_key":
        data["not_declared"] = True
    elif change == "missing_schema":
        del data["schema"]
    elif change == "wrong_schema":
        data["schema"] = "oasis.lp-cycle-declaration.v2"
    elif change == "bool_amount":
        data["amount_specified"] = True
    elif change == "numeric_decimal":
        data["min_benefit"] = 600
    elif change == "hold_candidate":
        data["candidates"][0]["action_type"] = "hold"
    elif change == "negative_term":
        data["candidates"][0]["expected_incremental_fees"] = "-1"
    elif change == "range_extra_field":
        data["candidates"][0]["target_range"]["not_a_field"] = 1
    elif change == "candidate_missing_term":
        del data["candidates"][0]["inventory_risk_charge"]
    else:
        data["dwell_met"] = 1
    with pytest.raises(error):
        parse_lp_cycle_declaration(json.dumps(data).encode())


def test_duplicate_keys_and_non_objects_are_refused():
    with pytest.raises(ValueError, match="duplicate JSON key"):
        parse_lp_cycle_declaration(b'{"min_benefit":"1","min_benefit":"2"}')
    with pytest.raises(ValueError, match="must be a JSON object"):
        parse_lp_cycle_declaration(b"[]")


def test_declared_gate_cannot_override_derived_economics(tmp_path):
    store, refs, window = market(tmp_path)
    try:
        tightened = declare_tightened()
        assembled = assemble(store, refs, window, tightened)
        gate = assembled.cycle.gate_input
        assert gate is not None
        assert gate.volatility == DERIVED_VOLATILITY
        assert gate.toxicity == DERIVED_TOXICITY
        assert gate.marginal_value == DECLARED_MARGINAL_VALUE
        assert assembled.action.action_type is LpActionType.RECENTER
        journal, service = consumer(tmp_path, store)
        try:
            consumption = service.consume_once(assembled.cycle, now=NOW)
            assert consumption.record["decision"] == "hold"
            assert consumption.record["rebalance_allowed"] is False
            assert any("volatility" in reason for reason in consumption.record["rebalance_reasons"])
            # The proposal stays in the audit record even when the gate denies it.
            assert consumption.record["range_action"]["action_type"] == "recenter"
        finally:
            journal.close()
    finally:
        store.close()


def declare_tightened() -> LpCycleDeclaration:
    return declaration(max_volatility=Decimal(1), max_toxicity=Decimal(2))


def test_min_benefit_above_the_lower_confidence_bound_holds(tmp_path):
    store, refs, window = market(tmp_path)
    try:
        assembled = assemble(store, refs, window, declaration(min_benefit=Decimal(800)))
        assert assembled.action.action_type is LpActionType.HOLD
        assert assembled.action.target_range is None
        journal, service = consumer(tmp_path, store, min_benefit=Decimal(800))
        try:
            consumption = service.consume_once(assembled.cycle, now=NOW)
            assert consumption.record["decision"] == "hold"
            assert consumption.record["range_action"]["action_type"] == "hold"
        finally:
            journal.close()
    finally:
        store.close()


def test_a_matching_position_scope_is_carried_into_the_cycle(tmp_path):
    store, refs, window = market(tmp_path)
    scope = LpPositionScope(CHAIN, MANAGER, 7, POOL_ID, "0x" + "9" * 40)
    try:
        assembled = assemble(store, refs, window, position_scope=scope)
        assert assembled.cycle.position_scope == scope
        journal, service = consumer(tmp_path, store)
        try:
            assert service.consume_once(assembled.cycle, now=NOW).record["decision"] == "recenter"
        finally:
            journal.close()
    finally:
        store.close()


def test_window_must_be_built_from_the_given_slot0_captures(tmp_path):
    store, refs, window = market(tmp_path)
    try:
        with pytest.raises(ValueError, match="was not built from these slot0 captures"):
            assemble(store, refs, replace(window, input_references=()))
        with pytest.raises(ValueError, match="two distinct slot0 captures are required"):
            build_lp_cycle(
                store,
                declaration(),
                window=window,
                slot0_references=(refs[1][0], refs[1][0]),
                retrieved_at=RETRIEVED_AT,
                **account_inputs(store),
            )
        with pytest.raises(ValueError, match="do not end at the window decision block"):
            assemble(store, refs, replace(window, decision_block=PREVIOUS_BLOCK))
    finally:
        store.close()


def test_declaration_identity_must_match_the_verified_window(tmp_path):
    store, refs, window = market(tmp_path)
    try:
        with pytest.raises(ValueError, match="chain, market window and header must agree"):
            assemble(store, refs, window, declaration(chain_id=1))
        with pytest.raises(ValueError, match="declared pool must be the market window's pool"):
            assemble(store, refs, window, declaration(pool_id="0x" + "6" * 64))
        with pytest.raises(ValueError, match="declared block must be the pinned decision block"):
            assemble(
                store,
                refs,
                window,
                declaration(
                    block_number=PREVIOUS_BLOCK.number, block_hash=PREVIOUS_BLOCK.block_hash
                ),
            )
        with pytest.raises(ValueError, match="position scope disagrees"):
            assemble(
                store,
                refs,
                window,
                position_scope=LpPositionScope(CHAIN, MANAGER, 7, "0x" + "7" * 64, "0x" + "9" * 40),
            )
    finally:
        store.close()


def test_account_inputs_must_be_retained_agreed_values(tmp_path):
    store, refs, window = market(tmp_path)
    try:
        with pytest.raises(TypeError, match="two-provider agreed integer"):
            assemble(store, refs, window, inventory0=3000)
        with pytest.raises(ValueError, match="not retained"):
            assemble(store, refs, window, inventory0=agreed("inventory0", 3000, 102))
        with pytest.raises(ValueError, match="named for its role"):
            assemble(store, refs, window, inventory0=retain_agreed(store, "inventory1", 1000))
        with pytest.raises(ValueError, match="agreed at the pinned decision block"):
            assemble(
                store,
                refs,
                window,
                inventory0=retain_agreed(store, "inventory0", 3000, PREVIOUS_BLOCK),
            )
        with pytest.raises(ValueError, match="window's own two providers"):
            assemble(
                store,
                refs,
                window,
                inventory0=retain_agreed(store, "inventory0", 3000, sources=("c", "d")),
            )
    finally:
        store.close()


def test_a_refused_assembly_retains_nothing_new(tmp_path):
    store, refs, window = market(tmp_path)
    path = tmp_path / "evidence.db"
    try:
        retained = account_inputs(store)
        before = evidence_rows(path)
        with pytest.raises(ValueError, match="was not built from these slot0 captures"):
            build_lp_cycle(
                store,
                declaration(),
                window=replace(window, input_references=()),
                slot0_references=refs[1],
                retrieved_at=RETRIEVED_AT,
                **retained,
            )
        assert evidence_rows(path) == before
        assemble(store, refs, window)
        assert evidence_rows(path) == before + 3  # declaration, header, cycle input
    finally:
        store.close()


def test_retained_records_bind_the_bytes_that_pinned_the_decision(tmp_path):
    store, refs, window = market(tmp_path)
    declared = declaration()
    try:
        assembled = assemble(store, refs, window, declared)
        cycle = assembled.cycle
        ordered = sorted(refs[1], key=lambda reference: reference.source)
        slot0 = store.get_record(ordered[0])
        header = store.get_record(cycle.header_evidence)
        assert cycle.header_evidence.kind == HEADER_KIND
        assert header.payload == canonical_json(asdict(cycle.header))
        assert header.request_payload == slot0.request_payload
        assert header.raw_response_payload == slot0.raw_response_payload
        header_body = json.loads(header.interpretation_payload)
        assert header_body["execution_eligible"] is False
        assert header_body["bytes_kind"] == "slot0_window_envelope_that_pinned_this_header"
        assert [parent["content_hash"] for parent in header_body["parents"]] == [
            reference.content_hash for reference in ordered
        ]
        inputs_record = store.get_record(cycle.input_evidence)
        assert cycle.input_evidence.kind == CYCLE_INPUT_KIND
        assert inputs_record.payload == cycle.input_payload
        body = json.loads(inputs_record.interpretation_payload)
        assert body["candidate_action"] == "recenter"
        assert body["declaration_digest"] == declared.digest
        assert body["derived_economics"] == list(DERIVED_GATE_NAMES)
        assert body["declared_gate_conditions"] == list(DECLARED_GATE_NAMES)
        assert body["state_digest"] == sha256_hex(canonical_json(serializable(asdict(cycle.state))))
        assert [parent["content_hash"] for parent in body["parents"]] == [
            reference.content_hash for reference in ordered
        ]
    finally:
        store.close()


def test_invalid_declarations_are_refused_at_construction():
    with pytest.raises(ValueError, match="cannot be declared"):
        candidate(action_type=LpActionType.HOLD)
    with pytest.raises(TypeError, match="tick range"):
        candidate(target_range=None)
    with pytest.raises(ValueError, match="finite nonnegative Decimal"):
        candidate(expected_incremental_fees=Decimal(-1))
    with pytest.raises(ValueError, match="at least one non-hold candidate"):
        declaration(candidates=())
    with pytest.raises(ValueError, match="one candidate per action type"):
        declaration(candidates=(candidate(), candidate()))
    with pytest.raises(ValueError, match="nonzero int256"):
        declaration(amount_specified=0)
    with pytest.raises(TypeError, match="must be a bool"):
        declaration(dwell_met=1)
    with pytest.raises(ValueError, match="finite Decimal"):
        declaration(min_benefit=Decimal("NaN"))
    with pytest.raises(ValueError, match="must be nonnegative"):
        declaration(max_volatility=Decimal(-1))
    with pytest.raises(ValueError, match="canonical pool id"):
        declaration(pool_id="0x12")
    with pytest.raises(ValueError, match="positive chain id"):
        declaration(chain_id=0)
    with pytest.raises(TypeError, match="typed swap direction"):
        declaration(direction="zero_for_one")
