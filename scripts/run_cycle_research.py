"""Replay a bounded cycle plan from retained files. Run with --help for inputs."""

import argparse
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from oasis_agent.arbitrage.cycle_case import review_case
from oasis_agent.arbitrage.cycle_evidence import load_exports, read_bounded
from oasis_agent.arbitrage.cycle_plan import Plan
from oasis_agent.arbitrage.cycle_research import VERSION, evaluate_windows
from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.models.common import canonical_json, sha256_hex


def _source(value: str) -> tuple[str, Path]:
    alias, sep, path = value.partition("=")
    if not sep or not alias or not path:
        raise argparse.ArgumentTypeError("evidence must use SOURCE=PATH")
    return alias, Path(path)


def _implementation_digest() -> str:
    root = Path(__file__).resolve().parent.parent
    paths = [Path(__file__), *Path(review_case.__code__.co_filename).parent.glob("cycle_*.py")]
    hashes = {
        str(path.resolve().relative_to(root)): sha256_hex(path.read_bytes()) for path in paths
    }
    return sha256_hex(canonical_json(hashes))


def _report(plan_path: Path, inputs: list[tuple[str, Path]]) -> dict[str, Any]:
    raw = read_bounded(plan_path, 128 * 1024)
    strict_json_object(raw)
    plan = Plan.model_validate_json(raw)
    config = plan.model_dump(by_alias=True, mode="json")
    config_id = sha256_hex(canonical_json(config))
    implementation = _implementation_digest()
    records = load_exports(inputs, plan.sources)
    windows = evaluate_windows(plan, records, config_id)
    case = review_case(plan, records)
    last = windows[-1]
    return {
        "schema": "oasis-cycle-report-v1",
        "implementation": VERSION,
        "implementation_sha256": implementation,
        "plan_file_sha256": sha256_hex(raw),
        "config_sha256": config_id,
        "config": config,
        "chain_id": plan.chain_id,
        "reporting_asset": plan.native.model_dump(),
        "scenario": plan.scenario,
        "evidence_manifest": [
            {
                **record.manifest(),
                "dataset_origin": (
                    "synthetic_fixture" if plan.scenario == "synthetic" else "historical_export"
                ),
            }
            for record in records
        ],
        "provider_independence": "unverified",
        "original_transport_bytes_retained": False,
        "first_observed_utc": None,
        "cross_chain_price_difference": "out_of_scope",
        "windows": windows,
        "window_denominator": len(windows),
        "window_decisions": dict(Counter(w["decision"] for w in windows)),
        "latest_window_positive_id": (
            last["best_evaluated_id"] if last["decision"] == "positive_conditional_quote" else None
        ),
        "historical_case": case,
        "atomic_simulation": "unavailable",
        "execution_eligible": False,
        "accounting": {
            "pool_fee_and_impact": "already_in_quote_output",
            "incremental_cost_unit": "native_raw_per_hypothetical_fill",
            "cost_bounds": [cost.model_dump() for cost in plan.costs],
            "inventory_mark_change_raw": None,
            "external_flows_raw": None,
            "window_failed_attempt_cost_raw": None,
            "window_realized_pnl_raw": None,
            "capital_basis": "scenario_assumptions",
        },
    }


def _publish(path: Path, report: dict[str, Any]) -> None:
    # Publish a complete file exactly once, even if two processes target the same path.
    with tempfile.TemporaryDirectory(dir=path.parent) as temp:
        staged = Path(temp) / "report.json"
        with staged.open("wb") as handle:
            handle.write(canonical_json(report) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(staged, path)


def _summary(report: dict[str, Any]) -> None:
    windows = report["window_denominator"]
    print(f"{report['scenario']} | chain {report['chain_id']} | windows {windows}")
    for window in report["windows"]:
        decisions = [window["decision"], window["reason"]]
        points = window["points"]
        best = next((p for p in points if p["id"] == window["best_evaluated_id"]), None)
        net = f" | best evaluated net {best['net_raw']} native raw" if best is not None else ""
        print(
            f"{window['name']} @ {window['requested_block']} | "
            f"{'/'.join(item for item in decisions if item)} | "
            f"{len(points)}/{window['planned_points']} evaluated{net}"
        )
    if report["historical_case"] is not None:
        print(f"Receipt review | {report['historical_case']['status']}")
    print("Atomic simulation unavailable | execution eligible false")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path, help="frozen research plan JSON")
    parser.add_argument(
        "--export",
        type=_source,
        action="append",
        default=[],
        help="SOURCE=PATH for each parsed export or bundle (repeatable)",
    )
    parser.add_argument("--output", required=True, type=Path, help="new JSON report path")
    args = parser.parse_args()
    try:
        report = _report(args.plan, args.export)
        _publish(args.output, report)
    except ValidationError as exc:
        # Pydantic's default formatter can include entire user-supplied inputs.
        print(
            f"Offline research input refused ({exc.error_count()} plan violations)", file=sys.stderr
        )
        return 2
    except FileExistsError:
        print("Offline research output already exists", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError) as exc:
        print(f"Offline research input or output refused ({type(exc).__name__})", file=sys.stderr)
        return 2
    _summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
