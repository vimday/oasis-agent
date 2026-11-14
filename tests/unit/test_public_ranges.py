"""Prospective single-provider ranges: exact bytes, missing data and import recovery."""

import json
import sys

import httpx
import pytest

from oasis_agent.arbitrage.cycle_plan import Plan
from oasis_agent.arbitrage.cycle_shadow import filters, shadow_config_sha256
from oasis_agent.evidence.chain_store import CanonicalChainStore
from oasis_agent.evidence.ingestion import replay_block_range_evidence
from oasis_agent.evidence.public_rpc_plan import PublicRangePlan
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.models.common import canonical_json
from scripts import capture_public_rpc as cli
from scripts import import_public_range, replay_cycle_shadow
from tests.fixtures import cycle_research as fixture
from tests.unit.test_public_rpc_capture import HASH, TARGET, counts, setup

PARENT = "0x" + "11" * 32
LAST = "0x" + "13" * 32
TOPIC = "0x" + "77" * 32


def plan_data():
    return {
        "schema": "oasis-public-log-range-plan-v1",
        "source": "publicnode_ethereum",
        "chain_id": 1,
        "start_block": 100,
        "end_block": 101,
        "parent_hash": PARENT,
        "address": TARGET,
        "topics": [TOPIC],
        "locked_utc": "2025-12-31T23:59:59Z",
    }


def install_mock(monkeypatch, root, plan, mutate=None, quiet=False):
    original = httpx.Client
    calls = []

    def handler(req):
        request = json.loads(req.content)
        calls.append(request)
        assert counts(root)[plan["source"]] >= len(calls)
        method, number = request["method"], request["id"]
        if method == "eth_chainId":
            result = hex(plan["chain_id"])
        elif method == "eth_getBlockByNumber":
            n = int(request["params"][0], 16)
            result = {
                "number": hex(n),
                "hash": HASH if n == 100 else LAST,
                "parentHash": PARENT if n == 100 else HASH,
                "timestamp": hex(1767225600 + n - 100),
            }
        else:
            result = (
                []
                if quiet
                else [
                    {
                        "address": plan["address"],
                        "topics": plan["topics"],
                        "data": "0x",
                        "blockNumber": "0x65",
                        "blockHash": LAST,
                        "transactionHash": "0x" + "66" * 32,
                        "transactionIndex": "0x0",
                        "logIndex": "0x0",
                        "removed": False,
                    }
                ]
            )
        response = {"jsonrpc": "2.0", "id": number, "result": result}
        if mutate:
            mutate(number, response)
        return httpx.Response(200, json=response)

    monkeypatch.setattr(
        cli.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    return calls


@pytest.mark.parametrize("quiet", [False, True])
def test_capture_raw_import_restart_and_cli(tmp_path, monkeypatch, quiet):
    root, path, _ = setup(tmp_path)
    plan = plan_data()
    path.write_bytes(canonical_json(plan))
    install_mock(monkeypatch, root, plan, quiet=quiet)
    assert cli.capture(root, path, "range") == 0
    report = cli.replay(root / "range")
    assert report["plan_kind"] == "prospective_log_range"
    assert report["reserved_requests"] == 5 and not report["execution_eligible"]
    store = EvidenceStore(tmp_path / "proof.db")
    first = import_public_range.import_range(root / "range", store)
    assert first == import_public_range.import_range(root / "range", store)
    assert replay_block_range_evidence(store, first.evidence) == first.dataset
    assert len(first.dataset.logs) == (0 if quiet else 1)
    store.close()
    args = [
        "import",
        "--capture",
        str(root / "range"),
        "--store",
        str(tmp_path / "proof.db"),
        "--chain-store",
        str(tmp_path / "chain.db"),
        "--stream",
        "swap",
        "--output",
        str(tmp_path / "result.json"),
    ]
    monkeypatch.setattr(sys, "argv", args)
    assert import_public_range.main() == 0
    result = json.loads((tmp_path / "result.json").read_bytes())
    assert result["coverage"]["status"] == "complete"
    assert result["source_trust"] == "single_provider_research"
    chain = CanonicalChainStore(tmp_path / "chain.db")
    assert chain.coverage("swap", 1, 100, 102)["missing_blocks"] == [102]
    assert chain.captures("swap", 1)[0]["evidence"] == result["evidence"]
    chain.close()


@pytest.mark.parametrize(
    "fault",
    [
        "parent",
        "timestamp",
        "null_head",
        "closing",
        "null_logs",
        "removed",
        "hash",
        "filter",
        "duplicate",
        "order",
        "data",
        "topic",
        "position_overflow",
    ],
)
def test_range_first_fault_stops_without_importable_partial_coverage(tmp_path, monkeypatch, fault):
    root, path, _ = setup(tmp_path)
    plan = plan_data()
    path.write_bytes(canonical_json(plan))

    def mutate(n, response):
        r = response["result"]
        if n == 3:
            if fault == "parent":
                r["parentHash"] = PARENT
            if fault == "timestamp":
                r["timestamp"] = hex(1767225599)
            if fault == "null_head":
                response["result"] = None
        if n == 5 and fault == "closing":
            r["hash"] = HASH
        if n == 4:
            if fault == "null_logs":
                response["result"] = None
            elif fault == "removed":
                r[0]["removed"] = True
            elif fault == "hash":
                r[0]["blockHash"] = HASH
            elif fault == "filter":
                r[0]["address"] = "0x" + "55" * 20
            elif fault == "duplicate":
                r.append(dict(r[0]))
            elif fault == "order":
                r.insert(0, dict(r[0], logIndex="0x2"))
            elif fault == "data":
                r[0]["data"] = "0x+1"
            elif fault == "position_overflow":
                r[0]["logIndex"] = hex(1 << 32)
            elif fault == "topic":
                r[0]["topics"] = ["0x+" + "1" * 63]

    calls = install_mock(monkeypatch, root, plan, mutate)
    assert cli.capture(root, path, "failed") == 3
    assert counts(root)[plan["source"]] == len(calls)
    assert (root / "STOP").exists()
    assert cli.replay(root / "failed")["status"] == "incomplete"
    store = EvidenceStore(tmp_path / "proof.db")
    with pytest.raises(ValueError, match="complete_public_range"):
        import_public_range.import_range(root / "failed", store)
    store.close()
    with pytest.raises(ValueError, match="stopped"):
        cli.capture(root, path, "retry")


@pytest.mark.parametrize(
    "key,value",
    [
        ("end_block", 116),
        ("start_block", True),
        ("chain_id", True),
        ("topics", []),
        ("topics", [TOPIC] * 5),
        ("address", "0x" + "00" * 20),
    ],
)
def test_invalid_plan_is_rejected(key, value):
    p = plan_data()
    p[key] = value
    with pytest.raises(ValueError):
        PublicRangePlan.model_validate_json(canonical_json(p))


def test_original_bytes_tamper_refused_before_import(tmp_path, monkeypatch):
    root, path, _ = setup(tmp_path)
    plan = plan_data()
    path.write_bytes(canonical_json(plan))
    install_mock(monkeypatch, root, plan)
    assert cli.capture(root, path, "range") == 0
    (root / "range" / "3.response.json").write_bytes(b"{}")
    store = EvidenceStore(tmp_path / "proof.db")
    with pytest.raises(ValueError):
        import_public_range.import_range(root / "range", store)
    store.close()


def test_public_captures_feed_cycle_shadow_entrypoint(tmp_path, monkeypatch):
    config = fixture.plan()
    config["sources"] = ["publicnode_elysium", "synthetic"]
    config["chain_id"] = 99901
    plan = Plan.model_validate_json(canonical_json(config))
    (tmp_path / "cycle.json").write_bytes(canonical_json(config))
    window = {
        "schema": "oasis-cycle-shadow-window-v1",
        "cycle_config_sha256": shadow_config_sha256(plan),
        "source": "publicnode_elysium",
        "start_block": 100,
        "end_block": 101,
        "parent_hash": PARENT,
        "locked_utc": "2025-12-31T23:59:59Z",
        "deadline_utc": "2100-01-01T00:00:00Z",
        "max_requests": 30,
    }
    (tmp_path / "window.json").write_bytes(canonical_json(window))
    root, path, _ = setup(tmp_path, "publicnode_elysium", cap=30)
    captures = []
    for i, (name, watch) in enumerate(filters(plan).items()):
        p = dict(
            plan_data(),
            source="publicnode_elysium",
            chain_id=99901,
            address=watch.address,
            topics=list(watch.topics),
        )
        path.write_bytes(canonical_json(p))
        # Restore before each mock to avoid wrapping the preceding HTTP factory.
        with monkeypatch.context() as patch:
            install_mock(patch, root, p, quiet=name != "third.sync")
            assert cli.capture(root, path, f"filter-{i}") == 0
        captures += ["--capture", f"{name}={root / f'filter-{i}'}"]
    args = [
        "replay",
        "--cycle-plan",
        str(tmp_path / "cycle.json"),
        "--window",
        str(tmp_path / "window.json"),
        "--store",
        str(tmp_path / "proof.db"),
        "--chain-store",
        str(tmp_path / "chain.db"),
        "--output",
        str(tmp_path / "out.json"),
        *captures,
    ]
    monkeypatch.setattr(sys, "argv", args)
    assert replay_cycle_shadow.main() == 0
    report = json.loads((tmp_path / "out.json").read_bytes())
    assert report["status"] == "observed"
    assert report["quote_wakeups"][0]["filters"] == ["third.sync"]
    assert all(item["status"] == "complete" for item in report["retained_filter_coverage"].values())
    assert not report["quote_windows"]  # No fabricated quotes or execution authority.
    assert not report["execution_eligible"]
    # Restart with one absent filter: retained historical coverage must not fill current input.
    del args[-2:]
    args[args.index("--output") + 1] = str(tmp_path / "partial.json")
    assert replay_cycle_shadow.main() == 0
    partial = json.loads((tmp_path / "partial.json").read_bytes())
    assert partial["status"] == "coverage_incomplete" and partial["quote_wakeups"] == []
