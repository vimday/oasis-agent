"""Raw two-provider account history must constrain observation and key admission."""

from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.chain import ChainError
from oasis_agent.live.precheck import check_live
from oasis_agent.live.rearm import ALLOWED_HALTS
from oasis_agent.live.state import day_of
from oasis_agent.live.valuation import capture_nav
from oasis_agent.models.common import BlockRef, canonical_json, sha256_hex
from scripts import capture_live_recovery as capture_cli
from scripts import live_recovery_checkpoint as checkpoint_cli
from tests.unit import test_live_approval_cash as approval
from tests.unit import test_live_rearm as rearm_fixture
from tests.unit.test_live_authority import setup_authority
from tests.unit.test_live_feed_transition import CONFIG as NAV_CONFIG
from tests.unit.test_live_valuation import NavFixture, synthetic_feed_pin  # noqa: F401
from tests.unit.test_lp_receipts import PRIMARY, SECONDARY, sessions


def checkpoint(tmp_path, evidence, monkeypatch):
    authority, key, fields, context = setup_authority(tmp_path, evidence, monkeypatch)
    loop = authority.loop
    market = loop.observe()
    state = loop.state
    risk = state.integer_risk()
    risk.observe(day=day_of(market.timestamp), at=market.timestamp, nav=market.valuation.nav_micros)
    state.last_units, state.nav_evidence = market.units, market.nav_evidence
    state.initial_nav_usd = risk.basis / 1_000_000
    state.checkpoint_nav_usd = state.last_nav_usd = risk.last_nav / 1_000_000
    state.checkpoint_at, state.day = risk.checkpoint_at, risk.day
    loop.store.save(state)
    return authority, key, fields, context


def advance(authority, fields, context, fault):
    original = authority.loop.rpc.fixture
    number, stamp, block_hash = original.block_number, original.timestamp, original.block_hash
    for f in (authority.loop.rpc.fixture, authority.loop.secondary.fixture):
        if fault == "same_block":
            continue
        f.block_number, f.timestamp, f.block_hash = number + 30, stamp + 60, "0x" + "44" * 32
        if fault == "nonce":
            f.nonce_after += 1
        if fault == "nonce_regress":
            f.nonce_after -= 1
        if fault == "height":
            f.block_number = number
        if fault == "height_regress":
            f.block_number = number - 1
        if fault in {"parent", "direct_child"}:
            f.block_number = number + 1
            f.parent_hash = "0x" + "99" * 32 if fault == "parent" else block_hash
        if fault == "timestamp":
            f.timestamp = stamp
    f = authority.loop.rpc.fixture
    context.update(block=f.block_number, block_hash=f.block_hash)
    fields[1] = f.nonce_after


@pytest.mark.parametrize(
    "fault", ["nonce", "nonce_regress", "height", "height_regress", "parent", "timestamp"]
)
def test_authority_refuses_before_provider_and_preserves_original_risk(
    tmp_path, monkeypatch, fault
):
    with closing(EvidenceStore(tmp_path / "proof.db")) as evidence:
        authority, key, fields, context = checkpoint(tmp_path, evidence, monkeypatch)
        prior = deepcopy(authority.loop.state)
        advance(authority, fields, context, fault)
        calls = []

        def forbidden(*_):
            calls.append(True)
            pytest.fail("unreconciled account reached key provider")

        monkeypatch.setattr(key, "sign", forbidden)
        with pytest.raises(ChainError, match="continuity"):
            authority.sign(fields, context)
        assert not calls
        saved = authority.loop.store.load("live", read_only=True)
        assert saved.halted and "continuity" in saved.halted
        assert saved.halted not in ALLOWED_HALTS
        assert saved.pending is None and saved.signing_attempt is None
        assert saved.exact_risk == prior.exact_risk and saved.nav_evidence == prior.nav_evidence
        assert saved.last_units == prior.last_units and saved.gas_spent_wei == prior.gas_spent_wei
        # The latch refuses the next request before any more acquisition.
        monkeypatch.setattr(
            authority.loop.rpc, "call", lambda *_: pytest.fail("halted authority attempted RPC")
        )
        with pytest.raises(ValueError, match="halted"):
            authority.sign(fields, context)


def test_readonly_precheck_cannot_report_agreeing_unexplained_nonce_as_healthy(
    tmp_path, monkeypatch
):
    with closing(EvidenceStore(tmp_path / "proof.db")) as evidence:
        authority, _, fields, context = checkpoint(tmp_path, evidence, monkeypatch)
        loop = authority.loop
        advance(authority, fields, context, "nonce")
        before = loop.store.path.read_bytes(), loop.store.journal_path.read_bytes()
        result = check_live(
            loop.c,
            loop.store,
            loop.rpc,
            loop.secondary,
            stop_file=tmp_path / "absent-STOP",
            receipt_costs=loop.receipt_costs,
        )
        assert not result["operational_checks_passed"]
        assert not result["execution_eligible"] and not result["signer_contacted"]
        assert (loop.store.path.read_bytes(), loop.store.journal_path.read_bytes()) == before


def test_transient_observation_refusal_does_not_save_or_contact_provider(tmp_path, monkeypatch):
    with closing(EvidenceStore(tmp_path / "proof.db")) as evidence:
        authority, key, fields, context = checkpoint(tmp_path, evidence, monkeypatch)

        def transient(*_):
            raise ChainError("complete account NAV unresolved (details redacted)")

        monkeypatch.setattr(authority.loop, "observe", transient)
        monkeypatch.setattr(
            authority.loop.store, "save", lambda *_: pytest.fail("transient failure saved state")
        )
        with pytest.raises(ChainError, match="NAV unresolved"):
            authority.sign(fields, context)
        assert not authority.loop.state.halted and key.calls == 0


def test_strategy_latches_before_decision_without_replacing_risk_checkpoint(tmp_path, monkeypatch):
    with closing(EvidenceStore(tmp_path / "proof.db")) as evidence:
        authority, _, fields, context = checkpoint(tmp_path, evidence, monkeypatch)
        loop = authority.loop
        prior = deepcopy(loop.state)
        advance(authority, fields, context, "nonce")
        monkeypatch.setattr(
            loop, "_decide", lambda *_: pytest.fail("unexpected nonce reached strategy")
        )
        result = loop.tick()
        assert "continuity" in result and loop.state.halted
        assert loop.state.exact_risk == prior.exact_risk
        assert (
            loop.state.nav_evidence == prior.nav_evidence
            and loop.state.last_units == prior.last_units
        )
        assert loop.store.load("live", read_only=True).halted == loop.state.halted
        assert loop.state.halted not in ALLOWED_HALTS


@pytest.mark.parametrize("fault", ["same_block", "direct_child", "newer"])
def test_unchanged_nonce_keeps_valid_same_or_newer_observations_readonly(
    tmp_path, monkeypatch, fault
):
    with closing(EvidenceStore(tmp_path / "proof.db")) as evidence:
        authority, key, fields, context = checkpoint(tmp_path, evidence, monkeypatch)
        loop = authority.loop
        advance(authority, fields, context, fault)
        before = canonical_json(asdict(loop.state))
        value = loop.observe()
        assert value.valuation.wallet.nonce == fields[1]
        assert canonical_json(asdict(loop.state)) == before and key.calls == 0


@pytest.mark.parametrize("nonce_delta", [0, 1, -1])
def test_recovery_replays_settled_cash_nonce_even_without_nav_checkpoint(
    tmp_path, monkeypatch, nonce_delta
):
    sources = (PRIMARY, SECONDARY)
    monkeypatch.setattr(checkpoint_cli, "SOURCES", sources)
    target = replace(
        approval.TEST_CONFIG,
        expected_pins=approval.TEST_CONFIG.expected_pins
        | {role: NAV_CONFIG.expected_pins[role] for role in ("state_view", "position_manager")},
    )
    old = replace(target, usdc_oracle_max_age_seconds=90_000)
    monkeypatch.setattr(approval, "TEST_CONFIG", old)
    with closing(EvidenceStore(tmp_path / "proof.db")) as evidence:
        fixture = approval.ApprovalFixture()
        loop = approval.approval_loop(tmp_path, evidence, fixture, replace(fixture))
        assert "confirmed" in loop.tick()
        state = loop.store.load("live", read_only=True)
        assert not state.nav_evidence
        current = NavFixture(
            position_enabled=False,
            native_after=fixture.native_after,
            token_after=fixture.token_after,
            nonce_after=fixture.nonce_after,
        )
        block = BlockRef(
            target.chain_id, current.block_number, current.block_hash, current.timestamp
        )
        value, _ = capture_nav(
            evidence,
            old,
            block,
            None,
            sources=sources,
            sessions=sessions({PRIMARY: current, SECONDARY: replace(current)}),
        )
        state.integer_risk().observe(
            day=day_of(current.timestamp - 30), at=current.timestamp - 30, nav=value.nav_micros
        )
        state.halted = "synthetic retained halt"
        current.nonce_after += nonce_delta
        _, refs = capture_nav(
            evidence,
            target,
            block,
            None,
            sources=sources,
            sessions=sessions({PRIMARY: current, SECONDARY: replace(current)}),
        )
        before, raw = deepcopy(state), (tmp_path / "proof.db").read_bytes()

        def check():
            capture_cli.review_checkpoint(
                target,
                state,
                sha256_hex(canonical_json(asdict(old))),
                evidence,
                fresh=refs,
                now=current.timestamp,
            )

        if nonce_delta:
            with pytest.raises(ValueError, match="reconcile"):
                check()
        else:
            check()  # Identical post-cash block is a valid read-only recapture.
        assert state == before and (tmp_path / "proof.db").read_bytes() == raw


@pytest.mark.parametrize("fault", ["height", "parent"])
def test_rearm_does_not_clear_halt_for_inconsistent_historical_block(tmp_path, monkeypatch, fault):
    data = rearm_fixture.pair(tmp_path, monkeypatch)
    directories, stores, config, _, refs = data
    old = stores["authority"].load("live", read_only=True).nav_evidence[0].block
    fixture = NavFixture(
        position_enabled=False,
        native_after=10**17,
        token_after=100_000_000,
        usdc_price=100_000_000,
        timestamp=old.timestamp + 20,
        block_number=old.number + int(fault == "parent"),
        block_hash="0x" + "44" * 32,
    )
    for role in directories:
        with closing(EvidenceStore(rearm_fixture.paths(directories[role], role)[1])) as db:
            _, nav = rearm_fixture.capture(db, config, fixture)
            refs[role] = {
                "nav": nav,
                "settlements": tuple(
                    rearm_fixture.persist_settlement_head(
                        db, rearm_fixture.wire_capture(nav[0].block, source=source)
                    )
                    for source in rearm_fixture.SOURCES
                ),
            }
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="reconcile"):
        rearm_fixture.review(data)
    assert {p: p.read_bytes() for p in before} == before
