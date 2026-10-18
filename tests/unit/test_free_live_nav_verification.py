"""An offline acceptance probe must not certify a stale head or repeat a campaign."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from oasis_agent.live.config import LiveConfig
from oasis_agent.models.common import canonical_json, sha256_hex
from scripts import verify_free_live_nav as probe


@pytest.mark.parametrize("timestamp", [None, 879, 1001])
def test_head_freshness_refuses_missing_stale_and_future(monkeypatch, timestamp):
    monkeypatch.setattr(probe.time, "time", lambda: 1000)
    with pytest.raises(ValueError):
        probe.require_recent_head(timestamp)


@pytest.mark.parametrize("timestamp", [880, 1000])
def test_head_freshness_accepts_boundary(monkeypatch, timestamp):
    monkeypatch.setattr(probe.time, "time", lambda: 1000)
    probe.require_recent_head(timestamp)


def test_existing_campaign_refused_before_any_rpc(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not make a network session")

    monkeypatch.setattr(probe, "one_shot_session", forbidden)
    config = LiveConfig.load(Path("configs/live.base-eth-usdc.json"))
    with pytest.raises(ValueError, match="repeated campaign"):
        probe.verify(tmp_path, config)


def test_failure_retains_redacted_report_without_more_calls(tmp_path, monkeypatch):
    calls = []

    class Rpc:
        def request(self, *args):
            calls.append(args)
            raise ValueError("SENSITIVE_PROVIDER_ERROR")

    monkeypatch.setattr(
        probe,
        "one_shot_session",
        lambda source, **kwargs: (Rpc(), SimpleNamespace(exchanges=[]), source),
    )
    report = probe.verify(
        tmp_path / "campaign", LiveConfig.load(Path("configs/live.base-eth-usdc.json"))
    )
    assert not report["nav_verified"] and not report["execution_eligible"]
    assert len(calls) == 1 and len(report["captures"]) == 1
    assert "SENSITIVE_PROVIDER_ERROR" not in json.dumps(report)
    digest = report.pop("evidence_hash")
    assert sha256_hex(canonical_json(report)) == digest
