"""Builder/parent correlation: preserve failures, clear contradictory context, no authority."""

import copy
import json

import httpx
import pytest

from oasis_agent.arbitrage.metric_context import ContextPlan, correlate
from oasis_agent.models.common import canonical_json, sha256_hex
from scripts import capture_public_rpc as capture
from scripts import research_metric_context as cli
from scripts import research_titan_state
from tests.unit import test_titan_state as titan

PARENT, TARGET = "0x" + "12" * 32, "0x" + "34" * 32
SOURCES = ["publicnode_ethereum", "mevblocker_ethereum"]
N = 26000000


def plan_data():
    return {
        "schema": "oasis-metric-builder-context-plan-v1",
        "chain_id": 1,
        "target_block": N,
        "target_timestamp": titan.NOW // 10**9,
        "sources": SOURCES,
        "titan_capture_sha256": "a" * 64,
        "titan_analysis_plan_sha256": "b" * 64,
    }


def plan(obj=None):
    return ContextPlan.model_validate_json(json.dumps(obj or plan_data()))


def builder():
    return {
        "status": "observed_declarations",
        "capture_sha256": "a" * 64,
        "plan_sha256": "b" * 64,
        "snapshots": [
            {"selected": [{"target_block": N, "target_timestamp_seconds": titan.NOW // 10**9}]}
        ],
    }


def block(n):
    return {
        "number": hex(n),
        "hash": PARENT if n == N - 1 else TARGET,
        "parentHash": "0x" + "01" * 32 if n == N - 1 else PARENT,
        "timestamp": hex(titan.NOW // 10**9 - (12 if n == N - 1 else 0)),
    }


def observations():
    return [
        {
            "source": s,
            "block_number": n,
            "status": "complete",
            "raw_header": block(n),
            "capture_sha256": "c" * 64,
        }
        for s in SOURCES
        for n in (N - 1, N)
    ]


def test_agreement_retains_each_source_without_independence_or_landing_claims():
    r = correlate(plan(), builder(), observations())
    assert r["status"] == "header_observations_agree" and r["missing"] == []
    assert r["observed_context"]["parent"]["hash"] == PARENT
    assert len(r["captures"]) == 4 and len(r["contexts"]) == 2
    assert all(v is False for k, v in r.items() if k.endswith("verified"))
    assert not r["execution_eligible"] and r["net_profit"] is None


def test_missing_failed_secondary_keeps_only_explicit_single_source_observation():
    obs = observations()[:3]
    obs[2].update(status="incomplete", raw_header=None)
    r = correlate(plan(), builder(), obs)
    assert r["status"] == "incomplete_or_rejected"
    assert r["observed_context"]["parent"]["hash"] == PARENT
    assert r["contexts"][1]["parent"] is None and not r["contexts"][1]["link_matches"]
    assert r["missing"] == [{"source": SOURCES[1], "block_number": n} for n in (N - 1, N)]


@pytest.mark.parametrize(
    "change",
    [
        "hash",
        "parent_link",
        "timestamp",
        "parent_time",
        "builder_target",
        "builder_time",
        "builder_failure",
        "builder_empty",
    ],
)
def test_conflicts_clear_context_while_retaining_original_observations(change):
    obs, b = observations(), builder()
    if change == "hash":
        obs[2]["raw_header"]["hash"] = TARGET
    elif change == "parent_link":
        for i in (1, 3):
            obs[i]["raw_header"]["parentHash"] = TARGET
    elif change == "timestamp":
        for i in (1, 3):
            obs[i]["raw_header"]["timestamp"] = hex(titan.NOW // 10**9 + 1)
    elif change == "parent_time":
        for i in (0, 2):
            obs[i]["raw_header"]["timestamp"] = hex(titan.NOW // 10**9)
    elif change == "builder_target":
        b["snapshots"][0]["selected"][0]["target_block"] += 1
    elif change == "builder_time":
        b["snapshots"][0]["selected"][0]["target_timestamp_seconds"] += 1
    elif change == "builder_failure":
        b["status"] = "incomplete_or_rejected"
    elif change == "builder_empty":
        b["snapshots"] = []
    r = correlate(plan(), b, obs)
    assert r["status"] == "incomplete_or_rejected" and r["observed_context"] is None
    assert len(r["captures"]) == 4


@pytest.mark.parametrize(
    "field,value",
    [
        ("chain_id", True),
        ("target_block", True),
        ("sources", ["publicnode_base"]),
        ("sources", [SOURCES[0], SOURCES[0]]),
    ],
)
def test_ambiguous_plan_rejected(field, value):
    obj = plan_data()
    obj[field] = value
    with pytest.raises(ValueError):
        plan(obj)


def test_wrong_capture_binding_and_duplicate_or_unplanned_headers_rejected():
    b = builder()
    b["capture_sha256"] = "d" * 64
    with pytest.raises(ValueError, match="binding"):
        correlate(plan(), b, observations())
    obs = observations()
    with pytest.raises(ValueError, match="duplicate"):
        correlate(plan(), builder(), [*obs, copy.deepcopy(obs[0])])
    obs[0]["block_number"] -= 1
    with pytest.raises(ValueError, match="unexpected"):
        correlate(plan(), builder(), obs)


def test_normal_raw_capture_cli_and_tamper_rejection(tmp_path, monkeypatch):
    original = httpx.Client
    tdir = tmp_path / "titan"
    tdir.mkdir()
    root, analysis, result, _ = titan.build_capture(tdir, monkeypatch)
    assert result == 0
    tr = research_titan_state.replay(analysis, root / "states")
    p = plan_data()
    p["titan_capture_sha256"] = tr["capture_sha256"]
    p["titan_analysis_plan_sha256"] = tr["plan_sha256"]
    pp = tmp_path / "context.json"
    pp.write_bytes(canonical_json(p))
    scope = tmp_path / "scope.json"
    scope.write_bytes(
        canonical_json(
            {
                "schema": "oasis-public-rpc-scope-v1",
                "purpose": "synthetic_parent_context",
                "expires_utc": "2100-01-01T00:00:00Z",
                "sources": [{"source": s, "max_requests": 4} for s in SOURCES],
                "max_capture_seconds": 30,
            }
        )
    )
    campaign = tmp_path / "headers"
    capture.initialize(scope, campaign)

    def handler(request):
        q = json.loads(request.content)
        value = "0x1" if q["method"] == "eth_chainId" else block(int(q["params"][0], 16))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": q["id"], "result": value})

    monkeypatch.setattr(
        capture.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    monkeypatch.setattr(capture.time, "time_ns", lambda: titan.NOW + 60 * 10**9)
    dirs = []
    for source in SOURCES:
        for n in (N - 1, N):
            name = f"{source}-{n}"
            hp = tmp_path / (name + ".json")
            hp.write_bytes(
                canonical_json(
                    {
                        "schema": "oasis-public-rpc-head-plan-v1",
                        "source": source,
                        "chain_id": 1,
                        "block_tag": n,
                    }
                )
            )
            assert capture.capture(campaign, hp, name) == 0
            dirs.append(campaign / name)
    r = cli.replay(pp, analysis, root / "states", dirs)
    assert r["status"] == "header_observations_agree"
    assert all(c["retained_bytes_verified"] for c in r["captures"])
    output = tmp_path / "out.json"
    argv = [
        "research_metric_context",
        "--plan",
        str(pp),
        "--titan-plan",
        str(analysis),
        "--titan-capture",
        str(root / "states"),
        "--output",
        str(output),
    ]
    for d in dirs:
        argv.extend(["--header-capture", str(d)])
    monkeypatch.setattr("sys.argv", argv)
    assert cli.main() == 0 and cli.main() == 2
    assert sha256_hex(output.read_bytes()) == sha256_hex(canonical_json(r) + b"\n")
    assert cli.replay(pp, analysis, root / "states", dirs[:2])["status"] == "incomplete_or_rejected"
    wire = dirs[0] / "1.response.json"
    wire.write_bytes(wire.read_bytes() + b" ")
    with pytest.raises(ValueError):
        cli.replay(pp, analysis, root / "states", dirs)
