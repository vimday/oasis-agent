import copy
import json
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path

import pytest

from scripts import run_cycle_research
from tests.fixtures import cycle_research as fixture

ROOT = Path(__file__).resolve().parents[2]


def run(tmp_path, monkeypatch, *, config=None, entries=None, name="run", expected=0):
    plan = tmp_path / f"{name}.plan.json"
    export = tmp_path / f"{name}.export.json"
    output = tmp_path / f"{name}.report.json"
    plan.write_text(json.dumps(fixture.plan() if config is None else config))
    fixture.write_bundle(export, fixture.records() if entries is None else entries)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_cycle_research",
            "--plan",
            str(plan),
            "--export",
            f"synthetic={export}",
            "--output",
            str(output),
        ],
    )
    assert run_cycle_research.main() == expected
    if expected:
        assert not output.exists()
        return None
    return json.loads(output.read_bytes())


def _at(records, method, predicate):
    return next(
        record
        for record in records
        if (record["request"]["method"] == method and predicate(record["request"]["params"]))
    )


def _quote_row(records, target, selector, block=101):
    return _at(
        records,
        "eth_call",
        lambda params: (
            params[1]["blockHash"] == fixture.block_hash(block)
            and params[0]["to"] == target
            and params[0]["data"].startswith(selector)
        ),
    )


def _best(window):
    return next(point for point in window["points"] if point["id"] == window["best_evaluated_id"])


def test_cli_research_receipts_and_competition(tmp_path, monkeypatch):
    def offline(*_args, **_kwargs):
        raise AssertionError("offline research tried to open a socket")

    monkeypatch.setattr(socket, "socket", offline)
    imported_before = set(sys.modules)
    report = run(tmp_path, monkeypatch)
    added = set(sys.modules) - imported_before
    assert not any(
        "signer" in module or module.startswith("oasis_agent.execution") for module in added
    )
    assert report["execution_eligible"] is False
    assert report["window_denominator"] == 3
    before, shock, after = report["windows"]
    assert [item["decision"] for item in (before, shock, after)] == [
        "no_positive_evaluated_quote",
        "positive_conditional_quote",
        "no_positive_evaluated_quote",
    ]
    assert shock["anomalies"][0]["anomalous"] and after["anomalies"][0]["anomalous"]
    assert report["latest_window_positive_id"] is None
    best = _best(shock)
    assert best["principal_raw"] == 10_000
    assert best["net_raw"] == 64_648
    assert best["atomic_simulation"] == "unavailable"
    assert len(best["legs"]) == 4
    assert [leg["input_raw"] for leg in best["legs"]] == [10_000, 20_000, 30_000, 74_688]
    assert [leg["output_raw"] for leg in best["legs"]] == [20_000, 30_000, 74_688, 74_688]
    assert [leg["asset_out"]["decimals"] for leg in best["legs"]] == [18, 6, 18, 18]
    assert best["quote_complete"] and not best["execution_eligible"]
    assert sum(cost["raw"] for cost in report["accounting"]["cost_bounds"]) == 40
    assert report["accounting"]["window_realized_pnl_raw"] is None
    assert report["accounting"]["window_failed_attempt_cost_raw"] is None
    case = report["historical_case"]
    assert case["status"] == "receipt_path_reconciled"
    assert case["first_observed_utc"] is None and case["exact_winner_prestate"] is None
    assert case["trigger"]["decoded"]["output_raw"] == fixture.TRIGGER_OUTPUT
    assert case["trigger"]["decoded"]["amount_out_min_raw"] == fixture.TRIGGER_OUTPUT - 150
    winner = case["winner"]["decoded"]
    assert winner["transaction_native_value_raw"] == 0
    assert winner["principal_exceeding_tx_value_raw"] == fixture.WINNER_PRINCIPAL
    assert winner["principal_native_raw"] != best["principal_raw"]
    assert winner["receipt_gas_product_native_raw"] == 77
    assert winner["solidly_input_fee"]["raw"] == fixture.WINNER_FEE
    assert winner["route_output_less_principal_and_receipt_gas_raw"] == (
        fixture.WINNER_OUTPUT - fixture.WINNER_PRINCIPAL - 77
    )
    assert winner["erc20_residuals"] == [] and winner["prefix_transaction_types"] == [106]
    assert winner["fully_reconciled_net_raw"] is None and winner["builder_payment_raw"] is None

    evidence = report["evidence_manifest"][0]
    assert (
        evidence["file_sha256"] == sha256((tmp_path / "run.export.json").read_bytes()).hexdigest()
    )
    assert evidence["kind"] == "parsed_historical_export"
    assert evidence["first_observed_utc"] is None
    assert evidence["original_http_request_sha256"] is None
    assert evidence["original_http_response_sha256"] is None


def test_as_of_selection_is_invariant_to_future_conflict_and_receipts(tmp_path, monkeypatch):
    original = fixture.records()
    cutoff = fixture.DAYS[1]
    prefix = [entry for entry in original if entry["fetched_utc"] <= cutoff]
    first = run(tmp_path, monkeypatch, entries=prefix)
    conflict = copy.deepcopy(_quote_row(prefix, fixture.QUOTER3, "0x"))
    conflict["fetched_utc"] = "2026-01-01T00:00:04Z"
    conflict["response"]["result"] = "0x00"
    full = run(tmp_path, monkeypatch, entries=[*original, conflict], name="complete")
    assert first["windows"][:2] == full["windows"][:2]
    assert first["historical_case"]["winner"]["receipt_path_status"] == "unresolved"
    assert full["historical_case"]["winner"]["receipt_path_status"] == "identity_bound_receipt_path"
    assert (
        first["evidence_manifest"][0]["file_sha256"] != full["evidence_manifest"][0]["file_sha256"]
    )


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("missing_v4", "missing_retained_reply"),
        ("failed_v3", "provider_call_failed"),
        ("partial_v3", "v3_partial_fill_possible"),
        ("decimals", "token_decimals_mismatch"),
        ("pool_tokens", "pool_metadata_mismatch"),
        ("code", "runtime_hash_mismatch"),
        ("cost", "incremental_cost_unknown"),
        ("inventory", "insufficient_uncommitted_inventory"),
        ("repeat", "repeated_or_shared_swap_target"),
        ("budget", "offline_query_budget_exhausted"),
    ],
)
def test_rejection_is_attributed_to_the_input(tmp_path, monkeypatch, fault, reason):
    plan, records = fixture.plan(), fixture.records()
    if fault in {"missing_v4", "failed_v3", "partial_v3"}:
        v4 = fault == "missing_v4"
        selector = fixture.topic(
            "quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))"
            if v4
            else "quoteExactInputSingle((address,address,uint256,uint24,uint160))"
        )[:10]
        row = _quote_row(records, fixture.QUOTER4 if v4 else fixture.QUOTER3, selector)
        if v4:
            # An otherwise matching retained latest reply must never substitute for hash pinning.
            row["request"]["params"][1] = "latest"
        elif fault == "failed_v3":
            row["response"].pop("result")
            row["response"]["error"] = {"code": -32602, "message": "hash pin unsupported"}
        else:
            row["response"]["result"] = fixture.data(
                ["uint256", "uint160", "uint32", "uint256"],
                [3000, 4295128740, 0, 1],
            )
    elif fault == "decimals":
        plan["assets"][2]["decimals"] = 18
    elif fault == "pool_tokens":
        row = _quote_row(records, fixture.V3, fixture.call("token0()"))
        row["response"]["result"] = fixture.data(["address"], [fixture.KNTQ])
    elif fault == "code":
        plan["pools"][2]["runtime_hash"] = fixture.block_hash(1)
    elif fault == "cost":
        plan["costs"][1].update(status="unknown", raw=None)
    elif fault == "inventory":
        plan["inventory_raw"] = 1000
    elif fault == "repeat":
        plan["pools"][1]["address"] = fixture.MANAGER
    else:
        plan["max_queries_per_window"] = 5
    report = run(tmp_path, monkeypatch, config=plan, entries=records)
    window = report["windows"][1]
    assert window["points"][0]["net_raw"] is None
    assert any(item["reason"] == reason for item in window["points"][0]["reasons"])
    assert all(query["pin"] != "latest" for query in window["queries"])


def test_wrong_chain_wrong_parent_and_reorg(tmp_path, monkeypatch):
    plan, records = fixture.plan(), fixture.records()
    chain = _at(records, "eth_chainId", lambda _: True)
    chain["response"]["result"] = "0x1"
    wrong_chain = run(tmp_path, monkeypatch, config=plan, entries=records, name="wrong")
    assert all(w["reason"] == "chain_id_mismatch" for w in wrong_chain["windows"])

    chain["response"]["result"] = hex(fixture.CHAIN)
    header = _at(records, "eth_getBlockByNumber", lambda p: p == ["0x65", False])
    header["response"]["result"]["parentHash"] = fixture.block_hash(9)
    wrong_parent = run(tmp_path, monkeypatch, config=plan, entries=records, name="parent")
    assert wrong_parent["windows"][1]["reason"] == "block_parent_or_timestamp_mismatch"
    assert wrong_parent["windows"][2]["reason"] == "unresolved_parent_lineage"
    assert wrong_parent["latest_window_positive_id"] is None

    header["response"]["result"]["parentHash"] = fixture.block_hash(100)
    header["response"]["result"]["hash"] = fixture.block_hash(999)
    replaced = run(tmp_path, monkeypatch, config=plan, entries=records, name="reorg")
    assert replaced["windows"][1]["reason"] == "planned_block_hash_mismatch"
    assert not replaced["windows"][1]["points"]


def test_hook_and_malformed_inputs_fail_closed(tmp_path, monkeypatch):
    plan = fixture.plan()
    plan["pools"][0]["hooks"] = fixture.address(5)
    plan["pools"][0]["pool_id"] = (
        "0x"
        + fixture.keccak(
            fixture.encode(
                [fixture.KEY_ABI], [(fixture.NATIVE, fixture.KNTQ, 3000, 60, fixture.address(5))]
            )
        ).hex()
    )
    report = run(tmp_path, monkeypatch, config=plan)
    assert report["windows"][1]["issues"]["first"] == "unsupported_v4_hook_or_dynamic_fee"
    assert all(point["net_raw"] is None for point in report["windows"][1]["points"])
    plan["pools"][0]["token0"], plan["pools"][0]["token1"] = (
        plan["pools"][0]["token1"],
        plan["pools"][0]["token0"],
    )
    run(tmp_path, monkeypatch, config=plan, name="token-order", expected=2)
    records = fixture.records()
    records[0]["original_http_response_sha256"] = "0" * 64
    run(tmp_path, monkeypatch, entries=records, name="false-origin", expected=2)


def test_restart_duplicate_inputs_and_concurrent_contexts(tmp_path, monkeypatch):
    baseline = run(tmp_path, monkeypatch)
    assert run_cycle_research.main() == 2  # Output is preserved across repeat or racing writers.
    assert json.loads((tmp_path / "run.report.json").read_bytes()) == baseline

    plan = fixture.plan()
    plan["sizes_raw"].reverse()
    reverse = run(tmp_path, monkeypatch, config=plan, name="reverse")
    assert [p["net_raw"] for p in reverse["windows"][1]["points"]] == [
        p["net_raw"] for p in baseline["windows"][1]["points"]
    ]

    path = tmp_path / "run.plan.json"
    export = ("synthetic", tmp_path / "run.export.json")
    different = tmp_path / "different.json"
    plan["windows"] = [plan["windows"][2]]
    different.write_text(json.dumps(plan))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_cycle_research._report, path, [export, export])
        second = pool.submit(run_cycle_research._report, different, [export])
        assert json.loads(json.dumps(first.result())) == baseline
        assert second.result()["windows"][0]["block"] == baseline["windows"][2]["block"]
        assert second.result()["latest_window_positive_id"] is None


def test_missing_real_exports_are_a_separate_result(tmp_path, monkeypatch):
    output = tmp_path / "elysium.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_cycle_research",
            "--plan",
            str(ROOT / "configs/cycle-research.elysium.example.json"),
            "--output",
            str(output),
        ],
    )
    assert run_cycle_research.main() == 0
    report = json.loads(output.read_bytes())
    assert report["chain_id"] == 99901 and report["scenario"] == "historical"
    assert report["evidence_manifest"] == []
    assert report["window_decisions"] == {"unavailable": 3}
    assert report["historical_case"]["status"] == "unresolved"
    assert report["historical_case"]["winner"]["reason"] == "missing_retained_reply"


def test_receipt_unattributed_flows_and_duplicates_block_reconciliation(tmp_path, monkeypatch):
    plan, records = fixture.plan(), fixture.records()
    row = _at(records, "eth_getTransactionReceipt", lambda p: p == [fixture.WINNER])
    logs = row["response"]["result"]["logs"]
    extra = copy.deepcopy(logs[3])
    extra["topics"][2] = fixture.data(["address"], [fixture.USER])
    extra["logIndex"] = hex(len(logs))
    logs.append(extra)
    report = run(tmp_path, monkeypatch, config=plan, entries=records)
    winner = report["historical_case"]["winner"]
    assert winner["receipt_path_status"] == "abi_decoded"
    assert winner["decoded"]["erc20_flow_issue"] == "case_unattributed_erc20_transfer"
    assert winner["decoded"]["fully_reconciled_net_raw"] is None
    logs.append(copy.deepcopy(extra))
    dup = run(tmp_path, monkeypatch, config=plan, entries=records, name="duplicate")
    assert dup["historical_case"]["winner"]["reason"] == "case_duplicate_or_unordered_logs"


def test_agreeing_sources_keep_distinct_provenance_without_independence_claim(tmp_path):
    plan = fixture.plan()
    plan["sources"].append("mirror")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    bundle = tmp_path / "input.json"
    fixture.write_bundle(bundle, fixture.records())
    report = run_cycle_research._report(plan_path, [("synthetic", bundle), ("mirror", bundle)])
    best = _best(report["windows"][1])
    assert best["net_raw"] == 64_648
    assert len(best["legs"][0]["evidence"]) == len(set(best["legs"][0]["evidence"]))
    chain = report["windows"][1]["queries"][0]
    assert len(chain["evidence"]) == 2
    assert {e["source"] for e in chain["evidence"]} == {"synthetic", "mirror"}
    assert report["provider_independence"] == "unverified"


def test_split_pool_fee_and_wrapped_burn_reconcile_without_native_payout(tmp_path, monkeypatch):
    rows = fixture.records()
    receipt = _at(rows, "eth_getTransactionReceipt", lambda p: p == [fixture.WINNER])
    logs = receipt["response"]["result"]["logs"]
    burn = logs[-1]
    burn["topics"] = [
        fixture.topic(fixture.TRANSFER),
        fixture.data(["address"], [fixture.EXECUTOR]),
        fixture.data(["address"], [fixture.NATIVE]),
    ]
    fee_log = next(log for log in logs if log["topics"][-1].endswith(fixture.FEE_VAULT[2:]))
    split = copy.deepcopy(fee_log)
    split["topics"][-1] = fixture.data(["address"], [fixture.address(125)])
    fee_log["data"] = fixture.data(["uint256"], [fixture.WINNER_FEE - 1])
    split["data"] = fixture.data(["uint256"], [1])
    logs.insert(logs.index(fee_log) + 1, split)
    for i, log in enumerate(logs):
        log["logIndex"] = hex(i)
    result = run(tmp_path, monkeypatch, entries=rows)
    case = result["historical_case"]
    assert case["status"] == "receipt_token_flows_reconciled"
    winner = case["winner"]["decoded"]
    assert winner["wrapped_output_raw"] == fixture.WINNER_OUTPUT
    assert winner["route_output_native_raw"] is None
    assert winner["wrapper_exit_evidence"] == "wrapped_burn_only"
    assert winner["solidly_input_fee"]["recipient"] is None
    assert winner["erc20_residuals"] == []
    split["data"] = fixture.data(["uint256"], [2])
    damaged = run(tmp_path, monkeypatch, entries=rows, name="bad_split")
    assert damaged["historical_case"]["winner"]["decoded"]["erc20_flow_issue"] == (
        "case_solidly_fee_transfers_do_not_match_event"
    )
