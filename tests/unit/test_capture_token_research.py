import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.capture_token_research import NoRedirect, cli, credentials, replay, run


def test_credentials_are_selected_and_do_not_execute_shell(tmp_path):
    path = tmp_path / "local.env"
    path.write_text("WALLET_PRIVATE_KEY=excluded\nOKX_API_KEY='literal$(false)'\n")
    assert credentials(path, ("OKX_API_KEY",)) == {"OKX_API_KEY": "literal$(false)"}
    path.write_text("OKX_API_KEY=one\nOKX_API_KEY=two\n")
    with pytest.raises(ValueError):
        credentials(path, ("OKX_API_KEY",))
    path.write_text("OKX_API_KEY=\n")
    with pytest.raises(ValueError):
        credentials(path, ("OKX_API_KEY",))


def test_redirect_never_forwards_credentials():
    with pytest.raises(ValueError):
        NoRedirect().redirect_request(None, None, 302, None, None, "https://other.invalid")


def fake_credentials(path, names):
    return dict.fromkeys(names, "fixture-credential")


def test_empty_market_different_from_failure_and_no_retry(tmp_path):
    def fake_cli(args):
        if args[0] == "config":
            return b""
        if args[0] == "--version":
            return b"fixture-version"
        raise ValueError("sensitive upstream error must not leak")

    with (
        patch("scripts.capture_token_research.credentials", fake_credentials),
        patch("scripts.capture_token_research.cli", fake_cli),
        patch("scripts.capture_token_research.time.sleep"),
        patch("scripts.capture_token_research.okx_query", return_value=b'{"code":0,"data":[]}'),
    ):
        run(tmp_path / "out", [8453, 4663], 2, 1, Path("unused"))
    raw = (tmp_path / "out/result.json").read_bytes()
    assert b"sensitive upstream" not in raw
    result = json.loads(raw)
    assert [r["status"] for r in result["captures"]] == [
        "failed",
        "captured",
        "skipped_provider_halted",
        "captured",
    ]
    assert result["discovery"]["items"] == []
    assert result["execution_eligible"] is False


def test_reflected_secret_is_not_persisted(tmp_path):
    with (
        patch("scripts.capture_token_research.credentials", fake_credentials),
        patch("scripts.capture_token_research.cli", return_value=b""),
        patch("scripts.capture_token_research.time.sleep"),
        patch(
            "scripts.capture_token_research.okx_query",
            return_value=b'{"code":0,"data":[],"msg":"fixture-credential"}',
        ),
    ):
        run(tmp_path / "out", [8453], 1, 0, Path("unused"))
    assert not list((tmp_path / "out").glob("*.raw.json"))
    assert b"fixture-credential" not in (tmp_path / "out/result.json").read_bytes()


def test_cli_drains_and_bounds_output(tmp_path, monkeypatch):
    executable = tmp_path / "gmgn-cli"
    executable.write_text("#!/bin/sh\nprintf '%s' 'test-output'\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert cli(["--version"]) == b"test-output"
    with patch("scripts.capture_token_research.MAX_BYTES", 2), pytest.raises(ValueError):
        cli(["--version"])


def test_scope_rejected_before_credentials(tmp_path):
    for chains, limit, enrich in (([1], 1, 1), ([8453], 26, 0), ([8453], 1, 6)):
        with pytest.raises(ValueError):
            run(tmp_path / "out", chains, limit, enrich, Path("absent"))
    assert not (tmp_path / "out").exists()


def test_offline_replay_detects_raw_and_claim_tampering(tmp_path):
    token = "0x" + "12" * 20
    body = json.dumps(
        {
            "code": 0,
            "data": {"rank": [{"chain": "base", "address": token, "rank": 1, "holder_count": 20}]},
        }
    ).encode()

    def fake_cli(args):
        if args[0] == "config":
            return b""
        if args[0] == "--version":
            return b"fixture-version"
        return body

    with (
        patch("scripts.capture_token_research.credentials", fake_credentials),
        patch("scripts.capture_token_research.cli", fake_cli),
        patch("scripts.capture_token_research.time.sleep"),
        patch("scripts.capture_token_research.okx_query", return_value=b'{"code":0,"data":[]}'),
    ):
        run(tmp_path / "out", [8453], 1, 0, Path("unused"))
    assert replay(tmp_path / "out")["replayed_observations"] == 1
    result = tmp_path / "out/result.json"
    original = result.read_bytes()
    payload = json.loads(original)
    payload["provider_observations"][0]["claims"]["holder_count"] = "999"
    result.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="claim replay mismatch"):
        replay(tmp_path / "out")
    result.write_bytes(original)
    for mutate in (
        lambda p: p["captures"][0].update(status="ignored"),
        lambda p: p["captures"][0].update(normalization="failed"),
        lambda p: p["captures"].pop(),
        lambda p: p["field_units"]["gmgn/trending"].update(top_10_holder_rate="percent"),
        lambda p: p["scope"].update(chains=[4663]),
        lambda p: p.update(selected_for_enrichment=[{}]),
    ):
        modified = json.loads(original)
        mutate(modified)
        result.write_text(json.dumps(modified))
        with pytest.raises(ValueError):
            replay(tmp_path / "out")
    result.write_bytes(original)
    raw_path = tmp_path / "out" / (payload["captures"][0]["raw_sha256"] + ".raw.json")
    raw_path.write_bytes(body + b" ")
    with pytest.raises(ValueError, match="hash mismatch"):
        replay(tmp_path / "out")
