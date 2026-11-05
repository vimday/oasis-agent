"""Transition review uses raw retained proofs and never rewrites a risk baseline."""

from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.feed_transition import historical_config, review_service
from oasis_agent.live.state import LoopState, Pending, Position, StateStore, day_of
from oasis_agent.live.valuation import capture_nav
from oasis_agent.models.common import BlockRef, canonical_json, sha256_hex
from scripts.review_live_feed_transition import review
from tests.unit.test_live_receipt_costs import position_config
from tests.unit.test_live_valuation import NavFixture, synthetic_feed_pin  # noqa: F401
from tests.unit.test_lp_receipts import PRIMARY, SECONDARY, sessions

SOURCES = (PRIMARY, SECONDARY)
CONFIG = position_config()
OLD = replace(CONFIG, usdc_oracle_max_age_seconds=90_000)
OLD_HASH = sha256_hex(canonical_json(asdict(OLD)))


def capture(store, config, fixture):
    return capture_nav(
        store,
        config,
        BlockRef(config.chain_id, fixture.block_number, fixture.block_hash, fixture.timestamp),
        Position(123, -100, 100, 77, 0),
        sources=SOURCES,
        sessions=sessions({PRIMARY: fixture, SECONDARY: replace(fixture)}),
    )


def setup(store):
    old = NavFixture(usdc_age=5000)
    value, refs = capture(store, OLD, old)
    state = LoopState("live", position=Position(123, -100, 100, 77, 0))
    state.integer_risk().observe(day=day_of(old.timestamp), at=old.timestamp, nav=value.nav_micros)
    state.last_units = [value.wallet.native, value.wallet.token, 77]
    state.nav_evidence = refs
    state.halted = "private retained halt reason"
    new = replace(
        old,
        usdc_age=10,
        timestamp=old.timestamp + 60,
        block_number=old.block_number + 30,
        block_hash="0x" + "44" * 32,
    )
    _, fresh = capture(store, CONFIG, new)
    return state, new, fresh


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "missing",
        "expired",
        "old_proof",
        "wallet",
        "position",
        "pending",
        "signing",
        "loss",
        "bad_history",
        "sources",
    ],
)
def test_proof_bound_review_preserves_state_and_evidence(tmp_path, fault):
    path = tmp_path / "evidence.db"
    with closing(EvidenceStore(path)) as evidence:
        state, new, fresh = setup(evidence)
        now, sources = new.timestamp, SOURCES
        if fault == "missing":
            fresh = ()
        if fault == "expired":
            now += 1000
        if fault == "old_proof":
            fresh = state.nav_evidence
        if fault == "wallet":
            state.last_units[0] += 1
        if fault == "position":
            state.position.liquidity += 1
        if fault == "pending":
            state.pending = Pending("mint", "0x" + "ab" * 32, 3, now)
        if fault == "signing":
            state.signing_attempt = {"nonce": 3}
        if fault == "loss":
            state.exact_risk.basis += 500_000_000
        if fault == "bad_history":
            state.exact_risk.last_nav += 1
        if fault == "sources":
            sources = (SECONDARY, PRIMARY)
        before, raw = deepcopy(state), path.read_bytes()
        result = review_service(
            CONFIG,
            state,
            OLD_HASH,
            evidence,
            source_payload=canonical_json(asdict(state)),
            sources=sources,
            fresh=fresh,
            now=now,
        )
        assert state == before and path.read_bytes() == raw
        assert not result["execution_eligible"] and result["halt_preserved"]
        assert "private retained" not in str(result)
        assert bool(result["blockers"]) == (fault is not None)
        if fault is None:
            assert result["historical_checkpoint_verified"] and result["fresh_nav_verified"]
            assert result["risk_preview"]["basis"] == state.exact_risk.basis
            assert result["risk_preview"]["external"] == state.exact_risk.external
            assert result["risk_preview"]["last_at"] == new.timestamp
        if fault == "missing":
            assert result["blockers"] == ["fresh_strict_nav_required"]
        if fault == "loss":
            assert result["blockers"] == ["loss_budget_exhausted"]


@pytest.mark.parametrize(
    "field,value",
    [("max_capital_usd", 499), ("max_daily_loss_usd", 199), ("oracle_max_age_seconds", 1700)],
)
def test_only_exact_old_usdc_age_is_recognized(field, value):
    with pytest.raises(ValueError):
        historical_config(replace(CONFIG, **{field: value}), OLD_HASH)


@pytest.mark.parametrize("different_head", [False, True])
def test_two_service_journal_and_readonly_evidence_review(tmp_path, different_head):
    paths, evidence_paths, fresh = {}, {}, {}
    for name in ("strategy", "authority"):
        evidence_paths[name] = tmp_path / (name + ".db")
        with closing(EvidenceStore(evidence_paths[name])) as evidence:
            state, new, refs = setup(evidence)
            if different_head and name == "authority":
                new = replace(new, block_number=new.block_number + 1, block_hash="0x" + "55" * 32)
                _, refs = capture(evidence, CONFIG, new)
        store = StateStore(tmp_path / (name + ".json"))
        store.bind(OLD_HASH)
        store.save(state)
        paths[name] = store.path
        fresh[name] = [asdict(r) for r in refs]
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    result = review(
        paths["strategy"], paths["authority"], evidence_paths, CONFIG, fresh, SOURCES, new.timestamp
    )
    assert result["evidence_review_passed"] is not different_head
    assert result["blockers"] == (["service_fresh_nav_disagreement"] if different_head else [])
    assert not result["migration_applied"]
    assert not result["execution_eligible"] and not result["state_write_supported"]
    digest = result.pop("evidence_hash")
    assert sha256_hex(canonical_json(result)) == digest
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


@pytest.mark.parametrize("elapsed_days", [0, 1])
def test_preview_preserves_funded_basis_and_conservative_loss_convention(tmp_path, elapsed_days):
    with closing(EvidenceStore(tmp_path / "proof.db")) as evidence:
        state, new, _ = setup(evidence)
        risk = state.exact_risk
        risk.basis += 10_000_000
        risk.external = 2_000_000
        risk.checkpoint += 8_000_000
        risk.daily_locked = 11_000_000
        new = replace(new, timestamp=new.timestamp + elapsed_days * 86400, eth_price=2400 * 10**8)
        value, refs = capture(evidence, CONFIG, new)
        before = deepcopy(state)
        result = review_service(
            CONFIG,
            state,
            OLD_HASH,
            evidence,
            source_payload=canonical_json(asdict(state)),
            sources=SOURCES,
            fresh=refs,
            now=new.timestamp,
        )
        assert not result["blockers"] and state == before
        preview = result["risk_preview"]
        assert preview["basis"] == risk.basis and preview["external"] == risk.external
        assert preview["checkpoint"] == value.nav_micros
        assert preview["daily_locked"] == (
            (risk.daily_locked if elapsed_days == 0 else 0)
            + max(0, risk.checkpoint - value.nav_micros)
        )
        assert preview["basis"] + preview["external"] - preview["last_nav"] == (
            risk.basis + risk.external - value.nav_micros
        )
