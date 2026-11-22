"""One-shot, anonymous Ethereum read-only V2 shadow observation and replay."""

import argparse
import hashlib
import itertools
import json
import math
import os
import re
import subprocess
import time
from pathlib import Path

from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

ENDPOINT = "https://ethereum-rpc.publicnode.com"
SOURCE = "ethereum-publicnode-free"
FACTORY = "0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f"
ROUTER = "0x7a250d5630b4cf539739df2c5dacb4c659f2488d"
ASSETS = {
    "WETH": ("0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", 18),
    "USDC": ("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6),
    "USDT": ("0xdac17f958d2ee523a2206206994597c13d831ec7", 6),
    "DAI": ("0x6b175474e89094c44da98b954eedeac495271d0f", 18),
}
SENDER = "0x000000000000000000000000000000000000d35c"
AMOUNTS = (10**14, 10**15, 10**16, 10**17, 10**18, 10**19)
CONTROL_AMOUNT = 10**16
SNAPSHOTS = 4
INTERVAL_SECONDS = 13
HTTP_LIMIT = 12
RPC_LIMIT = 80
DEADLINE_SECONDS = 110
BODY_LIMIT = 512 * 1024
POLICY = (BudgetPolicy(SOURCE, HTTP_LIMIT, 200),)
READ_METHODS = frozenset({"eth_chainId", "eth_getBlockByNumber", "eth_getCode", "eth_call"})
PAIRS = tuple(tuple(sorted(pair)) for pair in itertools.combinations(ASSETS, 2))
ROUTES = tuple(
    ("WETH", *middles, "WETH")
    for length in (2, 3)
    for middles in itertools.permutations(("USDC", "USDT", "DAI"), length)
)
RESEARCH_ROOT = (
    Path(__file__).resolve().parents[1] / "reports/research/multichain-forward-20261122/ethereum"
)


def wall_ms():
    return time.time_ns() // 1_000_000


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(body):
    return hashlib.sha256(body).hexdigest()


def write_new(path, data):
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def words(value, count=None):
    if not isinstance(value, str) or not re.fullmatch(r"0x(?:[0-9a-f]{64})+", value):
        raise ValueError("malformed ABI words")
    out = [int(value[pos : pos + 64], 16) for pos in range(2, len(value), 64)]
    if count is not None and len(out) != count:
        raise ValueError("unexpected ABI word count")
    return out


def address(value):
    (number,) = words(value, 1)
    if number >= 2**160:
        raise ValueError("malformed ABI address")
    return f"0x{number:040x}"


def word(value):
    return f"{int(value, 16) if isinstance(value, str) else value:064x}"


def hex_bytes(value, size=None):
    if not isinstance(value, str) or not re.fullmatch(r"0x(?:[0-9a-f]{2})+", value):
        raise ValueError("malformed hex bytes")
    out = bytes.fromhex(value[2:])
    if size is not None and len(out) != size:
        raise ValueError("unexpected hex byte length")
    return out


def abi_call(target, data, block):
    return "eth_call", [{"to": target, "data": data}, block]


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def invalid_json_constant(value):
    raise ValueError(f"nonstandard JSON number: {value}")


class Reads:
    def __init__(
        self,
        directory,
        budget=None,
        *,
        http_limit=HTTP_LIMIT,
        rpc_limit=RPC_LIMIT,
        deadline_seconds=DEADLINE_SECONDS,
    ):
        self.directory = directory
        self.budget = budget
        self.http_limit = http_limit
        self.rpc_limit = rpc_limit
        self.deadline = time.monotonic() + deadline_seconds
        self.http = self.logical = 0
        self.first_ms = None
        self.last_ms = None
        self.reserved_ms = []

    def batch(self, calls):
        if not calls or self.http >= self.http_limit or self.logical + len(calls) > self.rpc_limit:
            raise ValueError("frozen request limit")
        if any(method not in READ_METHODS for method, _ in calls):
            raise ValueError("RPC method outside the read-only experiment")
        self.http += 1
        post = [
            {"id": i, "jsonrpc": "2.0", "method": method, "params": params}
            for i, (method, params) in enumerate(calls, self.logical + 1)
        ]
        self.logical += len(calls)
        raw = canonical(post)
        path = self.directory / f"{self.http:02d}"
        request_path = path.with_suffix(".request.json")
        response_path = path.with_suffix(".response.json")
        record_path = path.with_suffix(".meta.json")
        if self.budget is not None:
            self.capture(raw, request_path, response_path, record_path)
        meta = json.loads(
            record_path.read_bytes(),
            object_pairs_hook=unique_pairs,
            parse_constant=invalid_json_constant,
        )
        body = response_path.read_bytes()
        if request_path.read_bytes() != raw or meta["request_sha256"] != digest(raw):
            raise ValueError("request evidence changed")
        if meta["response_sha256"] != digest(body) or meta["source"] != ENDPOINT:
            raise ValueError("response evidence changed")
        if (
            meta["reserved"] is not True
            or type(meta["started_ms"]) is not int
            or meta["started_ms"] < 0
        ):
            raise ValueError("invalid capture clocks or reservation")
        self.reserved_ms.append(meta["started_ms"])
        if (
            any(
                type(meta[key]) is not int or meta[key] < 0 for key in ("received_ms", "latency_ns")
            )
            or type(meta["curl_status"]) is not int
            or meta["started_ms"] > meta["received_ms"]
        ):
            raise ValueError("invalid capture clocks or reservation")
        if self.last_ms is not None and meta["started_ms"] < self.last_ms:
            raise ValueError("wall clock regressed")
        self.first_ms = self.first_ms if self.first_ms is not None else meta["started_ms"]
        self.last_ms = meta["received_ms"]
        if meta["curl_status"] != 0 or meta["http_status"] != "200":
            raise ValueError(
                f"transport stopped (curl {meta['curl_status']}, HTTP {meta['http_status']})"
            )
        response = json.loads(
            body,
            object_pairs_hook=unique_pairs,
            parse_constant=invalid_json_constant,
        )
        if not isinstance(response, list) or len(response) != len(post):
            raise ValueError("incomplete batch response")
        by_id = {}
        for row in response:
            if not isinstance(row, dict) or row.get("jsonrpc") != "2.0":
                raise ValueError("malformed RPC response envelope")
            if "error" in row:
                error = row["error"]
                if (
                    set(row) != {"jsonrpc", "id", "error"}
                    or not isinstance(error, dict)
                    or type(error.get("code")) is not int
                    or not isinstance(error.get("message"), str)
                ):
                    raise ValueError("malformed RPC error envelope")
                raise ValueError(f"RPC stopped (id {row['id']}, code {error['code']})")
            if set(row) != {"jsonrpc", "id", "result"}:
                raise ValueError("malformed RPC response envelope")
            if type(row["id"]) is not int or row["id"] in by_id:
                raise ValueError("invalid/duplicate RPC ID")
            by_id[row["id"]] = row["result"]
        if set(by_id) != {row["id"] for row in post}:
            raise ValueError("RPC IDs do not bind to request")
        return [by_id[row["id"]] for row in post], meta

    def verify_budget(self, ledger):
        if ledger.counts()[SOURCE] != self.http:
            raise ValueError("retained request count disagrees with original ledger")
        starts = [
            row["at_ms"]
            for (payload,) in ledger.db.execute("SELECT payload FROM rpc_usage ORDER BY seq")
            if (row := json.loads(payload))["source"] == SOURCE
        ]
        if starts != self.reserved_ms:
            raise ValueError("retained request clocks disagree with original reservations")

    def capture(self, raw, request_path, response_path, record_path):
        if time.monotonic() >= self.deadline:
            raise ValueError("campaign time limit")
        write_new(request_path, raw)
        time.sleep(0.21)
        started = wall_ms()
        started_mono = time.monotonic_ns()
        if self.budget.reserve(SOURCE, now_ms=started):
            raise ValueError("frozen budget pacing")
        process = subprocess.run(
            [
                "/usr/bin/curl",
                "-q",
                "--silent",
                "--show-error",
                "--connect-timeout",
                "4",
                "--max-time",
                "7",
                "--max-filesize",
                str(BODY_LIMIT),
                "--proto",
                "=https",
                "--header",
                "Content-Type: application/json",
                "--data-binary",
                "@-",
                "--write-out",
                "%{http_code}",
                "--output",
                str(response_path),
                ENDPOINT,
            ],
            input=raw,
            capture_output=True,
            check=False,
            env={"PATH": "/usr/bin:/bin"},
        )
        received_mono = time.monotonic_ns()
        received = wall_ms()
        if not response_path.exists():
            write_new(response_path, b"")
        with response_path.open("rb") as stream:
            body = stream.read(BODY_LIMIT + 1)
            os.fsync(stream.fileno())
        write_new(
            record_path,
            canonical(
                {
                    "request_sha256": digest(raw),
                    "response_sha256": digest(body),
                    "source": ENDPOINT,
                    "curl_status": process.returncode,
                    "http_status": process.stdout.decode("ascii"),
                    "started_ms": started,
                    "received_ms": received,
                    "reserved": True,
                    "latency_ns": received_mono - started_mono,
                }
            ),
        )
        if len(body) > BODY_LIMIT:
            raise ValueError("response limit")


def bound_and_quote(reserves, amount):
    """Return exact integer output and an all-size gross-profit bound."""
    a_total, b_total, c_total = 1, 1, 0
    for rin, rout in reserves:
        a, b, c = 997 * rout, 1000 * rin, 997
        amount = a * amount // (b + c * amount)
        a_total, b_total, c_total = a * a_total, b * b_total, b * c_total + c * a_total
        gcd = math.gcd(a_total, b_total, c_total)
        a_total, b_total, c_total = a_total // gcd, b_total // gcd, c_total // gcd
    upper = 0
    if a_total > b_total:
        numerator_upper = a_total + b_total - 2 * math.isqrt(a_total * b_total)
        upper = (numerator_upper + c_total - 1) // c_total
    return amount, upper, a_total, b_total


def checked_router_amounts(payload, directions, initial):
    size = len(directions) + 1
    offset, count, *amounts = words(payload, size + 2)
    if offset != 32 or count != size or amounts[0] != initial:
        raise ValueError("malformed Router02 return amounts")
    for depth, actual in enumerate(amounts[1:], 1):
        expected = bound_and_quote(directions[:depth], initial)[0]
        if actual != expected:
            raise ValueError(f"Router02 and model disagree at hop {depth}")
    return amounts


def observe(reads, report):
    pools = {}
    identities = {}
    control = None
    previous_number = -1
    for sample in range(SNAPSHOTS):
        if sample and reads.budget is not None:
            time.sleep(INTERVAL_SECONDS)
        (chain, head), clock = reads.batch(
            [
                ("eth_chainId", []),
                ("eth_getBlockByNumber", ["latest", False]),
            ]
        )
        if chain != "0x1":
            raise ValueError("unexpected chain ID")
        hex_bytes(head["hash"], 32)
        hex_bytes(head["parentHash"], 32)
        hex_bytes(head["stateRoot"], 32)
        number = int(head["number"], 16)
        block_ms = int(head["timestamp"], 16) * 1000
        if number <= previous_number or not 0 <= clock["received_ms"] - block_ms <= 30_000:
            raise ValueError("stale, repeated, reversed or future head")
        if (
            report["samples"]
            and number == previous_number + 1
            and head["parentHash"] != report["samples"][-1]["hash"]
        ):
            raise ValueError("head parent changed")
        previous_number = number
        block = {"blockHash": head["hash"], "requireCanonical": True}
        if sample == 0:
            factory_calls = [
                abi_call(FACTORY, "0xe6a43905" + word(ASSETS[x][0]) + word(ASSETS[y][0]), block)
                for x, y in PAIRS
            ]
            answers, _ = reads.batch(factory_calls)
            pools = {pair: address(result) for pair, result in zip(PAIRS, answers, strict=True)}
            pools = {pair: addr for pair, addr in pools.items() if int(addr, 16)}
            if len(set(pools.values())) != len(pools):
                raise ValueError("factory mapped distinct pairs to overlapping pools")
        state_calls = [abi_call(addr, "0x0902f1ac", block) for addr in pools.values()]
        if sample == 0:
            for addr in pools.values():
                state_calls.extend(
                    [
                        abi_call(addr, "0x0dfe1681", block),
                        abi_call(addr, "0xd21220a7", block),
                        ("eth_getCode", [addr, block]),
                    ]
                )
            state_calls.extend(("eth_getCode", [addr, block]) for addr in (FACTORY, ROUTER))
        state_calls.append(("eth_getBlockByNumber", [head["number"], False]))
        results, state_clock = reads.batch(state_calls)
        again = results[-1]
        if (again["number"], again["hash"], again["stateRoot"]) != (
            head["number"],
            head["hash"],
            head["stateRoot"],
        ):
            raise ValueError("pinned header changed")
        if sample == 0:
            for index, pair in enumerate(pools):
                offset = len(pools) + index * 3
                token0, token1, bytecode = results[offset : offset + 3]
                symbols = sorted(pair, key=lambda symbol: ASSETS[symbol][0])
                if [address(token0), address(token1)] != [ASSETS[s][0] for s in symbols]:
                    raise ValueError("factory pair token order disagrees")
                identities[pair] = (symbols, digest(hex_bytes(bytecode)))
            if not identities or len({entry[1] for entry in identities.values()}) != 1:
                raise ValueError("pool bytecode shape disagrees")
            report["deployment"] = {
                "factory_code_sha256": digest(hex_bytes(results[-3])),
                "router_code_sha256": digest(hex_bytes(results[-2])),
                "pools": [
                    {
                        "pool": addr,
                        "tokens": identities[pair][0],
                        "code_sha256": identities[pair][1],
                    }
                    for pair, addr in pools.items()
                ],
            }
        reserves_by_pair = {}
        pool_state = []
        for pair, result in zip(pools, results[: len(pools)], strict=True):
            r0, r1, last = words(result, 3)
            if max(r0, r1) >= 2**112 or last >= 2**32:
                raise ValueError("reserve fields out of ABI range")
            pool_state.append(
                {
                    "address": pools[pair],
                    "reserve0": r0,
                    "reserve1": r1,
                    "last_timestamp_modulo_2_32": last,
                }
            )
            if min(r0, r1) > 0:
                reserves_by_pair[pair] = dict(zip(identities[pair][0], (r0, r1), strict=True))
        gas_floor = 21_000 * int(head["baseFeePerGas"], 16)
        candidates = {}
        route_directions = {}
        for route in ROUTES:
            pairs = [tuple(sorted(pair)) for pair in itertools.pairwise(route)]
            if any(pair not in reserves_by_pair for pair in pairs):
                continue
            reserves = [
                (reserves_by_pair[pair][x], reserves_by_pair[pair][y])
                for pair, (x, y) in zip(pairs, itertools.pairwise(route), strict=True)
            ]
            route_directions[route] = reserves
            quotes = [bound_and_quote(reserves, amount) for amount in AMOUNTS]
            _, upper, a_total, b_total = quotes[0]
            candidates[route] = {
                "route": route,
                "pools": [pools[pair] for pair in pairs],
                "gross_upper_wei": upper,
                "after_fee_slope_gt_one": a_total > b_total,
                "gas_floor_excluded": upper <= gas_floor,
                "grid_out_wei": {
                    str(x): quote[0] for x, quote in zip(AMOUNTS, quotes, strict=True)
                },
            }
        tick = {
            "block": number,
            "hash": head["hash"],
            "parent_hash": head["parentHash"],
            "state_root": head["stateRoot"],
            "block_time_ms": block_ms,
            "head_received_ms": clock["received_ms"],
            "state_ready_ms": state_clock["received_ms"],
            "pool_state": pool_state,
            "base_fee_wei": int(head["baseFeePerGas"], 16),
            "gas_floor_wei": gas_floor,
            "routes": list(candidates.values()),
        }
        report["samples"].append(tick)
        if sample == 1 and candidates:
            route = max(
                candidates, key=lambda r: candidates[r]["grid_out_wei"][str(CONTROL_AMOUNT)]
            )
            expiry = int(head["timestamp"], 16) + 180
            data = "0x7ff36ab5" + word(0) + word(128) + word(SENDER) + word(expiry)
            data += word(len(route))
            data += "".join(word(ASSETS[symbol][0]) for symbol in route)
            control = (
                route,
                {
                    "to": ROUTER,
                    "from": SENDER,
                    "value": hex(CONTROL_AMOUNT),
                    "gas": hex(400_000),
                    "data": data,
                },
            )
        if control is not None and sample in (1, 2):
            route, call = control
            override = {SENDER: {"balance": hex(2 * 10**18), "code": "0x"}}
            report["execution_controls"].append(
                {"block": number, "route": route, "attempted": True}
            )
            (result,), call_clock = reads.batch([("eth_call", [call, block, override])])
            amounts = checked_router_amounts(result, route_directions[route], CONTROL_AMOUNT)
            report["execution_controls"][-1].update(
                {
                    "success": True,
                    "amounts": amounts,
                    "evm_started_ms": call_clock["started_ms"],
                    "evm_received_ms": call_clock["received_ms"],
                    "call": call,
                }
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("capture", "replay"))
    options = parser.parse_args()
    scope_path = RESEARCH_ROOT / "scope.json"
    scope = {
        "schema": 1,
        "source": ENDPOINT,
        "paid_usd": 0,
        "factory": FACTORY,
        "router": ROUTER,
        "assets": ASSETS,
        "candidate_routes": ROUTES,
        "snapshots": SNAPSHOTS,
        "interval_seconds": INTERVAL_SECONDS,
        "max_http": HTTP_LIMIT,
        "max_rpc": RPC_LIMIT,
        "campaign_deadline_seconds": DEADLINE_SECONDS,
        "grid_weth_units": AMOUNTS,
        "control_weth_units": CONTROL_AMOUNT,
        "sender": SENDER,
        "qualification": "fee-included all-size gross above intrinsic gas at observed base fee",
    }
    report = {"schema": 1, "samples": [], "execution_controls": []}
    budget = None
    ledger_path = RESEARCH_ROOT / "original-budget.sqlite3"
    if options.mode == "capture":
        RESEARCH_ROOT.mkdir(parents=True, exist_ok=False)
        write_new(scope_path, canonical(scope))
        budget = RpcBudget(ledger_path, POLICY)
    else:
        if scope_path.read_bytes() != canonical(scope):
            raise ValueError("scope changed")
    reads = Reads(RESEARCH_ROOT, budget)
    try:
        observe(reads, report)
        report["stop_reason"] = None
    except (ValueError, KeyError, TypeError, OSError) as error:
        report["stop_reason"] = str(error)
    report["http_attempts"] = reads.http
    report["logical_calls_selected"] = reads.logical
    report["first_request_started_ms"] = reads.first_ms
    report["last_response_received_ms"] = reads.last_ms
    report["scope_sha256"] = digest(scope_path.read_bytes())
    if budget is not None:
        budget.db.close()
        write_new(RESEARCH_ROOT / "result.json", canonical(report))
    else:
        offline_budget = RpcBudget(ledger_path, POLICY, read_only=True)
        try:
            reads.verify_budget(offline_budget)
        finally:
            offline_budget.db.close()
        if (RESEARCH_ROOT / "result.json").read_bytes() != canonical(report):
            raise ValueError("offline replay and retained result disagree")
    print(
        json.dumps(
            {key: value for key, value in report.items() if key not in {"samples", "deployment"}}
        )
    )
    return 3 if report["stop_reason"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
