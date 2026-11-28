"""Compare hash-bound mainnet references with the Base Anvil Linux release on Aleph."""

import argparse
import json
import platform
import re
import shlex
import subprocess
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import httpx
import rlp
from eth_hash.auto import keccak
from measure_base_fork_latency import UnsupportedProbe, local_exchange, port, server_for
from replay_base_fork_latency import STANDARD
from replay_base_mixed_whole import (
    AMOUNTS,
    CHAIN,
    DIR,
    HARNESS,
    WETH,
    abi,
    bound_state,
    data_bytes,
    digest,
    read,
    settlement,
    simulation,
    state_params,
    write,
)

from oasis_agent.evidence.base_denim import parse_header_timestamp_ms
from oasis_agent.evidence.state_trie import PRAGUE_FIELDS, hex_bytes, quantity
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts.measure_robinhood_quote_delivery import _body, _fetch, _read

ROOT = DIR.parent
SOURCE = {"archive": "https://base-rpc.publicnode.com", "official": "https://mainnet.base.org"}
POLICIES = (BudgetPolicy("archive", 150, 250), BudgetPolicy("official", 6, 250))
PAIRS = {
    "same": (
        63043739,
        "0xb8a7b643eb38ff32a8298d0a9dfb1a5fcb9fbf04c58b28b1a211d68e7ccc47dc",
        "0x9e630279da490b13e3081796122893f1077e92ebcbe1909de9af1f1ba65494f7",
    ),
    "cross": (
        63217836,
        "0xb8bbb72d3d3c4647231ae79746a3f8e07e03c19cc8ec36c14263d31cdbfb6e43",
        "0x19e317a1f5118d83a44e9b32758fb4414fa6ef52d29a70fa94d4232267f23dd5",
    ),
}
NATIVE = ROOT / "base-native-denim-supplement-20261128"
PAIR_PANEL = ROOT / "base-paired-simulators-live-20261128"
STOPPED = ROOT / "base-denim-aleph-pairs-20261128"
RELEASE_CHECKSUM = "9fc087a687e48ec626bcac9d86e67737f7cf095d11bd2033227fe4ccbdcb14ff"
ACCOUNT_METHODS = {
    "eth_getBalance": 2,
    "eth_getCode": 2,
    "eth_getStorageAt": 3,
    "eth_getTransactionCount": 2,
}
HEADER_FIELDS = ("number", "parentHash", "timestamp", "baseFeePerGas", "miner", "gasLimit")


def validate_header(raw, height, block_hash):
    fields = [
        quantity(raw[key], -width)
        if width < 0
        else hex_bytes(raw[key], width or 32, exact=width > 0)
        for key, width in PRAGUE_FIELDS
    ]
    assert "0x" + keccak(rlp.encode(fields)).hex() == raw["hash"] == block_hash
    assert quantity(raw["number"], 8) == height
    return parse_header_timestamp_ms(raw, require_denim=True)


def proof(root, stem, source, number, method, params, client=None):
    body = _body(number, method, params)
    prefix = root / stem
    if client is not None:
        with RpcBudget(root / "original.sqlite3", POLICIES, require_existing=True) as ledger:
            delay = ledger.reserve(source, now_ms=time.time_ns() // 10**6)
            if delay:
                time.sleep((delay + 1) / 1000)
                if ledger.reserve(source, now_ms=time.time_ns() // 10**6):
                    raise ValueError("archive limiter did not advance")
        _fetch(client, SOURCE[source], prefix, body)
    return _read(prefix, body, number, 1)[0]


def retained_data():
    values = {}
    for root in (STANDARD, NATIVE):
        for number, wire in enumerate(read(root / "report.json")["archive_wires"], 1):
            stem = root / wire["label"]
            req = read(stem.with_suffix(".request"))
            assert read(stem.with_suffix(".meta")) == wire["meta"]
            value = _read(stem, _body(number, req["method"], req["params"]), number, 1)[0]
            values[canonical_json([req["method"], req["params"]])] = value
    return values


def anchored_pair(root, name, client=None):
    if name == "same":
        root = STOPPED
        client = None
    height, parent_hash, child_hash = PAIRS[name]
    primary = []
    for src in ("official", "archive"):
        parent = proof(
            root,
            f"{name}-{src}-parent",
            src,
            1,
            "eth_getBlockByHash",
            [parent_hash, src == "archive"],
            client,
        )
        child = proof(
            root, f"{name}-{src}-child", src, 2, "eth_getBlockByHash", [child_hash, False], client
        )
        first_ms = validate_header(parent, height, parent_hash)
        next_ms = validate_header(child, height + 1, child_hash)
        assert child["parentHash"] == parent_hash and next_ms - first_ms == 200
        assert (parent["timestamp"] == child["timestamp"]) == (name == "same")
        primary.append((parent, child, first_ms, next_ms))
    assert primary[0][2:] == primary[1][2:]
    return primary[1]


class ForkProvider:
    def __init__(self, root, name, parent, retained, client):
        self.root = root
        self.name = name
        self.parent = parent
        self.retained = retained.copy()
        self.client = client
        self.error = None
        self.calls = []

    def handle(self, req):
        if self.error is not None:
            raise ValueError("scope stopped")
        try:
            return self.value(req["method"], req.get("params", []))
        except UnsupportedProbe:
            raise
        except (ValueError, AssertionError, KeyError, TypeError, OSError, httpx.HTTPError) as exc:
            self.error = str(exc) or "archive fork request invalid"
            raise ValueError(self.error) from exc

    def value(self, method, args):
        height, block_hash, _ = PAIRS[self.name]
        if method in ("anvil_nodeInfo", "eth_getAccountInfo"):
            # Signal method absence so Anvil uses the standard EIP-1898 account reads.
            raise UnsupportedProbe(method)
        if method in ("eth_chainId", "eth_blockNumber", "net_version", "eth_gasPrice") and not args:
            return {
                "eth_chainId": hex(CHAIN),
                "net_version": str(CHAIN),
                "eth_blockNumber": hex(height),
                "eth_gasPrice": self.parent["baseFeePerGas"],
            }[method]
        if method in ("eth_getBlockByNumber", "eth_getBlockByHash") and args[0] in (
            hex(height),
            block_hash,
        ):
            if args[1] is True:
                return self.parent
            assert args[1] is False
            return self.parent | {
                "transactions": [tx["hash"] for tx in self.parent["transactions"]]
            }
        assert method in ACCOUNT_METHODS and len(args) == ACCOUNT_METHODS[method]
        assert args[-1] in (
            hex(height),
            block_hash,
            {"blockHash": block_hash, "requireCanonical": True},
        )
        assert re.fullmatch(r"0x[0-9a-fA-F]{40}", args[0])
        if method == "eth_getStorageAt":
            assert re.fullmatch(r"0x[0-9a-fA-F]{1,64}", args[1])
        params = [*args[:-1], {"blockHash": block_hash, "requireCanonical": True}]
        key = canonical_json([method, params])
        if key not in self.retained:
            number = len(self.calls) + 1
            label = f"{self.name}-state-{number:03}"
            self.calls.append(label)
            self.retained[key] = proof(
                self.root, label, "archive", number, method, params, self.client
            )
        return self.retained[key]


@contextmanager
def aleph_fork(root, name, remote_dir, provider, version):
    height = PAIRS[name][0]
    local = port()
    server = server_for(provider)
    serving = Thread(target=server.serve_forever, daemon=True)
    serving.start()
    remote_port = 21510 if name == "same" else 21512
    archive_port = remote_port + 1
    executable = f"{remote_dir}/anvil"
    anvil_arguments = [
        executable,
        "--silent",
        "--host",
        "127.0.0.1",
        "--port",
        str(remote_port),
        "--accounts",
        "0",
        "--no-mining",
        "--optimism",
        "--base",
        "denim",
        "--chain-id",
        str(CHAIN),
        "--fork-url",
        f"http://127.0.0.1:{archive_port}",
        "--fork-block-number",
        str(height),
        "--retries",
        "0",
        "--no-storage-caching",
    ]
    ssh = [
        "ssh",
        "-S",
        "none",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "ExitOnForwardFailure=yes",
        "-L",
        f"127.0.0.1:{local}:127.0.0.1:{remote_port}",
        "-R",
        f"127.0.0.1:{archive_port}:127.0.0.1:{server.server_port}",
        "aleph",
    ]
    # All command words are fixed or validated by main. No shell expansion or external RPC on Aleph.
    with (root / f"{name}-anvil.log").open("xb") as output:
        process = subprocess.Popen(
            [*ssh, "timeout", "140", *anvil_arguments],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
        )
        try:
            endpoint = f"http://127.0.0.1:{local}"
            deadline = time.monotonic() + 35
            with httpx.Client(timeout=70, trust_env=False) as client:
                while True:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise ValueError(f"{name}: Aleph fork did not start")
                    try:
                        chain = client.post(
                            endpoint,
                            json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId"},
                            timeout=0.35,
                        )
                        if chain.status_code == 200 and chain.json().get("result") == hex(CHAIN):
                            break
                    except httpx.NetworkError, httpx.TimeoutException:
                        pass
                    time.sleep(0.1)
                yield client, endpoint
        finally:
            result = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=4",
                    "aleph",
                    "pkill",
                    "-TERM",
                    "-u",
                    version["user_id"],
                    "-f",
                    shlex.quote(f"^{re.escape(executable)} .*--port {remote_port} "),
                ],
                capture_output=True,
                check=False,
                timeout=8,
            )
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            server.shutdown()
            server.server_close()
            serving.join(timeout=2)
            if result.returncode not in (0, 1):
                raise ValueError(f"{name}: failed to stop Aleph research process")


def with_child_overrides(params, child):
    result = deepcopy(params)
    result[0]["blockStateCalls"][0]["blockOverrides"] = {
        "number": child["number"],
        "time": child["timestamp"],
        "baseFeePerGas": child["baseFeePerGas"],
        "feeRecipient": child["miner"],
        "gasLimit": child["gasLimit"],
        "prevRandao": child["mixHash"],
    }
    return result


def context_witness(params, height):
    witness = deepcopy(params)
    # Return NUMBER, TIMESTAMP, BASEFEE, COINBASE, BLOCKHASH(H), CHAINID without state changes.
    code = f"0x4360005242602052486040524160605263{height:08x}406080524660a05260c06000f3"
    block = witness[0]["blockStateCalls"][0]
    block["stateOverrides"][HARNESS] = {"code": code}
    block["calls"][0].update(value="0x0", data="0x")
    return witness


def whole(raw):
    [block] = raw
    [call] = block["calls"]
    fields = {k: call[k] for k in ("status", "returnData", "gasUsed")}
    return fields | {
        "logs": [
            {k: log[k] for k in ("address", "topics", "data", "logIndex")} for log in call["logs"]
        ]
    }


def capture_native(root, native, name, parent, params, retained, client, platform_info):
    fork = ForkProvider(root, name, parent, retained, client)
    with aleph_fork(root, name, native, fork, platform_info) as (local, endpoint):
        local_exchange(
            local,
            endpoint,
            root / f"{name}-local-parent",
            _body(1, "eth_getBlockByNumber", [parent["number"], False]),
        )
        for number, args in enumerate(params, 2):
            local_exchange(
                local,
                endpoint,
                root / f"{name}-native-{number}",
                _body(number, "eth_simulateV1", args),
            )
            if fork.error:
                raise ValueError(f"{name}: {fork.error}")
    return fork.calls


def result_for(root, name, remote, platform_info, client):
    height, parent_hash, child_hash = PAIRS[name]
    parent, child, first_ms, next_ms = anchored_pair(root, name, client)
    runtime = "0x" + (STANDARD / "BaseMixedWholeProbe.bin-runtime").read_text().strip()
    assert sha256_hex(bytes.fromhex(runtime[2:])) == read(STANDARD / "scope.json")["runtime_sha256"]
    params = simulation(height, runtime, WETH)
    child_parameters = with_child_overrides(params, child)
    native_params = [params, child_parameters, context_witness(child_parameters, height)]
    if name == "same":
        official, _ = _read(
            ROOT / "base-free-simulators-hash-20261128/official/call-2",
            _body(2, "eth_simulateV1", params),
            2,
            1,
        )
        fee_prefix, fee_id = DIR / "11-state", 11
    else:
        official, _ = _read(
            PAIR_PANEL / "official-05",
            _body(5, "eth_simulateV1", params),
            5,
            1,
        )
        fee_prefix, fee_id = PAIR_PANEL / "state-05", 5
    assert all(official[0][key] == child[key] for key in HEADER_FIELDS)
    fee_args, specs = state_params({"blockHash": parent_hash, "requireCanonical": True})
    fees = bound_state(_read(fee_prefix, _body(fee_id, "eth_call", fee_args), fee_id, 1)[0], specs)
    pin = SimpleNamespace(number=height, block_hash=parent_hash)
    cash = settlement(official, pin, fees, WETH)
    calls = (
        capture_native(
            root, remote, name, parent, native_params, retained_data(), client, platform_info
        )
        if remote
        else []
    )
    echo, _ = _read(
        root / f"{name}-local-parent",
        _body(1, "eth_getBlockByNumber", [parent["number"], False]),
        1,
        1,
    )
    assert echo["hash"] == parent_hash
    decoded = []
    for number, args in enumerate(native_params[:2], 2):
        stem = root / f"{name}-native-{number}"
        raw, _ = _read(stem, _body(number, "eth_simulateV1", args), number, 1)
        simulated, expected = raw[0], official[0]
        differences = [key for key in HEADER_FIELDS if simulated[key] != expected[key]]
        normalized = whole(raw)
        decoded.append(
            {
                "variant": "default" if number == 2 else "child_clock_and_fees",
                "complete_child_parity": not differences and normalized == whole(official),
                "header": {key: simulated[key] for key in HEADER_FIELDS},
                "header_differences": differences,
                "call_output_logs_and_gas_match": normalized == whole(official),
                "gas": int(normalized["gasUsed"], 16),
                "proof": read(stem.with_suffix(".meta")),
            }
        )
    context_path = root / f"{name}-native-4"
    context_raw, _ = _read(context_path, _body(4, "eth_simulateV1", native_params[2]), 4, 1)
    context = whole(context_raw)
    values = abi(
        ("uint256", "uint256", "uint256", "address", "bytes32", "uint256"),
        data_bytes(context["returnData"]),
    )
    observed = {
        "number": values[0],
        "timestamp": values[1],
        "base_fee": values[2],
        "coinbase": values[3],
        "blockhash_parent": "0x" + values[4].hex(),
        "chain_id": values[5],
    }
    expected = {
        "number": height + 1,
        "timestamp": int(child["timestamp"], 16),
        "base_fee": int(child["baseFeePerGas"], 16),
        "coinbase": child["miner"],
        "blockhash_parent": parent_hash,
        "chain_id": CHAIN,
    }
    anchor_root = STOPPED if name == "same" else root
    evidence = {
        "parent_echo_sha256": digest(root / f"{name}-local-parent.response"),
        "primary_parent_sha256": digest(anchor_root / f"{name}-official-parent.response"),
        "state_parent_sha256": digest(anchor_root / f"{name}-archive-parent.response"),
        "primary_child_sha256": digest(anchor_root / f"{name}-official-child.response"),
        "state_child_sha256": digest(anchor_root / f"{name}-archive-child.response"),
    }
    return {
        "name": name,
        "parent_height": height,
        "child_height": height + 1,
        "parent_hash": parent_hash,
        "child_hash": child_hash,
        "parent_ms": first_ms,
        "child_ms": next_ms,
        "public": {"header": {key: child[key] for key in HEADER_FIELDS}, "settlement": cash},
        "aleph": decoded,
        "context_witness": {
            "observed": observed,
            "differences": [key for key in expected if observed[key] != expected[key]],
            "proof": read(context_path.with_suffix(".meta")),
        },
        "proof": evidence,
        "state_calls": calls,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("capture", "replay"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--remote")
    args = parser.parse_args()
    assert bool(args.remote) == (args.mode == "capture")
    if args.remote:
        assert re.fullmatch(r"/tmp/oasis-base-denim\.[a-zA-Z0-9]+", args.remote)
        remote = subprocess.check_output(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=4",
                "aleph",
                "cd",
                args.remote,
                "&&",
                "uname",
                "-m",
                "&&",
                "sha256sum",
                "base-anvil.tar.gz",
                "anvil",
                "&&",
                "id",
                "-u",
                "&&",
                "./anvil",
                "--version",
            ],
            text=True,
        ).splitlines()
        assert remote[0] == "x86_64" and remote[1].split()[0] == RELEASE_CHECKSUM
        machine = {
            "architecture": remote[0],
            "release_sha256": RELEASE_CHECKSUM,
            "binary_sha256": remote[2].split()[0],
            "user_id": remote[3],
            "version": remote[4:],
            "controller": platform.platform(),
        }
        args.root.mkdir()
        scope = {
            "schema": "oasis.base.denim-aleph-pair.v1",
            "created_utc": datetime.now(UTC).isoformat(),
            "driver_sha256": digest(Path(__file__)),
            "limits": {"official": 6, "archive": 150, "interval_ms": 250, "usd": 0},
            "pairs": PAIRS,
            "chain_id": CHAIN,
            "principal_wei": AMOUNTS[WETH],
            "machine": machine,
        }
        write(args.root / "scope.json", scope)
        with RpcBudget(args.root / "original.sqlite3", POLICIES) as budget:
            assert sum(budget.counts().values()) == 0
    else:
        scope = read(args.root / "scope.json")
        assert scope["driver_sha256"] == digest(Path(__file__))
    rows = []
    fault = None
    try:
        with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as transport:
            rows.extend(
                result_for(
                    args.root,
                    name,
                    args.remote,
                    scope["machine"],
                    transport if args.remote else None,
                )
                for name in PAIRS
            )
    except (ValueError, KeyError, AssertionError, TypeError, OSError, httpx.HTTPError) as exc:
        fault = str(exc) or "canonical context or simulation output mismatch"
    if args.remote:
        write(args.root / "STOP.json", {"fault": fault})
    else:
        assert read(args.root / "STOP.json") == {"fault": fault}
        for row in rows:
            row["state_calls"] = [
                p.stem for p in sorted(args.root.glob(f"{row['name']}-state-*.request"))
            ]
            for number, name in enumerate(row["state_calls"], 1):
                stem = args.root / name
                req = read(stem.with_suffix(".request"))
                assert req["method"] in ACCOUNT_METHODS
                assert req["params"][-1] == {
                    "blockHash": row["parent_hash"],
                    "requireCanonical": True,
                }
                _read(stem, _body(number, req["method"], req["params"]), number, 1)
    with RpcBudget(args.root / "original.sqlite3", POLICIES, read_only=True) as budget:
        counts = budget.counts()
    print(
        json.dumps(
            {
                "scope_sha256": digest(args.root / "scope.json"),
                "rows": rows,
                "counts": counts,
                "fault": fault,
                "scope": scope,
            },
            sort_keys=True,
            indent=2,
        )
    )
    raise SystemExit(0 if fault is None else 2)


if __name__ == "__main__":
    main()
