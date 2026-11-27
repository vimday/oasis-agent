"""Rebuild the two prospective shadow windows and the independent Base control offline."""

import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

from oasis_agent.models.common import canonical_json
from scripts.replay_base_shadow_survival import replay

ROOT = Path(__file__).resolve().parents[3]
RESEARCH = ROOT / "reports/research"
DOCS = ROOT / "docs/verification"
BASE = RESEARCH / "y2026-base-20261127"
CONTROL = RESEARCH / "y2026-base-control-20261127"
RH = RESEARCH / "y2026-robinhood-20261127"


def read(path):
    return json.loads(path.read_bytes())


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def check_digest(path, expected):
    assert digest(path) == expected, path


def run_verifier(path, expected):
    check_digest(path, expected)
    env = os.environ | {"PYTHONPATH": os.pathsep.join(map(str, (ROOT, DOCS / "fixtures")))}
    return subprocess.run(
        [sys.executable, str(path)], env=env, cwd=ROOT, check=True, capture_output=True
    ).stdout


def evidence(root, files):
    return {str((root / name).relative_to(ROOT)): digest(root / name) for name in files}


def base():
    prereg = BASE / "preregistered.json"
    check_digest(prereg, "6f509a6e0382e321254e7599d56a08392486eda821435b06261d43a9fb791704")
    scope = read(prereg)
    check_digest(BASE / "run_capture.py", scope["driver_sha256"])
    r = replay(BASE / "run", arrival_deadlines=True)
    s = read(BASE / "run/shadow_summary.json")
    stream = [f["observation"] for f in r["head_schedule"]["frames"] if "observation" in f]
    frames = {frame["head"]["block_hash"]: frame for frame in stream}
    heads = []
    for h, deadline in zip(r["heads"], r["observed_raw_child_deadlines"], strict=True):
        head = frames[h["block_hash"]]
        (child,) = [
            f
            for f in stream
            if f["head"]["parent_hash"] == h["block_hash"] and f["head"]["height"] == h["block"] + 1
        ]
        delay = child["received_monotonic_ns"] - head["received_monotonic_ns"]
        assert (
            delay - h["head_to_discovery_ns"]
            == deadline["child_receive_minus_discovery_finished_ns"]
        )
        heads.append(
            {
                "height": h["block"],
                "hash": h["block_hash"],
                "parent_hash": h["parent_hash"],
                "child_hash": child["head"]["block_hash"],
                "child_height": child["head"]["height"],
                "cohorts": h["cohorts"],
                "head_to_child_ws_ns": delay,
                "head_to_state_ns": h["head_to_state_ns"],
                "head_to_discovery_ns": h["head_to_discovery_ns"],
                "deadline": deadline,
            }
        )
    assert len(heads) == 4 and r["normal_discovery_candidates"] == 0
    return {
        "chain_id": s["chain_id"],
        "start": read(BASE / "start.json"),
        "end": read(BASE / "end.json"),
        "frozen_utc": scope["frozen_utc"],
        "frame_count": len(stream),
        "selected_heads": heads,
        "selected_head_adjacent_pairs": r["arrival_deadlines"]["witnessed_direct_child_pairs"],
        "all_frames_witnessed_direct_children": len(heads),
        "discovery_finished_before_child_arrival": sum(
            h["head_to_discovery_ns"] < h["head_to_child_ws_ns"] for h in heads
        ),
        "normal_discovery_candidates": r["normal_discovery_candidates"],
        "whole_transaction_simulations": r["whole_transaction_simulations"],
        "same_envelope_child_simulations": r["same_envelope_child_simulations"],
        "candidate_net_wei": {"H": None, "K": None, "C": None},
        "positive_survival_rate": r["positive_survival_rate"],
        "max_total_primitives": scope["all_base_primitives_cap"],
        "rpc_attempts": s["mixed_base_state"]["rpc_attempts_by_source"]
        | s["base_newheads"]["rpc_attempts_by_source"],
        "status": s["base_newheads"]["status"],
        "stop_reason": s["base_newheads"]["stop_reason"],
        "source_identity": scope["code_identity"],
        "retained": evidence(
            BASE,
            [
                "preregistered.json",
                "run_capture.py",
                "run/shadow_summary.json",
                "run/shadow_attribution.jsonl",
                "run/mixed_base_state/original-budget.sqlite3",
                "run/base_newheads/original-budget.sqlite3",
            ],
        ),
    }


def control():
    prereg = CONTROL / "scope.json"
    check_digest(prereg, "ade0b09b1dac374dc98b883e2595c7c6fe967d61ca1784b5e7eb5456eac0024b")
    scope = read(prereg)
    check_digest(CONTROL / "control.py", scope["code_sha256"])
    run_verifier(
        CONTROL / "verify.py", "d6810b6cbb249d0b01fb0e15fb1535e9d63e9d9cdd9136efb4f8e29153432d10"
    )
    r = read(CONTROL / "report.json")
    k_request = read(CONTROL / "run/latest-immediate-header.meta")
    h_reply = r["stages"]["H"]["whole_reply_monotonic_ns"]
    stages = {}
    for label, source in (("H", "H"), ("K", "sim_reply_latest"), ("C", "Hplus1")):
        row = r["stages"][source]
        stage = {
            k: row.get(k)
            for k in (
                "pin",
                "quoted_amounts",
                "quoted_gross_wei",
                "quoted_net_at_max_cost_wei",
                "l1_upper_wei",
                "operator_upper_wei",
                "whole_gas_used",
                "whole_weth_delta",
                "whole_l2_wei",
                "whole_net_upper_cost_scenario_wei",
            )
        }
        if label == "K":
            stage["node_refusal"] = read(CONTROL / "run/sim_reply_latest-simulate.response")[
                "error"
            ]
        else:
            assert stage["whole_net_upper_cost_scenario_wei"] == (
                row["whole_weth_delta"]
                - scope["value"]
                - row["whole_l2_wei"]
                - row["l1_upper_wei"]
                - row["operator_upper_wei"]
            )
        stages[label] = stage
    return {
        "chain_id": scope["chain_id"],
        "kind": "preselected_negative_control",
        "frozen_utc": scope["frozen_utc"],
        "started_utc": read(CONTROL / "started.json")["utc"],
        "finished_utc": r["finished_utc"],
        "route": scope["route"],
        "principal_wei": scope["value"],
        "minout_wei": scope["min_out"],
        "from": r["envelope"]["from"],
        "to": r["envelope"]["to"],
        "nonce": int(r["envelope"]["nonce"], 16),
        "envelope_sha256": sha256(canonical_json(r["envelope"])).hexdigest(),
        "H_feed_to_whole_ns": h_reply - r["observed_head"]["received_monotonic_ns"],
        "K_latest_query_after_H_reply_ns": k_request["sent_ns"] - h_reply,
        "K_latest_response_after_H_reply_ns": k_request["received_ns"] - h_reply,
        "stages": stages,
        "status": r["status"],
        "first_refusal": r["reason"],
        "cost_basis": "simulated incremental WETH/ETH parity, L2 plus L1/operator upper bounds",
        "actual_settlement_net_wei": None,
        "rpc_attempts": r["official_counts"] | r["ws_counts"],
        "retained": evidence(
            CONTROL,
            [
                "scope.json",
                "control.py",
                "verify.py",
                "report.json",
                "STOP.json",
                "run/original.sqlite3",
                "run/heads/original-budget.sqlite3",
            ],
        ),
    }


def robinhood():
    scope_file = RH / "scope-valid.json"
    check_digest(scope_file, "f8489005a99860dc0308acdf13886d63feb2da94e3a75dabe341f3208d433b2e")
    scope = read(scope_file)
    check_digest(RH / "capture.py", scope["source_sha256"]["capture"])
    r = json.loads(
        run_verifier(
            RH / "replay.py", "c8335fcd416ac47e7ab7081cb281455dcd546aa84c2429be569164078e1420ff"
        )
    )
    raw = read(RH / "window/result.json")
    feeds = read(RH / "window/feeds-report.json")
    phases = {
        s: {k: v for k, v in phase.items() if k != "capacity"} for s, phase in r["phases"].items()
    }
    calibration = scope["calibration"]
    old_file = DOCS / "ROBINHOOD_ROUTE_GAS_CALIBRATION_20261127.json"
    check_digest(old_file, calibration["historical_calibration_sha256"])
    old = read(old_file)
    old_units = old["unit_estimates"]
    assert (old_units["optimistic"], old_units["point_median"]) == (
        calibration["optimistic_units"],
        calibration["point_units"],
    )
    old_height = old["holdout"]["phases"]["discovery"]["height"]
    assert old_height < phases["discovery"]["height"] and r["frozen"]["kind"] == "negative_control"
    rows = []
    for state in raw["states"]:
        cohort = [q for q in state["quotes"] if q["route"] in ("v4-2-e14", "v4-2-e15", "v4-2-e16")]
        assert [q["input_wei"] for q in cohort] == calibration["amounts_wei"]
        for q in cohort:
            gross = q["output_wei"] - q["input_wei"]
            fee = state["fee"]
            assert (
                gross == q["gross_wei"]
                and gross - 21000 * fee == q["optimistic_net_wei_before_other_costs"]
            )
            rows.append(
                {
                    "stage": state["stage"],
                    "height": state["n"],
                    "route": q["route"],
                    "principal_wei": q["input_wei"],
                    "gross_wei": gross,
                    "base_fee_wei_per_gas": fee,
                    "net_at_21000_wei": gross - 21000 * fee,
                    "net_at_old_min_wei": gross - old_units["optimistic"] * fee,
                    "net_at_old_median_wei": gross - old_units["point_median"] * fee,
                }
            )
    assert len(rows) == 9 and all(row["net_at_21000_wei"] < 0 for row in rows)
    assert all(p["estimated_gas"] is None and p["revert_detail"] for p in phases.values())
    return {
        "chain_id": scope["chain_id"],
        "sources": scope["sources"],
        "frozen_utc": scope["locked"]["utc"],
        "finished_utc": raw["completed"]["utc"],
        "source_identity": scope["source_sha256"],
        "feed_frames": {
            n: read(RH / f"window/feeds/{n}/capture.json")["frames_used"] for n in scope["feeds"]
        },
        "feed_stops": feeds["reasons"],
        "primary_sequences": feeds["primary_sequences"],
        "delayed_sequences": feeds["delayed_sequences"],
        "matched_feed_heads": r["matched_feed_heads"],
        "primary_internal_missing": r["primary_internal_missing"],
        "delayed_median_delta_ns": int(r["delayed_median_delta_ns"]),
        "rpc_attempts": r["source_counts"],
        "rpc_per_source_cap": scope["max_http_per_source"],
        "rpc_stop": read(RH / "window/STOP")["reason"],
        "new_positive_discovery_candidates": phases["discovery"]["positive_lower_cost_quotes"],
        "frozen": r["frozen"],
        "phases": phases,
        "quotes_by_phase": {state["stage"]: state["quotes"] for state in raw["states"]},
        "K_feed_bracket": read(RH / "window/return-head-selection.json"),
        "H_whole_vs_child_margin_ns": r["h_whole_vs_first_child_feed_margin_ns"],
        "actual_settlement_net_wei": None,
        "retained": evidence(
            RH,
            [
                "scope-valid.json",
                "capture.py",
                "replay.py",
                "window/result.json",
                "window/STOP",
                "window/original.sqlite3",
                "window/feeds-report.json",
                "window/return-head-selection.json",
            ],
        ),
        "new_calibration_comparison": {
            "rules_frozen": calibration,
            "training_windows": len(old["old_windows"]),
            "old_holdout_height_excluded": old_height,
            "new_windows": 1,
            "new_H_sizes": len(calibration["amounts_wei"]),
            "H_K_C_size_observations": len(rows),
            "full_control_estimates": 0,
            "prediction_error_wei": None,
            "positive_candidate_survival_rate": None,
            "rows": rows,
        },
    }


if __name__ == "__main__":
    report = {
        "schema": "oasis.y2026-base-rh-shadow.20261127.v1",
        "rpc_spend_usd": 0,
        "signatures": 0,
        "base_graph": base(),
        "base_independent_control": control(),
        "robinhood": robinhood(),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
