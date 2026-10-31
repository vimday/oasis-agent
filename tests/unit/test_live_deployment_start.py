"""No real Docker/RPC/key access: exercise startup ordering and uncertain outcomes."""

import json
import os
import subprocess

import pytest

from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.reporting import live_release as release_module
from scripts import start_live_deployment as cli

IMAGE = "sha256:" + "ab" * 32
RPC = {name: f"https://source-{i}.invalid/SECRET" for i, name in enumerate(cli.compose.RPC_NAMES)}


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
        assert service["environment"] == RPC
        assert service["command"] == cli.compose.COMMANDS[name]
        assert service["restart"] == "no"
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
        assert set(env) <= {"PATH", "HOME"}
        assert "SECRET" not in env.values()
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
        assert args[0] == "compose" and args[-2:] == ["authority", "strategy"]
        assert "--no-build" in args and args[args.index("--pull") + 1] == "never"
        assert args[args.index("--env-file") + 1] == os.devnull
        doc = json.loads(payload)
        assert doc == cli.runtime_document(IMAGE, RPC)
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
    "fault", ["remote", "missing_volume", "volume_driver", "existing", "changed_sources"]
)
def test_preflight_failure_never_starts_services(tmp_path, monkeypatch, fault):
    calls = setup(tmp_path, monkeypatch, fault)
    report = cli.start(IMAGE, tmp_path / "attempt")
    assert report["status"] == "refused" and not report["start_attempted"]
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
