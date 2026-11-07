"""No real Docker/RPC/key access: exercise startup ordering and uncertain outcomes."""

import json
import os
import stat
import subprocess
from dataclasses import asdict, replace

import pytest

from oasis_agent.live.gas_oracle import LEGACY_PIN_NAMES
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.reporting import live_release as release_module
from scripts import start_live_deployment as cli
from tests.unit.gas_oracle_fixtures import synthetic_gas_pins
from tests.unit.test_live_lp import CONFIG

IMAGE = "sha256:" + "ab" * 32
GUARD_ID = "ba" * 32
DOCKER_SOCKET = "unix:///test.sock"
CONFIG_HASH = "11" * 32
RPC_MAX_REQUESTS = 37
LIMITATIONS = [
    "no_current_chain_or_nav_acceptance",
    "no_service_start_or_execution_permit",
    "host_operator_and_original_volume_identity_remain_trusted",
    "journaled_history_only_no_account_transaction_discovery",
]
RPC = {
    name: f"https://source-{i}.invalid/SECRET-$HOME?literal=$${{UNSET:-token}}"
    for i, name in enumerate(cli.compose.RPC_NAMES)
}
STOP_REFUSAL = {
    "schema": "oasis.live-startup-state.v1",
    "startup_prerequisites_passed": False,
    "execution_eligible": False,
    "historical_only": True,
    "blockers": ["stop_present"],
    "blocked_service": "authority",
}
GENERIC_REFUSAL = {
    **{key: value for key, value in STOP_REFUSAL.items() if key != "blocked_service"},
    "blockers": ["original_runtime_state_unresolved"],
}


@pytest.mark.parametrize("start", [False, True])
def test_closed_release_precedes_any_external_access(tmp_path, monkeypatch, capsys, start):
    def forbidden(*args, **kwargs):
        pytest.fail("closed release must not access Docker, environment or output")

    monkeypatch.setattr(release_module, "RELEASE_OPEN", False)  # closed by owner switch
    monkeypatch.setattr(release_module, "ACCEPTANCE_PERMIT", None)  # and no permit

    monkeypatch.setattr(cli, "command", forbidden)
    monkeypatch.setattr(cli, "local_environment", forbidden)
    output = tmp_path / "absent"
    monkeypatch.setattr(
        "sys.argv", ["start", "--out", str(output), *(["--start"] if start else [])]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 3 and not output.exists()
    assert json.loads(capsys.readouterr().out)["new_actions_allowed"] is False
    with pytest.raises(ValueError, match="live release blocked"):
        cli.start(IMAGE, output)


def test_runtime_document_has_only_bound_services_image_and_existing_volumes():
    doc = cli.runtime_document(IMAGE, RPC)
    assert set(doc["services"]) == {"authority", "strategy"}
    assert all(v["external"] is True for v in doc["volumes"].values())
    for name, service in doc["services"].items():
        assert service["image"] == IMAGE and "build" not in service
        assert service["environment"] == list(cli.compose.RPC_NAMES)
        assert service["command"] == cli.compose.COMMANDS[name]
        assert service["restart"] == "no"
    assert all(value not in json.dumps(doc) for value in RPC.values())
    assert not any(m["target"] == "/secrets" for m in doc["services"]["strategy"]["volumes"])
    assert doc["services"]["authority"]["volumes"][-1]["read_only"] is True


@pytest.mark.parametrize(
    "image,rpc",
    [
        ("oasis-live:local", RPC),
        (IMAGE, {}),
        (IMAGE, dict.fromkeys(cli.compose.RPC_NAMES, "https://same.invalid")),
        (
            IMAGE,
            dict(
                zip(
                    cli.compose.RPC_NAMES,
                    ("https://same.invalid", "https://SAME.invalid."),
                    strict=True,
                )
            ),
        ),
        (
            IMAGE,
            dict(
                zip(
                    cli.compose.RPC_NAMES,
                    ("https://same.invalid", "https://${UNSET:-same}.invalid"),
                    strict=True,
                )
            ),
        ),
        (
            IMAGE,
            dict(
                zip(cli.compose.RPC_NAMES, ("http://a.invalid", "https://b.invalid"), strict=True)
            ),
        ),
    ],
)
def test_document_refuses_ambiguous_image_or_endpoints(image, rpc):
    with pytest.raises(ValueError):
        cli.runtime_document(image, rpc)


POLICY_FILES = {
    "configs/live.base-eth-usdc.json": "12" * 32,
    "configs/live.rpc.canary.json": "34" * 32,
}


def setup(tmp_path, monkeypatch, fault=None, *, observed_envs=None, overlap=None):
    for name in tuple(os.environ):
        if name.startswith("DOCKER_"):
            monkeypatch.delenv(name)
    for k, v in RPC.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("OASIS_PRIVATE_KEY", "SECRET")
    monkeypatch.setenv("COMPOSE_FILE", "SECRET")
    monkeypatch.setenv("COMPOSE_PROFILES", "setup,recovery")
    calls = []
    guard_exists = False
    sources = {"src/a.py": "fixed"} | POLICY_FILES
    if fault == "changed_profile_before_image":
        sources["configs/live.rpc.canary.json"] = "56" * 32
    if fault == "changed_config_before_image":
        sources["configs/live.base-eth-usdc.json"] = "78" * 32
    digest = sha256_hex(canonical_json(sources))
    monkeypatch.setattr(cli, "require_live_start", lambda: None)  # Synthetic future release only.
    monkeypatch.setattr(
        cli, "require_local_runtime_policy", lambda: (CONFIG_HASH, RPC_MAX_REQUESTS, POLICY_FILES)
    )  # Separate pin tests.
    monkeypatch.setattr(
        cli.compose,
        "inspect",
        lambda: {"compose_hash": sha256_hex(cli.compose.COMPOSE.read_bytes())},
    )

    def verify(*_, env=None):
        if observed_envs is not None:
            observed_envs.append(env or {})
        return {"image": IMAGE, "manifest_hash": digest, "files": sources}

    monkeypatch.setattr(cli, "verify", verify)
    monkeypatch.setattr(
        cli, "source_manifest", lambda: {} if fault == "changed_sources" else sources
    )

    def command(args, env, *, payload=None):
        nonlocal guard_exists
        if observed_envs is not None:
            observed_envs.append(env)
        if args[:2] == ["context", "inspect"]:
            assert set(env) <= {"PATH", "HOME"}
        elif args[0] == "compose":
            assert set(env) <= {"PATH", "HOME", "DOCKER_HOST", *RPC}
            assert {name: env[name] for name in RPC} == RPC
        else:
            assert set(env) <= {"PATH", "HOME", "DOCKER_HOST"}
            assert all("SECRET" not in value for value in env.values())
        calls.append((args, payload))
        if args[:2] == ["context", "inspect"]:
            return canonical_json(
                [
                    {
                        "Endpoints": {
                            "docker": {
                                "Host": "tcp://remote:2375"
                                if fault == "remote"
                                else "unix://relative.sock"
                                if fault == "relative_socket"
                                else DOCKER_SOCKET
                            }
                        }
                    }
                ]
            )
        if args[:2] == ["volume", "inspect"]:
            if fault == "missing_volume":
                raise subprocess.CalledProcessError(1, args, stderr=b"SECRET")
            return canonical_json(
                [
                    {
                        "Name": v,
                        "Driver": "local",
                        "Options": {"device": "remote"} if fault == "volume_driver" else None,
                    }
                    for v in cli.VOLUMES.values()
                ]
            )
        if args[0] == "ps":
            late = (fault == "late_existing" and guard_exists) or (
                fault == "existing_during_preflight" and any(a[0] == "run" for a, _ in calls)
            )
            return b"existing" if fault == "existing" or late else b""
        if args[0] == "create":
            assert args[args.index("--name") + 1] == "oasis-live-start-guard"
            assert args[args.index("--entrypoint") + 1] == "/bin/false" and args[-1] == IMAGE
            assert {"--pull=never", "--network=none", "--read-only"} <= set(args)
            assert not {"--mount", "--volume", "--env", "--env-file"} & set(args)
            if guard_exists:
                raise subprocess.CalledProcessError(1, args, stderr=b"SECRET conflict")
            guard_exists = True
            if fault == "guard_create_timeout":
                raise subprocess.TimeoutExpired(args, 60, output=b"SECRET")
            return (GUARD_ID + "\n").encode()
        if args[0] == "rm":
            assert guard_exists and args == ["rm", GUARD_ID]
            if fault == "guard_cleanup":
                raise subprocess.CalledProcessError(1, args, stderr=b"SECRET cleanup")
            guard_exists = False
            return GUARD_ID.encode()
        if args[0] == "run":
            assert "--network=none" in args and "--read-only" in args
            assert "--pull=never" in args and IMAGE in args
            assert "scripts.inspect_live_startup" in args
            mounts = [args[i + 1] for i, x in enumerate(args) if x == "--mount"]
            assert mounts == [
                "type=volume,src=oasis-live_strategy-state,dst=/strategy,readonly,volume-nocopy",
                "type=volume,src=oasis-live_authority-state,dst=/authority,readonly,volume-nocopy",
            ]
            assert not any(
                x in " ".join(args) for x in ("canary-keys", "authority-socket", "SECRET")
            )
            if fault == "state_refused":
                raise subprocess.CalledProcessError(3, args, output=b"SECRET")
            if fault in {"state_stop", "state_generic", "state_extra", "state_wrong_exit"}:
                refusal = dict(GENERIC_REFUSAL if fault == "state_generic" else STOP_REFUSAL)
                if fault == "state_extra":
                    refusal["debug"] = "SECRET"
                raise subprocess.CalledProcessError(
                    1 if fault == "state_wrong_exit" else 3,
                    args,
                    output=canonical_json(refusal),
                    stderr=b"SECRET",
                )
            service = {
                "checkpoint_verified": True,
                "settled_history_replayed": True,
                "settlement_count": 0,
                "config_hash": CONFIG_HASH,
                "journal_tip": "22" * 32,
                "position_identity_hash": "33" * 32,
                "rpc_remaining": {"live_primary": 1, "live_secondary": RPC_MAX_REQUESTS - 1},
                "settled_history_hash": "44" * 32,
                "financial_continuity_hash": "55" * 32,
            }
            authority = dict(service)
            if fault == "history_missing":
                authority.pop("settled_history_replayed")
            elif fault == "history_false":
                authority["settled_history_replayed"] = False
            elif fault == "history_hash_invalid":
                authority["settled_history_hash"] = "invalid SECRET"
            elif fault == "history_count_bool":
                authority["settlement_count"] = False
            elif fault == "history_count_large":
                authority["settlement_count"] = 10_001
            elif fault == "history_count_negative":
                authority["settlement_count"] = -1
            elif fault == "history_disagreement":
                authority["settled_history_hash"] = "66" * 32
            elif fault == "capital_disagreement":
                authority["financial_continuity_hash"] = "66" * 32
            elif fault == "config_disagreement":
                authority["config_hash"] = "66" * 32
            elif fault == "wrong_local_config":
                service["config_hash"] = authority["config_hash"] = "66" * 32
            elif fault == "checkpoint_unverified":
                authority["checkpoint_verified"] = False
            elif fault == "state_service_extra":
                authority["debug"] = "SECRET"
            elif fault == "state_rpc_missing_source":
                authority["rpc_remaining"] = {"live_primary": 1}
            elif fault == "state_rpc_boolean":
                authority["rpc_remaining"] = {"live_primary": True, "live_secondary": 1}
            elif fault == "state_rpc_exhausted":
                authority["rpc_remaining"] = {"live_primary": 0, "live_secondary": 1}
            elif fault == "state_rpc_unused":
                authority["rpc_remaining"] = {"live_primary": RPC_MAX_REQUESTS, "live_secondary": 1}
            result = {
                "schema": "oasis.live-startup-state.v1",
                "startup_prerequisites_passed": fault != "state_bad_report",
                "historical_only": True,
                "execution_eligible": False,
                "limitations": LIMITATIONS,
                "services": {"strategy": service, "authority": authority},
            }
            if fault == "state_success_extra":
                result["debug"] = "SECRET"
            if fault == "state_limitations":
                result["limitations"] = ["SECRET"]
            raw = canonical_json(result)
            if fault == "state_duplicate":
                raw = b'{"execution_eligible":true,' + raw[1:]
            if fault == "state_oversized":
                raw += b" " * 4096
            return raw
        assert args[0] == "compose" and args[-2:] == ["authority", "strategy"]
        assert "--no-build" in args and args[args.index("--pull") + 1] == "never"
        assert args[args.index("--env-file") + 1] == os.devnull
        doc = json.loads(payload)
        assert doc == cli.runtime_document(IMAGE, RPC)
        assert b"SECRET" not in payload
        assert (
            json.loads((tmp_path / "attempt" / "attempt.json").read_bytes())["status"]
            == "unresolved"
        )
        if overlap is not None and sum(a[0] == "compose" for a, _ in calls) == 1:
            overlap()
        if fault == "timeout":
            raise subprocess.TimeoutExpired(args, 60, output=b"SECRET")
        if fault == "partial_failure":
            raise subprocess.CalledProcessError(1, args, stderr=b"SECRET")
        return b"SECRET output must never be logged"

    monkeypatch.setattr(cli, "command", command)
    return calls


@pytest.mark.parametrize(
    "fault",
    [
        "remote",
        "relative_socket",
        "missing_volume",
        "volume_driver",
        "existing",
        "late_existing",
        "existing_during_preflight",
        "changed_sources",
        "changed_profile_before_image",
        "changed_config_before_image",
        "state_refused",
        "state_extra",
        "state_wrong_exit",
        "state_bad_report",
        "state_success_extra",
        "state_service_extra",
        "state_limitations",
        "state_duplicate",
        "state_oversized",
        "state_rpc_missing_source",
        "state_rpc_boolean",
        "state_rpc_exhausted",
        "state_rpc_unused",
        "history_missing",
        "history_false",
        "history_hash_invalid",
        "history_count_bool",
        "history_count_large",
        "history_count_negative",
        "history_disagreement",
        "capital_disagreement",
        "config_disagreement",
        "wrong_local_config",
        "checkpoint_unverified",
    ],
)
def test_preflight_failure_never_starts_services(tmp_path, monkeypatch, fault):
    calls = setup(tmp_path, monkeypatch, fault)
    output = tmp_path / "attempt"
    report = cli.start(IMAGE, output)
    assert report["status"] == "refused" and not report["start_attempted"]
    assert not any(args[0] == "compose" for args, _ in calls)
    assert "SECRET" not in json.dumps(report)
    assert not (output / "attempt.json").exists()
    assert json.loads((output / "report.json").read_bytes()) == report
    if fault in {"changed_sources", "existing_during_preflight"}:
        assert report["state_preflight_exit_code"] == 0
    else:
        assert "state_preflight" not in report
        assert "state_preflight_exit_code" not in report
    if fault in {"changed_profile_before_image", "changed_config_before_image"}:
        assert report["failure_stage"] == "image_sources"
        assert [args[0] for args, _ in calls] == ["context"]
        assert not (output / "guard-request.json").exists()
    if fault == "late_existing":
        assert report["failure_stage"] == "existing_services_recheck"
        assert sum(args[0] == "ps" for args, _ in calls) == 2
        assert all(args[0] != "run" for args, _ in calls)
    if fault == "existing_during_preflight":
        assert report["failure_stage"] == "existing_services_before_start"
        assert report["startup_guard_status"] == "released"


@pytest.mark.parametrize(
    "fault,refusal", [("state_stop", STOP_REFUSAL), ("state_generic", GENERIC_REFUSAL)]
)
def test_preflight_retains_expected_refusal_without_starting(tmp_path, monkeypatch, fault, refusal):
    calls = setup(tmp_path, monkeypatch, fault)
    output = tmp_path / "attempt"
    report = cli.start(IMAGE, output)
    assert report["status"] == "refused" and report["failure_stage"] == "original_runtime_state"
    assert report["state_preflight"] == refusal and report["state_preflight_exit_code"] == 3
    assert json.loads((output / "report.json").read_bytes()) == report
    assert not report["start_attempted"] and not (output / "attempt.json").exists()
    assert report["startup_guard_status"] == "released"
    assert calls[-1][0] == ["rm", GUARD_ID]
    assert not any(args[0] == "compose" for args, _ in calls)
    assert "SECRET" not in json.dumps(report)


@pytest.mark.parametrize("fault", [None, "timeout", "partial_failure"])
def test_one_start_attempt_preserves_uncertainty_and_redacts_secrets(tmp_path, monkeypatch, fault):
    envs = []
    calls = setup(tmp_path, monkeypatch, fault, observed_envs=envs)
    report = cli.start(IMAGE, tmp_path / "attempt")
    assert report["status"] == ("unresolved" if fault else "start_command_completed")
    assert (
        report["start_attempted"]
        and not report["services_ready"]
        and not report["execution_eligible"]
    )
    assert sum(args[0] == "compose" for args, _ in calls) == 1
    assert envs and "DOCKER_HOST" not in envs[0]
    assert all(env.get("DOCKER_HOST") == DOCKER_SOCKET for env in envs[1:])
    order = [args[0] for args, _ in calls]
    assert order[order.index("create") :] == ["create", "ps", "run", "ps", "compose"] + (
        [] if fault else ["rm"]
    )
    assert report["startup_guard_status"] == ("retained_for_recovery" if fault else "released")
    assert report["startup_guard_id"] == GUARD_ID
    assert (
        json.loads((tmp_path / "attempt/attempt.json").read_bytes())["startup_guard_id"] == GUARD_ID
    )
    assert report["docker_host_hash"] == sha256_hex(DOCKER_SOCKET.encode())
    assert DOCKER_SOCKET not in json.dumps(report)
    assert "SECRET" not in json.dumps(report)
    with pytest.raises(FileExistsError):
        cli.start(IMAGE, tmp_path / "attempt")
    assert sum(args[0] == "compose" for args, _ in calls) == 1


def test_start_guard_blocks_overlapping_and_post_timeout_attempts(tmp_path, monkeypatch):
    overlapped = []
    calls = setup(
        tmp_path,
        monkeypatch,
        "timeout",
        overlap=lambda: overlapped.append(cli.start(IMAGE, tmp_path / "parallel")),
    )
    first = cli.start(IMAGE, tmp_path / "attempt")
    later = cli.start(IMAGE, tmp_path / "later")
    assert (
        first["status"] == "unresolved" and first["startup_guard_status"] == "retained_for_recovery"
    )
    for folder, report in (("parallel", overlapped[0]), ("later", later)):
        assert report["status"] == "refused" and report["failure_stage"] == "startup_guard"
        assert (
            report["startup_guard_status"] == "creation_unresolved"
            and not report["start_attempted"]
        )
        assert "startup_guard_id" not in report and "SECRET" not in json.dumps(report)
        assert not (tmp_path / folder / "attempt.json").exists()
    assert [args[0] for args, _ in calls].count("compose") == 1
    assert [args[0] for args, _ in calls].count("run") == 1
    assert all(args[0] != "rm" for args, _ in calls)
    requests = [
        json.loads((tmp_path / name / "guard-request.json").read_bytes())["evidence_hash"]
        for name in ("attempt", "parallel", "later")
    ]
    assert len(set(requests)) == 3
    assert first["startup_guard_request_hash"] == requests[0]
    assert overlapped[0]["startup_guard_request_hash"] == requests[1]
    assert later["startup_guard_request_hash"] == requests[2]


@pytest.mark.parametrize("fault", ["guard_create_timeout", "guard_cleanup"])
def test_start_guard_uncertainty_is_redacted_and_requires_recovery(
    tmp_path, monkeypatch, capsys, fault
):
    calls = setup(tmp_path, monkeypatch, fault)
    output = tmp_path / "attempt"
    monkeypatch.setattr("sys.argv", ["start", "--start", "--image", IMAGE, "--out", str(output)])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 3
    report = json.loads(capsys.readouterr().out)
    assert "SECRET" not in json.dumps(report)
    if fault == "guard_create_timeout":
        assert report["status"] == "refused" and report["failure_stage"] == "startup_guard"
        assert (
            report["startup_guard_status"] == "creation_unresolved"
            and not report["start_attempted"]
        )
        assert "startup_guard_id" not in report and not (output / "attempt.json").exists()
        assert not any(args[0] in {"compose", "rm"} for args, _ in calls)
    else:
        assert (
            report["status"] == "start_command_completed"
            and report["startup_guard_status"] == "release_unresolved"
        )
        assert report["startup_guard_id"] == GUARD_ID and report["start_attempted"]
        assert (output / "attempt.json").is_file()
        assert [args[0] for args, _ in calls][-2:] == ["compose", "rm"]


def test_ambient_docker_override_refuses(tmp_path, monkeypatch):
    calls = setup(tmp_path, monkeypatch)
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote:2375")
    report = cli.start(IMAGE, tmp_path / "attempt")
    assert report["failure_stage"] == "local_context" and not calls


def test_guard_has_durable_unique_request_before_create(tmp_path, monkeypatch):
    calls = setup(tmp_path, monkeypatch)
    original_command = cli.command
    original_fsync = os.fsync
    synced = set()
    requests = []
    output = tmp_path / "attempt"

    def sync(fd):
        original_fsync(fd)
        info = os.fstat(fd)
        synced.add((info.st_dev, info.st_ino))

    def command(args, env, **kwargs):
        if args[0] == "create":
            path = output / "guard-request.json"
            request = json.loads(path.read_bytes())
            digest = request.pop("evidence_hash")
            assert digest == sha256_hex(canonical_json(request))
            assert args[args.index("--label") + 1] == (
                "org.oasis-agent.live-start-request=" + digest
            )
            assert request["schema"] == "oasis.live-start-guard-request.v1"
            assert request["docker_host_hash"] == sha256_hex(DOCKER_SOCKET.encode())
            assert request["image"] == IMAGE and request["config_hash"] == CONFIG_HASH
            assert request["startup_guard_name"] == cli.STARTUP_GUARD_NAME
            assert request["execution_eligible"] is False
            assert len(request["attempt_id"]) == 32
            for item in (path, output, output.parent):
                info = item.stat()
                assert (info.st_dev, info.st_ino) in synced
            assert "SECRET" not in path.read_text() and DOCKER_SOCKET not in path.read_text()
            requests.append(digest)
        return original_command(args, env, **kwargs)

    monkeypatch.setattr(cli.os, "fsync", sync)
    monkeypatch.setattr(cli, "command", command)
    for name in ("attempt", "second"):
        output = tmp_path / name
        report = cli.start(IMAGE, output)
        assert report["status"] == "start_command_completed"
        assert report["startup_guard_request_hash"] == requests[-1]
        attempt = json.loads((output / "attempt.json").read_bytes())
        assert attempt["startup_guard_request_hash"] == requests[-1]
    assert len(set(requests)) == 2
    assert sum(args[0] == "compose" for args, _ in calls) == 2


@pytest.mark.parametrize("failure", ["file", "directory", "parent"])
def test_guard_request_durability_failure_prevents_create(tmp_path, monkeypatch, failure):
    calls = setup(tmp_path, monkeypatch)
    original_fsync = os.fsync
    output = tmp_path / "attempt"
    failed = False

    def sync(fd):
        nonlocal failed
        info = os.fstat(fd)
        target = output if failure == "directory" else tmp_path
        matches = (
            stat.S_ISREG(info.st_mode) if failure == "file" else info.st_ino == target.stat().st_ino
        )
        if not failed and matches:
            failed = True
            raise OSError("SECRET fsync failure")
        original_fsync(fd)

    monkeypatch.setattr(cli.os, "fsync", sync)
    report = cli.start(IMAGE, output)
    assert failed and report["failure_stage"] == "startup_guard_record"
    assert report["start_attempted"] is False
    assert not any(args[0] in {"create", "compose", "rm"} for args, _ in calls)
    assert "SECRET" not in json.dumps(report)


@pytest.mark.parametrize("failure", ["timeout", "crash", "malformed_id"])
def test_guard_creation_uncertainty_keeps_ownership_evidence(tmp_path, monkeypatch, failure):
    calls = setup(tmp_path, monkeypatch)
    original_command = cli.command

    class ProcessDeath(BaseException):
        pass

    def command(args, env, **kwargs):
        result = original_command(args, env, **kwargs)
        if args[0] == "create":
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, 60, output=b"SECRET")
            if failure == "crash":
                raise ProcessDeath
            return b"SECRET invalid ID"
        return result

    monkeypatch.setattr(cli, "command", command)
    output = tmp_path / "attempt"
    if failure == "crash":
        with pytest.raises(ProcessDeath):
            cli.start(IMAGE, output)
        assert not (output / "report.json").exists()
    else:
        report = cli.start(IMAGE, output)
        assert report["startup_guard_status"] == "creation_unresolved"
        assert "startup_guard_id" not in report
        assert "SECRET" not in json.dumps(report)
    request = json.loads((output / "guard-request.json").read_bytes())
    create = next(args for args, _ in calls if args[0] == "create")
    assert create[create.index("--label") + 1].endswith("=" + request["evidence_hash"])
    assert not (output / "attempt.json").exists()
    assert not any(args[0] in {"compose", "rm"} for args, _ in calls)


def test_final_start_report_is_durable_before_return(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    original_fsync = os.fsync
    synced = []

    def sync(fd):
        original_fsync(fd)
        info = os.fstat(fd)
        synced.append((info.st_dev, info.st_ino))

    monkeypatch.setattr(cli.os, "fsync", sync)
    output = tmp_path / "attempt"
    report = cli.start(IMAGE, output)
    path = output / "report.json"
    assert json.loads(path.read_bytes()) == report
    assert synced[-3:] == [(p.stat().st_dev, p.stat().st_ino) for p in (path, output, tmp_path)]


@pytest.mark.parametrize("failure", ["publish", "file_sync", "directory_sync", "parent_sync"])
def test_final_report_failure_preserves_uncertain_attempt(tmp_path, monkeypatch, capsys, failure):
    calls = setup(tmp_path, monkeypatch)
    original_link, original_fsync = os.link, os.fsync
    output = tmp_path / "attempt"

    def link(source, target):
        if failure == "publish" and target == output / "report.json":
            raise OSError("SECRET publication failure")
        original_link(source, target)

    def sync(fd):
        info = os.fstat(fd)
        pending = output / ".report.json.pending"
        if failure == "file_sync" and pending.exists() and info.st_ino == pending.stat().st_ino:
            raise OSError("SECRET file sync failure")
        if (output / "report.json").exists():
            directory = output if failure == "directory_sync" else tmp_path
            if (
                failure in {"directory_sync", "parent_sync"}
                and info.st_ino == directory.stat().st_ino
            ):
                raise OSError("SECRET directory sync failure")
        original_fsync(fd)

    monkeypatch.setattr(cli.os, "link", link)
    monkeypatch.setattr(cli.os, "fsync", sync)
    monkeypatch.setattr("sys.argv", ["start", "--start", "--image", IMAGE, "--out", str(output)])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 3
    captured = capsys.readouterr()
    assert not captured.out and "SECRET" not in captured.err
    assert "Startup outcome unavailable" in captured.err
    attempt = json.loads((output / "attempt.json").read_bytes())
    assert attempt["status"] == "unresolved" and attempt["startup_guard_id"] == GUARD_ID
    assert sum(args[0] == "compose" for args, _ in calls) == 1
    if failure in {"publish", "file_sync"}:
        assert not (output / "report.json").exists()
        assert (output / ".report.json.pending").is_file()


def test_record_publication_never_replaces_previous_evidence(tmp_path):
    path = tmp_path / "report.json"
    original = b'{"original":"evidence"}'
    path.write_bytes(original)
    with pytest.raises(FileExistsError):
        cli._retain_record(path, {"replacement": True})
    assert path.read_bytes() == original


@pytest.mark.parametrize("complete_policy", [False, True])
def test_open_release_offline_readiness_does_not_inspect_docker(
    tmp_path, monkeypatch, capsys, complete_policy
):
    def forbidden(*args, **kwargs):
        pytest.fail("offline readiness must not inspect Docker or production state")

    monkeypatch.setattr(release_module, "RELEASE_OPEN", True)
    monkeypatch.setattr(cli, "command", forbidden)
    monkeypatch.setattr(cli, "state_preflight", forbidden)
    monkeypatch.setattr(cli, "local_environment", forbidden)
    monkeypatch.setattr("sys.argv", ["start"])
    config = (
        replace(CONFIG, expected_pins=CONFIG.expected_pins | synthetic_gas_pins())
        if complete_policy
        else replace(
            CONFIG,
            expected_pins={k: v for k, v in CONFIG.expected_pins.items() if k in LEGACY_PIN_NAMES},
        )
    )
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/live.base-eth-usdc.json").write_bytes(canonical_json(asdict(config)))
    monkeypatch.setattr(cli.compose, "ROOT", tmp_path)
    if complete_policy:
        (tmp_path / "configs/live.rpc.canary.json").write_bytes(
            canonical_json({"max_requests": RPC_MAX_REQUESTS, "min_interval_ms": 3000})
        )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == (0 if complete_policy else 3)
    result = json.loads(capsys.readouterr().out)
    assert result["new_actions_allowed"] is True
    assert result["startup"]["local_guards_passed"] is complete_policy
    assert result["startup"]["startup_prerequisites_passed"] is False
    if complete_policy:
        assert cli.require_local_runtime_policy() == (
            result["startup"]["configuration_hash"],
            RPC_MAX_REQUESTS,
            {name: sha256_hex((tmp_path / name).read_bytes()) for name in POLICY_FILES},
        )
