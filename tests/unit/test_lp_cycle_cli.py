"""The operator CLI that turns retained evidence into an LP shadow cycle.

`run_lp_shadow.py` consumes `LpCycle` records, so this producer is on the live
path. These tests assert the whole chain from retained evidence to a shadow
decision: the window is rebuilt from the retained captures, the declared
position's own inventory is derived from its position proofs, and the reference
price is derived from both pool currencies' retained feed observations rather
than supplied beside the request. No case ends with an execution permit, and a
refused input appends nothing.
"""

from dataclasses import asdict
from pathlib import Path

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.lp_reference_price import CURRENCY_FEED_KIND, ReferenceRequest
from oasis_agent.models.common import EvidenceRef, canonical_json
from oasis_agent.shadow.service import LpCycle
from scripts.assemble_lp_cycle import assemble
from tests.unit.test_live_shadow_producer import MANAGER
from tests.unit.test_lp_cycle_assembly import (
    DECISION_BLOCK,
    NOW,
    RETRIEVED_AT,
    consumer,
    declaration,
    market,
)
from tests.unit.test_lp_position_inventory import TOKEN_ID, retained, scope
from tests.unit.test_lp_reference_price import declarations
from tests.unit.test_lp_reference_price import retained as reference_providers
from tests.unit.test_market_window import POOL


def reference_request(
    store: EvidenceStore, *, sources: tuple[str, str] = ("a", "b")
) -> ReferenceRequest:
    """The declared reference, with both currencies' feeds retained per provider."""
    first, second = declarations()
    return ReferenceRequest(
        pool=POOL,
        currency0=first,
        currency1=second,
        providers=reference_providers(store, sources=sources),
        max_reference_deviation="0.05",
    )


def write_request(
    tmp_path: Path,
    refs: tuple[object, object],
    position_refs: tuple[object, ...],
    reference: ReferenceRequest,
    *,
    declared_scope: object | None = None,
) -> Path:
    """The operator request: retained window captures, the position, the reference."""
    path = tmp_path / "request.json"
    path.write_bytes(
        canonical_json(
            {
                "window": {
                    "swap_references": [asdict(item) for item in refs[0]],  # type: ignore[union-attr]
                    "price_references": [asdict(item) for item in refs[1]],  # type: ignore[union-attr]
                    "pool": asdict(POOL),
                    "pool_manager": MANAGER,
                    "horizon_seconds": 1,
                    "max_lateness_seconds": 1,
                },
                "position": {
                    "scope": asdict(scope() if declared_scope is None else declared_scope),  # type: ignore[arg-type]
                    "references": [asdict(item) for item in position_refs],
                },
                "reference": reference.model_dump(mode="json"),
                "retrieved_at": RETRIEVED_AT,
            }
        )
    )
    return path


def write_declaration(tmp_path: Path, declared: object | None = None) -> Path:
    path = tmp_path / "declaration.json"
    path.write_bytes(declaration().payload if declared is None else declared.payload)  # type: ignore[union-attr]
    return path


def test_the_cli_appends_a_cycle_the_shadow_consumer_accepts(tmp_path: Path) -> None:
    store, refs, _window = market(tmp_path)
    try:
        output = tmp_path / "cycles.jsonl"
        assemble(
            write_request(tmp_path, refs, retained(store), reference_request(store)),
            write_declaration(tmp_path),
            tmp_path / "evidence.db",
            output,
        )
        lines = output.read_bytes().splitlines()
        assert len(lines) == 1
        cycle = LpCycle.from_json(lines[0])
        assert cycle.position_scope == scope()
        assert cycle.state.block == DECISION_BLOCK
        journal, service = consumer(tmp_path, store)
        try:
            consumed = service.consume_once(cycle, now=NOW)
            assert consumed.record["decision"] == "recenter"
            assert consumed.record["execution_eligible"] is False
            assert consumed.duplicate is False and consumed.halted is False
        finally:
            journal.close()
    finally:
        store.close()


def test_the_reference_must_be_the_windows_own_retained_providers(tmp_path: Path) -> None:
    store, refs, _window = market(tmp_path)
    try:
        output = tmp_path / "cycles.jsonl"
        outsider = reference_request(store, sources=("c", "b"))
        with pytest.raises(ValueError, match="window's own two providers"):
            assemble(
                write_request(tmp_path, refs, retained(store), outsider),
                write_declaration(tmp_path),
                tmp_path / "evidence.db",
                output,
            )
        assert not output.exists()
        genuine = reference_request(store)
        foreign = EvidenceRef(
            kind=CURRENCY_FEED_KIND,
            content_hash="a" * 64,
            block=DECISION_BLOCK,
            source="a",
            request_hash="b" * 64,
            raw_response_hash="c" * 64,
            interpretation_hash="d" * 64,
        )
        unretained = genuine.model_copy(
            update={
                "providers": (
                    genuine.providers[0].model_copy(update={"currency0": foreign}),
                    genuine.providers[1],
                )
            }
        )
        with pytest.raises(ValueError, match="not retained"):
            assemble(
                write_request(tmp_path, refs, retained(store), unretained),
                write_declaration(tmp_path),
                tmp_path / "evidence.db",
                output,
            )
        assert not output.exists()
    finally:
        store.close()


def test_the_declared_scope_must_be_the_position_the_proofs_describe(tmp_path: Path) -> None:
    store, refs, _window = market(tmp_path)
    try:
        output = tmp_path / "cycles.jsonl"
        with pytest.raises(ValueError, match="declared scope"):
            assemble(
                write_request(
                    tmp_path,
                    refs,
                    retained(store),
                    reference_request(store),
                    declared_scope=scope(token_id=TOKEN_ID + 1),
                ),
                write_declaration(tmp_path),
                tmp_path / "evidence.db",
                output,
            )
        assert not output.exists()
    finally:
        store.close()


def test_a_declaration_for_another_block_is_refused(tmp_path: Path) -> None:
    store, refs, _window = market(tmp_path)
    try:
        output = tmp_path / "cycles.jsonl"
        with pytest.raises(ValueError, match="pinned decision block"):
            assemble(
                write_request(tmp_path, refs, retained(store), reference_request(store)),
                write_declaration(tmp_path, declaration(block_number=DECISION_BLOCK.number - 1)),
                tmp_path / "evidence.db",
                output,
            )
        assert not output.exists()
    finally:
        store.close()


def test_a_missing_evidence_database_is_refused(tmp_path: Path) -> None:
    store, refs, _window = market(tmp_path)
    try:
        request = write_request(tmp_path, refs, retained(store), reference_request(store))
    finally:
        store.close()
    with pytest.raises(ValueError, match="existing evidence database"):
        assemble(
            request,
            write_declaration(tmp_path),
            tmp_path / "absent.db",
            tmp_path / "cycles.jsonl",
        )
