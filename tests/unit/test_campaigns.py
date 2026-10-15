"""Managed campaign configuration, authority, isolation and recovery."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import asdict, replace

import pytest
from fastapi.testclient import TestClient

from oasis_agent.errors import ConfigurationError
from oasis_agent.models.common import canonical_json
from oasis_agent.server import campaigns
from oasis_agent.server.app import create_app
from oasis_agent.server.audit import ServerOpsJournal
from oasis_agent.server.campaigns import CampaignManager
from oasis_agent.server.config import ServerCampaignInput, load_server_config
from oasis_agent.server.routers_campaigns import _snapshot as campaign_snapshot
from oasis_agent.shadow.service import serializable

from .server_helpers import make_server_config
from .test_lp_shadow_service import setup
from .test_shadow_lp_runner import band_strategy

AUTH = {"Authorization": "Bearer campaign-test"}


def launch_document(client):
    policy = client.get("/api/v1/configuration").json()["templates"]["launch"]
    return json.dumps(
        {
            "kind": "launch",
            "mode": "observe",
            "policy": policy,
            "wallet": "0x" + "1" * 40,
            "rpc_primary": "primary",
            "rpc_secondary": "secondary",
        }
    )


def client_for(tmp_path):
    return TestClient(
        create_app(
            make_server_config(tmp_path), environ={"OASIS_SERVER_OPS_TOKEN": "campaign-test"}
        )
    )


def save(client, *, expected=0, document=None):
    return client.post(
        "/api/v1/campaigns/demo/configuration",
        headers=AUTH,
        json={"expected_revision": expected, "document": document or launch_document(client)},
    )


def test_configuration_cas_apply_and_restart_persistence(tmp_path):
    config = make_server_config(tmp_path)
    env = {"OASIS_SERVER_OPS_TOKEN": "campaign-test"}
    with TestClient(create_app(config, environ=env)) as client:
        assert save(client).status_code == 200
        assert save(client).status_code == 409
        assert save(client, expected=1).json()["desired_revision"] == 2
        applied = client.post(
            "/api/v1/campaigns/demo/apply",
            headers=AUTH,
            json={"expected_revision": 2, "revision": 1},
        )
        assert applied.status_code == 200
        assert applied.json()["applied_revision"] == 1
    with TestClient(create_app(config, environ=env)) as client:
        head = client.get("/api/v1/campaigns/demo").json()
        assert head["desired_revision"] == 2 and head["applied_revision"] == 1
        assert head["execution_eligible"] is False


def test_campaign_mutations_require_auth_and_reject_live(tmp_path):
    with client_for(tmp_path) as client:
        assert (
            client.post(
                "/api/v1/campaigns/demo/configuration",
                json={"expected_revision": 0, "document": launch_document(client)},
            ).status_code
            == 401
        )
        value = json.loads(launch_document(client))
        value["mode"] = "live"
        assert save(client, document=json.dumps(value)).status_code == 422
        value["mode"] = "observe"
        value["command"] = ["sh", "-c", "echo unsafe"]
        assert save(client, document=json.dumps(value)).status_code == 422


def test_rpc_credentials_are_ephemeral_and_never_echoed(tmp_path):
    with client_for(tmp_path) as client:
        response = client.post(
            "/api/v1/campaigns/rpc/primary",
            headers=AUTH,
            json={"url": "https://rpc.test/private-path?key=synthetic-credential"},
        )
        assert response.status_code == 200
        assert "synthetic-credential" not in response.text
        assert "private-path" not in client.get("/api/v1/campaigns").text
        assert save(client).status_code == 200
        client.post(
            "/api/v1/campaigns/demo/apply",
            headers=AUTH,
            json={"expected_revision": 1, "revision": 1},
        )
        assert (
            client.post(
                "/api/v1/campaigns/demo/start",
                headers=AUTH,
                json={"revision": 1, "request_id": "1" * 32},
            ).status_code
            == 409
        )
    assert all(
        b"synthetic-credential" not in p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    )


def test_lp_source_registry_does_not_accept_browser_file_paths(tmp_path):
    with client_for(tmp_path) as client:
        policy = client.get("/api/v1/configuration").json()["templates"]["lp"]
        document = json.dumps(
            {"kind": "lp", "mode": "shadow", "policy": policy, "input_source": "/etc/passwd"}
        )
        assert save(client, document=document).status_code == 422


def lp_source(tmp_path):

    store, journal, _, cycle = setup(tmp_path)
    now = int(time.time())
    block = replace(cycle.header.ref, timestamp=now)
    header = replace(cycle.header, ref=block)
    header_ref = store.put(
        "header",
        canonical_json(asdict(header)),
        block,
        "fixture",
        request={},
        raw_response=asdict(header),
        interpretation={"fixture": True},
    )
    cycle = replace(
        cycle,
        header=header,
        header_evidence=header_ref,
        state=replace(cycle.state, block=block, observed_at_timestamp=now, oracle_timestamp=now),
    )
    input_ref = store.put(
        "lp_inputs",
        cycle.input_payload,
        block,
        "fixture",
        request={},
        raw_response={"fixture": True},
        interpretation={"fixture": True},
    )
    cycle = replace(cycle, input_evidence=input_ref)
    store.close()
    journal.close()
    input_path = tmp_path / "cycles.jsonl"
    input_path.write_bytes(canonical_json(serializable(cycle)) + b"\n")
    policy = {
        "chain_id": 1,
        "max_age_seconds": 120,
        "min_benefit": "0",
        "canary_limits": None,
        # This fixture tests subprocess lifecycle and snapshot isolation, not
        # quote expiry. Leave room for the real interpreter to start under load;
        # the default five-second quote can expire before the worker reads it.
        "strategy": serializable(replace(band_strategy(), quote_validity_seconds=30)),
    }
    return ServerCampaignInput("fixture", input_path, tmp_path / "evidence.db"), json.dumps(policy)


def wait_terminal(client, name="demo"):

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        head = client.get(f"/api/v1/campaigns/{name}").json()
        if head["status"] in {"succeeded", "failed", "stopped"}:
            return head
        time.sleep(0.02)
    raise AssertionError("managed run did not reach a terminal state")


def test_real_lp_run_uses_applied_policy_snapshot_and_idempotent_start(tmp_path):

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source, policy = lp_source(source_dir)
    config = replace(make_server_config(tmp_path), campaign_inputs=(source,))
    with TestClient(
        create_app(config, environ={"OASIS_SERVER_OPS_TOKEN": "campaign-test"})
    ) as client:
        document = json.dumps(
            {
                "kind": "lp",
                "mode": "shadow",
                "policy": policy,
                "input_source": "fixture",
                "cycles": 1,
            }
        )
        assert save(client, document=document).status_code == 200
        assert (
            client.post(
                "/api/v1/campaigns/demo/apply",
                headers=AUTH,
                json={"expected_revision": 1, "revision": 1},
            ).status_code
            == 200
        )
        request = {"revision": 1, "request_id": "2" * 32}
        started = client.post("/api/v1/campaigns/demo/start", headers=AUTH, json=request)
        assert started.status_code == 200, started.text
        # Changing the producer file after the coherent copy cannot change this run.
        source.input.write_text("malformed new producer data\n")
        final = wait_terminal(client)
        assert final["status"] == "succeeded", final
        assert "recenter" in final["run"]["stdout_tail"]
        snapshot = client.get("/api/v1/campaigns/demo/snapshot").json()
        assert json.loads(snapshot["document"])["items"][0]["execution_eligible"] is False
        repeated = client.post("/api/v1/campaigns/demo/start", headers=AUTH, json=request).json()
        assert repeated["requested_run_id"] == final["run"]["run_id"]
        assert len(client.get("/api/v1/runs").json()["items"]) == 1


def test_launch_worker_has_no_ambient_key_and_fixed_observe_command(tmp_path, monkeypatch):

    fake_root = tmp_path / "worker"
    (fake_root / "scripts").mkdir(parents=True)
    (fake_root / "scripts/run_launch_monitor.py").write_text(
        "import os, sys\n"
        "assert 'OASIS_PRIVATE_KEY' not in os.environ\n"
        "assert '--dry-run' in sys.argv and '--live' not in sys.argv\n"
        "assert sys.argv[sys.argv.index('--env') + 1] == os.devnull\n"
        "print('observation authority verified')\n"
    )
    monkeypatch.setattr(campaigns, "_REPOSITORY", fake_root)
    monkeypatch.setenv("OASIS_PRIVATE_KEY", "synthetic-test-only")
    with client_for(tmp_path) as client:
        for name in ("primary", "secondary"):
            assert (
                client.post(
                    f"/api/v1/campaigns/rpc/{name}",
                    headers=AUTH,
                    json={"url": f"https://{name}.test"},
                ).status_code
                == 200
            )
        save(client)
        client.post(
            "/api/v1/campaigns/demo/apply",
            headers=AUTH,
            json={"expected_revision": 1, "revision": 1},
        )
        result = client.post(
            "/api/v1/campaigns/demo/start",
            headers=AUTH,
            json={"revision": 1, "request_id": "3" * 32},
        )
        assert result.status_code == 200
        final = wait_terminal(client)
        assert final["status"] == "succeeded"
        assert "observation authority verified" in final["run"]["stdout_tail"]


def test_single_controller_lock_and_revision_tampering_fail_closed(tmp_path):

    config = make_server_config(tmp_path)
    app = create_app(config, environ={"OASIS_SERVER_OPS_TOKEN": "campaign-test"})
    with TestClient(app) as client:
        with pytest.raises(ConfigurationError, match="already owns"):
            CampaignManager(config, app.state.journal, {})
        save(client)
        with sqlite3.connect(tmp_path / "campaigns/control.db") as db:
            db.execute("UPDATE revisions SET payload=?", (b"{}",))
        # Corrupt state cannot be used for a start, apply, or configuration display.
        assert client.get("/api/v1/campaigns/demo").status_code == 503


def test_running_policy_cannot_be_applied_and_stop_reaches_terminal(tmp_path, monkeypatch):
    fake_root = tmp_path / "worker"
    (fake_root / "scripts").mkdir(parents=True)
    (fake_root / "scripts/run_launch_monitor.py").write_text("import time; time.sleep(30)\n")
    monkeypatch.setattr(campaigns, "_REPOSITORY", fake_root)
    with client_for(tmp_path) as client:
        for name in ("primary", "secondary"):
            client.post(
                f"/api/v1/campaigns/rpc/{name}", headers=AUTH, json={"url": f"https://{name}.test"}
            )
        save(client)
        client.post(
            "/api/v1/campaigns/demo/apply",
            headers=AUTH,
            json={"expected_revision": 1, "revision": 1},
        )
        assert (
            client.post(
                "/api/v1/campaigns/demo/start",
                headers=AUTH,
                json={"revision": 1, "request_id": "4" * 32},
            ).status_code
            == 200
        )
        assert save(client, expected=1).status_code == 200
        assert (
            client.post(
                "/api/v1/campaigns/demo/apply",
                headers=AUTH,
                json={"expected_revision": 2, "revision": 2},
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/v1/campaigns/demo/start",
                headers=AUTH,
                json={"revision": 1, "request_id": "5" * 32},
            ).status_code
            == 409
        )
        assert client.post("/api/v1/campaigns/demo/stop", headers=AUTH).status_code == 200
        assert wait_terminal(client)["status"] == "stopped"
        assert client.post("/api/v1/campaigns/demo/stop", headers=AUTH).status_code == 200
        assert (
            client.post(
                "/api/v1/campaigns/demo/apply",
                headers=AUTH,
                json={"expected_revision": 2, "revision": 2},
            ).status_code
            == 200
        )


def test_rpc_validation_errors_never_return_raw_credentials(tmp_path):
    with client_for(tmp_path) as client:
        for payload in (
            {"url": "https://rpc.test/synthetic-secret", "extra": "synthetic-secret"},
            {"url": "https://rpc.test/" + "synthetic-secret" * 500},
        ):
            result = client.post("/api/v1/campaigns/rpc/primary", headers=AUTH, json=payload)
            assert result.status_code == 422
            assert "synthetic-secret" not in result.text


def test_orphan_worker_retains_lock_until_exit_then_restart_marks_interrupted(tmp_path):
    worker_root = tmp_path / "worker"
    (worker_root / "scripts").mkdir(parents=True)
    (worker_root / "scripts/run_launch_monitor.py").write_text("import time; time.sleep(2)\n")
    server_file = tmp_path / "server.toml"
    server_file.write_text(
        "ops_token_required = false\n[databases]\nops = " + json.dumps(str(tmp_path / "ops.db"))
    )
    supervisor = tmp_path / "supervisor.py"
    supervisor.write_text(
        """import asyncio, os
from pathlib import Path
from oasis_agent.server import campaigns
from oasis_agent.server.audit import ServerOpsJournal
from oasis_agent.server.config import load_server_config
async def main():
    config = load_server_config(Path(SERVER), environ={})
    journal = ServerOpsJournal(config.databases.ops)
    campaigns._REPOSITORY = Path(WORKER)
    manager = campaigns.CampaignManager(config, journal, {})
    manager.set_rpc("primary", "https://primary.test", "test")
    manager.set_rpc("secondary", "https://secondary.test", "test")
    manager.save("orphan", DOCUMENT, 0, "test")
    manager.apply("orphan", 1, 1, "test")
    await manager.start("orphan", 1, "test", "6" * 32)
    await asyncio.sleep(0.25)
    os._exit(0)
asyncio.run(main())
""".replace("SERVER", repr(str(server_file)))
        .replace("WORKER", repr(str(worker_root)))
        .replace(
            "DOCUMENT",
            repr(
                json.dumps(
                    {
                        "kind": "launch",
                        "mode": "observe",
                        "policy": "{}",
                        "wallet": "0x" + "1" * 40,
                        "rpc_primary": "primary",
                        "rpc_secondary": "secondary",
                    }
                )
            ),
        )
    )
    result = subprocess.run(
        [sys.executable, str(supervisor)],
        timeout=5,
        capture_output=True,
        check=False,
        env={"PATH": os.defpath},
    )
    assert result.returncode == 0, result.stderr
    config = load_server_config(server_file, environ={})
    with ServerOpsJournal(config.databases.ops) as journal:
        with pytest.raises(ConfigurationError, match="already owns"):
            CampaignManager(config, journal, {})
        deadline = time.monotonic() + 5
        while True:
            try:
                controller = CampaignManager(config, journal, {})
                break
            except ConfigurationError:
                assert time.monotonic() < deadline
                time.sleep(0.05)
        try:
            head = controller.head("orphan")
            assert head["status"] == "failed"
            assert "interrupted" in head["run"]["stderr_tail"]
        finally:
            asyncio.run(controller.aclose())


@pytest.mark.parametrize("table", ["campaign_runs", "start_requests"])
def test_persistence_failure_cannot_leave_untracked_process(tmp_path, monkeypatch, table):
    fake_root = tmp_path / "worker"
    (fake_root / "scripts").mkdir(parents=True)
    marker = tmp_path / "must-not-start"
    (fake_root / "scripts/run_launch_monitor.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    )
    monkeypatch.setattr(campaigns, "_REPOSITORY", fake_root)
    with client_for(tmp_path) as client:
        for name in ("primary", "secondary"):
            client.post(
                f"/api/v1/campaigns/rpc/{name}", headers=AUTH, json={"url": f"https://{name}.test"}
            )
        save(client)
        client.post(
            "/api/v1/campaigns/demo/apply",
            headers=AUTH,
            json={"expected_revision": 1, "revision": 1},
        )
        with sqlite3.connect(tmp_path / "campaigns/control.db") as db:
            db.execute(
                f"CREATE TRIGGER fail_run BEFORE INSERT ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
            )
        result = client.post(
            "/api/v1/campaigns/demo/start",
            headers=AUTH,
            json={"revision": 1, "request_id": "7" * 32},
        )
        assert result.status_code == 503
        assert not marker.exists()
        runs = client.get("/api/v1/runs").json()["items"]
        if table == "campaign_runs":
            assert runs[0]["status"] == "stopped"
        else:
            assert runs == []
        assert client.get("/api/v1/campaigns/demo").json()["status"] != "preparing"
        assert (
            client.post(
                "/api/v1/campaigns/demo/apply",
                headers=AUTH,
                json={"expected_revision": 1, "revision": 1},
            ).status_code
            == 200
        )


@pytest.mark.anyio
async def test_stop_during_input_preparation_prevents_process_creation(tmp_path, monkeypatch):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source, policy = lp_source(source_dir)
    config = replace(make_server_config(tmp_path), campaign_inputs=(source,))
    entered = threading.Event()
    release = threading.Event()

    def blocked_copy(source, folder):
        entered.set()
        assert release.wait(5)
        return {}

    monkeypatch.setattr(campaigns, "_copy_inputs", blocked_copy)
    with ServerOpsJournal(config.databases.ops) as journal:
        controller = CampaignManager(config, journal, {})
        controller.save(
            "demo",
            json.dumps(
                {"kind": "lp", "mode": "shadow", "policy": policy, "input_source": "fixture"}
            ),
            0,
            "test",
        )
        controller.apply("demo", 1, 1, "test")
        task = asyncio.create_task(controller.start("demo", 1, "test", "8" * 32))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            controller.stop("demo", "test")
            release.set()
            with pytest.raises(campaigns.CampaignError, match="cancelled"):
                await task
            assert journal.list_runs(limit=20) == ()
        finally:
            release.set()
            await controller.aclose()


def test_market_campaign_runs_fixed_worker_without_wallet_authority(tmp_path, monkeypatch):
    fake_root = tmp_path / "worker"
    (fake_root / "scripts").mkdir(parents=True)
    (fake_root / "scripts/__init__.py").write_text("")
    (fake_root / "scripts/run_lp_research.py").write_text(
        "import os, sys, json\nfrom pathlib import Path\n"
        "assert 'OASIS_PRIVATE_KEY' not in os.environ\n"
        "assert '--wallet' not in sys.argv and '--live' not in sys.argv\n"
        "out=Path(sys.argv[sys.argv.index('--output')+1]); out.mkdir()\n"
        "(out/'status.json').write_text(json.dumps({'schema':'oasis.lp-research-run.v1','stage':'completed','execution_eligible':False}))\n"
        "print('market research authority verified')\n"
    )
    monkeypatch.setattr(campaigns, "_REPOSITORY", fake_root)
    with client_for(tmp_path) as client:
        template = client.get("/api/v1/configuration").json()["templates"]["market"]
        document = {
            "kind": "market",
            "mode": "observe",
            "policy": template,
            "rpc_primary": "primary",
            "rpc_secondary": "secondary",
        }
        assert (
            save(client, document=json.dumps(document | {"wallet": "0x" + "1" * 40})).status_code
            == 422
        )
        assert save(client, document=json.dumps(document)).status_code == 200
        for name in ("primary", "secondary"):
            client.post(
                f"/api/v1/campaigns/rpc/{name}", headers=AUTH, json={"url": f"https://{name}.test"}
            )
        client.post(
            "/api/v1/campaigns/demo/apply",
            headers=AUTH,
            json={"expected_revision": 1, "revision": 1},
        )
        body = {"revision": 1, "request_id": "a" * 32}
        first = client.post("/api/v1/campaigns/demo/start", headers=AUTH, json=body)
        assert first.status_code == 200
        final = wait_terminal(client)
        assert final["status"] == "succeeded", final["run"]["stderr_tail"]
        again = client.post("/api/v1/campaigns/demo/start", headers=AUTH, json=body)
        assert again.json()["requested_run_id"] == final["run"]["run_id"]
        snapshot = client.get("/api/v1/campaigns/demo/snapshot").json()
        assert json.loads(snapshot["document"])["items"][0]["stage"] == "completed"
        assert snapshot["execution_eligible"] is False


def test_archive_starts_resume_same_revision_after_controller_restart(tmp_path, monkeypatch):
    fake_root = tmp_path / "worker"
    (fake_root / "scripts").mkdir(parents=True)
    (fake_root / "scripts/__init__.py").write_text("")
    (fake_root / "scripts/capture_lp_market_archive.py").write_text(
        "import os, sys, json\nfrom pathlib import Path\n"
        "assert 'OASIS_PRIVATE_KEY' not in os.environ\n"
        "assert '--wallet' not in sys.argv and '--max-chunks' in sys.argv\n"
        "out=Path(sys.argv[sys.argv.index('--output')+1]); out.mkdir(parents=True, exist_ok=True)\n"
        "p=out/'status.json'\n"
        "n=json.loads(p.read_text())['chunks_completed'] if p.exists() else 0\n"
        "p.write_text(json.dumps({'schema':'oasis.market-archive-status.v1','stage':'partial',"
        "'chunks_completed':n+1,'execution_eligible':False}))\n"
    )
    monkeypatch.setattr(campaigns, "_REPOSITORY", fake_root)
    monkeypatch.setenv("OASIS_PRIVATE_KEY", "synthetic-test-only")
    config = make_server_config(tmp_path)
    environ = {"OASIS_SERVER_OPS_TOKEN": "campaign-test"}
    for index in range(2):
        with TestClient(create_app(config, environ=environ)) as client:
            for name in ("primary", "secondary"):
                client.post(
                    f"/api/v1/campaigns/rpc/{name}",
                    headers=AUTH,
                    json={"url": f"https://{name}.test"},
                )
            if index == 0:
                policy = client.get("/api/v1/configuration").json()["templates"]["archive"]
                document = {
                    "kind": "archive",
                    "mode": "observe",
                    "policy": policy,
                    "rpc_primary": "primary",
                    "rpc_secondary": "secondary",
                    "cycles": 1,
                }
                assert (
                    save(client, document=json.dumps(document | {"cycles": 1001})).status_code
                    == 422
                )
                assert save(client, document=json.dumps(document)).status_code == 200
                client.post(
                    "/api/v1/campaigns/demo/apply",
                    headers=AUTH,
                    json={"expected_revision": 1, "revision": 1},
                )
            plan = client.get("/api/v1/campaigns/demo").json()["configuration_plan"]
            assert plan["revision"] == 1 and plan["minimum_total_requests_per_provider"] == 36
            assert (
                plan["distinct_rpc_profiles_configured"] and plan["configured_budget_covers_range"]
            )
            assert plan["execution_eligible"] is False
            body = {"revision": 1, "request_id": str(index + 1) * 32}
            assert (
                client.post("/api/v1/campaigns/demo/start", headers=AUTH, json=body).status_code
                == 200
            )
            final = wait_terminal(client)
            assert final["status"] == "succeeded", final["run"]["stderr_tail"]
            snapshot = client.get("/api/v1/campaigns/demo/snapshot").json()
            assert json.loads(snapshot["document"])["items"][0]["chunks_completed"] == index + 1
            assert snapshot["execution_eligible"] is False
            assert (
                client.post("/api/v1/campaigns/demo/start", headers=AUTH, json=body).status_code
                == 200
            )
    paths = tuple((tmp_path / "campaigns/archives").rglob("status.json"))
    assert len(paths) == 1


def test_large_archive_diagnostic_does_not_hide_runtime_status(tmp_path):
    from_placeholder = {
        "schema": "oasis.market-archive-status.v1",
        "stage": "completed",
        "execution_eligible": False,
    }
    folder = tmp_path / "archive"
    folder.mkdir()
    path = folder / "status.json"
    path.write_text(json.dumps(from_placeholder))
    (folder / "economics.json").write_bytes(b" " * 262145)
    snapshot = campaign_snapshot("archive", path, make_server_config(tmp_path))
    assert snapshot["items"][0]["stage"] == "completed"
    assert snapshot["diagnostics"]["economics.json"]["preview"] == "size_limit"
    assert snapshot["execution_eligible"] is False
