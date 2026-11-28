"""Measure a pinned Base whole-call fork with bounded reads and an air-gapped warm phase."""

import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import time
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import httpx
from replay_base_mixed_whole import (
    CHAIN,
    DIR,
    HEIGHT,
    TIP,
    WETH,
    bound_state,
    digest,
    read,
    settlement,
    simulation,
    state_params,
    write,
)

from oasis_agent.fastpath.base_mixed_snapshot import _header
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from oasis_agent.rpc.protocol import response_envelope
from scripts.measure_robinhood_quote_delivery import _body, _fetch, _read

ROOT = Path(__file__).resolve().parents[3]
ARCHIVE = "https://base-rpc.publicnode.com"
REFERENCE = ROOT / "docs/verification/BASE_MIXED_WHOLE_20261128.json"
PRIORITY = ROOT / "docs/verification/BASE_MIXED_HEAD_PRIORITY_20261128.json"
SOURCE = "archive"
POLICIES = (BudgetPolicy(SOURCE, 180, 250),)
SAMPLES = 24
_ACCOUNT_PARAMS = {
    "eth_getBalance": 2,
    "eth_getCode": 2,
    "eth_getTransactionCount": 2,
    "eth_getStorageAt": 3,
}


def historical_header():
    raw = (DIR / "03-official.response").read_bytes()
    result = response_envelope(raw, 3)["result"]
    return _header(canonical_json({"jsonrpc": "2.0", "id": 1, "result": result}))


def frozen(root, timestamp):
    report = read(REFERENCE)
    runtime = "0x" + (root / "BaseMixedWholeProbe.bin-runtime").read_text().strip()
    assert sha256_hex(bytes.fromhex(runtime[2:])) == report["scope"]["runtime_sha256"]
    return {
        "schema": "oasis.base-warm-fork-latency.v1",
        "scope": "historical same-payload compute and cold acquisition on a bounded fork",
        "chain": CHAIN,
        "height": HEIGHT,
        "hash": report["scope"]["h"],
        "prior_hash": report["scope"]["p"],
        "reference_sha256": digest(REFERENCE),
        "previous_live_sha256": digest(PRIORITY),
        "source_sha256": digest(Path(__file__)),
        "runtime_sha256": report["scope"]["runtime_sha256"],
        "request_sha256": sha256_hex(canonical_json(simulation(HEIGHT, runtime, WETH))),
        "cold": 1,
        "warm": SAMPLES,
        "archive_url": ARCHIVE,
        "archive_budget": {"limit": 180, "interval_ms": 250, "usd": 0},
        "frozen_utc": timestamp,
    }


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def block_selector(plan, value):
    pinned = {"blockHash": plan["hash"], "requireCanonical": True}
    if isinstance(value, dict):
        if (
            set(value) <= {"blockHash", "requireCanonical"}
            and value.get("blockHash") == plan["hash"]
        ) and value.get("requireCanonical", True) is True:
            return pinned
    elif value == plan["hash"] or (
        isinstance(value, str) and re.fullmatch("0x[0-9a-f]+", value) and int(value, 16) == HEIGHT
    ):
        return pinned
    raise ValueError("fork account read outside its frozen state")


def allow(plan, method, params):
    """Return the upstream pin and the expected block hash for any header request."""
    if params is None and method in (
        "eth_chainId",
        "eth_blockNumber",
        "net_version",
        "eth_gasPrice",
    ):
        return None, None
    if not isinstance(params, list):
        raise ValueError(f"fork request lacks positional JSON parameters: {method}")
    if method in _ACCOUNT_PARAMS and len(params) == _ACCOUNT_PARAMS[method]:
        if not isinstance(params[0], str) or not re.fullmatch("0x[0-9a-fA-F]{40}", params[0]):
            raise ValueError("fork read lacks canonical account")
        if len(params) == 3 and (
            not isinstance(params[1], str) or not re.fullmatch("0x[0-9a-fA-F]{1,64}", params[1])
        ):
            raise ValueError("invalid storage key")
        return [*params[:-1], block_selector(plan, params[-1])], None
    if method in ("eth_getBlockByNumber", "eth_getBlockByHash") and len(params) == 2:
        if type(params[1]) is not bool:
            raise ValueError("invalid fork header payload flag")
        block = {
            ("eth_getBlockByNumber", hex(HEIGHT)): plan["hash"],
            ("eth_getBlockByNumber", hex(HEIGHT - 1)): plan["prior_hash"],
            ("eth_getBlockByHash", plan["hash"]): plan["hash"],
            ("eth_getBlockByHash", plan["prior_hash"]): plan["prior_hash"],
        }.get((method, params[0]))
        if block is not None:
            return params, block
    if method in ("eth_chainId", "eth_blockNumber", "net_version", "eth_gasPrice") and not params:
        return None, None
    raise ValueError(f"unfrozen fork method or state: {method}")


class UnsupportedProbe(ValueError):
    pass


class Archive:
    def __init__(self, root, plan):
        self.root = root
        self.plan = plan
        self.client = httpx.Client(timeout=5, follow_redirects=False, trust_env=False)
        self.error = None
        self.blocked_after_seal = 0
        self.sealed = False
        self.wires = []
        self.memo = {}

    def handle(self, query):
        if self.sealed:
            self.blocked_after_seal += 1
            raise ValueError("the warm fork is isolated from upstream state")
        if self.error:
            raise ValueError("the frozen archive scope stopped")
        try:
            method = query["method"]
            if method in ("anvil_nodeInfo", "eth_getAccountInfo"):
                raise UnsupportedProbe("use the pinned standard JSON-RPC reads")
            params, expected_hash = allow(self.plan, method, query.get("params", []))
            if params is None:
                return {
                    "eth_chainId": hex(CHAIN),
                    "net_version": str(CHAIN),
                    "eth_blockNumber": hex(HEIGHT),
                    "eth_gasPrice": hex(historical_header().base_fee + TIP),
                }[method]
            if expected_hash:
                if params[1] is True and expected_hash == self.plan["hash"]:
                    retained = self.root.parent / "base-fork-latency-warm-20261128/archive-001"
                    value, _ = _read(
                        retained, _body(1, "eth_getBlockByNumber", [hex(HEIGHT), True]), 1, 1
                    )
                elif params[1] is False:
                    name = (
                        "03-official.response"
                        if expected_hash == self.plan["hash"]
                        else "19-official.response"
                    )
                    value = response_envelope((DIR / name).read_bytes(), int(name[:2]))["result"]
                else:
                    raise ValueError("unexpected full header outside measured H")
                if value["hash"] != expected_hash:
                    raise ValueError("retained fork header changed")
                return value
            key = canonical_json([method, params])
            if key in self.memo:
                return self.memo[key]
            value = self.fetch_account(key, method, params)
            self.memo[key] = value
            return value
        except UnsupportedProbe:
            raise
        except (ValueError, KeyError, TypeError, OSError, httpx.HTTPError) as exc:
            self.error = str(exc)
            raise

    def fetch_account(self, key, method, params):
        ordinal = len(self.wires) + 1
        prefix = self.root / f"archive-{ordinal:03d}"
        body = _body(ordinal, method, params)
        with RpcBudget(self.root / "original.sqlite3", POLICIES, require_existing=True) as budget:
            delay = budget.reserve(SOURCE, now_ms=time.time_ns() // 1_000_000)
            if delay:
                time.sleep((delay + 1) / 1000)
                if budget.reserve(SOURCE, now_ms=time.time_ns() // 1_000_000):
                    raise ValueError("archive pacing did not advance")
        _fetch(self.client, ARCHIVE, prefix, body)
        value, _ = _read(prefix, body, ordinal, 1)
        self.wires.append(
            {"label": prefix.name, "method": method, "meta": read(prefix.with_suffix(".meta"))}
        )
        return value


def server_for(archive):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *_):
            pass

        def do_POST(self):
            ident = None
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size < 1024 * 1024:
                    raise ValueError("invalid fork request body size")
                request = json.loads(self.rfile.read(size))
                ident = request["id"]
                result = {"result": archive.handle(request)}
            except UnsupportedProbe:
                result = {"error": {"code": -32601, "message": "Method not found"}}
            except (ValueError, KeyError, TypeError, OSError, httpx.HTTPError) as exc:
                result = {"error": {"code": -32000, "message": str(exc)}}
            raw = canonical_json(dict(jsonrpc="2.0", id=ident, **result))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return HTTPServer(("127.0.0.1", 0), Handler)


def local_exchange(client, endpoint, prefix, body):
    write_request = prefix.with_suffix(".request")
    with write_request.open("xb") as output:
        output.write(body)
    sent = time.monotonic_ns()
    response = client.post(endpoint, content=body, headers={"Content-Type": "application/json"})
    received = time.monotonic_ns()
    raw = response.content
    if len(raw) > 1024 * 1024:
        raise ValueError("local return exceeded the response limit")
    with prefix.with_suffix(".response").open("xb") as output:
        output.write(raw)
    write(
        prefix.with_suffix(".meta"),
        {
            "status": response.status_code,
            "sent_ns": sent,
            "received_ns": received,
            "retrieved_unix_ns": time.time_ns(),
            "request_sha256": sha256_hex(body),
            "response_sha256": sha256_hex(raw),
        },
    )


@contextmanager
def pinned_fork(root, archive):
    anvil = shutil.which("anvil")
    if not anvil:
        raise ValueError("Anvil is unavailable")
    version = subprocess.check_output([anvil, "--version"], text=True).splitlines()
    server = server_for(archive)
    serving = Thread(target=server.serve_forever, daemon=True)
    serving.start()
    local = f"http://127.0.0.1:{port()}"
    env = dict(os.environ)
    cache_dir = root / "foundry-cache"
    cache_dir.mkdir()
    env["FOUNDRY_CACHE_PATH"] = str(cache_dir)
    args = [
        anvil,
        "--silent",
        "--host",
        "127.0.0.1",
        "--port",
        local.rsplit(":", 1)[1],
        "--accounts",
        "0",
        "--no-mining",
        "--network",
        "optimism",
        "--chain-id",
        str(CHAIN),
        "--fork-url",
        f"http://127.0.0.1:{server.server_port}",
        "--fork-block-number",
        str(HEIGHT),
        "--no-bal",
        "--retries",
        "0",
        "--no-storage-caching",
    ]
    clock = time.monotonic_ns()
    with (root / "anvil.log").open("xb") as output:
        process = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=output, stderr=output, env=env
        )
        try:
            with httpx.Client(timeout=65, follow_redirects=False, trust_env=False) as client:
                while True:
                    if process.poll() is not None or time.monotonic_ns() - clock > 45 * 10**9:
                        raise ValueError("Anvil did not start from frozen H")
                    try:
                        result = client.post(
                            local,
                            headers={"Content-Type": "application/json"},
                            content=_body(1, "eth_chainId", []),
                            timeout=0.2,
                        )
                        if result.json()["result"] != hex(CHAIN):
                            raise ValueError("local fork has wrong chain ID")
                        break
                    except httpx.ConnectError:
                        time.sleep(0.05)
                pin = historical_header()
                assert (pin.number, pin.block_hash) == (HEIGHT, archive.plan["hash"])
                hdr = client.post(
                    local,
                    headers={"Content-Type": "application/json"},
                    content=_body(1, "eth_getBlockByNumber", [hex(HEIGHT), False]),
                )
                assert hdr.json()["result"]["hash"] == pin.block_hash
                yield client, local, pin, version
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            server.shutdown()
            server.server_close()
            serving.join(timeout=3)


def run_capture(root, plan):
    reference = read(REFERENCE)
    archive = Archive(root, plan)
    rows = []
    version = []
    error = None
    runtime = "0x" + (root / "BaseMixedWholeProbe.bin-runtime").read_text().strip()
    params = simulation(HEIGHT, runtime, WETH)
    with RpcBudget(root / "original.sqlite3", POLICIES):
        try:
            with pinned_fork(root, archive) as (client, local, pin, version):
                ready = time.monotonic_ns()
                state = bound_state(read(DIR / "11-state.response")["result"], state_params({})[1])
                expected = next(
                    row
                    for row in reference["rows"]
                    if row["height"] == HEIGHT and row["token"] == WETH
                )
                for i in range(1, SAMPLES + 2):
                    if i == 2:
                        archive.sealed = True
                    prefix = root / f"local-{i:03d}"
                    body = _body(i, "eth_simulateV1", params)
                    local_exchange(client, local, prefix, body)
                    raw, _ = _read(prefix, body, i, ready)
                    summary = settlement(
                        raw,
                        pin,
                        state,
                        WETH,
                        (expected["middle_raw"], expected["final_received_raw"]),
                    )
                    meta = read(prefix.with_suffix(".meta"))
                    if archive.error is not None or archive.blocked_after_seal:
                        raise ValueError("fork state or isolation failed")
                    rows.append({"label": prefix.name, "meta": meta, "result": summary})
        except (ValueError, AssertionError, KeyError, TypeError, OSError, httpx.HTTPError) as exc:
            error = str(exc) or "fork evidence assertion failed"
        finally:
            archive.client.close()
    with RpcBudget(root / "original.sqlite3", POLICIES, read_only=True) as budget:
        counts = budget.counts()
    return {
        "scope_sha256": digest(root / "scope.json"),
        "platform": platform.platform(),
        "anvil": version,
        "rows": rows,
        "archive_requests": counts,
        "archive_methods": Counter(row["method"] for row in archive.wires),
        "archive_wires": archive.wires,
        "blocked_after_seal": archive.blocked_after_seal,
        "fault": archive.error or error,
        "execution_eligible": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "capture"))
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "prepare":
        write(root / "scope.json", frozen(root, datetime.now(UTC).isoformat()))
    else:
        plan = read(root / "scope.json")
        assert plan == frozen(root, plan["frozen_utc"])
        report = run_capture(root, plan)
        write(root / "report.json", report)
        write(root / "STOP.json", {"fault": report["fault"]})
        print(json.dumps(report, sort_keys=True, indent=2))
        raise SystemExit(0 if report["fault"] is None else 2)


if __name__ == "__main__":
    main()
