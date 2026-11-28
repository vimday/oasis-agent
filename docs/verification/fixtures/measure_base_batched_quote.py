"""Compare two dependent quotes plus fees with one hash-pinned Base call."""

import argparse
import json
import time
from datetime import UTC, datetime
from itertools import permutations
from pathlib import Path
from threading import Event

import httpx
import replay_base_mixed_whole as mixed
from run_base_fixed_hkc import AMOUNT, fees, market, quote
from run_base_head_first_window import read_runtime

from oasis_agent.arbitrage.cycle_protocols import abi, calldata, data_bytes
from oasis_agent.evidence.base_newheads import replay_head_observation
from oasis_agent.fastpath.base_mixed_snapshot import _header
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from oasis_agent.rpc.protocol import response_envelope
from scripts.measure_robinhood_quote_delivery import _body, _fetch, _read

PROBE = "0x" + "00" * 18 + "a258"
ARTIFACT = "BaseBatchedQuoteProbe.bin-runtime"
SOURCE = Path(__file__).with_name("BaseBatchedQuoteProbe.sol")
POOLS = (
    mixed.UNI,
    mixed.AERO,
    "0xd0b53d9277642d899df5c87a3966a349a798f224",
    "0xdbc6998296caa1652a810dc8d3baf4a8294330f1",
    "0x4e392fbfe4d0557c82d2f97f02ec39daa31516dd",
    "0x6c561b446416e1a00e8e93e221854d6ea4171372",
)
PAIRS = tuple(permutations(range(len(POOLS)), 2))
LOTS = (AMOUNT, 250_000_000_000_000_000, 10**18)
DISCOVERY_GAS = 310_551
ENDPOINTS = {
    "state": mixed.ENDPOINTS["state"],
    "sim1": mixed.ENDPOINTS["official"],
    "sim2": "https://base.drpc.org",
}
EXPECTED = (112_289_127, 67_284_190_259_874_398)


class Lane:
    def __init__(self, root, key, policies, stop, client):
        self.root, self.key, self.policies, self.stop, self.client = (
            root,
            key,
            policies,
            stop,
            client,
        )
        self.count = 0

    def rpc(self, label, method, args, earliest=1):
        if self.stop.is_set():
            raise ValueError("the frozen scope has stopped")
        self.count += 1
        prefix = self.root / f"{self.key}-{self.count:03d}-{label}"
        request = _body(self.count, method, args)
        try:
            if self.client is not None:
                ledger = self.root / "original.sqlite3"
                with RpcBudget(ledger, self.policies, require_existing=True) as budget:
                    wait_ms = budget.reserve(self.key, now_ms=time.time_ns() // 1_000_000)
                    if wait_ms:
                        time.sleep((wait_ms + 1) / 1000)
                        if self.stop.is_set():
                            raise ValueError("the frozen scope has stopped")
                        if budget.reserve(self.key, now_ms=time.time_ns() // 1_000_000):
                            raise ValueError("source pacing did not advance")
                _fetch(self.client, ENDPOINTS[self.key], prefix, request)
            value, _ = _read(prefix, request, self.count, earliest)
            return value, mixed.read(prefix.with_suffix(".meta"))
        except OSError, ValueError, httpx.HTTPError:
            self.stop.set()
            raise


def quote_runtime(root):
    return "0x" + (root / ARTIFACT).read_text().strip()


def envelope(amount):
    original = mixed.envelope(mixed.WETH)
    return original | {
        "value": hex(amount),
        "data": calldata(
            "probe(bool,uint256,uint256)", ("bool", "uint256", "uint256"), (False, amount, 1)
        ),
    }


def pin(header):
    return {"blockHash": header.block_hash, "requireCanonical": True}


def packed_quote(lane, header, runtime, amounts, earliest, baseline=False):
    inputs = ("uint256[]", "uint256[]", "bool")
    payload = calldata(
        "quoteBatch(uint256[],uint256[],bool)",
        inputs,
        (amounts, [mixed.unsigned_size(envelope(amount)) for amount in amounts], baseline),
    )
    raw, proof = lane.rpc(
        f"{header.number}-batched",
        "eth_call",
        [
            {"to": PROBE, "data": payload, "gas": hex(18_000_000)},
            pin(header),
            {PROBE: {"code": runtime}},
        ],
        earliest,
    )
    (snapshot,) = abi(
        ("(uint256,uint256,uint256,uint256[],address[6],bytes32[6],(uint256,uint256)[])",),
        data_bytes(raw),
    )
    number, timestamp_ms, operator, l1, pools, code, quotes = snapshot
    if (number, timestamp_ms, pools) != (header.number, header.timestamp_ms, POOLS):
        raise ValueError("batched quote state or pool identity differs")
    expected_points = len(amounts) * (1 if baseline else len(PAIRS))
    if len(quotes) != expected_points or len(l1) != len(amounts) or not all(any(h) for h in code):
        raise ValueError("malformed batched quote")
    return {
        "operator_wei": operator,
        "l1_wei": list(l1),
        "pool_code": ["0x" + digest.hex() for digest in code],
        "quotes": [list(row) for row in quotes],
        "proof": proof,
    }


def historical_context():
    contexts = []
    for n in (19, 3, 14):
        raw = (mixed.DIR / f"{n:02d}-official.response").read_bytes()
        obj = response_envelope(raw, n)["result"]
        contexts.append(_header(canonical_json({"jsonrpc": "2.0", "id": 1, "result": obj})))
    live = mixed.DIR.parent / "base-fixed-hkc-live-denim-20261128"
    ref = replay_head_observation(live / "heads", mixed.read(live / "heads.json")[0])
    return [*contexts, market(live, ref.head, ref)]


def plan(root, source, stamp):
    return {
        "schema": "oasis.base.quote-batch-calibration.v1",
        "frozen_utc": stamp,
        "source": source,
        "endpoint": ENDPOINTS[source],
        "budget": 18,
        "usd": 0,
        "minimum_interval_ms": 250,
        "driver_sha256": mixed.digest(Path(__file__)),
        "solidity_sha256": mixed.digest(SOURCE),
        "compiler": (root / "solc-version.txt").read_text().strip(),
        "quote_runtime_sha256": sha256_hex(data_bytes(quote_runtime(root))),
        "whole_runtime_sha256": sha256_hex(data_bytes(read_runtime(root))),
        "principal_wei": AMOUNT,
        "historical_pins": [header.block_hash for header in historical_context()],
        "order": ["serial_first", "batched_first", "serial_first", "batched_first"],
    }


def calibration(root, scope, lane):
    if lane.rpc("chain", "eth_chainId", [])[0] != hex(mixed.CHAIN):
        raise ValueError("RPC is not Base")
    headers = historical_context()
    observed, permit = lane.rpc("quote-code", "eth_getCode", [PROBE, pin(headers[1])])
    if data_bytes(observed):
        raise ValueError("quote-only override address contains deployed code")
    rows = []
    for header, order in zip(headers, scope["order"], strict=True):
        observed = {}

        def serial(header=header):
            outputs, proofs = quote(lane, header, 1)
            fee, fee_proof = fees(lane, header, 1)
            return {
                "quotes": [outputs],
                "l1_wei": [fee["l1_upper_wei"][mixed.WETH]],
                "operator_wei": fee["operator_upper_wei"],
                "proof": [*proofs, fee_proof],
            }

        def batched(header=header):
            return packed_quote(lane, header, quote_runtime(root), (AMOUNT,), 1, True)

        steps = (("serial", serial), ("batched", batched))
        for name, execute in steps if order == "serial_first" else steps[::-1]:
            observed[name] = execute()
        old, new = observed["serial"], observed["batched"]
        for key in ("quotes", "l1_wei", "operator_wei"):
            if old[key] != new[key]:
                raise ValueError(f"batched versus serial {key} differs at {header.number}")
        if header.number == mixed.HEIGHT and tuple(new["quotes"][0]) != EXPECTED:
            raise ValueError("batched quote does not reproduce the historical whole positive")
        rows.append(
            {
                "head": header.number,
                "hash": header.block_hash,
                "serial_wire_and_pacing_ns": old["proof"][-1]["received_ns"]
                - old["proof"][0]["sent_ns"],
                "batched_wire_ns": new["proof"]["received_ns"] - new["proof"]["sent_ns"],
                **observed,
            }
        )
    return {"rows": rows, "override_code_witness": permit}


def run_calibration(root, scope, capture):
    source = scope["source"]
    policies = (BudgetPolicy(source, scope["budget"], scope["minimum_interval_ms"]),)
    stop = Event()
    if capture:
        with RpcBudget(root / "original.sqlite3", policies):
            pass
    report = {}
    fault = None
    try:
        with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
            lane = Lane(root, source, policies, stop, client if capture else None)
            report = calibration(root, scope, lane)
    except (ValueError, KeyError, TypeError, OSError, httpx.HTTPError) as exc:
        fault = str(exc)
    terminal = {"fault": fault}
    if capture:
        mixed.write(root / "STOP.json", terminal)
    else:
        assert mixed.read(root / "STOP.json") == terminal
    with RpcBudget(root / "original.sqlite3", policies, read_only=True) as budget:
        counts = budget.counts()
    return report | {
        "scope_sha256": mixed.digest(root / "scope.json"),
        "requests": counts,
        **terminal,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "capture", "replay"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--source", choices=tuple(ENDPOINTS))
    args = parser.parse_args()
    if args.mode == "prepare":
        if args.source is None:
            parser.error("--source is required with prepare")
        mixed.write(
            args.root / "scope.json", plan(args.root, args.source, datetime.now(UTC).isoformat())
        )
        return
    scope = mixed.read(args.root / "scope.json")
    assert scope == plan(args.root, scope["source"], scope["frozen_utc"])
    report = run_calibration(args.root, scope, args.mode == "capture")
    print(json.dumps(report, indent=2, sort_keys=True))
    raise SystemExit(2 if report["fault"] else 0)


if __name__ == "__main__":
    main()
