"""Original approval/cash/gas plus fresh canonical receipt replay, all synthetic."""

import json
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live import rearm
from oasis_agent.live.feed_transition import apply_service
from oasis_agent.live.rearm_history import replay_history
from oasis_agent.live.rearm_settlements import persist_settlement_head, require_current_settlements
from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.live.startup_state import paths
from oasis_agent.live.state import StateStore, day_of
from oasis_agent.live.valuation import capture_nav
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import BlockRef, canonical_json, sha256_hex
from oasis_agent.rpc.budget import RpcBudget
from tests.unit import test_live_approval_cash as approval
from tests.unit.test_live_rearm import rows, wire_capture
from tests.unit.test_live_receipt_costs import position_config
from tests.unit.test_live_valuation import NavFixture, synthetic_feed_pin  # noqa: F401
from tests.unit.test_lp_receipts import PARENT_HASH, PRIMARY, SECONDARY, sessions

SOURCES = (PRIMARY, SECONDARY)


def setup(tmp_path, evidence, monkeypatch):
    target = approval.TEST_CONFIG
    old = replace(target, usdc_oracle_max_age_seconds=90_000)
    monkeypatch.setattr(approval, "TEST_CONFIG", old)
    fixture = approval.ApprovalFixture()
    loop = approval.approval_loop(tmp_path, evidence, fixture, replace(fixture))
    assert "confirmed" in loop.tick()
    state = loop.store.load("live", read_only=True)
    journal_rows = [(row[1], row[4]) for row in rows(loop.store)]
    # Later epoch carries the event, then the bounded projection drops it. The
    # original event's config and proof must remain discoverable in the journal.
    journal_rows.append((sha256_hex(canonical_json(asdict(target))), canonical_json(asdict(state))))
    state.history = []
    journal_rows.append((sha256_hex(canonical_json(asdict(target))), canonical_json(asdict(state))))
    return target, fixture, state, journal_rows


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "gas",
        "missing_event",
        "duplicate_event",
        "missing_cash",
        "foreign_epoch",
        "wrong_anchor",
    ],
)
def test_whole_cost_history_survives_epoch_and_bounded_history(tmp_path, monkeypatch, fault):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as evidence:
        target, fixture, state, journal_rows = setup(tmp_path, evidence, monkeypatch)
        if fault == "gas":
            state.gas_spent_wei += 1
        elif fault == "missing_event":
            journal_rows = journal_rows[-1:]
        elif fault in {"duplicate_event", "missing_cash"}:
            row = json.loads(journal_rows[-2][1])
            if fault == "duplicate_event":
                row["history"][-1]["native_gas_units"] += 1
            else:
                row["history"][-1]["cash_evidence"] = []
                journal_rows = []
            journal_rows.append((sha256_hex(canonical_json(asdict(target))), canonical_json(row)))
        elif fault == "foreign_epoch":
            journal_rows[0] = ("f" * 64, journal_rows[0][1])
        elif fault == "wrong_anchor":
            state.settled_blocks[0]["hash"] = "0x" + "aa" * 32
        before = deepcopy(state)
        if fault:
            with pytest.raises(ValueError):
                replay_history(journal_rows, state, target, evidence, SOURCES)
        else:
            result = replay_history(journal_rows, state, target, evidence, SOURCES)
            assert result == (fixture._receipt(),)
            assert state.gas_spent_wei == 218
        assert state == before


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "status",
        "from",
        "l1Fee",
        "effectiveGasPrice",
        "logs",
        "missing",
        "old_head",
        "swapped_sources",
        "anchor_reorg",
    ],
)
def test_fresh_current_receipts_must_equal_original_reconciled_cash_and_gas(
    tmp_path, monkeypatch, fault
):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as evidence:
        target, fixture, state, journal_rows = setup(tmp_path, evidence, monkeypatch)
        historical = replay_history(journal_rows, state, target, evidence, SOURCES)
        head = BlockRef(
            target.chain_id, fixture.block_number + 20, "0x" + "44" * 32, fixture.timestamp + 40
        )
        references = []
        for source in SOURCES:
            receipts = deepcopy(historical)
            if source == SECONDARY and fault in {
                "status",
                "from",
                "l1Fee",
                "effectiveGasPrice",
                "logs",
            }:
                receipts[0][fault] = {
                    "status": "0x0",
                    "from": "0x" + "ab" * 20,
                    "l1Fee": "0x999",
                    "effectiveGasPrice": "0x777",
                    "logs": [{"removed": True}],
                }[fault]
            selected = replace(head, number=head.number - 1) if fault == "old_head" else head
            capture = wire_capture(selected, tuple(state.settled_blocks), receipts, source)
            references.append(persist_settlement_head(evidence, capture))
        if fault == "missing":
            references = references[:1]
        elif fault == "swapped_sources":
            references.reverse()
        elif fault == "anchor_reorg":
            state.settled_blocks[0]["hash"] = "0x" + "66" * 32
        kwargs = {
            "store": evidence,
            "refs": tuple(references),
            "sources": SOURCES,
            "nav_header": BlockSnapshot(head, PARENT_HASH),
            "anchors": tuple(state.settled_blocks),
            "historical": historical,
        }
        if fault:
            with pytest.raises(ValueError):
                require_current_settlements(**kwargs)
        else:
            require_current_settlements(**kwargs)


def test_actual_journal_epoch_transition_then_paired_rearm_replays_original_receipt(
    tmp_path, monkeypatch
):

    target = replace(
        approval.TEST_CONFIG,
        expected_pins={
            **position_config().expected_pins,
            "permit2": approval.TEST_CONFIG.expected_pins["permit2"],
        },
    )
    old = replace(target, usdc_oracle_max_age_seconds=90_000)
    monkeypatch.setattr(approval, "TEST_CONFIG", old)
    monkeypatch.setattr(rearm, "SOURCES", SOURCES)
    profile = LiveRpcProfile(max_requests=4, min_interval_ms=100)
    references, stores = {}, {}
    for role in rearm.ROLES:
        directory = tmp_path / role
        directory.mkdir(mode=0o700)
        state_path, evidence_path, lock_path = paths(directory, role)
        lock_path.touch()
        (directory / "STOP").touch()
        with closing(EvidenceStore(evidence_path)) as evidence:
            fixture = approval.ApprovalFixture()
            loop = approval.approval_loop(directory, evidence, fixture, replace(fixture))
            assert "confirmed" in loop.tick()
            state = loop.state
            state.halted = rearm.HALT
            nav_fixture = NavFixture(
                position_enabled=False,
                timestamp=fixture.timestamp + 10,
                block_number=105,
                block_hash="0x" + "88" * 32,
            )
            old_head = BlockRef(
                target.chain_id,
                nav_fixture.block_number,
                nav_fixture.block_hash,
                nav_fixture.timestamp,
            )
            value, old_refs = capture_nav(
                evidence,
                old,
                old_head,
                None,
                sources=SOURCES,
                sessions=sessions({PRIMARY: nav_fixture, SECONDARY: replace(nav_fixture)}),
            )
            state.nav_evidence = old_refs
            state.integer_risk().observe(
                day=day_of(old_head.timestamp), at=old_head.timestamp, nav=value.nav_micros
            )
            store = loop.store if role == "strategy" else StateStore(state_path)
            if role == "authority":
                store.bind(sha256_hex(canonical_json(asdict(old))))
            store.save(state)
            fresh_fixture = replace(
                nav_fixture,
                timestamp=nav_fixture.timestamp + 10,
                block_number=110,
                block_hash="0x" + "99" * 32,
            )
            head = BlockRef(
                target.chain_id,
                fresh_fixture.block_number,
                fresh_fixture.block_hash,
                fresh_fixture.timestamp,
            )
            _, fresh = capture_nav(
                evidence,
                target,
                head,
                None,
                sources=SOURCES,
                sessions=sessions({PRIMARY: fresh_fixture, SECONDARY: replace(fresh_fixture)}),
            )
            monkeypatch.setattr(
                "oasis_agent.live.feed_transition.time.time", lambda at=head.timestamp: at
            )
            apply_service(
                store,
                target,
                evidence,
                sources=SOURCES,
                fresh=fresh,
                expected_tip=store.journal.revision,
                stop_file=directory / "STOP",
                role=role,
            )
            settlement_refs = tuple(
                persist_settlement_head(
                    evidence,
                    wire_capture(head, tuple(state.settled_blocks), (fixture._receipt(),), source),
                )
                for source in SOURCES
            )
            references[role] = {"nav": fresh, "settlements": settlement_refs}
        with RpcBudget(directory / "live.receipt-budget.db", profile.policies(SOURCES)) as budget:
            for source in SOURCES:
                budget.reserve(source, now_ms=1000)
        stores[role] = store
    monkeypatch.setattr(rearm, "_clock", lambda: head.timestamp)
    report = rearm.review_pair(
        tmp_path / "strategy", tmp_path / "authority", target, profile, references
    )
    result = rearm.apply_pair(
        tmp_path / "strategy",
        tmp_path / "authority",
        target,
        profile,
        references,
        approved_review_hash=report["review_hash"],
    )
    assert result["rearm_applied"]
    for store in stores.values():
        state = store.load("live", read_only=True)
        assert (
            state.halted is None and state.gas_spent_wei == 218 and len(state.settled_blocks) == 1
        )


def test_fresh_nonce_without_journaled_settlement_is_not_reconciled(tmp_path, monkeypatch):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as evidence:
        target, _, state, journal_rows = setup(tmp_path, evidence, monkeypatch)
        with pytest.raises(ValueError, match="nonce advanced"):
            replay_history(journal_rows, state, target, evidence, SOURCES, current_nonce=5)
