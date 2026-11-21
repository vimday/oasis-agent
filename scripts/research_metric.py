"""Capture two bounded public Metric reads, or replay an existing capture offline."""

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from oasis_agent.arbitrage import metric
from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.evidence.rpc_validation import (
    strict_integer,
    strict_json_object,
    strict_list,
    strict_object,
)
from oasis_agent.models.common import canonical_json, sha256_hex

LIMIT = 2 * 1024 * 1024


def write_new(path: Path, raw: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def load_plan(path: Path) -> tuple[metric.MetricPlan, bytes]:
    raw = read_bounded(path, 32 * 1024)
    strict_json_object(raw)
    return metric.MetricPlan.model_validate_json(raw), raw


def capture(plan_path: Path, directory: Path) -> int:
    plan, raw = load_plan(plan_path)
    if plan.scenario != "provider_observation":
        raise ValueError("only provider observations may collect")
    return capture_requests(raw, metric.requests(plan), plan.max_capture_seconds, directory)


def capture_requests(
    raw: bytes, specs: tuple[dict[str, Any], ...], max_seconds: int, directory: Path
) -> int:
    """Shared recorder for internally generated, prebounded public Metric reads."""
    # Exclusive directory creation prevents retries/resume or competing writers
    # from replacing a failed attempt. The plan is retained before the first I/O.
    directory.mkdir()
    write_new(directory / "plan.json", raw)
    started = int(time.time())
    manifest: dict[str, Any] = {
        "schema": "oasis-metric-capture-v1",
        "plan_sha256": sha256_hex(raw),
        "started": started,
        "exchanges": [],
        "status": "incomplete",
    }
    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        for index, spec in enumerate(specs):
            request_raw = canonical_json(spec)
            write_new(directory / f"{index}.request.json", request_raw)
            row: dict[str, Any] = {
                "request_sha256": sha256_hex(request_raw),
                "started": int(time.time()),
                "received": None,
                "http_status": None,
                "response_sha256": None,
                "error": None,
            }
            body = bytearray()
            try:
                if row["started"] - started > max_seconds:
                    raise ValueError("capture_deadline")
                with client.stream(**spec) as response:
                    row["http_status"] = response.status_code
                    for chunk in response.iter_bytes():
                        if len(body) + len(chunk) > LIMIT:
                            raise ValueError("response_size_limit")
                        body.extend(chunk)
                        if int(time.time()) - started > max_seconds:
                            raise ValueError("capture_deadline")
                if int(time.time()) - started > max_seconds:
                    raise ValueError("capture_deadline")
                if row["http_status"] == 200:
                    strict_json_object(bytes(body))
            except httpx.HTTPError, ValueError:
                row["error"] = "transport_or_resource_limit"
            row["received"] = int(time.time())
            row["response_sha256"] = sha256_hex(bytes(body))
            write_new(directory / f"{index}.response.json", bytes(body))
            manifest["exchanges"].append(row)
            if row["error"] or row["http_status"] != 200:
                break
        else:
            manifest["status"] = "complete"
    write_new(directory / "capture.json", canonical_json(manifest) + b"\n")
    return 0 if manifest["status"] == "complete" else 3


def replay(directory: Path) -> dict[str, Any]:
    plan, plan_raw = load_plan(directory / "plan.json")
    payloads, provenance = replay_requests(
        plan_raw, metric.requests(plan), plan.max_capture_seconds, directory
    )
    report = metric.screen(plan, payloads[0], payloads[1], provenance["exchanges"][-1]["received"])
    report.update(provenance)
    report["implementation_sha256"] = sha256_hex(
        canonical_json(
            {
                "screen": sha256_hex(Path(metric.__file__).read_bytes()),
                "entrypoint": sha256_hex(Path(__file__).read_bytes()),
            }
        )
    )
    return report


def replay_requests(
    plan_raw: bytes, specs: tuple[dict[str, Any], ...], max_seconds: int, directory: Path
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    capture_raw = read_bounded(directory / "capture.json", 32 * 1024)
    manifest = strict_json_object(capture_raw)
    if (
        set(manifest) != {"schema", "plan_sha256", "started", "exchanges", "status"}
        or manifest.get("schema") != "oasis-metric-capture-v1"
        or manifest.get("plan_sha256") != sha256_hex(plan_raw)
        or manifest.get("status") != "complete"
    ):
        raise ValueError("incomplete_or_mismatched_capture")
    started = strict_integer(manifest.get("started"), "started")
    rows = strict_list(manifest.get("exchanges"), "exchanges")
    if len(rows) != len(specs):
        raise ValueError("capture_coverage")
    previous = started
    payloads = []
    for index, (item, spec) in enumerate(zip(rows, specs, strict=True)):
        row = strict_object(item, "exchange")
        if set(row) != {
            "request_sha256",
            "response_sha256",
            "started",
            "received",
            "http_status",
            "error",
        }:
            raise ValueError("unexpected_exchange_fields")
        begin = strict_integer(row.get("started"), "started")
        received = strict_integer(row.get("received"), "received")
        if not 0 <= started <= previous <= begin <= received <= started + max_seconds:
            raise ValueError("capture_clock_incoherent")
        previous = received
        request_raw = read_bounded(directory / f"{index}.request.json", 32 * 1024)
        body = read_bounded(directory / f"{index}.response.json", LIMIT)
        if (
            request_raw != canonical_json(spec)
            or row.get("request_sha256") != sha256_hex(request_raw)
            or row.get("response_sha256") != sha256_hex(body)
            or strict_integer(row.get("http_status"), "http_status") != 200
            or row.get("error") is not None
        ):
            raise ValueError("capture_integrity_failure")
        payloads.append(strict_json_object(body))
    return payloads, {
        "plan_sha256": sha256_hex(plan_raw),
        "capture_sha256": sha256_hex(capture_raw),
        "exchanges": rows,
        "request_specs": specs,
        "response_body_bytes_retained": True,
        "capture_clocks": "local_declarations_not_independent_attestation",
    }


def publish(path: Path, report: dict[str, Any]) -> None:
    with tempfile.TemporaryDirectory(dir=path.parent) as directory:
        staged = Path(directory) / "report.json"
        write_new(staged, canonical_json(report) + b"\n")
        os.link(staged, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("capture", help="two public HTTP reads, no RPC/auth/retries")
    collect.add_argument("--plan", required=True, type=Path)
    collect.add_argument("--out", required=True, type=Path, help="new directory, never resumed")
    offline = commands.add_parser(
        "replay", help="offline immutable capture validation and screening"
    )
    offline.add_argument("--capture", required=True, type=Path)
    offline.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "capture":
            code = capture(args.plan, args.out)
            print(
                "Metric public capture complete"
                if code == 0
                else "Metric capture stopped; retained"
            )
            return code
        report = replay(args.capture)
        publish(args.output, report)
        print(
            f"{report['evidence_class']} | {report['decision']} | "
            f"{report['cost_complete_points']}/{report['planned_points']} cost-complete | "
            f"{report['positive_estimates']} positive estimates | execution eligible false"
        )
        return 0
    except OSError, ValueError, TypeError, ValidationError:
        print(
            "Metric research refused: invalid input, incomplete capture or output exists",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
