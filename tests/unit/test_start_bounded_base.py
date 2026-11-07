"""Safety coverage for the Base one-shot operator boundary; no real provider or key."""

import io
import os
import subprocess
import tarfile
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from oasis_agent.live import base_acceptance_rpc as rpc_scope
from oasis_agent.live.startup_state import StartupBlocker, refusal_report
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.reporting.live_bounded import read_bounded_run
from scripts import live_bounded_worker as worker
from scripts import start_bounded_base as entry

ATTEMPT = "a" * 32


@pytest.mark.parametrize(
    ("owner", "expected"),
    [(None, None), ("", None), (ATTEMPT, ATTEMPT), ("unexpected", ValueError)],
)
def test_project_reads_optional_docker_run_label(tmp_path, monkeypatch, owner, expected):
    identity = "b" * 64
    image = "sha256:" + "c" * 64
    operation = entry.Run(image, tmp_path)

    def docker(_name, args):
        if args[0] == "ps":
            stdout = f"{identity}\n"
        else:
            assert args[0] == "inspect" and args[-1] == identity
            label = (
                canonical_json(owner).decode()
                if '{{json (index .Config.Labels "org.oasis-agent.bounded-run")}}' in args[2]
                else owner or "<no value>"
            )
            stdout = f"{identity} exited 0 false {image} strategy {label}\n"
        return subprocess.CompletedProcess(args, 0, stdout.encode(), b"")

    monkeypatch.setattr(operation, "docker", docker)
    if expected is ValueError:
        with pytest.raises(ValueError, match="unrecognized local service"):
            operation.project()
    else:
        assert operation.project() == {
            "strategy": {
                "id": identity,
                "state": "exited",
                "exit_code": 0,
                "oom_killed": False,
                "image": image,
                "attempt": expected,
            }
        }


@pytest.fixture
def roots(tmp_path, monkeypatch):
    directories = {role: tmp_path / role for role in worker.ROLES}
    for role, path in directories.items():
        path.mkdir(mode=0o700)
        marker = path / "STOP"
        marker.write_text(role)
        marker.chmod(0o600)
    monkeypatch.setattr(worker, "ROOTS", directories)
    return directories


@pytest.mark.parametrize("fail_second_unlink", [False, True])
def test_handoff_preserves_both_original_markers_after_full_or_partial_open(
    roots, monkeypatch, fail_second_unlink
):
    config_hash = "b" * 64
    plan = {"attempt_id": ATTEMPT, "config_hash": config_hash, "requested_ticks": 1}
    original = {
        role: {
            "inspection": {"rpc_remaining": {}},
            "stop_hash": sha256_hex((root / "STOP").read_bytes()),
            "stop_identity": worker._marker(root / "STOP"),
        }
        for role, root in roots.items()
    }
    fresh = {
        "schema": "oasis.base-bounded-fresh.v1",
        **plan,
        "passed": True,
        "nav_valid_until": 200,
        "original": original,
        "budget_after": {role: {} for role in roots},
    }
    fresh["evidence_hash"] = sha256_hex(canonical_json(fresh))
    monkeypatch.setattr(worker.time, "time", lambda: 100)
    monkeypatch.setattr(worker, "_service_lock", lambda *a: nullcontext(lambda: None))
    monkeypatch.setattr(worker, "_snapshot", lambda _, role, *a: (None, None, None, original[role]))
    monkeypatch.setattr(worker, "_budget", lambda *a: {})
    unlink = Path.unlink

    def remove(path, *args, **kwargs):
        if path == roots["authority"] / "STOP":
            raise OSError("injected partial handoff")
        return unlink(path, *args, **kwargs)

    if fail_second_unlink:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", remove)
            with pytest.raises(OSError, match="partial handoff"):
                worker.open_stops(None, None, plan, fresh)
        assert (roots["authority"] / "STOP").is_file()
    else:
        worker.open_stops(None, None, plan, fresh)
        assert not (roots["authority"] / "STOP").exists()
    assert not (roots["strategy"] / "STOP").exists()
    assert worker.restore_stops(ATTEMPT) == dict.fromkeys(roots, True)
    for role, root in roots.items():
        assert worker._marker(root / "STOP") == original[role]["stop_identity"]
        assert (root / "STOP").read_text() == role
        assert list(root.iterdir()) == [root / "STOP"]


def test_marker_conflict_never_overwrites_another_marker_or_skips_other_role(roots):
    for role, root in roots.items():
        os.rename(root / "STOP", worker._backup(root, ATTEMPT))
        if role == "strategy":
            (root / "STOP").write_text("operator marker")
            (root / "STOP").chmod(0o600)
    assert worker.restore_stops(ATTEMPT) == {"strategy": False, "authority": True}
    assert (roots["strategy"] / "STOP").read_text() == "operator marker"
    assert worker._backup(roots["strategy"], ATTEMPT).read_text() == "strategy"
    assert (roots["authority"] / "STOP").read_text() == "authority"


@pytest.mark.parametrize(
    "outcome",
    ["completed", "failed", "timed_out", "stop_error", "authority_pending", "count_mismatch"],
)
def test_terminal_outcomes_stop_owned_services_and_restore_both_markers(
    tmp_path, roots, monkeypatch, outcome
):
    for root in roots.values():
        os.rename(root / "STOP", worker._backup(root, ATTEMPT))
    monkeypatch.setattr(entry, "require_live_start", lambda: None)
    monkeypatch.setattr(entry.deploy, "require_local_runtime_policy", lambda: ("h", 4096, {}))
    monkeypatch.setattr(worker, "require_policy", lambda *a: None)
    monkeypatch.setattr(entry.uuid, "uuid4", lambda: SimpleNamespace(hex=ATTEMPT))
    services = {
        role: {
            "id": digit * 64,
            "attempt": ATTEMPT,
            "image": "image",
            "state": "running",
            "exit_code": 0,
            "oom_killed": False,
        }
        for role, digit in zip(worker.ROLES, ("1", "2"), strict=True)
    }
    actions = []

    def execute(operation, *args):
        operation.safety_scope_checked = True
        operation.start_attempted = True
        operation.strategy_started = True
        args[-1]["opening_settlement_count"] = 5
        operation.guard = "guard"
        operation.service_ids = {role: row["id"] for role, row in services.items()}
        operation.stage = "one_strategy_tick"
        if outcome == "failed":
            raise ValueError("fixture")
        if outcome == "timed_out":
            raise subprocess.TimeoutExpired(["docker", "wait"], 210)

    def docker(_operation, name, args, **kw):
        actions.append(name)
        if name.startswith("stop_"):
            role = name.removeprefix("stop_")
            assert args[-1] == services[role]["id"]
            if outcome == "stop_error" and role == "strategy":
                raise OSError("fixture")
            services[role]["state"] = "exited"
        tick = canonical_json(
            {
                "mode": "live",
                "halted": None,
                "pending_transaction": False,
                "signing_unresolved": False,
                "tick_status": "block 4 | hold",
                "completed_ticks": 1 if outcome == "failed" else 3,
                "reconciled_transactions": 1,
                "bounded_completed": outcome != "failed",
            }
        )
        return subprocess.CompletedProcess(args, 0, tick, b"")

    def keyless(_operation, mode, payload, **kw):
        actions.append(mode)
        result = worker.restore_stops(payload["attempt_id"])
        return subprocess.CompletedProcess([], 0, canonical_json(result), b"")

    def replay(_image, _env, *, config_hash, rpc_max_requests, expect_stop):
        assert expect_stop and config_hash == "h" and rpc_max_requests == 4096
        assert all(row["state"] == "exited" for row in services.values())
        assert all((root / "STOP").is_file() for root in roots.values())
        actions.append("post_stop_replay")
        if outcome == "authority_pending":
            return refusal_report(StartupBlocker.RECONCILIATION_REQUIRED, "authority")
        return {
            "schema": "oasis.live-startup-state.v1",
            "startup_prerequisites_passed": True,
            "stop_markers_present": True,
            "execution_eligible": False,
            "historical_only": True,
            "services": {
                role: {"settlement_count": 5 if outcome == "count_mismatch" else 6}
                for role in worker.ROLES
            },
        }

    monkeypatch.setattr(entry.deploy, "state_preflight", replay)
    monkeypatch.setattr(entry.Run, "execute", execute)
    monkeypatch.setattr(entry.Run, "project", lambda self: services)
    monkeypatch.setattr(entry.Run, "docker", docker)
    monkeypatch.setattr(entry.Run, "keyless", keyless)
    output = tmp_path / "evidence"
    result = entry.run("image", output, ticks=3)
    expected = (
        "failed" if outcome in {"stop_error", "authority_pending", "count_mismatch"} else outcome
    )
    assert result["result"] == expected
    if outcome == "authority_pending":
        assert result["failure_stage"] == "post_stop_replay"
        assert result["post_stop_replay"]["blocked_service"] == "authority"
    if outcome == "count_mismatch":
        assert result["failure_stage"] == "post_stop_replay"
    assert ("post_stop_replay" in actions) is (outcome != "stop_error")
    assert actions[:3] == ["stop_strategy", "stop_authority", "restore"]
    assert all((root / "STOP").is_file() for root in roots.values())
    assert result["cleanup"]["verified"] is (outcome != "stop_error")
    assert ("release_guard" in actions) is (outcome != "stop_error")
    projected = read_bounded_run(output, now=result["finished_at"])
    assert projected.status == ("cleanup_unverified" if outcome == "stop_error" else "stopped")
    assert projected.execution_eligible is False
    assert projected.requested_ticks == 3
    if outcome in {"completed", "failed"}:
        assert projected.completed_ticks == (3 if outcome == "completed" else 1)


def test_missing_terminal_never_claims_a_current_service_is_running_or_stopped(tmp_path):
    entry._retain(
        tmp_path / "attempt.json",
        {
            "schema": "oasis.base-bounded-attempt.v1",
            "attempt_id": ATTEMPT,
            "started_at": 1_000,
            "wait_seconds": 210,
            "execution_eligible": False,
        },
    )
    assert read_bounded_run(tmp_path, now=1_100).status == "launch_requested"
    assert read_bounded_run(tmp_path, now=1_241).status == "cleanup_unverified"
    (tmp_path / "report.json").write_text("{}")
    assert read_bounded_run(tmp_path, now=1_241).status == "invalid"


def test_multi_tick_attempt_projects_the_configured_deadline(tmp_path):
    attempt = {
        "schema": "oasis.base-bounded-attempt.v1",
        "attempt_id": ATTEMPT,
        "started_at": 1_000,
        "requested_ticks": 3,
        "wait_seconds": 1_230,
        "execution_eligible": False,
    }
    entry._retain(tmp_path / "attempt.json", attempt)
    running = read_bounded_run(tmp_path, now=2_260)
    assert running.requested_ticks == 3 and running.status == "launch_requested"
    assert read_bounded_run(tmp_path, now=2_261).status == "cleanup_unverified"
    (tmp_path / "attempt.json").unlink()
    entry._retain(tmp_path / "attempt.json", attempt | {"wait_seconds": 210})
    assert read_bounded_run(tmp_path, now=1_100).status == "invalid"


def test_authority_ready_is_local_and_never_overrides_expired_nav(tmp_path, monkeypatch):
    operation = entry.Run("image", tmp_path)
    operation.service_ids = {"authority": "a" * 64}
    calls = []

    def docker(name, args, **kw):
        calls.append((name, args, kw))
        return subprocess.CompletedProcess(args, 0, b"oasis authority ready\n", b"")

    monkeypatch.setattr(operation, "docker", docker)
    monkeypatch.setattr(entry.time, "time", lambda: 100)
    operation.wait_for_authority(120)
    assert calls == [("authority_ready_0", ["logs", "--tail", "1", "a" * 64], {"timeout": 3})]
    with pytest.raises(TimeoutError, match="fresh NAV deadline"):
        operation.wait_for_authority(104)
    assert len(calls) == 1


def test_uncertain_rpc_budget_refuses_before_capture(tmp_path, monkeypatch):
    operation = entry.Run("image", tmp_path)
    monkeypatch.setattr(operation, "prepare", lambda *_: ({"test": True}, {}))
    monkeypatch.setattr(operation, "project", lambda: {})
    checked = []

    def keyless(mode, payload, **kwargs):
        checked.append(mode)
        return subprocess.CompletedProcess(
            [], 0, canonical_json({"enough_for_capture": True, "ceiling_micros": 1_000_000}), b""
        )

    monkeypatch.setattr(operation, "keyless", keyless)
    with pytest.raises(ValueError, match="insufficient cumulative RPC allowance"):
        operation.execute("test", 4096, {}, {})
    assert checked == ["budget"]
    assert operation.stage == "cumulative_rpc_budget"


@pytest.mark.parametrize("tampered", [False, True])
def test_refused_capture_reports_rate_limit_only_with_intact_wire(tmp_path, monkeypatch, tampered):
    operation = entry.Run("image", tmp_path, ticks=3)
    monkeypatch.setattr(operation, "prepare", lambda *_: ({"requested_ticks": 3}, {}))
    monkeypatch.setattr(operation, "project", lambda: {})
    monkeypatch.setattr(operation, "docker", lambda *_: pytest.fail("capture refused before start"))
    wire = canonical_json(
        {"phase": "request_finished", "source": "live_secondary", "http_status": 429}
    )
    fresh = {
        "passed": False,
        "rpc_rate_limited_sources": ["live_secondary"],
        "artifacts": {"wire.jsonl": sha256_hex(wire)},
    }
    fresh["evidence_hash"] = sha256_hex(canonical_json(fresh))
    capture = io.BytesIO()
    with tarfile.open(fileobj=capture, mode="w") as archive:
        for name, data in (
            ("report.json", canonical_json(fresh)),
            ("wire.jsonl", b"tampered" if tampered else wire),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))

    def keyless(mode, payload, **kwargs):
        if mode == "budget":
            return subprocess.CompletedProcess(
                [],
                0,
                canonical_json(
                    {
                        "ceiling_micros": rpc_scope.CEILING_MICROS,
                        "cumulative_scope_upper_micros": rpc_scope.COST_UPPER_MICROS,
                        "requested_ticks": 3,
                        "enough_for_run": True,
                    }
                ),
                b"",
            )
        assert mode == "capture"
        return subprocess.CompletedProcess([], 3, capture.getvalue(), b"")

    monkeypatch.setattr(operation, "keyless", keyless)
    report = {}
    expected = "archive does not match" if tampered else "fresh NAV preflight refused"
    with pytest.raises(ValueError, match=expected):
        operation.execute("test", 4096, {}, report)
    assert operation.stage == "fresh_evidence" and not operation.start_attempted
    assert report.get("rpc_rate_limited_sources") == (None if tampered else ["live_secondary"])


@pytest.mark.parametrize("short_role", ["strategy", "authority"])
def test_three_tick_budget_checks_both_ledgers_before_capture(tmp_path, monkeypatch, short_role):
    operation = entry.Run("image", tmp_path, ticks=3)
    plan = {"requested_ticks": 3}
    monkeypatch.setattr(operation, "prepare", lambda *_: (plan, {}))
    monkeypatch.setattr(operation, "project", lambda: {})
    need = rpc_scope.run_requests(3, 5, include_capture=True)
    ledgers = {
        role: {"remaining": sum(requests.values()), "counts": dict.fromkeys(worker.SOURCES, 10)}
        for role, requests in need.items()
    }
    ledgers[short_role]["remaining"] -= 1
    monkeypatch.setattr(worker, "_budget", lambda root, _: ledgers[root.name])
    checked = []

    def keyless(mode, payload, **kwargs):
        checked.append(mode)
        assert payload["plan"]["requested_ticks"] == 3
        profile = SimpleNamespace(max_requests=4096)
        budget = worker.budget_readiness(profile, 3, 5)
        assert budget["enough_for_run"] is False
        assert worker.budget_readiness(profile, 2, 5)["enough_for_run"] is True
        return subprocess.CompletedProcess([], 0, canonical_json(budget), b"")

    monkeypatch.setattr(operation, "keyless", keyless)
    with pytest.raises(ValueError, match="insufficient cumulative RPC allowance"):
        operation.execute("test", 4096, {}, {})
    assert checked == ["budget"]


def test_three_tick_budget_includes_bounded_cash_proofs_and_provider_capacity():
    runtime = rpc_scope.run_requests(3, 5, include_capture=False)
    full = rpc_scope.run_requests(3, 5, include_capture=True)
    assert full == {
        "strategy": {"live_primary": 2135, "live_secondary": 1751},
        "authority": {"live_primary": 1671, "live_secondary": 1671},
    }
    assert all(full["strategy"][s] - runtime["strategy"][s] == 80 for s in worker.SOURCES)
    assert runtime["authority"] == full["authority"]
    # Counts from the retained two-tick postflight plus the bounded rehearsal.
    ledgers = {
        "strategy": {
            "remaining": rpc_scope.LIMITS["strategy"].additional_requests - 523 - 55,
            "counts": {"live_primary": 1506, "live_secondary": 1331},
        },
        "authority": {
            "remaining": rpc_scope.LIMITS["authority"].additional_requests,
            "counts": {"live_primary": 1015, "live_secondary": 996},
        },
    }
    assert rpc_scope.covers_requests(ledgers, full, 4096)
    ledgers["authority"]["counts"]["live_secondary"] = 4096 - 1671 + 1
    assert not rpc_scope.covers_requests(ledgers, full, 4096)
    assert rpc_scope.COST_UPPER_MICROS == 430_155 < rpc_scope.CEILING_MICROS == 3_000_000
