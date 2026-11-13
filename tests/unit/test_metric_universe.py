"""Same addresses across chains must not merge markets or evidence."""

import copy
import json
from unittest.mock import patch

import httpx
import pytest

from oasis_agent.arbitrage.metric_universe import UniversePlan, discover, requests
from scripts import discover_metric as cli
from scripts import research_metric as recorder
from tests.fixtures.metric_research import P0, P1, inputs


def data(scenario="synthetic"):
    return {
        "schema": "oasis-metric-universe-v1",
        "scenario": scenario,
        "chain_ids": [1, 8453, 4663, 99901],
        "pools_per_chain": 32,
        "max_capture_seconds": 60,
    }


def plan(raw=None):
    return UniversePlan.model_validate_json(json.dumps(raw or data()))


def metadata():
    m, _ = inputs()
    m.update(total=2, nextOffset=None)
    return m


def payloads():
    return [metadata() for _ in range(4)]


def test_every_chain_keeps_its_own_identical_addresses():
    r = discover(plan(), payloads())
    assert [c["chain_id"] for c in r["chains"]] == [1, 8453, 4663, 99901]
    assert all(c["pairs"][0]["same_pair_pool_count"] == 2 for c in r["chains"])
    assert r["cross_chain_routes"] == []
    assert r["net_pnl"] is None and not r["execution_eligible"]


def test_reverse_orientation_groups_identity_but_keeps_pool_direction():
    ps = payloads()
    row = ps[0]["data"][1]
    row["token0"], row["token1"] = row["token1"], row["token0"]
    row["token0Decimals"], row["token1Decimals"] = row["token1Decimals"], row["token0Decimals"]
    r = discover(plan(), ps)
    pair = r["chains"][0]["pairs"][0]
    assert pair["same_pair_pool_count"] == 2
    assert pair["pools"][0]["token0"] != pair["pools"][1]["token0"]


def test_empty_and_partial_are_not_promoted_to_market_coverage():
    ps = payloads()
    ps[0] = {"data": [], "total": 0, "nextOffset": None}
    ps[1].update(total=80, nextOffset=2)
    r = discover(plan(), ps)
    assert r["chains"][0]["pairs"] == []
    assert r["chains"][1]["coverage"] == "bounded_partial"
    assert len(requests(plan())) == 4
    assert all(x["params"]["offset"] == 0 for x in requests(plan()))


@pytest.mark.parametrize(
    "field,value",
    [
        ("chain_ids", [1, 1]),
        ("chain_ids", [True]),
        ("chain_ids", [1.0]),
        ("chain_ids", []),
        ("pools_per_chain", 33),
        ("max_capture_seconds", 121),
    ],
)
def test_plan_bounds(field, value):
    p = data()
    p[field] = value
    with pytest.raises(ValueError):
        plan(p)


@pytest.mark.parametrize(
    "field,value",
    [
        ("poolAddress", P0),
        ("token0Decimals", 6),
        ("token1Decimals", True),
        ("token0", "0x" + "00" * 20),
        ("pauseLevel", None),
    ],
)
def test_duplicate_conflicting_or_malformed_metadata_rejected(field, value):
    ps = payloads()
    ps[0]["data"][1][field] = value
    with pytest.raises(ValueError):
        discover(plan(), ps)


def test_excluded_pools_are_not_silently_discarded():
    ps = payloads()
    ps[0]["data"][0]["pauseLevel"] = 1
    ps[0]["data"][1]["contractVersion"] = "v9"
    c = discover(plan(), ps)["chains"][0]
    assert not c["pairs"]
    assert c["issues"] == [
        {"pool": P0, "reason": "pool_paused"},
        {"pool": P1, "reason": "unsupported_contract_version"},
    ]


@pytest.mark.parametrize(
    "update",
    [
        {"total": 0},
        {"total": True},
        {"nextOffset": 0},
        {"nextOffset": 2},
        {"total": 3, "nextOffset": None},
    ],
)
def test_incoherent_page_coverage_rejected(update):
    ps = payloads()
    ps[0].update(update)
    with pytest.raises(ValueError):
        discover(plan(), ps)


def test_capture_replay_preserves_all_chains_and_integrity(tmp_path):
    path, directory = tmp_path / "plan.json", tmp_path / "capture"
    path.write_text(json.dumps(data("provider_observation")))
    seen = []

    def handle(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=metadata())

    client = httpx.Client(transport=httpx.MockTransport(handle))
    with patch.object(recorder.httpx, "Client", return_value=client):
        assert cli.capture(path, directory) == 0
    r = cli.replay(directory)
    assert len(seen) == 4 and len(r["chains"]) == 4
    assert r == cli.replay(directory)
    assert r["response_body_bytes_retained"]
    with pytest.raises(FileExistsError):
        cli.capture(path, directory)
    raw = (directory / "2.response.json").read_bytes()
    (directory / "2.response.json").write_bytes(raw + b" ")
    with pytest.raises(ValueError, match="integrity"):
        cli.replay(directory)


def test_failure_stops_whole_campaign_without_next_chain_or_retry(tmp_path):
    path, directory = tmp_path / "plan.json", tmp_path / "capture"
    path.write_text(json.dumps(data("provider_observation")))
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(429)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    with patch.object(recorder.httpx, "Client", return_value=client):
        assert cli.capture(path, directory) == 3
    assert len(calls) == 1
    assert json.loads((directory / "capture.json").read_text())["status"] == "incomplete"
    with pytest.raises(ValueError):
        cli.replay(directory)


def test_single_chain_capture_cannot_be_relabelled_multichain(tmp_path):
    p = data()
    p["chain_ids"] = [1]
    with pytest.raises(ValueError, match="coverage"):
        discover(plan(p), payloads())
    ps = payloads()
    ps[0]["data"].extend(copy.deepcopy(ps[0]["data"]) * 16)
    with pytest.raises(ValueError, match="coverage"):
        discover(plan(), ps)
