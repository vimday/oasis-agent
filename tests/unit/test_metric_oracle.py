"""Reference guards are diagnostic evidence, never an executable price or permission."""

import json

import pytest
from eth_hash.auto import keccak

from oasis_agent.arbitrage.cycle_evidence import load_exports
from oasis_agent.arbitrage.metric_oracle import OraclePlan, diagnose
from tests.fixtures.cycle_research import RUNTIME, RUNTIME_HASH, address
from tests.fixtures.metric_v3 import (
    IMMUTABLE_RAW,
    MP,
    MQ,
    block_hash,
    bundle,
    call,
    data,
    plan_data,
)

PROVIDER, ORACLE = address(20), address(21)
BASE, QUOTE = block_hash(2), block_hash(7)
NOW = 1767225600


def plan_data_oracle():
    p = plan_data()
    return {
        "schema": "oasis-metric-oracle-plan-v1",
        "chain_id": 1,
        "sources": ["alpha", "beta"],
        "window": p["window"],
        "pool": MP,
        "pool_hash": RUNTIME_HASH,
        "immutables_hash": p["metric"]["immutables_hash"],
        "provider": PROVIDER,
        "provider_hash": RUNTIME_HASH,
        "oracle": ORACLE,
        "oracle_hash": RUNTIME_HASH,
        "oracle_kind": "pyth-lazer",
        "reader": MQ,
        "reader_hash": RUNTIME_HASH,
        "base_feed": BASE,
        "quote_feed": QUOTE,
        "max_queries": 80,
    }


def oracle_bundle(source, age=15, spread=1, mid=3000 * 10**8, guard=(0, 0)):
    b = bundle(source)

    def add(method, params, result):
        i = len(b["records"]) + 1
        b["records"].append(
            {
                "endpoint": f"fixture://{source}",
                "fetched_utc": "2026-01-01T00:00:10Z",
                "request": {"jsonrpc": "2.0", "id": i, "method": method, "params": params},
                "response": {"jsonrpc": "2.0", "id": i, "result": result},
            }
        )

    pin = {"blockHash": block_hash(100), "requireCanonical": True}

    def read(target, sig, types, values, input_types=(), inputs=()):
        add(
            "eth_call",
            [{"to": target, "data": call(sig, input_types, inputs)}, pin],
            data(types, values),
        )

    for target in (PROVIDER, ORACLE):
        add("eth_getCode", [target, pin], RUNTIME)
    p = plan_data()
    for sig, kind, value in [
        ("token0()", "address", p["metric"]["token0"]),
        ("token1()", "address", p["metric"]["token1"]),
        ("offchainOracle()", "address", ORACLE),
        ("baseFeedId()", "bytes32", bytes.fromhex(BASE[2:])),
        ("quoteFeedId()", "bytes32", bytes.fromhex(QUOTE[2:])),
        ("MAX_REF_STALENESS()", "uint256", 15),
        ("baseMaxStaleness()", "uint256", 15),
        ("quoteMaxStaleness()", "uint256", 15),
    ]:
        read(PROVIDER, sig, [kind], [value])
    read(ORACLE, "kind()", ["string"], ["pyth-lazer"])
    read(ORACLE, "version()", ["string"], ["1.1.0"])
    for feed_id in (BASE, QUOTE):
        feed = bytes.fromhex(feed_id[2:])
        read(
            ORACLE,
            "price(bytes32,address)",
            ["uint256", "uint256", "uint16", "uint256"],
            [mid, spread, 65535, NOW - age],
            ["bytes32", "address"],
            [feed, MQ],
        )
        read(ORACLE, "priceGuard(bytes32)", ["uint128", "uint128"], guard, ["bytes32"], [feed])
    return b


def run(tmp_path, plan=None, a=None, b=None):
    p = OraclePlan.model_validate_json(json.dumps(plan or plan_data_oracle()))
    paths = []
    for source, value in (("alpha", a), ("beta", b)):
        path = tmp_path / (source + ".json")
        path.write_text(json.dumps(value if value is not None else oracle_bundle(source)))
        paths.append((source, path))
    return diagnose(p, load_exports(paths, p.sources))


def test_exact_age_boundary_passes_reference_guards_only(tmp_path):
    r = run(tmp_path)
    assert r["status"] == "reference_guards_pass"
    assert len(r["feeds"]) == 2 and all(
        x["observation"]["max_age_seconds"] == 15 for x in r["feeds"]
    )
    assert all(x["observation"]["mid_e8"] == 3000 * 10**8 for x in r["feeds"])
    assert not r["execution_eligible"] and not r["onchain_read_access_proven"]
    assert not r["source_shaping_checked"] and not r["feed_economic_pair_verified"]
    assert not r["original_http_bytes_verified"]
    # Pyth spread1=65535 is not the spread0 stall sentinel; it must be ignored.
    assert all(x["reasons"] == [] for x in r["feeds"])
    assert all(q["query"][0].get("from") is None for q in r["queries"] if q["method"] == "eth_call")


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"age": 16}, "reference_too_old"),
        ({"age": -1}, "future_reference_time"),
        ({"age": NOW}, "missing_reference_time"),
        ({"age": NOW - NOW * 1000}, "future_reference_time"),
        ({"mid": 0}, "zero_reference_price"),
        ({"spread": 65534}, "saturated_or_stalled_spread"),
        ({"spread": 65535}, "saturated_or_stalled_spread"),
        ({"spread": 65536}, "saturated_or_stalled_spread"),
        ({"guard": (3000 * 10**8 + 1, 0)}, "price_guard_violation"),
        ({"guard": (0, 1)}, "price_guard_violation"),
    ],
)
def test_bad_reference_cannot_become_a_valid_quote(tmp_path, kwargs, reason):
    r = run(tmp_path, a=oracle_bundle("alpha", **kwargs), b=oracle_bundle("beta", **kwargs))
    assert r["status"] == "reference_guard_reject"
    assert all(reason in f["reasons"] for f in r["feeds"])


@pytest.mark.parametrize("failure", ["missing", "rpc_error", "noncanonical", "conflict"])
def test_missing_or_conflicting_price_never_uses_disabled_raw_getter(tmp_path, failure):
    b = oracle_bundle("beta")
    prefix = call("price(bytes32,address)")[:10]
    row = next(
        x
        for x in b["records"]
        if x["request"]["method"] == "eth_call"
        and x["request"]["params"][0]["data"].startswith(prefix)
    )
    if failure == "missing":
        b["records"].remove(row)
    elif failure == "rpc_error":
        row["response"].pop("result")
        row["response"]["error"] = {"code": 3, "message": "reverted", "data": "0x987d5c54"}
    elif failure == "noncanonical":
        row["response"]["result"] += "00" * 32
    else:
        row["response"]["result"] = data(
            ["uint256", "uint256", "uint16", "uint256"], [1, 1, 65535, NOW]
        )
    r = run(tmp_path, b=b)
    assert r["status"] == "incomplete_evidence" and r["feeds"][0]["observation"] is None
    disabled_prefix = call("getOracleData(bytes32)")[:10]
    assert all(
        not q["query"][0]["data"].startswith(disabled_prefix)
        for q in r["queries"]
        if q["method"] == "eth_call"
    )


@pytest.mark.parametrize(
    "field", ["provider_hash", "oracle_hash", "reader_hash", "pool_hash", "immutables_hash"]
)
def test_identity_failure_clears_every_feed(tmp_path, field):
    p = plan_data_oracle()
    p[field] = block_hash(999)
    r = run(tmp_path, p)
    assert r["status"] == "incomplete_evidence" and not r["feeds"] and r["block"] is None


def test_mutable_provider_cannot_be_inferred_from_immutable_slot(tmp_path):
    p, a, b = plan_data_oracle(), oracle_bundle("alpha"), oracle_bundle("beta")
    # Replace the provider word with zero while preserving a matching immutable hash.
    changed = IMMUTABLE_RAW[: 2 + 8 * 64] + "0" * 64 + IMMUTABLE_RAW[2 + 9 * 64 :]
    p["immutables_hash"] = "0x" + keccak(bytes.fromhex(changed[2:])).hex()
    for bundle_data in (a, b):
        next(
            x
            for x in bundle_data["records"]
            if x["request"]["method"] == "eth_call"
            and x["request"]["params"][0]["data"] == call("getImmutables()")
        )["response"]["result"] = changed
    r = run(tmp_path, p, a, b)
    assert r["errors"] == ["immutable_provider_mismatch_or_mutable_provider"]


def test_impossible_clock_discards_already_derived_guards(tmp_path):
    a, b = oracle_bundle("alpha"), oracle_bundle("beta")
    b["records"][-2]["fetched_utc"] = "2025-12-31T23:59:59Z"
    r = run(tmp_path, a=a, b=b)
    assert r["errors"] == ["retrieval_precedes_block_timestamp"] and r["feeds"] == []


def test_query_budget_refusal_keeps_unknown_status(tmp_path):
    p = plan_data_oracle()
    p["max_queries"] = 1
    r = run(tmp_path, p)
    assert r["errors"] == ["offline_query_budget_exhausted"]
