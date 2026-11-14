"""ABI, identity, full-input and cross-source regressions for conditional quotes."""

import copy
import json

import pytest
from eth_hash.auto import keccak

from oasis_agent.arbitrage.cycle_evidence import load_exports
from oasis_agent.arbitrage.metric_v3 import CrossVenuePlan, evaluate, metric_calldata
from oasis_agent.uniswap_v4.liquidity_math import MAX_SQRT_PRICE, MIN_SQRT_PRICE
from scripts import research_metric_v3 as cli
from tests.fixtures.metric_v3 import (
    ETH,
    IMMUTABLE_ABI,
    IMMUTABLES,
    METRIC_SIG,
    MP,
    MQ,
    USD,
    V3_SIG,
    VQ,
    block_hash,
    bundle,
    call,
    data,
    plan_data,
)


def plan(p=None):
    return CrossVenuePlan.model_validate_json(json.dumps(p or plan_data()))


def run(tmp_path, p=None, a=None, b=None):
    p = plan(p)
    inputs = []
    for source, value in (("alpha", a), ("beta", b)):
        if source not in p.sources:
            continue
        path = tmp_path / f"{source}.json"
        path.write_text(json.dumps(bundle(source) if value is None else value))
        inputs.append((source, path))
    return evaluate(p, load_exports(inputs, p.sources))


def quote_rows(b, target):
    prefix = call(METRIC_SIG if target == MQ else V3_SIG)[:10]
    return [
        r
        for r in b["records"]
        if r["request"]["method"] == "eth_call"
        and r["request"]["params"][0]["to"] == target
        and r["request"]["params"][0]["data"].startswith(prefix)
    ]


def test_both_directions_chain_closed_and_full_costs(tmp_path):
    r = run(tmp_path)
    assert r["planned_points"] == r["quote_complete_points"] == r["cost_complete_points"] == 8
    assert r["positive_estimates"] == 4
    for p in r["points"]:
        first, second = p["legs"]
        assert first["output_raw"] == second["requested_input_raw"]
        assert first["asset_in"] == second["asset_out"] == p["start_asset"]
        assert first["asset_out"] == second["asset_in"]
        assert p["net_estimate_raw"] == p["output_raw"] - p["principal_raw"] - 5
    assert r["block"]["ref"]["block_hash"] == block_hash(100)
    assert not r["execution_eligible"] and r["atomic_simulation"] == "unavailable"
    assert r["realized_pnl_raw"] is None and not r["original_http_bytes_verified"]
    assert all(
        q["pin"] == {"blockHash": block_hash(100), "requireCanonical": True}
        for q in r["queries"]
        if q["method"] in ("eth_call", "eth_getCode")
    )
    assert all("from" not in q["query"][0] for q in r["queries"] if q["method"] == "eth_call")


@pytest.mark.parametrize("chain", [1, 8453, 4663, 99901])
def test_same_engine_requires_matching_chain_per_source(tmp_path, chain):
    p = plan_data()
    p["chain_id"] = chain
    a, b = bundle("alpha"), bundle("beta")
    for values in (a, b):
        values["records"][0]["response"]["result"] = hex(chain)
    assert run(tmp_path, p, a, b)["positive_estimates"] == 4
    b["records"][0]["response"]["result"] = hex(chain + 1)
    r = run(tmp_path, p, a, b)
    assert r["quote_complete_points"] == 0
    assert all(x["reasons"] == ["provider_results_conflict"] for x in r["points"])


@pytest.mark.parametrize("consumed", [0, 10**18 - 1, 10**18 + 1])
def test_partial_or_excess_metric_input_never_enters_second_leg(tmp_path, consumed):
    a, b = bundle("alpha"), bundle("beta")
    for x in (a, b):
        quote_rows(x, MQ)[0]["response"]["result"] = data(["uint256", "uint256"], [consumed, 999])
    first = run(tmp_path, a=a, b=b)["points"][0]
    assert first["reasons"] == ["metric_partial_or_excess_input"]
    assert first["legs"] == [] and first["net_estimate_raw"] is None


@pytest.mark.parametrize("limit", [MIN_SQRT_PRICE + 1, MAX_SQRT_PRICE - 1])
def test_v3_open_limit_cannot_certify_input_completion(tmp_path, limit):
    a, b = bundle("alpha"), bundle("beta")
    for x in (a, b):
        quote_rows(x, VQ)[0]["response"]["result"] = data(
            ["uint256", "uint160", "uint32", "uint256"], [10**20, limit, 0, 1]
        )
    first = run(tmp_path, a=a, b=b)["points"][0]
    assert first["reasons"] == ["v3_partial_fill_possible"]
    assert len(first["legs"]) == 1 and first["net_estimate_raw"] is None


@pytest.mark.parametrize(
    "result",
    ["0x", "0x00", data(["uint256"], [1]), data(["uint256", "uint256", "uint256"], [10**18, 1, 0])],
)
def test_metric_return_must_be_canonical_two_words(tmp_path, result):
    a, b = bundle("alpha"), bundle("beta")
    for x in (a, b):
        quote_rows(x, MQ)[0]["response"]["result"] = result
    assert run(tmp_path, a=a, b=b)["points"][0]["reasons"] == ["malformed_retained_evidence"]


def test_source_aliases_cannot_fake_independence(tmp_path):
    a, b = bundle("alpha"), bundle("beta")
    for r in b["records"]:
        r["endpoint"] = "fixture://alpha"
    with pytest.raises(ValueError, match="distinct_endpoints"):
        run(tmp_path, a=a, b=b)


@pytest.mark.parametrize("mutation", ["late", "missing", "error", "conflict", "number_pin"])
def test_missing_stale_and_conflicting_quote_keeps_failed_point(tmp_path, mutation):
    b = bundle("beta")
    row = quote_rows(b, MQ)[0]
    if mutation == "late":
        row["fetched_utc"] = "2026-01-01T00:00:31Z"
    elif mutation == "missing":
        b["records"].remove(row)
    elif mutation == "error":
        del row["response"]["result"]
        row["response"]["error"] = {"code": -32000, "message": "reverted"}
    elif mutation == "conflict":
        row["response"]["result"] = data(["uint256", "uint256"], [10**18, 999])
    else:
        row["request"]["params"][1] = "0x64"
    r = run(tmp_path, b=b)
    assert len(r["points"]) == 8 and r["points"][0]["net_estimate_raw"] is None
    assert r["points"][0]["reasons"]


@pytest.mark.parametrize("index,value", [(1, USD), (3, 100), (4, 1)])
def test_immutables_hash_cannot_hide_wrong_pair_or_scale(tmp_path, index, value):
    p, a, b = plan_data(), bundle("alpha"), bundle("beta")
    values = copy.deepcopy(IMMUTABLES)
    values[index] = value
    result = data(IMMUTABLE_ABI, values)
    p["metric"]["immutables_hash"] = "0x" + keccak(bytes.fromhex(result[2:])).hex()
    for x in (a, b):
        next(
            r
            for r in x["records"]
            if r["request"]["method"] == "eth_call"
            and r["request"]["params"][0] == {"to": MP, "data": call("getImmutables()")}
        )["response"]["result"] = result
    r = run(tmp_path, p, a, b)
    assert r["quote_complete_points"] == 0
    assert r["points"][0]["reasons"] == ["metric_immutable_identity_mismatch"]


def test_runtime_mismatch_blocks_all_points(tmp_path):
    p = plan_data()
    p["metric"]["sender_hash"] = block_hash(999)
    r = run(tmp_path, p)
    assert r["quote_complete_points"] == 0
    assert r["points"][0]["reasons"] == ["runtime_hash_mismatch"]


def test_impossible_retrieval_clock_clears_previously_computed_quotes(tmp_path):
    b = bundle("beta")
    quote_rows(b, MQ)[0]["fetched_utc"] = "2025-12-31T23:59:59Z"
    r = run(tmp_path, b=b)
    assert r["block"] is None and r["positive_estimates"] == 0
    assert r["quote_complete_points"] == r["cost_complete_points"] == 0
    assert all(not p["legs"] and p["net_estimate_raw"] is None for p in r["points"])
    assert r["points"][0]["reasons"] == ["retrieval_precedes_block_timestamp"]


def test_declared_context_binds_request_identity(tmp_path):
    p = plan_data()
    p["metric"]["extension_data"] = "0x"
    r = run(tmp_path, p)
    assert r["quote_complete_points"] == 0
    assert all("missing_retained_reply" in x["reasons"] for x in r["points"])


@pytest.mark.parametrize("field", ["capital_raw", "commitments_raw", "reserve_raw", "costs"])
def test_unknown_economics_never_authorizes_profit(tmp_path, field):
    p = plan_data()
    if field == "costs":
        p["starts"][0][field][0]["raw"] = None
    else:
        p["starts"][0][field] = None
    r = run(tmp_path, p)
    assert all(x["quote_complete"] and x["net_estimate_raw"] is None for x in r["points"][:4])


def test_commitments_and_reserve_are_not_reused_as_headroom(tmp_path):
    p = plan_data()
    p["starts"][0].update(capital_raw=10**18 + 5, reserve_raw=1)
    r = run(tmp_path, p)
    assert r["points"][0]["reasons"] == ["insufficient_uncommitted_inventory"]


def test_query_budget_and_missing_context_keep_denominator(tmp_path):
    p = plan_data()
    p["max_queries"] = 1
    r = run(tmp_path, p)
    assert len(r["points"]) == 8 and r["query_count"] == 1
    assert r["points"][0]["reasons"] == ["offline_query_budget_exhausted"]
    r = evaluate(plan(), ())
    assert r["planned_points"] == 8 and r["decision"] == "incomplete_evidence"


@pytest.mark.parametrize("value", [True, 1.0, "100", -1, 2**127])
def test_strict_money(value):
    p = plan_data()
    p["starts"][0]["sizes_raw"][0] = value
    with pytest.raises(ValueError):
        plan(p)


def test_metric_orientation_is_not_v3_sorted_order():
    p = plan()
    assert p.metric.token0 == ETH and p.v3.token0 == USD
    encoded = metric_calldata(p.metric, ETH, 10**18)
    # Independently checked with Foundry cast, against the pinned interface.
    assert encoded[:10] == "0x67a00105"
    assert call(V3_SIG)[:10] == "0xc6a5026a"
    assert encoded[10 + 3 * 64 : 10 + 4 * 64] == "0" * 63 + "1"
    assert metric_calldata(p.metric, USD, 10**6)[10 + 3 * 64 : 10 + 4 * 64] == "0" * 64


def test_cli_replay_is_deterministic_and_atomic(tmp_path, monkeypatch):
    path, output = tmp_path / "plan.json", tmp_path / "report.json"
    path.write_text(json.dumps(plan_data()))
    for s in ("alpha", "beta"):
        (tmp_path / f"{s}.json").write_text(json.dumps(bundle(s)))
    args = ["research", "--plan", str(path), "--output", str(output)]
    args += [x for s in ("alpha", "beta") for x in ("--export", f"{s}={tmp_path / (s + '.json')}")]
    monkeypatch.setattr("sys.argv", args)
    assert cli.main() == 0
    raw = output.read_bytes()
    assert cli.main() == 2 and raw == output.read_bytes()
    args[4] = str(tmp_path / "report2.json")
    assert cli.main() == 0
    assert raw == (tmp_path / "report2.json").read_bytes()


@pytest.mark.parametrize(
    "payload,expected",
    [
        ("0x9a0423af", ["0x9a0423af"]),
        ("0x9A0423AF" + "00" * 32, ["0x9a0423af"]),
        ("0x123", []),
        ("0x12345678x", []),
        ({"data": "0x9a0423af"}, []),
        ("0x9a0423af" + "00" * 4096, []),
    ],
)
def test_rpc_error_retains_only_bounded_selector_not_untrusted_message(tmp_path, payload, expected):
    b = bundle("beta")
    row = quote_rows(b, MQ)[0]
    row["response"].pop("result")
    row["response"]["error"] = {"code": 3, "message": "do-not-publish-message", "data": payload}
    r = run(tmp_path, b=b)
    assert r["provider_revert_selectors"] == expected
    assert "do-not-publish-message" not in json.dumps(r)
    assert r["points"][0]["net_estimate_raw"] is None


def test_stale_unqueried_error_does_not_appear_as_current_failure(tmp_path):
    b = bundle("beta")
    row = quote_rows(b, MQ)[0]
    row["fetched_utc"] = "2026-01-01T00:00:31Z"
    row["response"].pop("result")
    row["response"]["error"] = {"code": 3, "data": "0x9a0423af"}
    assert run(tmp_path, b=b)["provider_revert_selectors"] == []


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("hash", block_hash(999), "planned_block_hash_mismatch"),
        ("timestamp", hex(1767225631), "block_timestamp_after_cutoff"),
    ],
)
def test_block_conflict_never_leaves_optimistic_quotes(tmp_path, field, value, reason):
    a, b = bundle("alpha"), bundle("beta")
    for x in (a, b):
        x["records"][1]["response"]["result"][field] = value
    r = run(tmp_path, a=a, b=b)
    assert r["block"] is None and r["quote_complete_points"] == 0
    assert all(p["reasons"] == [reason] for p in r["points"])
