"""CLI capture: authentic historical prices, synthetic wire, real temporary quota ledger."""

import itertools
import json
import sys
from contextlib import closing
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.store import ReadOnlyEvidenceStore
from oasis_agent.execution import account_capture
from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.live.signed_nav import verify_signed_nav_quorum
from oasis_agent.live.state import Position
from oasis_agent.models.common import canonical_json, evidence_ref_from_json, sha256_hex
from oasis_agent.rpc.budget import BudgetedPoster, RpcBudget
from scripts import capture_signed_nav as cli
from tests.unit.test_live_receipt_costs import position_config
from tests.unit.test_live_signed_nav import fixture
from tests.unit.test_signed_prices import FIXTURE, TIMESTAMP

POSITION = Position(123, -100, 100, 77, 0)


def setup(tmp_path, monkeypatch, *, position=False, capture=True):
    config = position_config()
    profile = LiveRpcProfile(64, 100)
    paths = {
        n: tmp_path / n for n in ("config", "prices", "position", "rpc-profile", "budget", "out")
    }
    paths["config"].write_bytes(canonical_json(asdict(config)))
    paths["prices"].write_bytes(FIXTURE.read_bytes())
    paths["position"].write_bytes(canonical_json(asdict(POSITION) if position else None))
    paths["rpc-profile"].write_bytes(canonical_json(asdict(profile)))
    with RpcBudget(paths["budget"], profile.policies(cli.SOURCES)) as ledger:
        ledger.reserve(cli.SOURCES[0], now_ms=1000)
    before = paths["budget"].read_bytes()
    argv = ["capture"]
    for key, path in paths.items():
        argv += ["--" + key, str(path)]
    if capture:
        argv.append("--capture")
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr(cli.time, "time_ns", lambda: TIMESTAMP * 1_000_000)
    monkeypatch.setenv("BASE_RPC_PRIMARY_ALT", "https://first.invalid/SECRET_A")
    monkeypatch.setenv(config.secondary_rpc_env, "https://second.invalid/SECRET_B")
    wires = {"first.invalid": fixture(), "second.invalid": fixture()}
    calls = []

    class Poster:
        def __init__(self, url, timeout):
            self.host = cli.urlsplit(url).hostname

        def post(self, raw):
            request = json.loads(raw)
            calls.append((self.host, request))
            if (
                request["method"] == "eth_getBlockByNumber"
                and request["params"] == ["latest", False]
                and hasattr(wires[self.host], "_result")
            ):
                return canonical_json(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": wires[self.host]._result("eth_getBlockByNumber"),
                    }
                )
            return wires[self.host].post(raw)

    ticks = itertools.count(2000, 1000)
    monkeypatch.setattr(account_capture, "_UrlLibPoster", Poster)
    monkeypatch.setattr(
        account_capture,
        "BudgetedPoster",
        lambda *a, **kw: BudgetedPoster(*a, **kw, clock=lambda: next(ticks)),
    )
    return paths, calls, wires, before


def run(paths, expected):
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == expected
    report = json.loads((paths["out"] / "report.json").read_bytes())
    assert report["execution_eligible"] is False
    digest = report.pop("evidence_hash")
    assert digest == sha256_hex(canonical_json(report))
    assert "SECRET" not in json.dumps(report)
    return report


@pytest.mark.parametrize("position", [False, True])
def test_capture_replays_raw_pair_and_preserves_cumulative_quota(tmp_path, monkeypatch, position):
    paths, calls, _, _ = setup(tmp_path, monkeypatch, position=position)
    report = run(paths, 0)
    count = 26 if position else 15
    expected = dict(zip(cli.SOURCES, (count + 1, count), strict=True))
    assert report["request_attempts"] == report["request_limits"] == expected
    assert len(calls) == 2 * count + 1
    assert report["request_counts_before"] == {cli.SOURCES[0]: 1, cli.SOURCES[1]: 0}
    assert report["request_counts_after"] == {cli.SOURCES[0]: count + 2, cli.SOURCES[1]: count}
    assert report["candidate_verified"]
    request = json.loads((paths["out"] / "check-input.json").read_bytes())
    with closing(ReadOnlyEvidenceStore(paths["out"] / "evidence.db")) as store:
        result = verify_signed_nav_quorum(
            store,
            position_config(),
            tuple(evidence_ref_from_json(r) for r in request["evidence"]),
            sources=cli.SOURCES,
            position=POSITION if position else None,
            now_ms=TIMESTAMP,
        )
    assert result == report["result"]
    assert not any(
        "send" in call["method"].lower() or "trace" in call["method"].lower() for _, call in calls
    )


def test_default_plan_has_no_rpc_or_budget_mutation(tmp_path, monkeypatch):
    paths, calls, _, before = setup(tmp_path, monkeypatch, capture=False)
    monkeypatch.delenv("BASE_RPC_PRIMARY_ALT")
    report = run(paths, 0)
    assert report["mode"] == "offline_plan" and not report["candidate_verified"]
    assert not calls and paths["budget"].read_bytes() == before
    assert not (paths["out"] / "evidence.db").exists()


@pytest.mark.parametrize(
    "fault",
    [
        "stale",
        "tampered",
        "missing_budget",
        "policy_change",
        "quota",
        "bad_position",
        "duplicate",
        "symlink",
        "oversize",
        "missing_env",
        "same_host",
    ],
)
def test_local_refusals_spend_nothing(tmp_path, monkeypatch, fault):
    paths, calls, _, _ = setup(tmp_path, monkeypatch)
    if fault == "stale":
        monkeypatch.setattr(cli.time, "time_ns", lambda: (TIMESTAMP + 1800001) * 1_000_000)
    elif fault == "tampered":
        raw = json.loads(paths["prices"].read_bytes())
        raw["USDC"][0]["dataPoints"][0]["value"] = 2
        paths["prices"].write_text(json.dumps(raw))
    elif fault == "missing_budget":
        paths["budget"].unlink()
    elif fault == "policy_change":
        paths["rpc-profile"].write_text('{"max_requests":63,"min_interval_ms":100}')
    elif fault == "quota":
        with RpcBudget(paths["budget"], LiveRpcProfile(64, 100).policies(cli.SOURCES)) as ledger:
            for i in range(49):
                ledger.reserve(cli.SOURCES[0], now_ms=2000 + i * 100)
    elif fault == "bad_position":
        paths["position"].write_bytes(canonical_json(asdict(POSITION) | {"opened_at": False}))
    elif fault == "duplicate":
        paths["rpc-profile"].write_text(
            '{"max_requests":64,"max_requests":64,"min_interval_ms":100}'
        )
    elif fault == "symlink":
        paths["prices"].unlink()
        paths["prices"].symlink_to(FIXTURE.resolve())
    elif fault == "oversize":
        paths["prices"].write_bytes(b" " * (cli.MAX_RAW_BYTES + 1))
    elif fault == "missing_env":
        monkeypatch.delenv("BASE_RPC_PRIMARY_ALT")
    else:
        monkeypatch.setenv("BASE_RPC_PRIMARY_ALT", "https://second.invalid/OTHER_SECRET")
    before = paths["budget"].read_bytes() if paths["budget"].exists() else None
    report = run(paths, 3)
    assert not calls and not report["candidate_verified"]
    assert (paths["budget"].read_bytes() if paths["budget"].exists() else None) == before
    assert not (paths["out"] / "check-input.json").exists()


@pytest.mark.parametrize(
    "fault", ["head_stale", "head_future", "disagree", "transport", "expire", "regress"]
)
def test_partial_capture_never_publishes_accepted_pair(tmp_path, monkeypatch, fault):
    paths, calls, wires, _ = setup(tmp_path, monkeypatch)
    if fault.startswith("head_"):
        wires["first.invalid"] = replace(
            fixture(), timestamp=TIMESTAMP // 1000 + (-121 if fault == "head_stale" else 1)
        )
    elif fault == "disagree":
        wires["second.invalid"] = fixture(token_after=2001)
    elif fault == "transport":

        class Broken:
            def post(self, raw):
                raise RuntimeError("SECRET transport failed")

        wires["first.invalid"] = Broken()
    else:

        def clock():
            return (
                (TIMESTAMP + (120001 if fault == "expire" else -1)) * 1_000_000
                if len(calls) >= 1
                else TIMESTAMP * 1_000_000
            )

        monkeypatch.setattr(cli.time, "time_ns", clock)
    report = run(paths, 3)
    assert not report["candidate_verified"]
    assert report["refusal_stage"] == ("head_capture" if fault == "transport" else "nav_capture")
    assert not (paths["out"] / "check-input.json").exists()
    if fault != "disagree":
        assert len(calls) == 1  # No retry and no second lookup after failure/expiry.
    assert report["request_counts_after"][cli.SOURCES[0]] >= 2


def test_existing_output_is_not_reused(tmp_path, monkeypatch):
    paths, calls, _, before = setup(tmp_path, monkeypatch)
    paths["out"].mkdir()
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert not calls and paths["budget"].read_bytes() == before


def test_historical_fixture_refuses_under_actual_host_clock(tmp_path, monkeypatch):
    real_clock = cli.time.time_ns
    paths, calls, _, before = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.time, "time_ns", real_clock)
    assert run(paths, 3)["refusal_stage"] == "price_verification"
    assert not calls and paths["budget"].read_bytes() == before


def test_budget_disappearing_after_head_is_never_recreated(tmp_path, monkeypatch):
    paths, calls, _, _ = setup(tmp_path, monkeypatch)
    original = cli.capture_signed_nav_candidate

    def capture(*args, **kwargs):
        paths["budget"].unlink()
        return original(*args, **kwargs)

    monkeypatch.setattr(cli, "capture_signed_nav_candidate", capture)
    report = run(paths, 3)
    assert report["refusal_stage"] == "final_budget"
    assert not paths["budget"].exists() and len(calls) == 1
    assert not (paths["out"] / "check-input.json").exists()


@pytest.mark.parametrize("offset", [120001, -1])
def test_clock_change_during_final_replay_refuses_publication(tmp_path, monkeypatch, offset):
    paths, calls, _, _ = setup(tmp_path, monkeypatch)
    original = cli.verify_signed_nav_quorum

    def verify(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setattr(cli.time, "time_ns", lambda: (TIMESTAMP + offset) * 1_000_000)
        return result

    monkeypatch.setattr(cli, "verify_signed_nav_quorum", verify)
    report = run(paths, 3)
    assert report["refusal_stage"] == "current_clock_replay" and len(calls) == 31
    assert not (paths["out"] / "check-input.json").exists()


def test_new_output_reuses_spent_quota(tmp_path, monkeypatch):
    paths, calls, _, _ = setup(tmp_path, monkeypatch)
    first = run(paths, 0)
    new_output = tmp_path / "second-output"
    argv = list(sys.argv)
    argv[argv.index("--out") + 1] = str(new_output)
    monkeypatch.setattr("sys.argv", argv)
    paths["out"] = new_output
    second = run(paths, 0)
    assert second["request_counts_before"] == first["request_counts_after"]
    assert second["request_counts_after"] == {cli.SOURCES[0]: 33, cli.SOURCES[1]: 30}
    assert len(calls) == 62
