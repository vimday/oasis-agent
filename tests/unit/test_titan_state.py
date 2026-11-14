"""Synthetic builder declarations: exact units, coverage, failures and no execution path."""

import copy
import json

import httpx
import pytest

from oasis_agent.arbitrage.titan_state import (
    BEACON_GENESIS,
    TitanAnalysisPlan,
    compare,
    decode_snapshot,
    inspect_snapshot,
)
from oasis_agent.evidence.public_rpc_plan import PublicHeadPlan, PublicReadPlan, PublicTitanPlan
from oasis_agent.models.common import canonical_json
from scripts import capture_public_rpc as capture
from scripts import research_titan_state as cli
from tests.unit.test_public_rpc_capture import counts

QUOTE = "0x" + "11" * 20
TOKEN_A, TOKEN_B = "0x" + "22" * 20, "0x" + "33" * 20
SLOT = 15_000_000
NOW = (BEACON_GENESIS + 12 * SLOT) * 1_000_000_000
SEQUENCE = 284752574598007134814732506


def plan_data():
    return {
        "schema": "oasis-titan-state-analysis-v1",
        "chain_id": 1,
        "metric_quote_addresses": [QUOTE],
        "max_snapshot_age_ms": 15000,
        "max_future_ms": 1000,
        "max_slot_age_seconds": 36,
    }


def snapshot():
    return {
        "venues": [
            {
                "maker": "Metric",
                "quoteAddress": QUOTE,
                "pricedAddresses": [QUOTE],
                "assetPairs": [{"token_1": TOKEN_A, "token_2": TOKEN_B}],
                "pairSequences": [SEQUENCE],
                "slot": SLOT,
                "blockNumber": 26000000,
                "timestamp": NOW,
                "stateOverride": {QUOTE: {"stateDiff": {"0x" + "00" * 32: "0x" + "01" * 32}}},
            }
        ]
    }


def inspect(value=None, now=NOW):
    plan = TitanAnalysisPlan.model_validate_json(json.dumps(plan_data()))
    return inspect_snapshot(plan, snapshot() if value is None else value, now)


def test_integer_precision_and_override_boundary():
    r = inspect()
    v = r["selected"][0]
    assert v["pairs"][0]["sequence"] == SEQUENCE > 2**53
    assert v["snapshot_timestamp_ns"] == NOW
    assert v["target_timestamp_seconds"] == BEACON_GENESIS + 12 * SLOT
    assert v["status"] == "observed_declaration"
    assert v["override_summary"]["storage_slots"] == 1
    assert "stateOverride" not in json.dumps(r) and not r["missing"]


@pytest.mark.parametrize(
    "field,bad",
    [
        ("timestamp", True),
        ("timestamp", float(NOW)),
        ("timestamp", str(NOW)),
        ("timestamp", 1 << 64),
        ("slot", -1),
        ("slot", 0),
        ("blockNumber", 0),
        ("pairSequences", [float(SEQUENCE)]),
        ("pairSequences", [True]),
        ("pairSequences", [1 << 256]),
        ("pairSequences", []),
        ("pricedAddresses", []),
        ("pricedAddresses", [QUOTE, QUOTE]),
        ("extra", "unsupported"),
    ],
)
def test_malformed_units_and_identity_rejected(field, bad):
    obj = snapshot()
    obj["venues"][0][field] = bad
    with pytest.raises(ValueError):
        decode_snapshot(obj)


@pytest.mark.parametrize(
    "age_ns,reason",
    [
        (15_000_000_000, None),
        (15_000_000_001, "stale_snapshot"),
        (-1_000_000_000, None),
        (-1_000_000_001, "future_snapshot"),
    ],
)
def test_snapshot_age_exact_boundaries(age_ns, reason):
    r = inspect(now=NOW + age_ns)["selected"][0]
    assert r["reasons"] == ([] if reason is None else [reason])


@pytest.mark.parametrize("offset,reason", [(4, "future_target_slot"), (-4, "stale_target_slot")])
def test_slot_clock_is_separate_from_snapshot_clock(offset, reason):
    obj = snapshot()
    obj["venues"][0]["slot"] += offset
    assert inspect(obj)["selected"][0]["reasons"] == [reason]


@pytest.mark.parametrize(
    "fields",
    [
        {"state": {}, "stateDiff": {}},
        {"nonce": "0x00"},
        {"balance": 100},
        {"nonce": hex(1 << 64)},
        {"code": "0x1"},
        {"code": "0x" + "00" * 32769},
        {"stateDiff": {"0x00": "0x" + "00" * 32}},
        {"call": "arbitrary"},
        {},
    ],
)
def test_hostile_override_shape_is_never_used(fields):
    obj = snapshot()
    obj["venues"][0]["stateOverride"][QUOTE] = fields
    with pytest.raises(ValueError):
        inspect(obj)


def test_unknown_venue_is_retained_as_unselected_not_metric_liquidity():
    obj = snapshot()
    other = copy.deepcopy(obj["venues"][0])
    other.update(maker="Unknown maker", quoteAddress=TOKEN_A, pricedAddresses=[TOKEN_A])
    obj["venues"].append(other)
    r = inspect(obj)
    assert r["venues_observed"] == 2 and len(r["selected"]) == 1
    assert r["unselected_quote_addresses"] == [TOKEN_A]
    obj["venues"][0]["maker"] = "Unknown maker"
    assert inspect(obj)["selected"][0]["reasons"] == ["maker_identity_mismatch"]


def test_missing_and_empty_snapshots_never_reuse_old_state():
    old, new = inspect(), inspect({"venues": []})
    assert new["selected"] == [] and new["missing"] == [QUOTE]
    assert compare(old, new) == [{"quote_address": QUOTE, "status": "missing_in_next_snapshot"}]


@pytest.mark.parametrize(
    "change,status",
    [
        ("none", "duplicate_state"),
        ("state", "same_version_conflict"),
        ("sequence", "observed_update"),
        ("regressed_sequence", "sequence_regression"),
        ("time", "context_regression"),
        ("block", "context_regression"),
        ("pairs", "pair_coverage_changed"),
    ],
)
def test_snapshot_transitions_do_not_claim_contiguous_events(change, status):
    obj = snapshot()
    v = obj["venues"][0]
    if change == "state":
        v["stateOverride"][QUOTE]["stateDiff"]["0x" + "00" * 32] = "0x" + "02" * 32
    elif change in {"sequence", "regressed_sequence"}:
        v["pairSequences"][0] += 100 if change == "sequence" else -1
    elif change == "time":
        v["timestamp"] -= 1
    elif change == "block":
        v["blockNumber"] -= 1
    elif change == "pairs":
        v["assetPairs"][0]["token_1"] = QUOTE
    assert compare(inspect(), inspect(obj)) == [{"quote_address": QUOTE, "status": status}]


def test_duplicate_or_case_aliased_identities_are_rejected():
    obj = snapshot()
    obj["venues"].append(copy.deepcopy(obj["venues"][0]))
    with pytest.raises(ValueError, match="duplicate_quote"):
        inspect(obj)
    obj = snapshot()
    obj["venues"][0]["assetPairs"].append({"token_1": TOKEN_B, "token_2": TOKEN_A})
    obj["venues"][0]["pairSequences"].append(1)
    with pytest.raises(ValueError, match="ambiguous_asset_pair"):
        inspect(obj)
    obj = snapshot()
    a = "0x" + "ab" * 20
    obj["venues"][0]["stateOverride"] = {a: {"balance": "0x0"}, "0x" + "AB" * 20: {"nonce": "0x0"}}
    with pytest.raises(ValueError, match="aliased_override_address"):
        inspect(obj)


def build_capture(tmp_path, monkeypatch, failure=None):
    scope_path, capture_plan = tmp_path / "scope.json", tmp_path / "capture-plan.json"
    scope_path.write_bytes(
        canonical_json(
            {
                "schema": "oasis-public-rpc-scope-v1",
                "purpose": "synthetic_titan_test",
                "expires_utc": "2100-01-01T00:00:00Z",
                "sources": [{"source": "titan_ap", "max_requests": 3}],
                "max_capture_seconds": 30,
            }
        )
    )
    capture_plan.write_bytes(
        canonical_json(
            {
                "schema": "oasis-titan-state-capture-plan-v1",
                "source": "titan_ap",
                "chain_id": 1,
                "samples": 3,
            }
        )
    )
    root = tmp_path / "campaign"
    capture.initialize(scope_path, root)
    original = httpx.Client
    requests = []

    def handler(request):
        req = json.loads(request.content)
        requests.append(req)
        assert str(request.url) == "https://ap.data.titanbuilder.xyz/data"
        assert counts(root)["titan_ap"] == len(requests)
        assert req["method"] == "titan_getPammVenues" and req["params"] == []
        assert "authorization" not in request.headers
        if req["id"] == 2 and failure == "http":
            return httpx.Response(429, text="limited")
        obj = snapshot()
        if req["id"] == 2 and failure == "shape":
            obj["venues"][0]["pairSequences"][0] = 1.5
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": req["id"], "result": obj})

    monkeypatch.setattr(
        capture.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    moments = iter(NOW + i * 1_000_000 for i in range(10))
    monkeypatch.setattr(capture.time, "time_ns", lambda: next(moments))
    result = capture.capture(root, capture_plan, "states")
    analysis = tmp_path / "analysis.json"
    analysis.write_bytes(canonical_json(plan_data()))
    return root, analysis, result, requests


def test_full_public_capture_and_offline_diagnostic(tmp_path, monkeypatch):
    root, plan, result, requests = build_capture(tmp_path, monkeypatch)
    assert result == 0 and len(requests) == 3
    r = cli.replay(plan, root / "states")
    assert r["status"] == "observed_declarations"
    assert r["reserved_requests"] == 3
    assert r["original_http_bytes_verified"] and r["retained_bytes_verified"]
    assert all(t["changes"][0]["status"] == "duplicate_state" for t in r["transitions"])
    assert r["canonical_base_block_hash"] is None and r["net_profit"] is None
    for field in (
        "execution_eligible",
        "canonical_chain_verified",
        "state_overrides_applied",
        "orderflow_completeness_verified",
        "feed_reference_freshness_verified",
    ):
        assert r[field] is False
    assert canonical_json(cli.replay(plan, root / "states")) == canonical_json(r)
    out = tmp_path / "out.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "research_titan_state",
            "--plan",
            str(plan),
            "--capture",
            str(root / "states"),
            "--output",
            str(out),
        ],
    )
    assert cli.main() == 0
    assert cli.main() == 2


@pytest.mark.parametrize("failure", ["http", "shape"])
def test_first_failure_consumes_budget_and_clears_completion(tmp_path, monkeypatch, failure):
    root, plan, result, requests = build_capture(tmp_path, monkeypatch, failure)
    assert result == 3 and len(requests) == counts(root)["titan_ap"] == 2
    assert (root / "STOP").exists()
    r = cli.replay(plan, root / "states")
    assert r["status"] == "incomplete_or_rejected" and r["reasons"] == ["incomplete_capture"]
    assert len(r["snapshots"]) == 1


def test_titan_cannot_be_a_chain_source_or_inject_an_rpc():
    with pytest.raises(ValueError):
        PublicHeadPlan.model_validate_json(
            json.dumps(
                {
                    "schema": "oasis-public-rpc-head-plan-v1",
                    "source": "titan_ap",
                    "chain_id": 1,
                    "block_tag": "latest",
                }
            )
        )
    with pytest.raises(ValueError):
        PublicReadPlan.model_validate_json(
            json.dumps(
                {
                    "schema": "oasis-public-rpc-read-plan-v1",
                    "source": "titan_ap",
                    "chain_id": 1,
                    "block_number": 1,
                    "block_hash": "0x" + "01" * 32,
                    "reads": [{"method": "eth_getCode", "target": QUOTE}],
                }
            )
        )
    with pytest.raises(ValueError):
        PublicTitanPlan.model_validate_json(
            json.dumps(
                {
                    "schema": "oasis-titan-state-capture-plan-v1",
                    "source": "titan_ap",
                    "chain_id": 1,
                    "samples": 1,
                    "method": "eth_sendRawTransaction",
                }
            )
        )


@pytest.mark.parametrize("bad", [True, False, 1.0, "1", 2])
def test_chain_claim_requires_exact_integer_one(bad):
    for model, payload in (
        (
            PublicTitanPlan,
            {
                "schema": "oasis-titan-state-capture-plan-v1",
                "source": "titan_ap",
                "chain_id": 1,
                "samples": 1,
            },
        ),
        (TitanAnalysisPlan, plan_data()),
    ):
        payload["chain_id"] = bad
        with pytest.raises(ValueError):
            model.model_validate_json(json.dumps(payload))
