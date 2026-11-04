"""Local-state reporting is bounded, redacted and never trading authorization."""

import json
import os
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.chain import ChainError
from oasis_agent.live.receipts import RetainedReceiptCostReader
from oasis_agent.live.runner import LiveLoop
from oasis_agent.live.state import LoopState, StateStore
from oasis_agent.reporting.live_status import MAX_STATE_BYTES, read_live_status
from oasis_agent.server.config import load_server_config
from tests.unit.server_helpers import make_server_config
from tests.unit.test_live_lp import CONFIG, ScriptedRpc, live_loop, market
from tests.unit.test_live_precheck_evidence import SOURCES, capture
from tests.unit.test_live_receipt_costs import position_config
from tests.unit.test_live_valuation import NavFixture, synthetic_feed_pin  # noqa: F401
from tests.unit.test_server_api import client_for

NOW = 1_791_470_000


def observed_state() -> dict:
    state = asdict(LoopState("live"))
    state["last_observation"] = {
        "chain_id": 8453,
        "account": CONFIG.account,
        "pool_id": CONFIG.pool_id,
        "config_hash": "ab" * 32,
        "block": 123,
        "block_hash": "0x" + "12" * 32,
        "block_timestamp": NOW - 5,
        "observed_at": NOW,
        "nav_usd": 480.0,
        "daily_loss_usd": 40.0,
        "cumulative_loss_usd": 20.0,
        "capital_limit_usd": 500.0,
        "daily_loss_limit_usd": 200.0,
        "cumulative_loss_limit_usd": 500.0,
        "valuation_basis": "two_feed_principal_and_fees",
        "nav_valid_until": NOW + 115,
        "eth_feed_max_age_seconds": 1800,
        "usdc_feed_max_age_seconds": 1800,
    }
    return state


def write_state(tmp_path: Path, state: dict) -> Path:
    path = tmp_path / "live.state.json"
    path.write_text(json.dumps(state))
    return path


def test_status_never_infers_freshness_from_file_mtime_or_claims_execution(tmp_path: Path) -> None:
    path = write_state(tmp_path, observed_state())
    before = path.read_bytes()
    fresh = read_live_status(path, now=NOW)
    assert fresh.status == "observed"
    assert not fresh.execution_eligible and not fresh.evidence_replayed
    assert fresh.profitability == "unresolved"
    assert fresh.native_gas_units == "0"
    stale = read_live_status(path, now=NOW + 121)
    assert stale.status == "stale" and "observation_stale" in stale.blockers
    assert stale.state_hash == fresh.state_hash and path.read_bytes() == before


def test_nav_feed_deadline_expires_before_recent_block(tmp_path: Path) -> None:
    state = observed_state()
    state["last_observation"]["nav_valid_until"] = NOW + 1
    path = write_state(tmp_path, state)
    assert read_live_status(path, now=NOW + 1).status == "observed"
    expired = read_live_status(path, now=NOW + 2)
    assert expired.status == "stale"
    assert expired.blockers == ["nav_validity_expired"]
    assert expired.observation_age_seconds == 2
    assert expired.release.new_actions_allowed and not expired.execution_eligible


@pytest.mark.parametrize("fault", ["missing", "legacy", "eth_policy", "usdc_policy", "deadline"])
def test_recent_nav_requires_recorded_strict_feed_freshness(tmp_path: Path, fault: str) -> None:
    state = observed_state()
    observation = state["last_observation"]
    if fault == "missing":
        for field in ("nav_valid_until", "eth_feed_max_age_seconds", "usdc_feed_max_age_seconds"):
            observation.pop(field)
        blocker = "nav_validity_unverified"
    elif fault == "legacy":
        observation["valuation_basis"] = "legacy_principal_only"
        blocker = "nav_validity_unverified"
    elif fault == "deadline":
        observation["nav_valid_until"] = observation["block_timestamp"] + 121
        blocker = "nav_validity_unverified"
    else:
        observation[
            "eth_feed_max_age_seconds" if fault == "eth_policy" else "usdc_feed_max_age_seconds"
        ] = 90_000
        blocker = "usd_feed_policy_mismatch"
    report = read_live_status(write_state(tmp_path, state), now=NOW)
    assert report.status == "stale" and blocker in report.blockers
    assert report.observation is not None and report.release.new_actions_allowed
    assert report.evidence_replayed is False and report.execution_eligible is False


def test_tighter_feed_limits_are_reported_without_being_widened(tmp_path: Path) -> None:
    state = observed_state()
    state["last_observation"].update(eth_feed_max_age_seconds=900, usdc_feed_max_age_seconds=60)
    report = read_live_status(write_state(tmp_path, state), now=NOW)
    assert report.status == "observed"
    assert report.observation.eth_feed_max_age_seconds == 900
    assert report.observation.usdc_feed_max_age_seconds == 60


@pytest.mark.parametrize("eth_limit,usdc_limit", [(1800, 1800), (900, 60), (1800, 90_000)])
def test_runtime_records_retained_feed_deadline_without_refreshing_it(
    tmp_path, monkeypatch, eth_limit, usdc_limit
):
    config = replace(
        position_config(),
        oracle_max_age_seconds=eth_limit,
        usdc_oracle_max_age_seconds=usdc_limit,
    )
    fixture = NavFixture(
        position_enabled=False,
        native_after=10**17,
        token_after=100_000_000,
        usdc_price=100_000_000,
        usdc_age=usdc_limit - 1,
    )
    store = StateStore(tmp_path / "state.json")
    loop = LiveLoop.recovery(config, ScriptedRpc(), store, "live")
    monkeypatch.setattr("oasis_agent.live.runner.time.time", lambda: fixture.timestamp)
    with closing(EvidenceStore(tmp_path / "proof.db")) as evidence:
        value, refs = capture(evidence, config, fixture)

        def forbidden(*_):
            raise AssertionError("recording status must replay proof without network")

        loop.receipt_costs = RetainedReceiptCostReader(
            evidence,
            chain_id=config.chain_id,
            account=config.account,
            native_decimals=config.decimals0,
            sources=SOURCES,
            sessions=forbidden,
        )
        loop.observe = lambda: market(
            block=fixture.block_number,
            block_hash=fixture.block_hash,
            timestamp=fixture.timestamp,
            eth=value.wallet.native,
            usdc=value.wallet.token,
            valuation=value,
            nav_evidence=refs,
        )
        loop._decide = lambda _: None
        loop.tick()
    saved = store.load("live").last_observation
    assert saved["nav_valid_until"] == fixture.timestamp + 1
    assert saved["eth_feed_max_age_seconds"] == eth_limit
    assert saved["usdc_feed_max_age_seconds"] == usdc_limit
    expected = "observed" if usdc_limit <= 1800 else "stale"
    assert read_live_status(store.path, now=fixture.timestamp + 1).status == expected
    assert read_live_status(store.path, now=fixture.timestamp + 2).status == "stale"


@pytest.mark.parametrize("halted", [None, "private halt message"])
@pytest.mark.parametrize("attempt", [{}, {"nonce": 3, "opaque": "private signing data"}])
def test_uncertain_signature_is_never_presented_as_recent_idle_state(tmp_path, halted, attempt):
    state = observed_state()
    state.update(halted=halted, signing_attempt=attempt)
    path = write_state(tmp_path, state)
    before = path.read_bytes()
    report = read_live_status(path, now=NOW)
    assert report.status == "signing_unknown"
    assert report.signing_unresolved is True
    assert "signing_outcome_unresolved" in report.blockers
    assert report.pending is None and report.execution_eligible is False
    assert "private" not in report.model_dump_json()
    assert "signing_attempt" not in report.model_dump_json()
    assert path.read_bytes() == before
    state["signing_attempt"] = None
    state["halted"] = None
    recovered = read_live_status(write_state(tmp_path, state), now=NOW)
    assert recovered.status == "observed" and recovered.signing_unresolved is False
    assert "signing_outcome_unresolved" not in recovered.blockers


@pytest.mark.parametrize("attempt", [False, 3, "private signing data", []])
def test_malformed_signing_reservation_clears_observation(tmp_path, attempt):
    state = observed_state()
    state["signing_attempt"] = attempt
    report = read_live_status(write_state(tmp_path, state), now=NOW)
    assert report.status == "invalid"
    assert report.signing_unresolved is None and report.observation is None
    assert "private" not in report.model_dump_json()


@pytest.mark.parametrize("change", ["missing", "future", "future_block", "old_block", "loss"])
def test_missing_clock_stale_and_exhausted_budget_are_explicit(tmp_path: Path, change: str) -> None:
    state = observed_state()
    if change == "missing":
        del state["last_observation"]
    elif change == "future":
        state["last_observation"]["observed_at"] += 1
    elif change == "future_block":
        state["last_observation"]["block_timestamp"] = NOW + 1
    elif change == "old_block":
        state["last_observation"]["block_timestamp"] = NOW - 121
    else:
        state["last_observation"]["daily_loss_usd"] = 200.0
    report = read_live_status(write_state(tmp_path, state), now=NOW)
    assert report.status == "stale" and report.blockers


@pytest.mark.parametrize("change", ["nan", "bool", "negative", "shadow", "huge", "pending"])
def test_invalid_financial_state_clears_all_numbers(tmp_path: Path, change: str) -> None:
    state = observed_state()
    if change == "nan":
        state["last_observation"]["nav_usd"] = float("nan")
    elif change == "bool":
        state["gas_spent_wei"] = False
    elif change == "negative":
        state["gas_spent_wei"] = -1
    elif change == "shadow":
        state["mode"] = "shadow"
    elif change == "huge":
        state["gas_spent_wei"] = 2**256
    else:
        state["pending"] = {"kind": "arbitrary", "tx_hash": "bad"}
    report = read_live_status(write_state(tmp_path, state), now=NOW)
    assert report.status == "invalid"
    assert report.observation is None and report.native_gas_units is None


def test_halt_pending_and_history_never_echo_arbitrary_fields(tmp_path: Path) -> None:
    state = observed_state()
    marker = "opaque-private-value-do-not-display"
    state.update(halted=marker, unexpected=marker)
    state["pending"] = {
        "kind": "swap",
        "tx_hash": "0x" + "ab" * 32,
        "nonce": 2**63,
        "signed_at": NOW,
        "intent": {"extra": marker},
        "raw_signed": marker,
    }
    state["history"] = [
        {
            "event": "reverted",
            "tx": "0x" + "ab" * 32,
            "native_gas_units": 2**64 + 1,
            "cost_evidence": [{"private": marker}],
            "cash_evidence": [{"private": marker}],
            "reason": marker,
        },
    ]
    report = read_live_status(write_state(tmp_path, state), now=NOW)
    assert report.status == "halted"
    assert "pending_requires_reconciliation" in report.blockers
    assert marker not in report.model_dump_json()
    assert report.recent_actions[0].native_gas_units == str(2**64 + 1)
    assert report.recent_actions[0].cash_references == 1 and not report.evidence_replayed
    assert report.pending.nonce == 2**63


def test_legacy_nonzero_gas_remains_incomplete(tmp_path: Path) -> None:
    state = observed_state()
    del state["legacy_gas_cost_incomplete"]
    state["gas_spent_wei"] = 100
    report = read_live_status(write_state(tmp_path, state), now=NOW)
    assert "historical_native_cost_incomplete" in report.blockers


def test_legacy_state_without_signing_field_remains_a_local_declaration(tmp_path):
    state = observed_state()
    state.pop("signing_attempt")
    report = read_live_status(write_state(tmp_path, state), now=NOW)
    assert report.status == "observed" and report.signing_unresolved is False
    assert report.evidence_replayed is False and report.execution_eligible is False


def test_file_failures_are_redacted_and_never_create_a_state(tmp_path: Path) -> None:
    assert read_live_status(None).status == "unconfigured"
    missing = tmp_path / "absent"
    assert read_live_status(missing).status == "missing" and not missing.exists()
    target = write_state(tmp_path, observed_state())
    link = tmp_path / "link"
    link.symlink_to(target)
    assert read_live_status(link).status == "invalid"
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert read_live_status(fifo).status == "invalid"
    target.write_bytes(b" " * (MAX_STATE_BYTES + 1))
    assert read_live_status(target).status == "invalid"
    target.write_text('{"mode":"live","mode":"live"}')
    assert read_live_status(target).status == "invalid"


def test_read_only_endpoint_refreshes_after_atomic_replace_without_leaking_paths(
    tmp_path: Path,
) -> None:
    config = make_server_config(tmp_path)
    path = tmp_path / "local-state.json"
    config = replace(config, databases=replace(config.databases, live_state=path))
    with client_for(tmp_path, config=config) as client:
        assert client.get("/api/v1/live/status").json()["status"] == "missing"
        replacement = write_state(tmp_path, observed_state())
        replacement.replace(path)
        first = client.get("/api/v1/live/status")
        assert first.status_code == 200
        assert first.json()["observation"]["nav_usd"] == 480
        uncertain = observed_state()
        uncertain["signing_attempt"] = {"opaque": "private-reservation-marker"}
        path.write_text(json.dumps(uncertain))
        unknown = client.get(
            "/api/v1/live/status", headers={"If-None-Match": first.headers["etag"]}
        )
        assert unknown.status_code == 200 and unknown.json()["status"] == "signing_unknown"
        assert unknown.json()["signing_unresolved"] is True
        assert unknown.headers["etag"] != first.headers["etag"]
        assert "private-reservation-marker" not in unknown.text
        path.write_text("invalid sensitive text")
        response = client.get(
            "/api/v1/live/status", headers={"If-None-Match": first.headers["etag"]}
        )
        assert response.status_code == 200 and response.json()["status"] == "invalid"
        assert response.json()["signing_unresolved"] is None
        assert response.json()["observation"] is None
        assert str(path) not in response.text and "sensitive text" not in response.text
        assert client.post("/api/v1/live/status").status_code == 405
        meta = client.get("/api/v1/meta").json()
        endpoint = next(x for x in meta["endpoints"] if x["path"] == "/api/v1/live/status")
        assert endpoint["console_view"] == "execution" and endpoint["methods"] == ["GET"]


def test_config_can_monitor_a_not_yet_created_state(tmp_path: Path) -> None:
    config = tmp_path / "server.toml"
    missing = tmp_path / "not-started.json"
    config.write_text(f'[databases]\nlive_state = "{missing}"\n')
    assert load_server_config(config).databases.live_state == missing
    assert not missing.exists()


def test_loop_records_actual_observation_and_does_not_refresh_it_on_failure(tmp_path: Path) -> None:
    loop = live_loop(tmp_path, ScriptedRpc())
    loop.observe = market
    loop._decide = lambda _: None
    loop.tick()
    saved = StateStore(loop.store.path).load("shadow").last_observation
    assert saved["block"] == market().block
    assert saved["nav_usd"] == loop.nav_usd(market())
    assert saved["daily_loss_limit_usd"] == CONFIG.max_daily_loss_usd

    def fail():
        raise ChainError("provider unavailable")

    loop.observe = fail
    loop.tick()
    assert loop.state.last_observation == saved


def light_state(*, full_age: int, light_age: int, block: int = 200) -> dict:
    state = observed_state()
    state["last_observation"]["observed_at"] = NOW - full_age
    state["last_observation"]["block_timestamp"] = NOW - full_age - 5
    state["last_observation"]["nav_valid_until"] = NOW - full_age + 115
    state["last_light_check"] = {
        "block": block,
        "block_timestamp": NOW - light_age - 2,
        "observed_at": NOW - light_age,
        "tick": -199_100,
    }
    return state


@pytest.mark.parametrize("full_age", [121, 900, 1801, 4020])
def test_recent_tick_check_never_refreshes_account_nav(tmp_path: Path, full_age: int) -> None:
    report = read_live_status(
        write_state(tmp_path, light_state(full_age=full_age, light_age=60)), now=NOW
    )
    assert report.status == "stale" and "observation_stale" in report.blockers
    assert report.light_check is not None and report.light_check.tick == -199_100
    assert report.observation_age_seconds == full_age


@pytest.mark.parametrize(
    ("full_age", "light_age", "block"),
    [
        (4021, 60, 200),  # the full NAV/loss observation itself is overdue
        (900, 421, 200),  # the light check is too old
        (900, 60, 123),  # light check is not newer than the full observation
        (900, -5, 200),  # light check from the future
    ],
)
def test_light_check_never_masks_a_stale_or_inconsistent_observation(
    tmp_path: Path, full_age: int, light_age: int, block: int
) -> None:
    state = light_state(full_age=full_age, light_age=light_age, block=block)
    report = read_live_status(write_state(tmp_path, state), now=NOW)
    assert report.status == "stale" and "observation_stale" in report.blockers


def test_light_check_cannot_hide_loss_latch_or_halt(tmp_path: Path) -> None:
    state = light_state(full_age=300, light_age=30)
    state["last_observation"]["daily_loss_usd"] = 200.0
    state["halted"] = "operator stop"
    report = read_live_status(write_state(tmp_path, state), now=NOW)
    assert {"recorded_loss_budget_exhausted", "state_halted"} <= set(report.blockers)
    assert report.status == "halted"


@pytest.mark.parametrize(
    "light",
    [
        {"block": 1, "block_timestamp": 2, "observed_at": 1, "tick": 0},  # block after observation
        {"block": 1, "block_timestamp": 1, "observed_at": 1, "tick": 900_000},
        {"block": 1, "block_timestamp": 1, "observed_at": 1},
        {"block": 1, "block_timestamp": 1, "observed_at": 1, "tick": 0, "nav": 1},
        {"block": True, "block_timestamp": 1, "observed_at": 1, "tick": 0},
    ],
)
def test_corrupt_light_check_record_is_refused(tmp_path: Path, light: dict) -> None:
    store = StateStore(tmp_path / "state.json")
    state = LoopState("live")
    state.last_light_check = light
    with pytest.raises(ValueError):
        store.save(state)
