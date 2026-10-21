"""Strategy endpoint resolution must never load an incidental wallet credential file."""

from pathlib import Path

import pytest

from scripts import run_live_lp as cli


@pytest.mark.parametrize("value", [None, "", "  "])
def test_missing_rpc_never_reads_dotenv(tmp_path, monkeypatch, value):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("BASE_RPC_PRIMARY_ALT=https://fixture.invalid\n")
    if value is None:
        monkeypatch.delenv("BASE_RPC_PRIMARY_ALT", raising=False)
    else:
        monkeypatch.setenv("BASE_RPC_PRIMARY_ALT", value)

    def forbidden(*args, **kwargs):
        raise AssertionError("strategy must not read a mixed credential file")

    monkeypatch.setattr(Path, "read_text", forbidden)
    with pytest.raises(SystemExit, match="missing or empty"):
        cli.rpc_url("BASE_RPC_PRIMARY_ALT")


def test_explicit_endpoint_is_returned_without_file_access(monkeypatch):
    endpoint = "https://fixture.invalid/rpc"
    monkeypatch.setenv("BASE_RPC_PRIMARY_ALT", endpoint)

    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected credential file access")

    monkeypatch.setattr(Path, "read_text", forbidden)
    assert cli.rpc_url("BASE_RPC_PRIMARY_ALT") == endpoint


def test_missing_environment_diagnostic_does_not_echo_input(monkeypatch):
    name = "untrusted-input-marker"
    monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as caught:
        cli.rpc_url(name)
    assert name not in str(caught.value)
