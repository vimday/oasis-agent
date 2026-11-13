"""Adversarial provider claims, integer depth economics and retained capture integrity."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx
import pytest

from oasis_agent.arbitrage.metric import (
    MAX128,
    MetricPlan,
    depth_fill,
    parse_book,
    raw_uint,
    requests,
    screen,
)
from oasis_agent.models.common import canonical_json, sha256_hex
from scripts import research_metric as cli
from tests.fixtures.metric_research import P0, P1, T1, book, inputs, level, plan_data


def plan(data=None):
    return MetricPlan.model_validate_json(json.dumps(data or plan_data()))


def evaluate(data=None, metadata=None, batch=None):
    m, b = inputs()
    return screen(plan(data), metadata or m, batch or b, 101)


def test_closed_cycle_uses_native_amounts_and_preserves_all_alternatives():
    result = evaluate()
    assert result["planned_points"] == 4
    first = result["points"][0]
    assert first["fills"][0]["output_raw"] == 2_100_000_000
    assert first["fills"][0]["asset_in"]["decimals"] == 18
    assert first["fills"][0]["asset_out"]["address"] == T1
    assert first["fills"][0]["asset_out"]["decimals"] == 6
    assert first["fills"][1]["asset_in"] == first["fills"][0]["asset_out"]
    assert first["fills"][1]["asset_out"] == first["fills"][0]["asset_in"]
    assert first["output_raw"] == 2_100_000_000 * 10**19 // 20_010_000_000
    assert first["net_estimate_raw"] == first["output_raw"] - 10**18
    assert result["positive_estimates"] == 2
    assert result["points"][2]["net_estimate_raw"] < 0
    assert result["execution_eligible"] is False
    assert result["window_realized_pnl_raw"] is None
    assert result["block_identity"] is None


def test_reverse_start_token_and_costs_have_explicit_units():
    data = plan_data()
    data.update(start_token=1, sizes_raw=[2001 * 10**6])
    data["costs"][0]["raw"] = 4 * 10**6
    result = evaluate(data)
    point = result["points"][1]
    assert point["output_raw"] == 2100 * 10**6
    assert point["net_estimate_raw"] == 95 * 10**6
    assert result["reporting_asset"]["address"] == T1


@pytest.mark.parametrize("value", [True, 1, 1.1, "01", "-1", "1e18", "", "NaN", str(2**256)])
def test_money_never_coerces(value):
    with pytest.raises(ValueError):
        raw_uint(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("serverTs", 85),
        ("serverTs", 102),
        ("serverTs", True),
        ("priceProviderStatus", "feed_down"),
        ("depth", None),
        ("poolAddress", P1),
        ("bidAdj", str(3000 * 2**64)),
        ("totalToken1Available", "1"),
    ],
)
def test_stale_future_paused_identity_and_missing_depth_clear_candidate(field, value):
    _, batch = inputs()
    batch["results"][0]["quote"][field] = value
    result = evaluate(batch=batch)
    assert result["positive_estimates"] == 0
    assert all(p["net_estimate_raw"] is None for p in result["points"])


def test_one_sided_healthy_quote_is_not_usable_in_that_direction():
    _, batch = inputs()
    q = batch["results"][0]["quote"]
    q.update(bidAdj="0")
    q["depth"]["bids"] = []
    result = evaluate(batch=batch)
    assert result["points"][0]["output_raw"] is None
    assert "partial_or_empty_depth" in result["points"][0]["reasons"]
    assert result["points"][2]["net_estimate_raw"] is not None
    q.update(askAdj=str(MAX128))
    q["depth"]["asks"] = []
    assert all(p["net_estimate_raw"] is None for p in evaluate(batch=batch)["points"])


def test_healthy_price_with_empty_liquidity_is_incomplete_evidence():
    _, batch = inputs()
    q = batch["results"][1]["quote"]
    q.update(totalToken0Available="0", totalToken1Available="1")
    for side in ("bids", "asks"):
        for row in q["depth"][side]:
            row.update(cumulativeInputVolume="0", cumulativeVolume="0")
    result = evaluate(batch=batch)
    assert result["decision"] == "incomplete_evidence"
    assert result["depth_complete_points"] == result["cost_complete_points"] == 0
    assert result["planned_points"] == 4


def test_repeated_empty_bins_rounding_and_partial_fill():
    q = book(P0, 2, 3)
    q["depth"]["bids"] = [
        level(0, 2 * 2**64, 3, 5),
        level(-1, 2**64, 3, 5),
        level(-2, 2**64, 7, 7),
    ]
    b = parse_book(q, P0, 101, plan())
    assert depth_fill(b, 0, 2).output_raw == 3
    assert depth_fill(b, 0, 4).output_raw == 5
    assert depth_fill(b, 0, 8).unfilled_raw == 1
    assert depth_fill(b, 0, 8).consumed_raw == 7
    previous = 0
    for size in range(1, 12):
        fill = depth_fill(b, 0, size)
        assert fill.consumed_raw + fill.unfilled_raw == size
        assert previous <= fill.output_raw <= 7
        previous = fill.output_raw


@pytest.mark.parametrize(
    "update",
    [
        {"cumulativeInputVolume": "0"},
        {"cumulativeVolume": "0"},
        {"binIdx": True},
        {"price": str(MAX128)},
        {"price": str(3000 * 2**64)},
    ],
)
def test_invalid_depth_rejects_whole_book(update):
    q = book(P0)
    q["depth"]["bids"][0].update(update)
    with pytest.raises(ValueError):
        parse_book(q, P0, 101, plan())


@pytest.mark.parametrize("field", ["capital_raw", "commitments_raw", "reserve_raw"])
def test_missing_capital_never_becomes_zero(field):
    data = plan_data()
    data[field] = None
    assert all(p["net_estimate_raw"] is None for p in evaluate(data)["points"])


def test_cost_unknown_overflow_and_competing_commitments():
    for cost in (None, 2**256 - 1):
        data = plan_data()
        data["costs"][0]["raw"] = cost
        data["costs"][1]["raw"] = 1
        assert evaluate(data)["positive_estimates"] == 0
    data = plan_data()
    data.update(capital_raw=10**18, commitments_raw=1)
    assert evaluate(data)["positive_estimates"] == 0


def test_mismatch_gaps_errors_and_skew_are_not_no_opportunity():
    metadata, batch = inputs()
    metadata["data"][0]["token0Decimals"] = 6
    assert evaluate(metadata=metadata)["positive_estimates"] == 0
    metadata, batch = inputs()
    batch["results"][1] = {"poolAddress": P1, "error": {"status": 503}}
    assert evaluate(batch=batch)["pool_issues"][P1] == "provider_quote_error"
    batch["results"].pop()
    assert evaluate(batch=batch)["pool_issues"][P1] == "quote_missing"
    _, batch = inputs()
    batch["results"][1]["quote"]["serverTs"] = 97
    assert "snapshot_skew" in evaluate(batch=batch)["points"][0]["reasons"]


def test_duplicate_and_ambiguous_entries_rejected():
    for ambiguous in (False, True):
        _, batch = inputs()
        if ambiguous:
            batch["results"][0]["error"] = {}
        else:
            batch["results"][1] = copy.deepcopy(batch["results"][0])
        with pytest.raises(ValueError):
            evaluate(batch=batch)


def retained(tmp_path):
    data = plan_data()
    data["scenario"] = "provider_observation"
    source = tmp_path / "plan.json"
    source.write_bytes(canonical_json(data))
    destination = tmp_path / "capture"
    meta, batch = inputs()
    calls = []

    def handler(request):
        calls.append(request)
        assert "authorization" not in request.headers
        assert request.url.host == "api.metric.xyz"
        return httpx.Response(200, json=meta if len(calls) == 1 else batch)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with (
        patch.object(cli.httpx, "Client", return_value=client),
        patch.object(cli.time, "time", return_value=101),
    ):
        assert cli.capture(source, destination) == 0
    assert len(calls) == 2
    return source, destination


def test_offline_replay_is_deterministic_and_refuses_overwrite(tmp_path):
    source, directory = retained(tmp_path)
    a, b = cli.replay(directory), cli.replay(directory)
    assert canonical_json(a) == canonical_json(b)
    assert a["evidence_class"] == "provider_claim"
    with pytest.raises(FileExistsError):
        cli.capture(source, directory)
    out = tmp_path / "report.json"
    cli.publish(out, a)
    with pytest.raises(FileExistsError):
        cli.publish(out, b)


@pytest.mark.parametrize("filename", ["plan.json", "0.request.json", "1.response.json"])
def test_capture_tampering_refused(tmp_path, filename):
    _, directory = retained(tmp_path)
    with (directory / filename).open("ab") as handle:
        handle.write(b" ")
    with pytest.raises(ValueError):
        cli.replay(directory)


def test_hashes_do_not_hide_incoherent_or_future_clocks(tmp_path):
    _, directory = retained(tmp_path)
    manifest = json.loads((directory / "capture.json").read_bytes())
    manifest["exchanges"][1]["received"] = 100
    (directory / "capture.json").write_bytes(canonical_json(manifest))
    with pytest.raises(ValueError, match="clock"):
        cli.replay(directory)


def test_unknown_capture_fields_are_not_reflected_into_reports(tmp_path):
    _, directory = retained(tmp_path)
    manifest = json.loads((directory / "capture.json").read_bytes())
    manifest["exchanges"][1]["unexpected"] = "untrusted metadata"
    (directory / "capture.json").write_bytes(canonical_json(manifest))
    with pytest.raises(ValueError, match="fields"):
        cli.replay(directory)


def test_http_refusal_retains_body_stops_and_never_retries(tmp_path):
    source = tmp_path / "plan.json"
    data = plan_data()
    data["scenario"] = "provider_observation"
    source.write_bytes(canonical_json(data))
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, content=b'{"error":"rate limited"}')

    client = httpx.Client(transport=httpx.MockTransport(handler))
    directory = tmp_path / "capture"
    with patch.object(cli.httpx, "Client", return_value=client):
        assert cli.capture(source, directory) == 3
    assert len(calls) == 1
    manifest = json.loads((directory / "capture.json").read_bytes())
    assert manifest["status"] == "incomplete"
    assert manifest["exchanges"][0]["response_sha256"] == sha256_hex(
        (directory / "0.response.json").read_bytes()
    )
    with pytest.raises(ValueError, match="incomplete"):
        cli.replay(directory)


def test_requests_bind_chain_pools_and_depth_without_auth():
    p = plan()
    specs = requests(p)
    assert specs[1]["json"] == {"pools": [P0, P1], "depth": True}
    assert specs[1]["url"] == "https://api.metric.xyz/public/v1/evm/1/bid_ask/batch"


@pytest.mark.parametrize("direction", [True, False, 0.0, "1"])
def test_direction_does_not_accept_bool_float_or_string(direction):
    data = plan_data()
    data["start_token"] = direction
    with pytest.raises(ValueError):
        plan(data)


def test_unit_rescaling_preserves_estimate_up_to_conservative_rounding():
    metadata, batch = inputs()
    original = evaluate()["points"][0]
    data = plan_data()
    data["sizes_raw"] = [10**19]
    for token in data["tokens"]:
        token["decimals"] += 1
    for row in metadata["data"]:
        row["token0Decimals"] += 1
        row["token1Decimals"] += 1
    for row in batch["results"]:
        q = row["quote"]
        for name in ("totalToken0Available", "totalToken1Available"):
            q[name] = str(int(q[name]) * 10)
        for side in ("bids", "asks"):
            for depth_level in q["depth"][side]:
                for name in ("cumulativeInputVolume", "cumulativeVolume"):
                    depth_level[name] = str(int(depth_level[name]) * 10)
    rescaled = evaluate(data, metadata, batch)["points"][0]
    assert 0 <= rescaled["net_estimate_raw"] - original["net_estimate_raw"] * 10 < 10


def test_two_writers_publish_exactly_one_complete_report(tmp_path):
    out = tmp_path / "report.json"

    def attempt(i):
        try:
            cli.publish(out, {"writer": i})
            return True
        except FileExistsError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(attempt, range(2))) == 1
    assert json.loads(out.read_bytes())["writer"] in (0, 1)


@pytest.mark.parametrize("failure", ["timeout", "html", "oversize", "redirect"])
def test_partial_transport_failures_are_retained_without_followup(tmp_path, failure):
    data = plan_data()
    data["scenario"] = "provider_observation"
    source = tmp_path / "plan.json"
    source.write_bytes(canonical_json(data))
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("provider timeout", request=request)
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "https://untrusted.invalid"})
        return httpx.Response(
            200, content=b"x" * (cli.LIMIT + 1) if failure == "oversize" else b"html"
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    directory = tmp_path / "capture"
    with patch.object(cli.httpx, "Client", return_value=client):
        assert cli.capture(source, directory) == 3
    assert len(calls) == 1
    with pytest.raises(ValueError, match="incomplete"):
        cli.replay(directory)
