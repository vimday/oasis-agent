"""Thirteen synthetic reads, original SQLite budgets, and a real process boundary."""

import json
import os
import sqlite3
import sys
import time
from dataclasses import asdict, replace
from urllib.error import HTTPError

import pytest

from oasis_agent.live.gas_oracle import LEGACY_PIN_NAMES, ORACLE
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import RpcBudget
from scripts import capture_live_gas_oracle as cli
from scripts import capture_live_recovery as recovery
from tests.unit import test_live_recovery_capture as fixtures
from tests.unit.test_live_valuation import synthetic_feed_pin  # noqa: F401

CONFIG = replace(
    fixtures.CONFIG,
    expected_pins={k: v for k, v in fixtures.CONFIG.expected_pins.items() if k in LEGACY_PIN_NAMES},
)
IMPLEMENTATION = "0x" + "ab" * 20


class Poster:
    def __init__(self, current, fault=None):
        self.current, self.fault, self.requests = current, fault, []

    def post(self, raw):
        request = json.loads(raw)
        self.requests.append(request)
        method, params = request["method"], request["params"]
        if method == "eth_getBlockByNumber":
            value = self.current._result(method)
        elif method == "eth_chainId":
            value = "0x2105"
        elif method == "eth_getStorageAt":
            value = "0x" + "00" * 12 + "ab" * 20
        else:
            assert method == "eth_getCode" and params[0] in {ORACLE, IMPLEMENTATION}
            value = "0x6000"
        if self.fault:
            value = self.fault(request, value, len(self.requests))
        return canonical_json({"jsonrpc": "2.0", "id": request["id"], "result": value})


def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(fixtures, "CONFIG", CONFIG)
    roots, original, scope, posters, clock = fixtures.setup(tmp_path, monkeypatch)
    with cli.prepared(*roots, CONFIG, fixtures.PROFILE) as data:
        plan = data[0]
    providers = {url: Poster(poster.current) for url, poster in posters.items()}
    return roots, plan, scope, providers, clock, original


def run(tmp_path, data, *, approved=None):
    roots, plan, scope, providers, clock, _ = data
    return cli.capture(
        *roots,
        CONFIG,
        fixtures.PROFILE,
        tmp_path / "attempt",
        approved or plan["plan_hash"],
        scope,
        transport=lambda url, _: providers[url],
        clock=clock.wall,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


def snapshot(roots, *, budgets=True):
    return {
        str(path): (path.stat().st_ino, sha256_hex(path.read_bytes()))
        for root in roots
        for path in root.iterdir()
        if budgets or path.name != "live.receipt-budget.db"
    }


def test_default_plan_is_offline_distinct_and_does_not_reuse_nav_grant(tmp_path, monkeypatch):
    data = setup(tmp_path, monkeypatch)
    roots, plan, _, providers, _, original = data
    before = snapshot(roots)
    for env in recovery.RPC_ENV:
        monkeypatch.delenv(env)
    with cli.prepared(*roots, CONFIG, fixtures.PROFILE) as actual:
        assert actual[0] == plan
    assert snapshot(roots) == before and all(not p.requests for p in providers.values())
    assert plan["total_requests"] == 13 and plan["max_seconds"] == 120 and plan["retries"] == 0
    assert plan["request_limits"] == {
        "strategy": dict(zip(recovery.SOURCES, (7, 6), strict=True)),
        "authority": dict.fromkeys(recovery.SOURCES, 0),
    }
    assert plan["original_recovery_plan_hash"] == original["plan_hash"]
    assert plan["plan_hash"] != original["plan_hash"]
    assert plan["runtime_approved"] is False and plan["configuration_applied"] is False


def test_capture_preserves_originals_and_publishes_only_replayable_observation(
    tmp_path, monkeypatch
):
    data = setup(tmp_path, monkeypatch)
    roots, plan, _, providers, clock, _ = data
    before = snapshot(roots, budgets=False)
    authority_budget = (roots[1] / "live.receipt-budget.db").read_bytes()
    result = run(tmp_path, data)
    assert result["capture_passed"] and result["exit_code"] == 0, result
    assert result["request_counts"] == plan["request_limits"]
    assert [len(p.requests) for p in providers.values()] == [7, 6]
    assert snapshot(roots, budgets=False) == before
    assert (roots[1] / "live.receipt-budget.db").read_bytes() == authority_budget
    with RpcBudget(
        roots[0] / "live.receipt-budget.db",
        fixtures.PROFILE.policies(recovery.SOURCES),
        read_only=True,
    ) as q:
        assert q.counts() == dict(zip(recovery.SOURCES, (8, 7), strict=True))
    out = tmp_path / "attempt"
    assert not any((out / name).exists() for name in cli.PUBLICATIONS)
    monkeypatch.setattr(cli.time, "time", clock.wall)
    published = cli.publications(out, result, CONFIG)
    candidate = json.loads(published["candidate.json"])
    assert not candidate["runtime_approved"] and not candidate["execution_eligible"]
    assert candidate["candidate_config"]["expected_pins"]["gas_oracle_implementation"].endswith(
        "ab" * 20
    )
    wire = json.loads(published["gas-wire.json"])
    for provider in wire["providers"]:
        calls = [json.loads(bytes.fromhex(e["payload"])) for e in provider["request"]["exchanges"]]
        assert calls[4]["params"][0] == IMPLEMENTATION
        assert all(
            c["params"][-1] == {"blockHash": wire["block"]["block_hash"], "requireCanonical": True}
            for c in calls[2:5]
        )
    assert not (out / "fresh-nav.json").exists() and not (out / "references.json").exists()


def test_old_full_recovery_plan_cannot_authorize_gas_discovery(tmp_path, monkeypatch):
    data = setup(tmp_path, monkeypatch)
    before = snapshot(data[0])
    result = run(tmp_path, data, approved=data[-1]["plan_hash"])
    assert not result["capture_passed"] and result["request_counts"] is None
    assert snapshot(data[0]) == before and all(not p.requests for p in data[3].values())


@pytest.mark.parametrize("fault", ["paid", "expired", "endpoint", "same_host"])
def test_invalid_cost_or_endpoint_scope_spends_nothing(tmp_path, monkeypatch, fault):
    data = setup(tmp_path, monkeypatch)
    scope = json.loads(data[2].read_bytes())
    if fault == "paid":
        scope["declared_cost_usd"] = "0.01"
    elif fault == "expired":
        scope["valid_until_ms"] = int(data[4].wall() * 1000)
    elif fault == "endpoint":
        scope["endpoint_hashes"][recovery.SOURCES[0]] = "f" * 64
    else:
        monkeypatch.setenv(recovery.RPC_ENV[1], fixtures.URLS[recovery.SOURCES[0]])
    data[2].write_bytes(canonical_json(scope))
    before = snapshot(data[0])
    result = run(tmp_path, data)
    assert not result["capture_passed"] and all(not p.requests for p in data[3].values())
    assert snapshot(data[0]) == before


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("http", 1),
        ("stale", 1),
        ("chain", 2),
        ("header", 3),
        ("empty_proxy", 4),
        ("slot", 5),
        ("empty_impl", 6),
        ("closing", 7),
        ("secondary_proxy", 10),
    ],
)
def test_failure_stops_without_retry_or_quorum_publication(tmp_path, monkeypatch, fault, expected):
    data = setup(tmp_path, monkeypatch)
    providers = list(data[3].values())
    before = snapshot(data[0], budgets=False)

    def bad(request, value, n):
        if fault == "http":
            raise HTTPError(
                "https://secret.invalid/credential", 403, "private diagnostic", {}, None
            )
        if fault == "stale" and n == 1:
            return value | {"timestamp": hex(data[4].wall().__int__() - 121)}
        if fault == "chain" and request["method"] == "eth_chainId":
            return "0x1"
        if fault == "header" and n == 3:
            return value | {"parentHash": "0x" + "12" * 32}
        if fault in {"empty_proxy", "empty_impl"} and n == (4 if fault == "empty_proxy" else 6):
            return "0x"
        if fault == "slot" and request["method"] == "eth_getStorageAt":
            return "0x" + "00" * 32
        if fault == "closing" and n == 7:
            return value | {"hash": "0x" + "12" * 32}
        return "0x6001" if fault == "secondary_proxy" and n == 3 else value

    providers[1 if fault == "secondary_proxy" else 0].fault = bad
    result = run(tmp_path, data)
    assert not result["capture_passed"] and result["exit_code"] == 3
    assert sum(len(p.requests) for p in providers) == expected
    assert snapshot(data[0], budgets=False) == before
    out = tmp_path / "attempt"
    assert not (out / "candidate-gas-wire.json").exists()
    assert not (out / "evidence.db").exists()
    assert all(not (out / name).exists() for name in cli.PUBLICATIONS)
    assert "credential" not in (out / "wire.jsonl").read_text()


@pytest.mark.parametrize("artifact", ["wire", "proposal", "report", "stored_proof"])
def test_parent_refuses_forged_worker_artifacts(tmp_path, monkeypatch, artifact):
    data = setup(tmp_path, monkeypatch)
    result = run(tmp_path, data)
    assert result["capture_passed"]
    out = tmp_path / "attempt"
    monkeypatch.setattr(cli.time, "time", data[4].wall)
    if artifact == "stored_proof":
        (out / "evidence.db").write_bytes(b"not a database")
    elif artifact == "report":
        result["runtime_approved"] = True
    elif artifact == "proposal":
        path = out / "candidate-proposal.json"
        proposal = json.loads(path.read_bytes())
        proposal["runtime_approved"] = True
        path.write_bytes(canonical_json(proposal))
        result["candidate_hash"] = sha256_hex(path.read_bytes())
    else:
        path = out / "candidate-gas-wire.json"
        wire = json.loads(path.read_bytes())
        wire["providers"][1]["source"] = "foreign"
        path.write_bytes(canonical_json(wire))
        result["wire_hash"] = sha256_hex(path.read_bytes())
    result.pop("evidence_hash")
    result["evidence_hash"] = sha256_hex(canonical_json(result))
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        cli.publications(out, result, CONFIG)
    assert all(not (out / name).exists() for name in cli.PUBLICATIONS)


def test_supervisor_runs_parent_replay_and_exports_no_nav(tmp_path, monkeypatch):
    data = setup(tmp_path, monkeypatch)
    out = tmp_path / "attempt"
    monkeypatch.setattr(cli.time, "time", data[4].wall)
    report = recovery.supervise(
        lambda: run(tmp_path, data),
        out,
        seconds=10,
        publisher=lambda directory, worker: cli.publications(directory, worker, CONFIG),
        publication_names=cli.PUBLICATIONS,
    )
    assert report["exit_code"] == 0 and report["capture_passed"], report
    assert all((out / name).exists() for name in cli.PUBLICATIONS)
    assert not (out / "references.json").exists() and not (out / "fresh-nav.json").exists()


def test_supervisor_does_not_publish_a_failed_or_overdue_discovery(tmp_path):
    out = tmp_path / "attempt"

    def stuck():
        out.mkdir()
        time.sleep(5)
        return {"capture_passed": True, "exit_code": 0}

    result = recovery.supervise(
        stuck,
        out,
        seconds=0.1,
        publisher=lambda *_: pytest.fail("late worker reached publication"),
        publication_names=cli.PUBLICATIONS,
    )
    assert result["status"] == "hard_deadline_refused"
    assert all(not (out / name).exists() for name in cli.PUBLICATIONS)


def test_overdue_parent_replay_removes_all_discovery_publications(tmp_path):
    out = tmp_path / "attempt"

    def operation():
        out.mkdir()
        (out / "report.json").write_bytes(canonical_json({"capture_passed": True, "exit_code": 0}))
        return {"capture_passed": True, "exit_code": 0}

    def slow(*_):
        time.sleep(0.15)
        return dict.fromkeys(cli.PUBLICATIONS, b"synthetic")

    report = recovery.supervise(
        operation, out, seconds=0.1, publisher=slow, publication_names=cli.PUBLICATIONS
    )
    assert not report["capture_passed"] and report["status"] == "hard_deadline_refused"
    assert all(not (out / name).exists() for name in cli.PUBLICATIONS)


@pytest.mark.parametrize("name", ["..", "../outside", "/outside", "."])
def test_supervisor_rejects_publication_escape_before_fork(tmp_path, name):
    with pytest.raises(ValueError):
        recovery.supervise(
            lambda: pytest.fail("must not fork"), tmp_path / "attempt", publication_names=(name,)
        )
    assert not (tmp_path / "attempt").exists()


def test_cli_default_plan_uses_no_network_or_environment_values(tmp_path, monkeypatch, capsys):
    data = setup(tmp_path, monkeypatch)
    config, profile = tmp_path / "config.json", tmp_path / "profile.json"
    config.write_bytes(canonical_json(asdict(CONFIG)))
    profile.write_bytes(canonical_json(asdict(fixtures.PROFILE)))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "capture_live_gas_oracle",
            "--strategy-dir",
            str(data[0][0]),
            "--authority-dir",
            str(data[0][1]),
            "--config",
            str(config),
            "--rpc-profile",
            str(profile),
        ],
    )
    for env in recovery.RPC_ENV:
        monkeypatch.delenv(env)
    monkeypatch.setattr(cli, "capture", lambda *_: pytest.fail("offline CLI attempted capture"))
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["plan_hash"] == data[1]["plan_hash"] and plan["total_requests"] == 13


@pytest.mark.parametrize("missing", ["STOP", "live.receipt-budget.db"])
def test_missing_original_prerequisite_refuses_without_initializing_replacement(
    tmp_path, monkeypatch, missing
):
    data = setup(tmp_path, monkeypatch)
    path = data[0][0] / missing
    path.unlink()
    result = run(tmp_path, data)
    assert not result["capture_passed"] and result["request_counts"] is None
    assert not path.exists() and all(not p.requests for p in data[3].values())


def test_unready_child_never_targets_an_unconfirmed_process_group(tmp_path, monkeypatch):
    def denied(*_):
        raise PermissionError("synthetic foreign process group")

    monkeypatch.setattr(os, "killpg", denied)
    monkeypatch.setattr(os, "setsid", lambda: time.sleep(0.5))
    started = time.monotonic()
    result = recovery.supervise(
        lambda: pytest.fail("unready child must not acquire"),
        tmp_path / "attempt",
        seconds=0.05,
        publisher=lambda *_: pytest.fail("unready child must not publish"),
        publication_names=cli.PUBLICATIONS,
    )
    assert not result["capture_passed"] and time.monotonic() - started < 1


def test_ready_group_cleanup_denial_cannot_publish_success(tmp_path, monkeypatch):
    def denied(*_):
        raise PermissionError("synthetic denied group cleanup")

    monkeypatch.setattr(os, "killpg", denied)
    result = recovery.supervise(
        lambda: {"capture_passed": True, "exit_code": 0},
        tmp_path / "attempt",
        seconds=1,
        publisher=lambda *_: pytest.fail("unverified cleanup must not publish"),
        publication_names=cli.PUBLICATIONS,
    )
    assert result["worker_group_ready"] is True and result["cleanup_refused"] is True
    assert result["capture_passed"] is False and result["exit_code"] == 3
