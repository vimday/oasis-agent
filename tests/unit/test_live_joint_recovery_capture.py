"""Joint same-block discovery over synthetic RPC and real disposable SQLite only."""

import json
import sys
import time
from dataclasses import asdict, replace

import pytest

from oasis_agent.live.gas_oracle import IMPLEMENTATION_SLOT, LEGACY_PIN_NAMES, ORACLE
from oasis_agent.live.state import StateStore
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import RpcBudget
from scripts import capture_live_recovery as cli
from tests.unit import test_live_recovery_capture as fixtures
from tests.unit.gas_oracle_fixtures import synthetic_gas_pins
from tests.unit.test_live_valuation import synthetic_feed_pin  # noqa: F401

CONFIG = replace(
    fixtures.CONFIG,
    expected_pins={k: v for k, v in fixtures.CONFIG.expected_pins.items() if k in LEGACY_PIN_NAMES},
)
IMPLEMENTATION = "0x" + "ab" * 20


class JointPoster:
    def __init__(self, base, fault=None):
        self.base, self.fault, self.requests = base, fault, []

    def post(self, payload):
        request = json.loads(payload)
        self.requests.append(request)
        method, params = request["method"], request["params"]
        if method == "eth_getStorageAt" and params[0] == ORACLE:
            assert params[1] == IMPLEMENTATION_SLOT
            result = "0x" + "00" * 12 + "ab" * 20
        elif method == "eth_getCode" and params[0] in {ORACLE, IMPLEMENTATION}:
            result = "0x6000"
        else:
            result = json.loads(self.base.post(payload))["result"]
        if self.fault:
            result = self.fault(request, result, len(self.requests))
        return canonical_json({"jsonrpc": "2.0", "id": request["id"], "result": result})


def setup(tmp_path, monkeypatch, *, legacy=False):
    monkeypatch.setattr(fixtures, "CONFIG", CONFIG)
    roots, original, scope, posters, clock = fixtures.setup(tmp_path, monkeypatch, legacy=legacy)
    for role, root in zip(cli.ROLES, roots, strict=True):
        store = StateStore(cli.paths(root, role)[0], require_existing=True)
        state = store.load("live", read_only=True)
        if not state.halted:
            state.halted = "synthetic operator halt"
            store.save(state)
    with cli.prepared(*roots, CONFIG, fixtures.PROFILE, discover_gas_oracle=True) as prepared:
        plan = prepared[0]
    return roots, plan, scope, {u: JointPoster(p) for u, p in posters.items()}, clock, original


def run(tmp_path, data, *, approved=None):
    roots, plan, scope, posters, clock, _ = data
    return cli.capture(
        *roots,
        CONFIG,
        fixtures.PROFILE,
        tmp_path / "attempt",
        approved or plan["plan_hash"],
        scope,
        discover_gas_oracle=True,
        transport=lambda url, _: posters[url],
        clock=clock.wall,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


def snapshot(roots):
    return {p: (p.stat().st_ino, p.read_bytes()) for root in roots for p in root.iterdir()}


def publications(tmp_path, data, result):
    return cli._runtime_publications(tmp_path / "attempt", result, CONFIG)


def test_joint_plan_has_new_exact_scope_and_reads_no_endpoint(tmp_path, monkeypatch):
    roots, plan, _, posters, _, original = setup(tmp_path, monkeypatch)
    before = snapshot(roots)
    for env in cli.RPC_ENV:
        monkeypatch.delenv(env)
    with cli.prepared(*roots, CONFIG, fixtures.PROFILE, discover_gas_oracle=True) as prepared:
        assert prepared[0] == plan
    assert snapshot(roots) == before and all(not p.requests for p in posters.values())
    assert plan["schema"] == "oasis.live-runtime-recovery-capture-plan.v1"
    assert plan["plan_hash"] != original["plan_hash"] and plan["total_requests"] == 209
    assert plan["request_limits"] == {
        "strategy": dict(zip(cli.SOURCES, (56, 55), strict=True)),
        "authority": dict.fromkeys(cli.SOURCES, 49),
    }
    assert plan["gas_reads_per_source"] == {
        "eth_chainId": 1,
        "eth_getBlockByNumber": 2,
        "eth_getCode": 2,
        "eth_getStorageAt": 1,
    }
    assert plan["runtime_approved"] is False and plan["configuration_applied"] is False
    assert plan["max_seconds"] == 120 and plan["retries"] == 0


@pytest.mark.parametrize("legacy", [False, True])
def test_complete_capture_replays_shared_head_and_keeps_old_journals(tmp_path, monkeypatch, legacy):
    data = setup(tmp_path, monkeypatch, legacy=legacy)
    roots, plan, _, posters, clock, _ = data
    before = fixtures.unchanged_files(roots)
    monkeypatch.setattr(StateStore, "save", lambda *_: pytest.fail("capture wrote journal/state"))
    result = run(tmp_path, data)
    assert result["capture_passed"] and result["exit_code"] == 0, result
    assert result["request_counts"] == plan["request_limits"]
    assert sum(len(p.requests) for p in posters.values()) == 209
    assert (
        sum(
            r["method"] == "eth_getBlockByNumber" and r["params"][0] == "latest"
            for p in posters.values()
            for r in p.requests
        )
        == 1
    )
    monkeypatch.setattr(time, "time", clock.wall)
    output = publications(tmp_path, data, result)
    assert set(output) == set(cli.RUNTIME_PUBLICATIONS)
    review = json.loads(output["runtime-review.json"])
    assert review["review_passed"] and review["block"] == result["head"]
    assert review["rpc_requests"] == 0
    for flag in (
        "execution_eligible",
        "runtime_approved",
        "migration_applied",
        "state_write_supported",
    ):
        assert review[flag] is False
    assert review["target_config_hash"] == result["target_config_hash"]
    candidate = json.loads((tmp_path / "attempt/gas-candidate.json").read_bytes())
    assert candidate["source_config_hash"] == sha256_hex(canonical_json(asdict(CONFIG)))
    assert len(candidate["candidate_config"]["expected_pins"]) == 8
    assert fixtures.unchanged_files(roots) == before
    for role, root in zip(cli.ROLES, roots, strict=True):
        with RpcBudget(
            root / "live.receipt-budget.db", fixtures.PROFILE.policies(cli.SOURCES), read_only=True
        ) as budget:
            assert budget.counts() == {s: 1 + plan["request_limits"][role][s] for s in cli.SOURCES}
    assert not (tmp_path / "attempt/runtime-references.json").exists()


def test_old_197_grant_is_refused_before_first_request(tmp_path, monkeypatch):
    data = setup(tmp_path, monkeypatch)
    before = snapshot(data[0])
    result = run(tmp_path, data, approved=data[5]["plan_hash"])
    assert not result["capture_passed"] and result["request_counts"] is None
    assert all(not p.requests for p in data[3].values()) and snapshot(data[0]) == before


@pytest.mark.parametrize(
    "fault,source,count",
    [
        ("chain", 0, 2),
        ("parent", 0, 3),
        ("code", 0, 4),
        ("slot", 0, 5),
        ("implementation", 0, 6),
        ("closing", 0, 7),
        ("disagreement", 1, 3),
    ],
)
def test_first_bad_gas_read_stops_without_nav_or_retry(tmp_path, monkeypatch, fault, source, count):
    data = setup(tmp_path, monkeypatch)
    poster = list(data[3].values())[source]

    def corrupt(request, result, sequence):
        if sequence != count:
            return result
        if fault == "chain":
            return "0x1"
        if fault in {"parent", "closing"}:
            return result | {"parentHash": "0x" + "99" * 32}
        if fault == "slot":
            return "0x" + "00" * 32
        if fault == "disagreement":
            return "0x6001"
        return "0x"

    poster.fault = corrupt
    result = run(tmp_path, data)
    assert not result["capture_passed"] and len(poster.requests) == count
    assert result["request_counts"]["authority"] == dict.fromkeys(cli.SOURCES, 0)
    assert not (tmp_path / "attempt/candidate-runtime-references.json").exists()
    assert not (tmp_path / "attempt/gas-evidence.db").exists()


def test_expired_or_tampered_output_never_publishes(tmp_path, monkeypatch):
    data = setup(tmp_path, monkeypatch)
    result = run(tmp_path, data)
    assert result["capture_passed"]
    monkeypatch.setattr(time, "time", lambda: data[4].wall() + 121)
    with pytest.raises(ValueError, match=r"stale|fresh"):
        publications(tmp_path, data, result)
    monkeypatch.setattr(time, "time", data[4].wall)
    path = tmp_path / "attempt/gas-candidate.json"
    candidate = json.loads(path.read_bytes())
    candidate["candidate_config"]["gas_reserve_wei"] += 1
    path.write_bytes(canonical_json(candidate))
    with pytest.raises(ValueError):
        publications(tmp_path, data, result)


def test_joint_mode_requires_halted_old_policy_before_any_request(tmp_path, monkeypatch):
    data = setup(tmp_path, monkeypatch)
    store = StateStore(cli.paths(data[0][1], "authority")[0], require_existing=True)
    state = store.load("live", read_only=True)
    state.halted = None
    store.save(state)
    with (
        pytest.raises(ValueError, match="halted"),
        cli.prepared(*data[0], CONFIG, fixtures.PROFILE, discover_gas_oracle=True),
    ):
        pytest.fail("unhalted joint mode admitted")
    target = replace(CONFIG, expected_pins=CONFIG.expected_pins | synthetic_gas_pins())
    with (
        pytest.raises(ValueError, match="five-pin"),
        cli.prepared(*data[0], target, fixtures.PROFILE, discover_gas_oracle=True),
    ):
        pytest.fail("undeclared target admitted")
    assert all(not p.requests for p in data[3].values())


def test_gas_and_nav_parent_mismatch_cannot_form_one_capture(tmp_path, monkeypatch):
    data = setup(tmp_path, monkeypatch)
    poster = next(iter(data[3].values()))

    def corrupt(request, result, sequence):
        if sequence > 7 and request["method"] == "eth_getBlockByNumber":
            return result | {"parentHash": "0x" + "99" * 32}
        return result

    poster.fault = corrupt
    result = run(tmp_path, data)
    assert not result["capture_passed"]
    assert result["request_counts"]["authority"] == dict.fromkeys(cli.SOURCES, 0)
    assert not (tmp_path / "attempt/candidate-runtime-references.json").exists()


def test_one_joint_deadline_includes_gas_discovery(tmp_path, monkeypatch):
    data = setup(tmp_path, monkeypatch)
    poster = next(iter(data[3].values()))

    def expire(_, result, sequence):
        if sequence == 6:
            data[4].sleep(121)
        return result

    poster.fault = expire
    result = run(tmp_path, data)
    assert not result["capture_passed"] and len(poster.requests) == 6
    assert result["request_counts"]["authority"] == dict.fromkeys(cli.SOURCES, 0)
    assert not (tmp_path / "attempt/gas-evidence.db").exists()


def test_supervisor_publishes_only_after_joint_parent_review(tmp_path, monkeypatch):
    data = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(time, "time", data[4].wall)
    out = tmp_path / "attempt"
    result = cli.supervise(
        lambda: run(tmp_path, data),
        out,
        publisher=lambda directory, worker: cli._runtime_publications(directory, worker, CONFIG),
        publication_names=cli.RUNTIME_PUBLICATIONS,
    )
    assert result["capture_passed"] and result["worker_exit_code"] == 0, result
    assert all((out / name).is_file() for name in cli.RUNTIME_PUBLICATIONS)
    assert json.loads((out / "runtime-review.json").read_bytes())["execution_eligible"] is False


def test_cli_selects_separate_offline_scope_and_never_reads_endpoint(tmp_path, monkeypatch, capsys):
    data = setup(tmp_path, monkeypatch)
    config, profile = tmp_path / "config.json", tmp_path / "profile.json"
    config.write_bytes(canonical_json(asdict(CONFIG)))
    profile.write_bytes(canonical_json(asdict(fixtures.PROFILE)))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "capture_live_recovery",
            "--strategy-dir",
            str(data[0][0]),
            "--authority-dir",
            str(data[0][1]),
            "--config",
            str(config),
            "--rpc-profile",
            str(profile),
            "--discover-gas-oracle",
        ],
    )
    for env in cli.RPC_ENV:
        monkeypatch.delenv(env)
    monkeypatch.setattr(cli, "capture", lambda *_: pytest.fail("offline CLI acquired data"))
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 0
    assert json.loads(capsys.readouterr().out)["plan_hash"] == data[1]["plan_hash"]
