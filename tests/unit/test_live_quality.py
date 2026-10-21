"""Actual child exit codes and timeouts must survive quality orchestration."""

import json
import sys
import time
from pathlib import Path

import pytest

from oasis_agent.models.common import canonical_json, sha256_hex
from scripts import check_live_quality as cli


def command(source):
    return (sys.executable, "-c", source)


def test_failed_check_cannot_be_masked_by_later_success(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli,
        "CHECKS",
        (
            ("first", command("print('first diagnostic'); raise SystemExit(7)")),
            ("later", command("print('must not run')")),
        ),
    )
    directory = tmp_path / "run"
    report = cli.run_checks(directory)
    assert report["passed"] is False
    assert len(report["checks"]) == 1
    assert report["checks"][0]["exit_code"] == 7
    assert (directory / "first.log").read_text().strip() == "first diagnostic"
    assert not (directory / "later.log").exists()
    assert json.loads((directory / "report.json").read_bytes()) == report


def test_all_exits_required_and_logs_hashed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli,
        "CHECKS",
        (
            ("first", command("print('first')")),
            ("second", command("print('second')")),
        ),
    )
    directory = tmp_path / "run"
    report = cli.run_checks(directory)
    assert report["passed"] is True and report["execution_eligible"] is False
    for row in report["checks"]:
        assert row["exit_code"] == 0 and row["passed"] is True
        assert row["log_hash"] == sha256_hex((directory / row["log"]).read_bytes())
    digest = report.pop("evidence_hash")
    assert digest == sha256_hex(canonical_json(report))
    with pytest.raises(FileExistsError):
        cli.run_checks(directory)


def test_launch_failure_is_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CHECKS", (("missing", (str(tmp_path / "missing-executable"),)),))
    report = cli.run_checks(tmp_path / "run")
    assert report["passed"] is False
    assert report["checks"][0]["launch_failed"] is True
    assert report["checks"][0]["exit_code"] is None


def test_timeout_is_retained_and_descendants_do_not_continue(tmp_path, monkeypatch):
    marker = tmp_path / "orphan-marker"
    child = f"import time; from pathlib import Path; time.sleep(2); Path({str(marker)!r}).touch()"
    source = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)"
    )
    monkeypatch.setattr(cli, "CHECKS", (("slow", command(source)),))
    report = cli.run_checks(tmp_path / "run", timeout=1)
    assert report["passed"] is False and report["checks"][0]["timed_out"] is True
    # Bounded wait outside the killed group establishes that the child cannot finish.
    time.sleep(1.5)
    assert not marker.exists()


@pytest.mark.parametrize("timeout", [True, 0, 601])
def test_invalid_timeout_writes_nothing(tmp_path: Path, timeout):
    directory = tmp_path / "run"
    with pytest.raises(ValueError):
        cli.run_checks(directory, timeout=timeout)
    assert not directory.exists()
