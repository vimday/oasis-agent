"""No real Docker/RPC/key access: exercise startup ordering and uncertain outcomes."""

import json
import os
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


def setup(tmp_path, monkeypatch, fault=None):
    for name in tuple(os.environ):
        if name.startswith("DOCKER_"):
            monkeypatch.delenv(name)
    for k, v in RPC.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("OASIS_PRIVATE_KEY", "SECRET")
    monkeypatch.setenv("COMPOSE_FILE", "SECRET")
    monkeypatch.setenv("COMPOSE_PROFILES", "setup,recovery")
    calls = []
    sources = {"src/a.py": "fixed"}
    digest = sha256_hex(canonical_json(sources))
    monkeypatch.setattr(cli, "require_live_start", lambda: None)  # Synthetic future release only.
    monkeypatch.setattr(cli, "require_local_runtime_policy", lambda: None)  # Separate pin tests.
    monkeypatch.setattr(
        cli.compose,
        "inspect",
        lambda: {"compose_hash": sha256_hex(cli.compose.COMPOSE.read_bytes())},
    )
    monkeypatch.setattr(cli, "verify", lambda *_: {"image": IMAGE, "manifest_hash": digest})
    monkeypatch.setattr(
        cli, "source_manifest", lambda: {} if fault == "changed_sources" else sources
    )

    def command(args, env, *, payload=None):
        if args[0] == "compose":
            assert set(env) <= {"PATH", "HOME", *RPC}
            assert {name: env[name] for name in RPC} == RPC
        else:
            assert set(env) <= {"PATH", "HOME"}
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
                                else "unix:///test.sock"
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
            return b"existing" if fault == "existing" else b""
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
                "config_hash": "11" * 32,
                "journal_tip": "22" * 32,
                "position_identity_hash": "33" * 32,
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
            elif fault == "checkpoint_unverified":
                authority["checkpoint_verified"] = False
            return canonical_json(
                {
                    "schema": "oasis.live-startup-state.v1",
                    "startup_prerequisites_passed": fault != "state_bad_report",
                    "historical_only": True,
                    "execution_eligible": False,
                    "services": {"strategy": dict(service), "authority": authority},
                }
            )
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
        "missing_volume",
        "volume_driver",
        "existing",
        "changed_sources",
        "state_refused",
        "state_extra",
        "state_wrong_exit",
        "state_bad_report",
        "history_missing",
        "history_false",
        "history_hash_invalid",
        "history_count_bool",
        "history_count_large",
        "history_count_negative",
        "history_disagreement",
        "capital_disagreement",
        "config_disagreement",
        "checkpoint_unverified",
    ],
)
def test_preflight_failure_never_starts_services(tmp_path, monkeypatch, fault):
    calls = setup(tmp_path, monkeypatch, fault)
    report = cli.start(IMAGE, tmp_path / "attempt")
    assert report["status"] == "refused" and not report["start_attempted"]
    assert not any(args[0] == "compose" for args, _ in calls)
    assert "SECRET" not in json.dumps(report)
    if fault == "changed_sources":
        assert report["state_preflight_exit_code"] == 0
    else:
        assert "state_preflight" not in report
        assert "state_preflight_exit_code" not in report


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
    assert not any(args[0] == "compose" for args, _ in calls)
    assert "SECRET" not in json.dumps(report)


@pytest.mark.parametrize("fault", [None, "timeout", "partial_failure"])
def test_one_start_attempt_preserves_uncertainty_and_redacts_secrets(tmp_path, monkeypatch, fault):
    calls = setup(tmp_path, monkeypatch, fault)
    report = cli.start(IMAGE, tmp_path / "attempt")
    assert report["status"] == ("unresolved" if fault else "start_command_completed")
    assert (
        report["start_attempted"]
        and not report["services_ready"]
        and not report["execution_eligible"]
    )
    assert sum(args[0] == "compose" for args, _ in calls) == 1
    assert "SECRET" not in json.dumps(report)
    with pytest.raises(FileExistsError):
        cli.start(IMAGE, tmp_path / "attempt")
    assert sum(args[0] == "compose" for args, _ in calls) == 1


def test_ambient_docker_override_refuses(tmp_path, monkeypatch):
    calls = setup(tmp_path, monkeypatch)
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote:2375")
    report = cli.start(IMAGE, tmp_path / "attempt")
    assert report["failure_stage"] == "local_context" and not calls


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
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == (0 if complete_policy else 3)
    result = json.loads(capsys.readouterr().out)
    assert result["new_actions_allowed"] is True
    assert result["startup"]["local_guards_passed"] is complete_policy
    assert result["startup"]["startup_prerequisites_passed"] is False
