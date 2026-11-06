"""Redacted NAV refusal diagnostics over synthetic joint capture and disposable state."""

import json

import pytest

from oasis_agent.live.valuation import NavRefusal
from oasis_agent.models.common import canonical_json, sha256_hex
from scripts import capture_live_recovery as cli
from tests.unit import test_live_joint_recovery_capture as joint
from tests.unit import test_live_recovery_capture as fixtures
from tests.unit.test_live_valuation import synthetic_feed_pin  # noqa: F401


def assert_stopped_after_nav(tmp_path, data, result, before):
    roots, _, _, posters, _, _ = data
    assert result["capture_passed"] is False and result["execution_eligible"] is False
    assert result["status"] == "refused" and result["exit_code"] == 3
    assert result["request_counts"] == {
        "strategy": dict(zip(cli.SOURCES, (43, 42), strict=True)),
        "authority": dict.fromkeys(cli.SOURCES, 0),
    }
    requests = [request for poster in posters.values() for request in poster.requests]
    assert len(requests) == 85
    assert all(request["method"] != "eth_getTransactionReceipt" for request in requests)
    wire = [
        json.loads(line) for line in (tmp_path / "attempt/wire.jsonl").read_bytes().splitlines()
    ]
    finished = [event for event in wire if event["phase"] == "request_finished"]
    assert len(wire) == 170 and len(finished) == 85
    assert all(
        event["outcome"] == "response_received" and event["failure_category"] is None
        for event in finished
    )
    assert fixtures.unchanged_files(roots) == before
    for name in (
        "candidate-references.json",
        "candidate-runtime-references.json",
        *cli.RUNTIME_PUBLICATIONS,
    ):
        assert not (tmp_path / "attempt" / name).exists()
    retained = json.loads((tmp_path / "attempt/report.json").read_bytes())
    assert canonical_json(retained) == canonical_json(result)
    digest = retained.pop("evidence_hash")
    assert digest == sha256_hex(canonical_json(retained))


@pytest.mark.parametrize("feed", ["eth", "usdc"])
def test_stale_feed_reports_safe_code_after_successful_raw_nav_capture(tmp_path, monkeypatch, feed):
    data = joint.setup(tmp_path, monkeypatch)
    before = fixtures.unchanged_files(data[0])
    for poster in data[3].values():
        setattr(poster.base.current, f"{feed}_age", 1801)
    result = joint.run(tmp_path, data)
    assert_stopped_after_nav(tmp_path, data, result, before)
    assert result["refusal_code"] == f"nav_{feed}_usd_feed_incomplete_stale_or_future"


class HostileText:
    def __str__(self):
        raise AssertionError("exception diagnostic text must never be rendered")


class HostileNavRefusal(NavRefusal):
    def __str__(self):
        raise AssertionError("exception diagnostic text must never be rendered")


@pytest.mark.parametrize(
    "error",
    [
        NavRefusal("HOSTILE_DIAGNOSTIC_PAYLOAD https://rpc.invalid/private-token"),
        NavRefusal("NAV USDC / USD feed incomplete, stale or future HOSTILE_DIAGNOSTIC_PAYLOAD"),
        NavRefusal("NAV USDC / USD feed incomplete, stale or future", "HOSTILE_DIAGNOSTIC_PAYLOAD"),
        ValueError("NAV USDC / USD feed incomplete, stale or future"),
        NavRefusal(HostileText()),
        HostileNavRefusal("NAV USDC / USD feed incomplete, stale or future"),
    ],
    ids=["unknown", "suffix", "extra_argument", "wrong_type", "hostile_argument", "subclass"],
)
def test_unknown_nav_failure_remains_generic_without_rendering_exception(
    tmp_path, monkeypatch, error
):
    data = joint.setup(tmp_path, monkeypatch)
    before = fixtures.unchanged_files(data[0])
    capture_nav = cli.capture_nav

    def refuse_after_nav(*args, **kwargs):
        capture_nav(*args, **kwargs)
        raise error

    monkeypatch.setattr(cli, "capture_nav", refuse_after_nav)
    result = joint.run(tmp_path, data)
    assert_stopped_after_nav(tmp_path, data, result, before)
    assert result["refusal_code"] == "capture_unresolved"
    report = (tmp_path / "attempt/report.json").read_bytes()
    assert b"HOSTILE_DIAGNOSTIC_PAYLOAD" not in report
    assert b"rpc.invalid" not in report and b"private-token" not in report
    assert b"NAV USDC / USD" not in report
