"""Migration must retain the nonce/block anchor of its verified wallet checkpoint."""

from contextlib import ExitStack, closing
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.feed_transition import apply_pair, review_service
from oasis_agent.live.state import day_of
from oasis_agent.live.valuation import capture_nav
from oasis_agent.models.common import BlockRef, canonical_json
from oasis_agent.rpc.transport import JsonRpcTransport
from tests.unit import test_live_approval_cash as approval
from tests.unit.test_live_feed_migration import apply, prepare
from tests.unit.test_live_feed_pair_atomic import pair
from tests.unit.test_live_feed_transition import CONFIG, OLD_HASH, SOURCES, capture, setup
from tests.unit.test_live_valuation import NavFixture, synthetic_feed_pin  # noqa: F401
from tests.unit.test_lp_receipts import sessions


@pytest.mark.parametrize("anchor", ["nav", "settled_cash"])
@pytest.mark.parametrize(
    "fault",
    [None, "nonce_advance", "nonce_regress", "height", "height_regress", "parent", "timestamp"],
)
def test_review_refuses_unexplained_nonce_or_block_change_without_writing(
    tmp_path, monkeypatch, anchor, fault
):
    path = tmp_path / "proof.db"
    with closing(EvidenceStore(path)) as evidence:
        target = CONFIG
        if anchor == "nav":
            state, new, _ = setup(evidence)
            # setup's historical NAV is 60 seconds and 30 blocks earlier.
            old_height = new.block_number - 30
            old_hash = state.nav_evidence[0].block.block_hash
            old_timestamp = new.timestamp - 60
            binding = OLD_HASH
        else:
            target = replace(
                approval.TEST_CONFIG,
                expected_pins=approval.TEST_CONFIG.expected_pins
                | {role: CONFIG.expected_pins[role] for role in ("state_view", "position_manager")},
            )
            old = replace(target, usdc_oracle_max_age_seconds=90_000)
            monkeypatch.setattr(approval, "TEST_CONFIG", old)
            fixture = approval.ApprovalFixture()
            loop = approval.approval_loop(tmp_path, evidence, fixture, replace(fixture))
            assert "confirmed" in loop.tick()
            state = loop.store.load("live", read_only=True)
            assert not state.nav_evidence
            state.halted = "private retained halt"
            old_height, old_hash = fixture.block_number, fixture.block_hash
            old_timestamp = fixture.timestamp
            new = NavFixture(
                position_enabled=False,
                native_after=fixture.native_after,
                token_after=fixture.token_after,
                nonce_after=fixture.nonce_after,
                timestamp=fixture.timestamp + 60,
                block_number=old_height + 30,
                block_hash="0x" + "44" * 32,
            )
            # The approval helper has no funded risk baseline. Derive this
            # synthetic fixture's baseline independently, leaving its post-action
            # NAV refs cleared so review must use the settled cash anchor.
            prior = replace(
                new,
                timestamp=fixture.timestamp,
                block_number=old_height,
                block_hash=old_hash,
            )
            value, _ = capture_nav(
                evidence,
                old,
                BlockRef(old.chain_id, old_height, old_hash, prior.timestamp),
                None,
                sources=SOURCES,
                sessions=sessions(dict(zip(SOURCES, (prior, replace(prior)), strict=True))),
            )
            state.integer_risk().observe(
                day=day_of(prior.timestamp - 30), at=prior.timestamp - 30, nav=value.nav_micros
            )
            binding = loop.store.journal.config_hash
        if fault in {"nonce_advance", "nonce_regress"}:
            new = replace(
                new, nonce_after=new.nonce_after + (1 if fault == "nonce_advance" else -1)
            )
        if fault in {"height", "height_regress"}:
            new = replace(new, block_number=old_height - int(fault == "height_regress"))
        if fault == "parent":
            new = replace(new, block_number=old_height + 1, parent_hash="0x" + "99" * 32)
        if fault == "timestamp":
            new = replace(
                new, block_number=old_height + 1, parent_hash=old_hash, timestamp=old_timestamp
            )
        if fault is None:
            # Exercise the accepted direct-child boundary too.
            new = replace(new, block_number=old_height + 1, parent_hash=old_hash)
        _, fresh = capture_nav(
            evidence,
            target,
            BlockRef(target.chain_id, new.block_number, new.block_hash, new.timestamp),
            state.position,
            sources=SOURCES,
            sessions=sessions(dict(zip(SOURCES, (new, replace(new)), strict=True))),
        )
        before, raw = deepcopy(state), path.read_bytes()
        monkeypatch.setattr(
            JsonRpcTransport, "request", lambda *_: pytest.fail("offline review attempted RPC")
        )
        result = review_service(
            target,
            state,
            binding,
            evidence,
            source_payload=canonical_json(asdict(state)),
            sources=SOURCES,
            fresh=fresh,
            now=new.timestamp,
        )
        assert state == before and path.read_bytes() == raw
        assert result["historical_checkpoint_verified"]
        assert not result["execution_eligible"] and result["halt_preserved"]
        assert bool(result["blockers"]) is (fault is not None)
        assert result["fresh_nav_verified"] is (fault is None)
        assert "private retained" not in str(result)


@pytest.mark.parametrize("fault", ["nonce_advance", "nonce_regress", "height", "parent"])
def test_single_service_refusal_preserves_journal_projection_stop_and_proofs(
    tmp_path, monkeypatch, fault
):
    with closing(EvidenceStore(tmp_path / "proof.db")) as evidence:
        store, state, _, stop = prepare(tmp_path, evidence, monkeypatch)
        _, new, _ = setup(evidence)
        if fault in {"nonce_advance", "nonce_regress"}:
            new = replace(
                new, nonce_after=new.nonce_after + (1 if fault == "nonce_advance" else -1)
            )
        elif fault == "height":
            new = replace(new, block_number=state.nav_evidence[0].block.number)
        else:
            new = replace(
                new,
                block_number=state.nav_evidence[0].block.number + 1,
                parent_hash="0x" + "99" * 32,
            )
        _, fresh = capture(evidence, CONFIG, new)
        before = {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
        with pytest.raises(ValueError, match="transition evidence remains unresolved"):
            apply(store, fresh, stop, evidence)
        assert {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before
        assert store.journal.config_hash == OLD_HASH


def test_pair_cannot_migrate_two_agreeing_unreconciled_nonces(tmp_path, monkeypatch):
    with ExitStack() as resources:
        stores, evidence, fresh = pair(tmp_path, monkeypatch, resources)
        for role in stores:
            _, new, _ = setup(evidence[role])
            _, fresh[role] = capture(evidence[role], CONFIG, replace(new, nonce_after=5))
        before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        with pytest.raises(ValueError, match="passing fresh evidence review"):
            apply_pair(
                stores["strategy"], stores["authority"], CONFIG, evidence, fresh, sources=SOURCES
            )
        assert {p: p.read_bytes() for p in before} == before
        assert not any(p.name.endswith(("-journal", "-wal", "-shm")) for p in tmp_path.rglob("*"))
        assert all(store.journal.config_hash == OLD_HASH for store in stores.values())
