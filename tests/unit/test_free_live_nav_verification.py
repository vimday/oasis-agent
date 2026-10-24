"""An offline acceptance probe must not certify a stale head or repeat a campaign."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from oasis_agent.execution import account_capture
from oasis_agent.live.config import LiveConfig
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts import verify_free_live_nav as probe
from tests.unit.test_live_receipt_costs import position_config
from tests.unit.test_live_valuation import NavFixture, synthetic_feed_pin  # noqa: F401


@pytest.fixture(autouse=True)
def isolated_budget(tmp_path, monkeypatch):
    path = tmp_path / "existing-budget.db"
    monkeypatch.setattr(probe, "BUDGET", path, raising=False)
    monkeypatch.setattr(probe, "POLICIES", tuple(BudgetPolicy(s, 32, 0) for s in probe.URLS))
    with RpcBudget(path, probe.POLICIES):
        pass
    return path


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


@pytest.mark.parametrize("fault", ["missing", "corrupt", "symlink", "quota"])
def test_new_output_cannot_create_or_replace_budget(tmp_path, monkeypatch, isolated_budget, fault):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not create a network session")

    monkeypatch.setattr(probe, "one_shot_session", forbidden)
    if fault == "missing":
        isolated_budget.unlink()
    elif fault == "corrupt":
        isolated_budget.write_bytes(b"invalid database")
    elif fault == "symlink":
        target = tmp_path / "original.db"
        isolated_budget.rename(target)
        isolated_budget.symlink_to(target)
    else:
        with RpcBudget(isolated_budget, probe.POLICIES, require_existing=True) as ledger:
            for i in range(9):
                assert ledger.reserve("base_public", now_ms=1000 * (i + 1)) == 0
    before = isolated_budget.read_bytes() if isolated_budget.exists() else None
    for name in ("first", "renamed-output"):
        output = tmp_path / name
        report = probe.verify(output, LiveConfig.load(Path("configs/live.base-eth-usdc.json")))
        assert report["nav_verified"] is False
        assert report["captures"] == []
        assert not (output / "budget.db").exists()
        assert not (output / "evidence.db").exists()
    if before is None:
        assert not isolated_budget.exists()
    else:
        assert isolated_budget.read_bytes() == before


def test_budget_disappearance_during_capture_is_not_recreated(
    tmp_path, monkeypatch, isolated_budget
):
    class Rpc:
        def request(self, *args):
            isolated_budget.unlink()
            raise ValueError("SENSITIVE_PROVIDER_ERROR")

    def session(source, **kwargs):
        assert kwargs["require_existing_budget"] is True
        assert kwargs["budget"] == isolated_budget
        return Rpc(), SimpleNamespace(exchanges=[]), source

    monkeypatch.setattr(probe, "one_shot_session", session)
    report = probe.verify(
        tmp_path / "capture", LiveConfig.load(Path("configs/live.base-eth-usdc.json"))
    )
    assert not report["nav_verified"] and report["request_counts"] is None
    assert "SENSITIVE" not in json.dumps(report)
    assert not isolated_budget.exists()


@pytest.mark.parametrize("expire", [False, True])
def test_capture_spends_original_quota_and_refuses_renamed_output(
    tmp_path, monkeypatch, isolated_budget, expire
):
    calls = []
    fixture = NavFixture()

    class PublicFixture:
        def post(self, raw):
            request = json.loads(raw)
            calls.append(request["method"])
            if request["method"] == "eth_getBlockByNumber" and request["params"][0] == "latest":
                request["params"][0] = hex(fixture.block_number)
            return fixture.post(canonical_json(request))

    monkeypatch.setattr(account_capture, "_UrlLibPoster", lambda *args: PublicFixture())
    monkeypatch.setattr(probe.time, "time", lambda: fixture.timestamp)
    if expire:
        fixture.usdc_age = 1800
        capture = probe.capture_nav

        def capture_then_advance(*args, **kwargs):
            result = capture(*args, **kwargs)
            monkeypatch.setattr(probe.time, "time", lambda: fixture.timestamp + 1)
            return result

        monkeypatch.setattr(probe, "capture_nav", capture_then_advance)
    report = probe.verify(tmp_path / "first", position_config())
    assert report["nav_verified"] is (not expire) and report["execution_eligible"] is False
    assert report["request_limits"] == {"base_public": 24, "publicnode": 23}
    assert report["request_counts"] == report["request_limits"]
    assert len(calls) == 47
    second = probe.verify(tmp_path / "renamed", position_config())
    assert second["nav_verified"] is False and second["captures"] == []
    assert second["request_counts"] == report["request_counts"]
    assert len(calls) == 47
    assert not (tmp_path / "renamed" / "budget.db").exists()


def test_cli_refusal_is_nonzero_and_cwd_does_not_select_budget(
    tmp_path, monkeypatch, isolated_budget
):
    isolated_budget.unlink()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify_free_live_nav", "--out", "report"])
    with pytest.raises(SystemExit) as refusal:
        probe.main()
    assert refusal.value.code == 3
    assert not isolated_budget.exists()
    assert not Path("report/budget.db").exists()
