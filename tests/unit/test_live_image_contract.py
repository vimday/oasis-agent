"""Offline Docker metadata mutations; no daemon, volume, key or network access."""

import copy
import json
import subprocess
from pathlib import Path

import pytest

from oasis_agent.models.common import canonical_json, sha256_hex
from scripts import verify_live_isolation as cli

BASE_ID = "sha256:" + "a" * 64
IMAGE_ID = "sha256:" + "b" * 64


def images():
    base = {
        "Id": BASE_ID,
        "RepoDigests": [cli.BASE_REPO_DIGEST],
        "Config": {"Env": ["PATH=/usr/local/bin:/usr/bin:/bin", "LANG=C.UTF-8"]},
        "RootFS": {"Type": "layers", "Layers": ["sha256:" + "c" * 64]},
    }
    image = {
        "Id": IMAGE_ID,
        "Config": {
            "Env": [
                *base["Config"]["Env"],
                "PYTHONDONTWRITEBYTECODE=1",
                "PYTHONUNBUFFERED=1",
                "PYTHONPATH=/app/src:/app",
            ],
            "Entrypoint": None,
            "Cmd": cli.RUNTIME_COMMAND.copy(),
            "User": "10001:10001",
            "WorkingDir": "/app",
            "Volumes": None,
        },
        "RootFS": {"Type": "layers", "Layers": [*base["RootFS"]["Layers"], "sha256:" + "d" * 64]},
    }
    return image, base


def test_metadata_contract_matches_dockerfile_and_is_not_source_attestation():
    image, base = images()
    result = cli.runtime_contract(image, base)
    assert result["image"] == IMAGE_ID and result["base_id"] == BASE_ID
    assert result["source_identity_verified"] is False
    assert result["configuration_hash"] == sha256_hex(canonical_json(image["Config"]))
    assert (
        Path("deploy/live/Dockerfile").read_text().splitlines()[0]
        == f"FROM {cli.BASE_IMAGE} AS runtime"
    )
    image["Config"]["Env"].reverse()
    image["Config"]["Entrypoint"] = []
    image["Config"]["Volumes"] = {}
    cli.runtime_contract(image, base)


@pytest.mark.parametrize(
    "field,value",
    [
        ("Entrypoint", ["/unexpected-wrapper"]),
        ("Entrypoint", ""),
        ("Cmd", ["python", "-m", "scripts.run_live_lp", "--mode", "live"]),
        ("Cmd", None),
        ("User", "0:0"),
        ("User", "10001"),
        ("WorkingDir", "/other"),
        ("Volumes", {"/app": {}}),
        ("Healthcheck", {"Test": ["CMD", "unexpected"]}),
    ],
)
def test_image_defaults_refuse_before_creating_any_resource(tmp_path, monkeypatch, field, value):
    image, base = images()
    image["Config"][field] = value
    calls = []

    def docker(*args):
        calls.append(args)
        assert args[:2] == ("image", "inspect"), "invalid image must not start a container"
        if "--format" in args:
            return IMAGE_ID  # The previous verifier only requested this ID.
        return json.dumps([base if args[2] == cli.BASE_IMAGE else image])

    monkeypatch.setattr(cli, "docker", docker)
    with pytest.raises(ValueError, match="configuration"):
        cli.verify("candidate", tmp_path / "report.json")
    assert len(calls) == 2 and not (tmp_path / "report.json").exists()


@pytest.mark.parametrize(
    "extra",
    [
        "LD_PRELOAD=/other",
        "PYTHONPATH=/other",
        "PATH=/other",
        "API_KEY=synthetic_marker",
        "INVALID",
    ],
)
def test_added_or_duplicate_environment_refuses_without_disclosing_values(extra):
    image, base = images()
    image["Config"]["Env"].append(extra)
    with pytest.raises(ValueError) as exc:
        cli.runtime_contract(image, base)
    assert extra not in str(exc.value)


def test_missing_required_environment_refuses():
    image, base = images()
    image["Config"]["Env"].pop()
    with pytest.raises(ValueError):
        cli.runtime_contract(image, base)


@pytest.mark.parametrize("fault", ["foreign", "empty", "type", "id", "base_id"])
def test_base_identity_and_layer_ancestry_cannot_be_asserted(fault):
    image, base = images()
    if fault == "foreign":
        image["RootFS"]["Layers"].reverse()
    elif fault == "empty":
        base["RootFS"]["Layers"] = []
    elif fault == "type":
        image["RootFS"]["Type"] = "unknown"
    elif fault == "id":
        image["Id"] = "oasis-live:local"
    else:
        base["Id"] = "sha256:" + "g" * 64
    with pytest.raises(ValueError):
        cli.runtime_contract(image, base)


def test_good_contract_binds_report_and_runs_only_immutable_image(tmp_path, monkeypatch):
    image, base = images()
    originals = copy.deepcopy((image, base))
    calls = []

    def docker(*args):
        calls.append(args)
        if args[:2] == ("image", "inspect"):
            return json.dumps([base if args[2] == cli.BASE_IMAGE else image])
        if args[0] == "run":
            assert IMAGE_ID in args and "--network=none" in args
            assert "candidate" not in args
            return "synthetic successful drill output"
        assert args[:2] in (("volume", "create"), ("volume", "rm"))
        assert args[2].startswith("oasis-isolation-")
        return args[2]

    monkeypatch.setattr(cli, "docker", docker)
    report = cli.verify("candidate", tmp_path / "report.json")
    assert report["schema"] == "oasis.live-isolation-drill.v2"
    assert report["runtime_contract"] == cli.runtime_contract(image, base)
    assert report["verifier_hash"] == sha256_hex(Path(cli.__file__).read_bytes())
    assert report["execution_eligible"] is False
    assert report["synthetic_markers_only"] is True
    assert (image, base) == originals
    assert len([a for a in calls if a[:2] == ("volume", "rm")]) == 4


def test_existing_report_refuses_before_even_inspection(tmp_path, monkeypatch):
    output = tmp_path / "report.json"
    output.write_text("previous evidence")

    def forbidden(*args):
        raise AssertionError("existing report must not repeat a drill")

    monkeypatch.setattr(cli, "docker", forbidden)
    with pytest.raises(ValueError, match="preserve"):
        cli.verify("candidate", output)
    assert output.read_text() == "previous evidence"


@pytest.mark.parametrize("fault", [None, "wrong_digest", "missing_digest", "ambiguous", "missing"])
def test_base_id_lookup_requires_exact_repository_digest(monkeypatch, fault):
    _, base = images()
    if fault == "wrong_digest":
        base["RepoDigests"] = ["python@sha256:" + "e" * 64]
    elif fault == "missing_digest":
        base.pop("RepoDigests")
    calls = []

    def docker(*args):
        calls.append(args)
        if args == ("image", "inspect", cli.BASE_IMAGE):
            raise subprocess.CalledProcessError(1, ["docker", *args])
        if args[:2] == ("image", "ls"):
            assert args[-1] == "reference=python:3.12-slim"
            return (
                ""
                if fault == "missing"
                else BASE_ID + ("\n" + IMAGE_ID if fault == "ambiguous" else "")
            )
        assert args == ("image", "inspect", BASE_ID)
        return json.dumps([base])

    monkeypatch.setattr(cli, "docker", docker)
    if fault is None:
        assert cli.inspect_base() == base
    else:
        with pytest.raises(ValueError):
            cli.inspect_base()
    assert all(a[0] == "image" for a in calls)
