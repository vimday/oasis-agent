"""Historical five-pin evidence must keep its original epoch after pin expansion."""

from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.config_epochs import retained_config, retained_configurations
from oasis_agent.live.feed_transition import historical_config
from oasis_agent.live.journal import LiveJournal
from oasis_agent.live.rearm_history import replay_history
from oasis_agent.live.runner import LiveLoop
from oasis_agent.live.state import StateStore
from oasis_agent.models.common import canonical_json, sha256_hex
from scripts import capture_live_recovery as capture_cli
from tests.unit import test_live_approval_cash as approval
from tests.unit.gas_oracle_fixtures import synthetic_gas_pins
from tests.unit.test_live_feed_transition import CONFIG as FEED_CONFIG
from tests.unit.test_live_feed_transition import OLD_HASH, capture
from tests.unit.test_live_feed_transition import setup as checkpoint_setup
from tests.unit.test_live_rearm import rows as journal_rows
from tests.unit.test_live_rearm_history import SOURCES, setup
from tests.unit.test_live_recovery_capture import CONFIG, PROFILE
from tests.unit.test_live_recovery_capture import setup as recovery_setup
from tests.unit.test_live_valuation import synthetic_feed_pin  # noqa: F401


def test_expanded_policy_replays_original_five_pin_settlement_epoch(tmp_path, monkeypatch):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as evidence:
        target, fixture, state, rows = setup(tmp_path, evidence, monkeypatch)
        expanded = replace(target, expected_pins=target.expected_pins | synthetic_gas_pins())
        before = deepcopy(state)
        assert replay_history(rows, state, expanded, evidence, SOURCES) == (fixture._receipt(),)
        assert state == before and state.gas_spent_wei == 218


@pytest.mark.parametrize("legacy", [False, True])
def test_expanded_policy_can_plan_original_five_pin_checkpoint_without_writes(
    tmp_path, monkeypatch, legacy
):
    roots, prior_plan, *_ = recovery_setup(tmp_path, monkeypatch, legacy=legacy)
    expanded = replace(CONFIG, expected_pins=CONFIG.expected_pins | synthetic_gas_pins())
    before = {p: p.read_bytes() for root in roots for p in root.iterdir()}
    with capture_cli.prepared(*roots, expanded, PROFILE) as prepared:
        plan = prepared[0]
    assert plan["plan_hash"] != prior_plan["plan_hash"]
    assert {p: p.read_bytes() for root in roots for p in root.iterdir()} == before
    # Historical compatibility never changes admission or the original journal
    # binding. A normal writer configured with the new hash still refuses.
    target_hash = sha256_hex(canonical_json(asdict(expanded)))
    for role, root in zip(capture_cli.ROLES, roots, strict=True):
        path, _, _ = capture_cli.paths(root, role)
        store = StateStore(path)
        with pytest.raises(ValueError, match="configuration mismatch"):
            LiveJournal(store.journal_path, target_hash, require_existing=True).load(read_only=True)


@pytest.mark.parametrize("full_pins", [False, True])
@pytest.mark.parametrize("age", [1800, 90000])
def test_retained_hash_selects_exact_original_bytes(full_pins, age):
    target = replace(CONFIG, expected_pins=CONFIG.expected_pins | synthetic_gas_pins())
    original = replace(target if full_pins else CONFIG, usdc_oracle_max_age_seconds=age)
    binding = sha256_hex(canonical_json(asdict(original)))
    resolved = retained_config(target, binding)
    assert asdict(resolved) == asdict(original)
    assert sha256_hex(canonical_json(asdict(resolved))) == binding
    if full_pins and age == 1800:
        assert resolved is target
    if age == 90000:
        with pytest.raises(ValueError, match="bounded"):
            resolved.validate()  # a historical object never becomes a valid live policy


def test_five_pin_target_cannot_resolve_an_eight_pin_epoch():
    expanded = replace(CONFIG, expected_pins=CONFIG.expected_pins | synthetic_gas_pins())
    assert len(retained_configurations(CONFIG)) == 2
    assert len(retained_configurations(expanded)) == 4
    with pytest.raises(ValueError, match="unsupported"):
        retained_config(CONFIG, sha256_hex(canonical_json(asdict(expanded))))


@pytest.mark.parametrize("binding", ["f" * 64, "F" * 64, "short", None, True])
def test_unknown_or_noncanonical_binding_has_no_fallback(binding):
    with pytest.raises(ValueError):
        retained_config(CONFIG, binding)


@pytest.mark.parametrize(
    "change",
    [
        {"account": "0x" + "01" * 20},
        {"max_capital_usd": 499},
        {"max_daily_loss_usd": 199},
        {"max_cumulative_loss_usd": 499},
        {"oracle_max_age_seconds": 1799},
        {"gas_reserve_wei": CONFIG.gas_reserve_wei + 1},
        {"priority_fee_wei": CONFIG.priority_fee_wei - 1},
        {"expected_pins": CONFIG.expected_pins | {"state_view": "0x" + "01" * 32}},
        {"secondary_rpc_env": "FOREIGN_RPC"},
    ],
)
def test_prior_account_old_pin_or_any_other_policy_difference_is_not_a_known_epoch(change):
    target = replace(CONFIG, expected_pins=CONFIG.expected_pins | synthetic_gas_pins())
    foreign = replace(CONFIG, **change)
    with pytest.raises(ValueError, match="unsupported"):
        retained_config(target, sha256_hex(canonical_json(asdict(foreign))))


def test_different_previously_pinned_gas_runtime_is_not_silently_replaced():
    target = replace(CONFIG, expected_pins=CONFIG.expected_pins | synthetic_gas_pins())
    foreign = replace(
        target, expected_pins=target.expected_pins | {"gas_oracle_proxy": "0x" + "01" * 32}
    )
    with pytest.raises(ValueError, match="unsupported"):
        retained_config(target, sha256_hex(canonical_json(asdict(foreign))))


def test_first_epoch_cash_proof_cannot_be_relabelled_with_the_new_policy(tmp_path, monkeypatch):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as evidence:
        target, _, state, rows = setup(tmp_path, evidence, monkeypatch)
        expanded = replace(target, expected_pins=target.expected_pins | synthetic_gas_pins())
        new_hash = sha256_hex(canonical_json(asdict(expanded)))
        with pytest.raises(ValueError):
            replay_history(
                [(new_hash, payload) for _, payload in rows], state, expanded, evidence, SOURCES
            )


def test_strict_five_pin_cash_proofs_also_replay_at_their_original_hash(tmp_path, monkeypatch):
    original = approval.TEST_CONFIG
    with closing(EvidenceStore(tmp_path / "evidence.db")) as evidence:
        fixture = approval.ApprovalFixture()
        loop = approval.approval_loop(tmp_path, evidence, fixture, replace(fixture))
        assert "confirmed" in loop.tick()
        state = loop.store.load("live", read_only=True)
        rows = [(row[1], row[4]) for row in journal_rows(loop.store)]
        expanded = replace(original, expected_pins=original.expected_pins | synthetic_gas_pins())
        assert replay_history(rows, state, expanded, evidence, SOURCES) == (fixture._receipt(),)


@pytest.mark.parametrize("fresh_under_old_policy", [False, True])
def test_checkpoint_uses_original_proof_but_fresh_nav_requires_current_policy(
    tmp_path, monkeypatch, fresh_under_old_policy
):
    monkeypatch.setattr(capture_cli, "SOURCES", SOURCES)
    with closing(EvidenceStore(tmp_path / "evidence.db")) as evidence:
        state, fixture, old_fresh = checkpoint_setup(evidence)
        target = replace(
            FEED_CONFIG, expected_pins=FEED_CONFIG.expected_pins | synthetic_gas_pins()
        )
        _, fresh = capture(evidence, target, fixture)
        before, raw = deepcopy(state), (tmp_path / "evidence.db").read_bytes()
        if fresh_under_old_policy:
            with pytest.raises(ValueError):
                capture_cli.review_checkpoint(
                    target, state, OLD_HASH, evidence, fresh=old_fresh, now=fixture.timestamp
                )
        else:
            capture_cli.review_checkpoint(
                target, state, OLD_HASH, evidence, fresh=fresh, now=fixture.timestamp
            )
        assert state == before and (tmp_path / "evidence.db").read_bytes() == raw


def test_current_policy_recovery_still_requires_halt_before_proof_access(tmp_path, monkeypatch):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as evidence:
        state, fixture, _ = checkpoint_setup(evidence)
        state.halted = None
        target = replace(
            FEED_CONFIG, expected_pins=FEED_CONFIG.expected_pins | synthetic_gas_pins()
        )
        binding = sha256_hex(canonical_json(asdict(target)))
        monkeypatch.setattr(
            LiveLoop,
            "_check_nav_checkpoint",
            lambda *_: pytest.fail("unhalted current policy must refuse before evidence"),
        )
        with pytest.raises(ValueError, match="halted"):
            capture_cli.review_checkpoint(target, state, binding, evidence, now=fixture.timestamp)


def test_feed_only_migration_still_cannot_apply_the_runtime_pin_expansion():
    target = replace(CONFIG, expected_pins=CONFIG.expected_pins | synthetic_gas_pins())
    old = replace(CONFIG, usdc_oracle_max_age_seconds=90000)
    with pytest.raises(ValueError, match="unrecognized"):
        historical_config(target, sha256_hex(canonical_json(asdict(old))))
