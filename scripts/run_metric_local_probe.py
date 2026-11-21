"""Own a zero-account, network-free Anvil child; retain and replay one synthetic probe call."""

import argparse
import hashlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from oasis_agent.arbitrage import metric_local, metric_probe, metric_probe_build
from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.evidence.rpc_validation import strict_digest, strict_integer, strict_json_object
from oasis_agent.models.common import canonical_json, sha256_hex

from .research_metric import publish, write_new

LIMIT = 8 * 1024 * 1024
METHODS = {
    "web3_clientVersion",
    "eth_accounts",
    "eth_chainId",
    "eth_getBlockByNumber",
    "anvil_dumpState",
    "eth_getProof",
    "eth_getCode",
    "eth_call",
}


def response(raw: bytes, identity: int) -> dict[str, Any]:
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise ValueError("local_response_framing")
    obj = strict_json_object(raw)
    if (
        obj.get("jsonrpc") != "2.0"
        or type(obj.get("id")) is not int
        or obj["id"] != identity
        or set(obj) not in ({"jsonrpc", "id", "result"}, {"jsonrpc", "id", "error"})
    ):
        raise ValueError("local_response_envelope")
    return obj


class Journal:
    def __init__(self, directory: Path, ipc: Path, deadline: float) -> None:
        self.directory, self.ipc, self.deadline = directory, ipc, deadline
        self.rows: list[dict[str, Any]] = []

    def call(self, method: str, params: list[object]) -> dict[str, Any]:
        if method not in METHODS or len(self.rows) >= 11 or time.monotonic() >= self.deadline:
            raise ValueError("local_request_scope_or_deadline")
        identity = len(self.rows) + 1
        raw = (
            canonical_json({"jsonrpc": "2.0", "id": identity, "method": method, "params": params})
            + b"\n"
        )
        write_new(self.directory / f"{identity}.request.json", raw)
        row = {
            "id": identity,
            "request_sha256": sha256_hex(raw),
            "started_ns": time.time_ns(),
            "response_sha256": None,
            "received_ns": None,
            "complete": False,
        }
        self.rows.append(row)
        body = bytearray()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(min(10, max(0.001, self.deadline - time.monotonic())))
                client.connect(str(self.ipc))
                client.sendall(raw)
                while True:
                    if time.monotonic() >= self.deadline:
                        raise ValueError("local_capture_deadline")
                    chunk = client.recv(min(65536, LIMIT + 1 - len(body)))
                    if not chunk:
                        raise ValueError("local_response_eof")
                    body.extend(chunk)
                    if len(body) > LIMIT:
                        raise ValueError("local_response_size")
                    if b"\n" in chunk:
                        if not chunk.endswith(b"\n") or chunk.count(b"\n") != 1:
                            raise ValueError("local_response_framing")
                        break
            row["complete"] = True
        finally:
            write_new(self.directory / f"{identity}.response.json", bytes(body))
            row["response_sha256"] = sha256_hex(bytes(body))
            row["received_ns"] = time.time_ns()
        return response(bytes(body), identity)


def inputs(directory: Path) -> tuple[metric_probe.Plan, metric_local.Image, bytes]:
    plan_raw = read_bounded(directory / "plan.json", 32 * 1024)
    image_raw = read_bounded(directory / "image.json", 4 * 1024 * 1024)
    strict_json_object(plan_raw)
    strict_json_object(image_raw)
    plan = metric_probe.Plan.model_validate_json(plan_raw)
    image = metric_local.Image.model_validate_json(image_raw)
    image.validate_plan(plan)
    runtime = metric_probe_build.materialize(
        plan, read_bounded(directory / "artifact.json", 4 * 1024 * 1024)
    )
    return plan, image, runtime


def run(plan_path: Path, image_path: Path, artifact_path: Path, directory: Path) -> dict[str, Any]:
    directory.mkdir()
    raw_inputs = {}
    for name, path, limit in (
        ("plan.json", plan_path, 32 * 1024),
        ("image.json", image_path, 4 * 1024 * 1024),
        ("artifact.json", artifact_path, 4 * 1024 * 1024),
    ):
        raw = read_bounded(path, limit)
        write_new(directory / name, raw)
        raw_inputs[name] = sha256_hex(raw)
    plan, image, runtime = inputs(directory)
    genesis = canonical_json(image.genesis(plan))
    write_new(directory / "genesis.json", genesis)
    binary = shutil.which("anvil")
    if binary is None:
        raise ValueError("local_anvil_missing")
    executable = Path(binary).resolve()
    with executable.open("rb") as handle:
        binary_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    started_ns = time.time_ns()
    initial = {
        "schema": "oasis-metric-local-run-v1",
        "inputs": raw_inputs,
        "genesis_sha256": sha256_hex(genesis),
        "anvil_binary_sha256": binary_hash,
        "started_ns": started_ns,
        "max_seconds": 60,
        "max_requests": 11,
        "remote_rpc_requests": 0,
        "request_framing": "newline_included",
        "implementation_sha256": implementation(),
        "status": "incomplete",
    }
    publish(directory / "manifest.json", initial)
    outcome, reason, child_exit, child_digest = None, None, None, None
    journal = None
    with tempfile.TemporaryDirectory(prefix="oasis-probe-") as temp:
        ipc = Path(temp) / "anvil.ipc"
        endpoint_digest = sha256_hex(str(ipc).encode())
        command = [
            str(executable),
            "--accounts",
            "0",
            "--quiet",
            "--no-mining",
            "--hardfork",
            "cancun",
            "--chain-id",
            str(plan.chain_id),
            "--number",
            str(plan.block_number),
            "--timestamp",
            str(plan.block_timestamp),
            "--init",
            str((directory / "genesis.json").resolve()),
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--ipc",
            str(ipc),
            "--disable-default-create2-deployer",
            "--no-cors",
        ]
        env = {k: os.environ[k] for k in ("PATH", "TMPDIR", "LANG") if k in os.environ}
        deadline = time.monotonic() + 60
        child = subprocess.Popen(
            command, cwd=temp, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        try:
            child_raw = canonical_json(
                {
                    "pid": child.pid,
                    "ipc_path": str(ipc),
                    "anvil_binary_sha256": binary_hash,
                    "spawned_ns": time.time_ns(),
                }
            )
            write_new(directory / "child.json", child_raw)
            child_digest = sha256_hex(child_raw)
            startup_deadline = time.monotonic() + 5
            while not ipc.exists():
                if child.poll() is not None or time.monotonic() >= startup_deadline:
                    raise ValueError("local_anvil_startup_failed")
                time.sleep(0.02)
            journal = Journal(directory, ipc, deadline)
            outcome = metric_local.exercise(plan, image, runtime, endpoint_digest, journal.call)
        except (ValueError, TypeError, KeyError, OSError, EOFError) as exc:
            reason = (
                str(exc)
                if isinstance(exc, ValueError) and str(exc).startswith(("local_", "probe_"))
                else "local_capture_or_integrity_failure"
            )
        finally:
            child.terminate()
            try:
                child_exit = child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child_exit = child.wait(timeout=3)
                reason = "local_child_required_kill"
        if child_exit != 0 and reason is None:
            reason = "local_child_exit_failure"
    record = {
        **initial,
        "status": "complete" if reason is None else "incomplete",
        "finished_ns": time.time_ns(),
        "child_exit": child_exit,
        "child_sha256": child_digest,
        "reason": reason,
        "endpoint_sha256": endpoint_digest,
        "exchanges": journal.rows if journal else [],
        "outcome": outcome if reason is None else None,
    }
    if record["status"] == "complete":
        try:
            chronology(record)
        except ValueError, TypeError, KeyError:
            record.update(
                status="incomplete", reason="local_clock_or_coverage_failure", outcome=None
            )
    publish(directory / "result.json", record)
    return record


def chronology(record: dict[str, Any]) -> None:
    start = strict_integer(record["started_ns"], "run start")
    finish = strict_integer(record["finished_ns"], "run finish")
    if not 0 < start <= finish <= start + 65 * 10**9:
        raise ValueError("local_run_clock")
    rows = record["exchanges"]
    if not isinstance(rows, list) or len(rows) != 11:
        raise ValueError("local_exchange_coverage")
    previous = start
    for index, row in enumerate(rows, 1):
        if set(row) != {
            "id",
            "started_ns",
            "received_ns",
            "complete",
            "request_sha256",
            "response_sha256",
        }:
            raise ValueError("local_exchange_shape")
        begin = strict_integer(row["started_ns"], "request start")
        end = strict_integer(row["received_ns"], "response time")
        if (
            strict_integer(row["id"], "id") != index
            or row["complete"] is not True
            or not previous <= begin <= end <= finish
        ):
            raise ValueError("local_exchange_clock_or_coverage")
        previous = end


def replay(directory: Path) -> dict[str, Any]:
    record: dict[str, Any] = strict_json_object(read_bounded(directory / "result.json", 64 * 1024))
    initial = strict_json_object(read_bounded(directory / "manifest.json", 8192))
    expected = {
        "schema",
        "inputs",
        "genesis_sha256",
        "anvil_binary_sha256",
        "started_ns",
        "max_seconds",
        "max_requests",
        "remote_rpc_requests",
        "status",
        "request_framing",
        "implementation_sha256",
    }
    if (
        set(initial) != expected
        or initial["status"] != "incomplete"
        or set(record)
        != expected
        | {
            "finished_ns",
            "child_exit",
            "child_sha256",
            "reason",
            "endpoint_sha256",
            "exchanges",
            "outcome",
        }
    ):
        raise ValueError("local_manifest_shape")
    if any(record.get(k) != v for k, v in initial.items() if k != "status"):
        raise ValueError("local_manifest_changed")
    if (
        record.get("status") != "complete"
        or strict_integer(record.get("child_exit"), "child exit") != 0
        or record.get("schema") != "oasis-metric-local-run-v1"
        or record.get("request_framing") != "newline_included"
        or strict_integer(record.get("max_seconds"), "deadline") != 60
        or strict_integer(record.get("max_requests"), "request limit") != 11
    ):
        raise ValueError("local_run_incomplete")
    strict_digest(record.get("endpoint_sha256"), "local endpoint")
    strict_integer(record.get("started_ns"), "run start")
    strict_integer(record.get("finished_ns"), "run finish")
    chronology(record)
    child_raw = read_bounded(directory / "child.json", 8192)
    child = strict_json_object(child_raw)
    if (
        sha256_hex(child_raw) != record["child_sha256"]
        or strict_integer(child.get("pid"), "child PID") <= 0
        or child.get("anvil_binary_sha256") != record["anvil_binary_sha256"]
        or not isinstance(child.get("ipc_path"), str)
        or sha256_hex(str(child["ipc_path"]).encode()) != record["endpoint_sha256"]
        or not record["started_ns"]
        <= strict_integer(child.get("spawned_ns"), "child start")
        <= record["exchanges"][0]["started_ns"]
    ):
        raise ValueError("local_child_identity")
    for name in ("plan.json", "image.json", "artifact.json"):
        if sha256_hex(read_bounded(directory / name, 4 * 1024 * 1024)) != record["inputs"][name]:
            raise ValueError("local_input_hash_changed")
    plan, image, runtime = inputs(directory)
    genesis = read_bounded(directory / "genesis.json", 4 * 1024 * 1024)
    if (
        genesis != canonical_json(image.genesis(plan))
        or sha256_hex(genesis) != record["genesis_sha256"]
    ):
        raise ValueError("local_genesis_changed")
    rows = record["exchanges"]
    if not isinstance(rows, list) or len(rows) != 11:
        raise ValueError("local_exchange_coverage")
    index = 0

    def retained(method: str, params: list[object]) -> dict[str, Any]:
        nonlocal index
        index += 1
        row = rows[index - 1]
        for field in ("id", "started_ns", "received_ns"):
            strict_integer(row[field], field)
        req = read_bounded(directory / f"{index}.request.json", LIMIT)
        res = read_bounded(directory / f"{index}.response.json", LIMIT)
        expected = {"jsonrpc": "2.0", "id": index, "method": method, "params": params}
        if (
            row["id"] != index
            or not req.endswith(b"\n")
            or row["complete"] is not True
            or sha256_hex(req) != row["request_sha256"]
            or sha256_hex(res) != row["response_sha256"]
            or req != canonical_json(expected) + b"\n"
            or not record["started_ns"]
            <= row["started_ns"]
            <= row["received_ns"]
            <= record["finished_ns"]
            or record["finished_ns"] - record["started_ns"] > 65 * 10**9
        ):
            raise ValueError("local_exchange_integrity")
        return response(res, index)

    result = metric_local.exercise(plan, image, runtime, record["endpoint_sha256"], retained)
    if index != len(rows) or canonical_json(result) != canonical_json(record["outcome"]):
        raise ValueError("local_replay_outcome_changed")
    return {
        "schema": "oasis-metric-local-replay-v1",
        "outcome": result,
        "retained_bytes_verified": True,
        "child_exit": record["child_exit"],
        "result_file_sha256": sha256_hex(read_bounded(directory / "result.json", 64 * 1024)),
        "independent_provider_evidence": False,
        "execution_eligible": False,
        "replayer_implementation_sha256": implementation(),
    }


def implementation() -> dict[str, str]:
    result = {"entry_point": sha256_hex(Path(__file__).read_bytes())}
    for module in (metric_local, metric_probe, metric_probe_build):
        if module.__file__ is None:
            raise ValueError("local_implementation_source_missing")
        path = Path(module.__file__)
        result[path.name] = sha256_hex(path.read_bytes())
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--image", type=Path, required=True)
    execute.add_argument("--artifact", type=Path, required=True)
    execute.add_argument("--out", type=Path, required=True)
    offline = commands.add_parser("replay")
    offline.add_argument("--capture", type=Path, required=True)
    offline.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "run":
            record = run(args.plan, args.image, args.artifact, args.out)
            ok = (
                record["status"] == "complete"
                and record["outcome"]["status"] == "local_probe_observed"
            )
        else:
            result = replay(args.capture)
            publish(args.output, result)
            ok = result["outcome"]["status"] == "local_probe_observed"
        print("Local synthetic probe observed" if ok else "Local synthetic probe refused")
        return 0 if ok else 3
    except ValueError, TypeError, KeyError, OSError, EOFError, subprocess.SubprocessError:
        print("Local probe refused; retain failed directory, no automatic retry", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
