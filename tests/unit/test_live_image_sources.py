"""Application identity checks over tar bytes; no Docker or external I/O."""

import io
import json
import tarfile
from types import SimpleNamespace

import pytest

from oasis_agent.models.common import sha256_hex
from scripts import verify_live_image_sources as cli
from tests.unit.test_live_image_contract import IMAGE_ID, images

FILES = {"src/app.py": b"reviewed code", "scripts/run.py": b"entry point"}
EXPECTED = {n: sha256_hex(b) for n, b in FILES.items()}


def archive(files=None, extra=None):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        for name, body in (FILES if files is None else files).items():
            member = tarfile.TarInfo("./" + name)
            member.size = len(body)
            tar.addfile(member, io.BytesIO(body))
        if extra is not None:
            tar.addfile(extra, io.BytesIO(b""))
    stream.seek(0)
    return stream


def test_exact_archive_and_root_directories_pass_without_extracting():
    root = tarfile.TarInfo(".")
    root.type = tarfile.DIRTYPE
    assert cli.verify_archive(archive(extra=root), EXPECTED) == EXPECTED


@pytest.mark.parametrize(
    "fault",
    [
        "changed",
        "missing",
        "extra",
        "duplicate",
        "parent",
        "absolute",
        "symlink",
        "hardlink",
        "device",
        "directory",
    ],
)
def test_stale_extra_or_ambiguous_image_contents_refuse(fault):
    files = dict(FILES)
    extra = None
    if fault == "changed":
        files["src/app.py"] = b"old code"
    elif fault == "missing":
        files.pop("src/app.py")
    elif fault == "extra":
        files["src/extra.py"] = b"unexpected"
    else:
        extra = tarfile.TarInfo(
            {"parent": "../outside", "absolute": "/outside", "duplicate": "src/app.py"}.get(
                fault, "src/link"
            )
        )
        extra.type = {
            "symlink": tarfile.SYMTYPE,
            "hardlink": tarfile.LNKTYPE,
            "device": tarfile.CHRTYPE,
            "directory": tarfile.DIRTYPE,
        }.get(fault, tarfile.REGTYPE)
        extra.linkname = "src/app.py"
    with pytest.raises(ValueError):
        cli.verify_archive(archive(files, extra), EXPECTED)


@pytest.mark.parametrize("fault", [None, "copy", "content", "changed_during_check", "cleanup"])
def test_stopped_immutable_container_cleanup_and_report(tmp_path, monkeypatch, fault):
    image, base = images()
    calls = []
    manifests = [EXPECTED, EXPECTED if fault != "changed_during_check" else {}]
    monkeypatch.setattr(cli, "source_manifest", lambda: manifests.pop(0))
    monkeypatch.setattr(cli, "inspect_image", lambda _: image)
    monkeypatch.setattr(cli, "inspect_base", lambda: base)

    def docker(*args):
        calls.append(args)
        assert args[0] in {"create", "rm"}
        if args[0] == "create":
            assert args[-1] == IMAGE_ID and "--network=none" in args and "--read-only" in args
            return "unused-container-id"
        if fault == "cleanup":
            raise OSError("cleanup failed")
        return "removed"

    def run(command, **kwargs):
        assert command[:2] == ["docker", "cp"]
        assert command[2].startswith("oasis-source-check-")
        assert command[2].endswith(":/app/.") and command[-1] == "-"
        assert kwargs["timeout"] == 60
        if fault == "copy":
            raise OSError("copy failed")
        kwargs["stdout"].write(archive({} if fault == "content" else None).getvalue())

    monkeypatch.setattr(cli, "docker", docker)
    monkeypatch.setattr(cli.subprocess, "run", run)
    out = tmp_path / "report.json"
    if fault:
        with pytest.raises((OSError, ValueError)):
            cli.verify("mutable-tag", out)
        assert not out.exists()
    else:
        report = cli.verify("mutable-tag", out)
        assert report["application_sources_verified"] and not report["execution_eligible"]
        assert not report["container_started"] and report["files"] == EXPECTED
        assert json.loads(out.read_text()) == report
    assert calls[-1] == ("rm", calls[0][2])


def test_existing_output_refuses_before_inventory_or_docker(tmp_path, monkeypatch):
    path = tmp_path / "report.json"
    path.write_text("old")
    monkeypatch.setattr(cli, "source_manifest", lambda: pytest.fail("must not inspect"))
    with pytest.raises(ValueError):
        cli.verify("image", path)
    assert path.read_text() == "old"


@pytest.mark.parametrize("fault", [None, "symlink", "missing_config"])
def test_local_manifest_reads_only_tracked_runtime_inputs(tmp_path, monkeypatch, fault):
    files = dict.fromkeys((*cli.INPUTS[2:], "src/app.py"), b"sample")
    for name, body in files.items():
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(body)
    (tmp_path / ".env").write_text("DO_NOT_READ")
    if fault == "symlink":
        (tmp_path / "src/app.py").unlink()
        (tmp_path / "src/app.py").symlink_to(tmp_path / ".env")
    elif fault == "missing_config":
        files.pop(cli.INPUTS[-1])
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(stdout=("\0".join(files) + "\0").encode()),
    )
    if fault:
        with pytest.raises(ValueError):
            cli.source_manifest(tmp_path)
    else:
        assert cli.source_manifest(tmp_path) == {n: sha256_hex(b) for n, b in files.items()}
