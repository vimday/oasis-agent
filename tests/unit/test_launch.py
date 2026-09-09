"""Economic boundaries and fail-closed retained-launch integration tests."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from oasis_agent.launch.approvals import Approval, approval_status
from oasis_agent.launch.entry_price import below_fdv, minimum_received
from oasis_agent.launch.models import Cycle, Observation, Quote, Snapshot, Target
from oasis_agent.launch.quotes import matrix_reasons
from oasis_agent.launch.watcher import CandidateWatcher, PoolEvent
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.server.app import create_app
from oasis_agent.server.config import load_server_config
from oasis_agent.shadow.sniper_runner import SniperJournal, evaluate, read_records

CA = Target().target_ca
H = "0x" + "1" * 64
D = "2" * 64
POOL = "0x" + "3" * 40
OTHER = "0x" + "0" * 39 + "1"


def target() -> Target:
    return Target(
        runtime_hash=H,
        control_baseline=D,
        token_decimals=18,
        max_age_seconds=60,
        max_age_blocks=5,
        max_impact_bps=100,
        buy_tranches_wei=(10**17, 2 * 10**17),
    )


def cycle() -> Cycle:
    quotes = tuple(
        Quote(spend_usd_micros=usd, native_wei=wei, received_base_units=usd * 10**13)
        for usd, wei in ((330_000_000, 10**17), (660_000_000, 2 * 10**17))
    )
    observation = Observation(
        snapshot=Snapshot(
            chain_id=8453, number=100, timestamp=1000, block_hash=H, parent_hash="0x" + "0" * 64
        ),
        source="provider_a",
        retrieved_at=1001,
        raw_evidence_digest=D,
        target_ca=CA,
        runtime_hash=H,
        token_decimals=18,
        control_digest=D,
        pool=POOL,
        token0=OTHER,
        token1=CA,
        quotes=quotes,
    )
    return Cycle(
        observations=(observation, observation.model_copy(update={"source": "provider_b"})),
        evaluated_at=1002,
        current_block=100,
    )


def test_strict_fdv_exact_boundary() -> None:
    kwargs = {
        "native_wei": 10**18,
        "eth_usd_micros": 500_000_000,
        "token_decimals": 18,
        "quoted_base_units": 2500 * 10**18,
        "slippage_bps": 1000,
    }
    assert minimum_received(**kwargs) == 2500 * 10**18 + 1
    assert not below_fdv(
        spend_usd_micros=500_000_000, received_base_units=2500 * 10**18, token_decimals=18
    )
    assert below_fdv(
        spend_usd_micros=500_000_000,
        received_base_units=minimum_received(**kwargs),
        token_decimals=18,
    )


@pytest.mark.parametrize("decimals", [0, 6, 9, 18, 36])
def test_minimum_is_strict_smallest_integer_and_monotone(decimals: int) -> None:
    previous = 0
    for spend in range(1, 90):
        result = minimum_received(
            native_wei=spend * 10**15,
            eth_usd_micros=3000_000_001,
            token_decimals=decimals,
            quoted_base_units=1,
            slippage_bps=9999,
        )
        n = spend * 10**15 * 3000_000_001 * 10**9 * 10**decimals
        d = 10**18 * 200_000_000_000_000
        assert (result - 1) * d <= n < result * d
        assert result >= previous
        previous = result


@pytest.mark.parametrize("invalid", [True, 1.5, -1, 2**256])
def test_price_rejects_invalid_amount(invalid: int) -> None:
    with pytest.raises(ValueError):
        minimum_received(
            native_wei=invalid,
            eth_usd_micros=1,
            token_decimals=18,
            quoted_base_units=1,
            slippage_bps=1,
        )


def test_slippage_floor_can_be_stronger() -> None:
    assert (
        minimum_received(
            native_wei=10**18,
            eth_usd_micros=500_000_000,
            token_decimals=0,
            quoted_base_units=5000,
            slippage_bps=100,
        )
        == 4950
    )


def test_strict_import_does_not_coerce_or_accept_extra_flags() -> None:
    payload = cycle().model_dump(mode="json")
    for invalid in (True, "100", 100.0):
        payload["current_block"] = invalid
        with pytest.raises(ValidationError):
            Cycle.model_validate_json(json.dumps(payload))
    with pytest.raises(ValidationError):
        Target.model_validate_json('{"execution_eligible":true}')


def test_complete_grid_and_unknown_proofs_fail_closed() -> None:
    c = cycle()
    assert matrix_reasons(c.observations, target()) == []
    result = evaluate(c, target())
    assert result["execution_eligible"] is False
    assert result["decision"] == "reject"
    assert all(not gate["passed"] for gate in result["gates"].values())
    missing = c.observations[0].model_copy(update={"quotes": c.observations[0].quotes[:1]})
    assert "quote_grid_incomplete_or_duplicate" in matrix_reasons(
        (missing, c.observations[1]), target()
    )
    assert matrix_reasons((c.observations[0],) * 2, target()) == ["two_distinct_sources_required"]


def test_cross_block_and_control_change_stand_down() -> None:
    c = cycle()
    changed = c.observations[1].model_copy(update={"control_digest": "4" * 64})
    result = evaluate(c.model_copy(update={"observations": (c.observations[0], changed)}), target())
    assert "control_baseline_mismatch" in result["stand_down_reasons"]
    assert result["decision"] == "stand_down"
    stale = evaluate(c.model_copy(update={"evaluated_at": 2000}), target())
    assert "stale_or_future_snapshot" in stale["stand_down_reasons"]
    assert (
        evaluate(Cycle(observations=(), evaluated_at=0, current_block=0), Target())["decision"]
        == "stand_down"
    )


def test_journal_restart_idempotence_config_lock_and_corruption(tmp_path: Path) -> None:
    path = tmp_path / "launch.db"
    journal = SniperJournal(path, target())
    first = journal.append(cycle())
    assert journal.append(cycle()) == first
    journal.close()
    journal = SniperJournal(path, target())
    assert journal.append(cycle()) == first
    journal.close()
    with pytest.raises(ValueError, match="code/config"):
        SniperJournal(path, Target())
    with sqlite3.connect(path) as db:
        db.execute("UPDATE launch_decisions SET digest=?", ("f" * 64,))
    with pytest.raises(ValueError, match="corrupted"):
        SniperJournal(path, target())


def test_stand_down_latches_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "launch.db"
    j = SniperJournal(path, target())
    j.append(cycle().model_copy(update={"evaluated_at": 2000}))
    j.close()
    j = SniperJournal(path, target())
    result = j.append(cycle())
    assert result["decision"] == "stand_down"
    assert "stand_down_latched" in result["stand_down_reasons"]
    assert len(read_records(j.db)) == 2
    j.close()


def event() -> PoolEvent:
    return PoolEvent(
        chain_id=8453,
        source="a",
        factory=OTHER,
        pool=POOL,
        block_hash=H,
        transaction_hash=H,
        log_index=0,
        phase="pending",
        kind="created",
        removed=False,
    )


def test_watcher_retains_providers_log_indices_and_final_separately() -> None:
    w = CandidateWatcher()
    e = event()
    assert w.ingest(e)
    assert not w.ingest(e.model_copy(update={"source": "b"}))
    assert len(next(iter(w.events.values()))) == 2
    assert w.ingest(e.model_copy(update={"log_index": 1, "kind": "liquidity_added"}))
    assert w.ingest(e.model_copy(update={"phase": "final"}))
    assert not w.ingest(e.model_copy(update={"removed": True}))
    assert w.stand_down


def test_dedup_eviction_requires_reconciliation() -> None:
    w = CandidateWatcher(capacity=1)
    assert w.ingest(event())
    assert not w.ingest(event().model_copy(update={"log_index": 1}))
    assert w.stand_down


def test_plain_erc20_approval_is_not_expiring() -> None:
    a = Approval(
        owner=OTHER, spender=POOL, amount=100, expiration=2000, revoked=False, mechanism="erc20"
    )
    kwargs = {"now": 1000, "required": 100, "maximum": 110, "owner": OTHER, "spender": POOL}
    assert approval_status(a, **kwargs) == "expiry_not_enforced"
    a = a.model_copy(update={"mechanism": "permit2"})
    assert approval_status(a, **kwargs) == "ready_observed"
    assert approval_status(a.model_copy(update={"expiration": 1000}), **kwargs) == "expired"
    assert approval_status(a.model_copy(update={"amount": 111}), **kwargs) == "amount_out_of_bounds"
    assert approval_status(a.model_copy(update={"revoked": True}), **kwargs) == "revoked"


def test_readonly_api_shared_decisions_etag_and_tampering(tmp_path: Path) -> None:
    path = tmp_path / "launch.db"
    j = SniperJournal(path, target())
    expected = j.append(cycle())
    j.close()
    config = load_server_config(None, environ={})
    config = replace(
        config,
        ops_token_required=False,
        databases=replace(config.databases, launch=path, ops=tmp_path / "ops.db"),
    )
    with TestClient(create_app(config, environ={})) as client:
        for name in ("targets", "candidates", "quotes", "decisions", "approvals"):
            url = "/api/v1/launch/" + name
            response = client.get(url)
            assert response.status_code == 200
            assert response.json()["execution_eligible"] is False
            assert (
                client.get(url, headers={"If-None-Match": response.headers["etag"]}).status_code
                == 304
            )
            assert client.post(url).status_code == 405
        assert client.get("/api/v1/launch/decisions").json()["items"] == [expected]
        assert client.get("/api/v1/decisions?strategy=launch").json()["items"] == [expected]
        with sqlite3.connect(path) as db:
            db.execute("UPDATE launch_decisions SET digest=?", ("a" * 64,))
        assert client.get("/api/v1/launch/quotes").status_code == 503


def test_unconfigured_api_is_not_live_or_ready(tmp_path: Path) -> None:
    config = load_server_config(None, environ={})
    config = replace(
        config,
        ops_token_required=False,
        databases=replace(config.databases, ops=tmp_path / "ops.db"),
    )
    with TestClient(create_app(config, environ={})) as client:
        assert client.get("/api/v1/launch/targets").json()["status"] == "unconfigured"
        assert client.get("/api/v1/launch/approvals").json()["items"][0]["status"] == "unresolved"


def test_websocket_includes_launch_decisions(tmp_path: Path) -> None:
    path = tmp_path / "launch.db"
    journal = SniperJournal(path, target())
    expected = journal.append(cycle())
    journal.close()
    config = load_server_config(None, environ={})
    config = replace(
        config,
        ops_token_required=False,
        databases=replace(config.databases, launch=path, ops=tmp_path / "ops.db"),
    )
    with (
        TestClient(create_app(config, environ={})) as client,
        client.websocket_connect("/api/v1/stream") as ws,
    ):
        assert ws.receive_json()["type"] == "heartbeat"
        event = ws.receive_json()
        assert event["type"] == "launch_decisions"
        assert event["items"] == [expected]


@pytest.mark.parametrize(
    "raw",
    [
        '{"chain_id":8453.0}',
        '{"principal_limit_usd_micros":500000000.0}',
        '{"chain_id":8453,"chain_id":8453}',
    ],
)
def test_literal_floats_and_duplicate_json_keys_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        Target.from_json(raw)


def test_rehashed_forged_gate_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "launch.db"
    journal = SniperJournal(path, target())
    record = journal.append(cycle())
    journal.close()
    record.pop("digest")
    record["gates"]["G8"] = {"passed": True, "reasons": []}
    raw = canonical_json(record)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE launch_decisions SET payload=?, digest=?", (raw, sha256_hex(raw)))
    with pytest.raises(ValueError, match="does not match"):
        SniperJournal(path, target())


def test_nonobject_journal_payload_is_a_controlled_integrity_error(tmp_path: Path) -> None:
    path = tmp_path / "launch.db"
    journal = SniperJournal(path, target())
    journal.append(cycle())
    with journal.db:
        journal.db.execute("UPDATE launch_decisions SET payload=?", (b"[]",))
    with pytest.raises(ValueError, match="must be an object"):
        read_records(journal.db)
    journal.close()
