import json
import sys
from dataclasses import asdict

import pytest

from oasis_agent.arbitrage.cycle_plan import Plan
from oasis_agent.arbitrage.cycle_shadow import filters, shadow_config_sha256
from oasis_agent.evidence.rpc_capture import persist_block_range_evidence
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.models.chain import BlockSnapshot, LogRecord
from oasis_agent.models.common import BlockRef
from oasis_agent.rpc.collector import BlockRangeDataset
from scripts import replay_cycle_shadow
from tests.fixtures import cycle_research as fixture
from tests.rpc_evidence_helpers import range_exchanges


@pytest.mark.parametrize("fault", [None, "missing", "late", "different_hash", "small_budget"])
def test_shadow_replay_through_cli(tmp_path, monkeypatch, fault):
    cycle_path, window_path, refs_path, output_path, store_path = (
        tmp_path / name
        for name in ("cycle.json", "window.json", "refs.json", "out.json", "proof.db")
    )
    cycle_path.write_text(json.dumps(fixture.plan()))
    plan = Plan.model_validate_json(cycle_path.read_bytes())
    window = {
        "schema": "oasis-cycle-shadow-window-v1",
        "cycle_config_sha256": shadow_config_sha256(plan),
        "source": "synthetic",
        "start_block": 100,
        "end_block": 101,
        "parent_hash": fixture.block_hash(99),
        "locked_utc": "2025-12-31T23:59:59Z",
        "deadline_utc": "2026-01-01T00:00:04Z",
        "max_requests": 29 if fault == "small_budget" else 30,
    }
    window_path.write_text(json.dumps(window))
    left = BlockSnapshot(
        BlockRef(fixture.CHAIN, 100, fixture.block_hash(100), fixture.TIMESTAMP),
        fixture.block_hash(99),
    )
    refs = {}
    store = EvidenceStore(store_path)
    try:
        for name, item in filters(plan).items():
            if name == "first.liquidity" and fault == "missing":
                continue
            block_hash = fixture.block_hash(
                999 if name == "first.liquidity" and fault == "different_hash" else 101
            )
            right = BlockSnapshot(
                BlockRef(fixture.CHAIN, 101, block_hash, fixture.TIMESTAMP), left.ref.block_hash
            )
            logs = ()
            if name == "third.sync":
                logs = (
                    LogRecord(
                        item.address,
                        item.topics,
                        "0x",
                        101,
                        fixture.block_hash(808),
                        3,
                        right.ref.block_hash,
                        1,
                    ),
                )
            dataset = BlockRangeDataset("synthetic", (left, right), logs, item.address, item.topics)
            refs[name] = asdict(
                persist_block_range_evidence(
                    store,
                    dataset,
                    range_exchanges(dataset),
                    retrieved_at="2026-01-01T00:00:05Z" if fault == "late" else fixture.DAYS[2],
                )
            )
    finally:
        store.close()
    refs_path.write_text(json.dumps(refs))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "replay_cycle_shadow",
            "--cycle-plan",
            str(cycle_path),
            "--window",
            str(window_path),
            "--store",
            str(store_path),
            "--refs",
            str(refs_path),
            "--output",
            str(output_path),
        ],
    )
    if fault in ("late", "different_hash", "small_budget"):
        assert replay_cycle_shadow.main() == 2
        assert not output_path.exists()
        return
    assert replay_cycle_shadow.main() == 0
    report = json.loads(output_path.read_bytes())
    assert report["execution_eligible"] is False
    assert report["window_denominator"] == 1
    assert report["coverage"]["third.sync"] == "observed_events"
    assert report["coverage"]["first.swap"] == "observed_empty"
    if fault == "missing":
        assert report["status"] == "coverage_incomplete"
        assert report["coverage"]["first.liquidity"] == "missing"
        assert report["quote_wakeups"] == []
        return
    assert report["status"] == "observed"
    assert report["requests_in_retained_exchanges"] == 30
    assert report["quote_wakeups"] == [
        {
            "block": 101,
            "block_hash": fixture.block_hash(101),
            "filters": ["third.sync"],
            "log_count": 1,
        },
    ]


@pytest.mark.parametrize(
    "mode",
    ["events", "checkpoint", "unready", "quote_reorg", "forged_initialize", "missing_lp", "quiet"],
)
def test_v4_discovery_and_affected_quote_paths_through_cli(tmp_path, monkeypatch, mode):
    config = fixture.plan()
    config["pools"].append(
        {
            **config["pools"][2],
            "name": "alternate",
            "address": fixture.address(200),
        }
    )
    config["windows"][1]["expected_hash"] = None
    if mode != "unready":
        config["windows"][1]["cutoff_utc"] = fixture.DAYS[2]
    cycle = tmp_path / "cycle.json"
    cycle.write_text(json.dumps(config))
    plan = Plan.model_validate_json(cycle.read_bytes())
    plan_window = tmp_path / "window.json"
    plan_window.write_text(
        json.dumps(
            {
                "schema": "oasis-cycle-shadow-window-v1",
                "cycle_config_sha256": shadow_config_sha256(plan),
                "source": "synthetic",
                "start_block": 100,
                "end_block": 101,
                "parent_hash": fixture.block_hash(99),
                "locked_utc": "2025-12-31T23:59:59Z",
                "deadline_utc": "2026-01-01T00:00:04Z",
                "max_requests": 40,
                "discover_v4_pools": True,
                "quote_checkpoint_at_end": mode == "checkpoint",
            }
        )
    )
    blocks = tuple(
        BlockSnapshot(
            BlockRef(fixture.CHAIN, number, fixture.block_hash(number), fixture.TIMESTAMP),
            fixture.block_hash(number - 1),
        )
        for number in (100, 101)
    )
    key = (fixture.NATIVE, fixture.KNTQ, 500, 10, fixture.NATIVE)
    pool_id = "0x" + fixture.keccak(fixture.encode([fixture.KEY_ABI], [key])).hex()
    store_path = tmp_path / "evidence.db"
    refs = {}
    store = EvidenceStore(store_path)
    try:
        for name, watch in filters(plan, discover_v4_pools=True).items():
            logs = ()
            if watch.event == "initialize":
                if mode == "missing_lp":
                    continue
                observed_id = fixture.block_hash(400) if mode == "forged_initialize" else pool_id
                logs = (
                    LogRecord(
                        watch.address,
                        (
                            *watch.topics,
                            observed_id,
                            fixture.data(["address"], [key[0]]),
                            fixture.data(["address"], [key[1]]),
                        ),
                        fixture.data(
                            ["uint24", "int24", "address", "uint160", "int24"],
                            [key[2], key[3], key[4], 1 << 96, 0],
                        ),
                        100,
                        fixture.block_hash(801),
                        1,
                        blocks[0].ref.block_hash,
                        0,
                    ),
                )
            if name == "third.sync" and mode not in ("checkpoint", "quiet"):
                logs = tuple(
                    LogRecord(
                        watch.address,
                        watch.topics,
                        fixture.data(["uint256", "uint256"], fixture.IMBALANCE),
                        101,
                        fixture.block_hash(802),
                        index,
                        blocks[1].ref.block_hash,
                        1,
                    )
                    for index in (2, 3)
                )
            dataset = BlockRangeDataset("synthetic", blocks, logs, watch.address, watch.topics)
            refs[name] = asdict(
                persist_block_range_evidence(
                    store,
                    dataset,
                    range_exchanges(dataset),
                    retrieved_at=fixture.DAYS[2],
                )
            )
    finally:
        store.close()
    refs_path = tmp_path / "refs.json"
    refs_path.write_text(json.dumps(refs))
    quotes = fixture.records()
    if mode == "quote_reorg":
        block_reply = next(
            row
            for row in quotes
            if row["request"]["method"] == "eth_getBlockByNumber"
            and row["request"]["params"] == ["0x65", False]
        )
        block_reply["response"]["result"]["hash"] = fixture.block_hash(700)
    quotes_path = tmp_path / "quotes.json"
    fixture.write_bundle(quotes_path, quotes)
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "replay_cycle_shadow",
            "--cycle-plan",
            str(cycle),
            "--window",
            str(plan_window),
            "--store",
            str(store_path),
            "--refs",
            str(refs_path),
            "--output",
            str(output),
            "--export",
            f"synthetic={quotes_path}",
        ],
    )
    if mode == "forged_initialize":
        assert replay_cycle_shadow.main() == 2
        assert not output.exists()
        return
    assert replay_cycle_shadow.main() == 0
    report = json.loads(output.read_bytes())
    assert report["execution_eligible"] is False
    if mode == "missing_lp":
        assert report["status"] == "coverage_incomplete"
        assert report["quote_missing_filters"] == [] and len(report["lp_missing_filters"]) == 1
        assert report["requests_in_retained_exchanges"] == 35
        assert report["lp_discoveries"] == []
    else:
        assert report["requests_in_retained_exchanges"] == 40
        assert report["initialize_events_observed"] == 1
        (discovery,) = report["lp_discoveries"]
        assert discovery["event"]["observation"]["pool_id"] == pool_id
        assert discovery["event"]["block"]["number"] == 100
        assert discovery["lp_access"] is None and discovery["active_liquidity"] is None
    if mode == "quiet":
        assert (
            report["quote_wakeups"] == report["quote_dispatches"] == report["quote_windows"] == []
        )
        assert report["point_comparison"]["affected_path_points"] == 0
        return
    (dispatch,) = report["quote_dispatches"]
    assert dispatch["block"]["ref"]["number"] == 101
    assert report["point_comparison"]["full_scan_points"] == 12
    assert dispatch["quote_points"] == (6 if mode == "checkpoint" else 3)
    if mode == "unready":
        assert dispatch["status"] == "missing_usable_quote_cutoff"
        assert report["quote_windows"] == []
        return
    (quoted,) = report["quote_windows"]
    if mode == "quote_reorg":
        assert quoted["reason"] == "shadow_quote_block_mismatch"
        assert all(query["method"] != "eth_call" for query in quoted["queries"])
        return
    assert quoted["decision"] == "positive_conditional_quote"
    assert (
        max(point["net_raw"] for point in quoted["points"] if point["net_raw"] is not None) == 64648
    )
    if mode == "checkpoint":
        assert dispatch["reason"] == "closing_checkpoint"
        assert len(dispatch["paths"]) == 2
        assert report["quote_wakeups"] == []
    else:
        assert report["quote_wakeups"][0]["log_count"] == 2
        assert dispatch["paths"] == [["first", "second", "third"]]
        assert "alternate" not in quoted["issues"]
