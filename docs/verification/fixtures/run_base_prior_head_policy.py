"""Capture or replay frozen Base whole-call decisions without signing or sending a transaction."""

import argparse
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from queue import Queue
from threading import Event

import httpx
import rlp

from oasis_agent.arbitrage.base_survival import decode_base_simulation
from oasis_agent.arbitrage.cycle_protocols import abi, calldata, data_bytes, topic
from oasis_agent.errors import OasisError
from oasis_agent.evidence.base_newheads import (
    ENDPOINT,
    POLICIES,
    NewHeadsSession,
    replay_head_observation,
)
from oasis_agent.evidence.rpc_validation import rpc_quantity
from oasis_agent.fastpath.base_mixed_snapshot import _header
from oasis_agent.fastpath.multicall_universe import MULTICALL3, _encode_aggregate3_calldata
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts.capture_public_rpc import durable_new
from scripts.measure_robinhood_quote_delivery import _body, _fetch, _read

OFFICIAL = "https://mainnet.base.org"
PUBLICNODE = "https://base-rpc.publicnode.com"
SOURCES = {"simulator": OFFICIAL, "state": PUBLICNODE}
ROUTER = "0xcf77a3ba9a5ca399b7c97c74d54e5b1beb874e43"
ORACLE = "0x420000000000000000000000000000000000000f"
SENDER = "0x000000000000000000000000000000000000dead"
FACTORY = "0x420dd381b31aef6683db6b902084cb0ffece40da"
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
AERO = "0x940181a94a35a4569e4529a3cdfb74e38fd98631"
POOLS = (
    "0xcdac0d6c6c59727a65f871236188350531885c43",
    "0x6cdcb1c4a4d1c3c6d054b27ac5b77e89eafb971d",
    "0x7f670f78b17dec44d5ef68a48740b6f8849cc2e6",
)
RUNTIME = "c6e4a984eb0404737cb87baa6628812afb319cbf2ad1dcd2e9a0f6f5b40765bd"
ABI = "(address,address,bool,address)[]"
ROUTES = {
    "clockwise": ((WETH, USDC, AERO, WETH), POOLS, 950_000_000_000_000),
    "counterclockwise": ((WETH, AERO, USDC, WETH), POOLS[::-1], 950_000_000_000_000),
    "minimum_output_control": ((WETH, USDC, AERO, WETH), POOLS, 1_050_000_000_000_000),
}
ANCHORS = (1, 5, 9)
POSITIONS = {x for start in ANCHORS for x in range(start, start + 3)}
AMOUNT = 10**15
GAS = 550_000
CAP = 250_000_000
TIP = 1_000_000
NONCE = 0
BUDGETS = (BudgetPolicy("simulator", 30, 250), BudgetPolicy("state", 11, 250))


def write(path, value):
    durable_new(path, canonical_json(value))


def read(path):
    return json.loads(path.read_bytes())


def utc():
    return datetime.now(UTC).isoformat()


def plan(deadline, frozen, baseline, code_path):
    return {
        "schema": "oasis-base-exact-prior-vs-head-v1",
        "chain_id": 8453,
        "deadline": deadline,
        "frozen_utc": frozen,
        "baseline_git": baseline,
        "scope_usd": 0,
        "signatures": 0,
        "broadcasts": 0,
        "no_state_overrides": True,
        "budget": {p.source: p.max_requests for p in BUDGETS},
        "providers": SOURCES | {"heads": ENDPOINT},
        "interval_ms": 250,
        "positions": sorted(POSITIONS),
        "earliest_positive_wei": 1,
        "envelopes": {k: envelope(k, deadline) for k in ROUTES},
        "pools": {k: list(v[1]) for k, v in ROUTES.items()},
        "official_router_runtime_sha256": RUNTIME,
        "purpose": (
            "Each fixed route has identical envelopes at P, H and C. A uses the latest available "
            "whole result from P. B waits for H. Only a positive net before C may propose. "
            "C evaluates the effect of the same exact transaction one step later. The high-minimum "
            "negative control is excluded from economic totals. A revert or late/missing result is "
            "a hold. Track stale-versus-fresh, realized-simulation costs and complete-case counts."
        ),
        "code_sha256": sha256_hex(code_path.read_bytes()),
    }


def envelope(key, deadline):
    coins, _, minimum = ROUTES[key]
    legs = tuple((a, b, False, FACTORY) for a, b in pairwise(coins))
    return dict(
        **{"from": SENDER},
        to=ROUTER,
        nonce=hex(NONCE),
        value=hex(AMOUNT),
        gas=hex(GAS),
        maxFeePerGas=hex(CAP),
        maxPriorityFeePerGas=hex(TIP),
        accessList=[],
        data=calldata(
            f"swapExactETHForTokens(uint256,{ABI},address,uint256)",
            ("uint256", ABI, "address", "uint256"),
            (minimum, legs, SENDER, deadline),
        ),
    )


def unsigned_size(env):
    return len(
        bytes([2])
        + rlp.encode(
            [8453, NONCE, TIP, CAP, GAS, data_bytes(ROUTER), AMOUNT, data_bytes(env["data"]), []]
        )
    )


def fee_request(block_hash, envs):
    def row(to, signature, types, args):
        return to, False, data_bytes(calldata(signature, types, args))

    calls = [
        row(ORACLE, "getOperatorFee(uint256)", ("uint256",), (GAS,)),
        row(MULTICALL3, "getEthBalance(address)", ("address",), (SENDER,)),
        *(
            row(ORACLE, "getL1FeeUpperBound(uint256)", ("uint256",), (n,))
            for n in sorted({unsigned_size(env) for env in envs.values()})
        ),
    ]
    return [
        {"to": MULTICALL3, "data": _encode_aggregate3_calldata(calls)},
        {"blockHash": block_hash, "requireCanonical": True},
    ]


def sim_request(number, env):
    return [
        {"blockStateCalls": [{"calls": [env]}], "validation": True, "traceTransfers": False},
        hex(number),
    ]


def indexed(addr):
    return "0x" + "00" * 12 + addr[2:]


def validate_payment(out, logs, key):
    coins, pools, minimum = ROUTES[key]
    (amounts,) = abi(("uint256[]",), out.return_data)
    if (
        len(amounts) != len(coins)
        or min(amounts) <= 0
        or amounts[0] != AMOUNT
        or out.weth_delta != amounts[-1]
        or out.weth_delta < minimum
    ):
        raise ValueError("wrong fixed principal or actual recipient return")

    def movement(token, topics, amount):
        matches = [
            rpc_quantity(x["logIndex"])
            for x in logs
            if x["address"] == token
            and x["topics"] == topics
            and abi(("uint256",), data_bytes(x["data"])) == (amount,)
        ]
        if len(matches) != 1:
            raise ValueError("payment must map to one actual movement")
        return matches[0]

    deposit = movement(WETH, [topic("Deposit(address,uint256)"), indexed(ROUTER)], AMOUNT)
    pays = [
        movement(
            token, [topic("Transfer(address,address,uint256)"), indexed(a), indexed(b)], amount
        )
        for token, a, b, amount in zip(
            coins, (ROUTER, *pools), (*pools, SENDER), amounts, strict=True
        )
    ]
    if [deposit, *pays] != sorted({deposit, *pays}):
        raise ValueError("wrong funding and payment order")
    return [list(amounts), [deposit, *pays]]


class Wire:
    def __init__(self, root, source, stop, capture):
        self.root = root
        self.source = source
        self.stop = stop
        self.capture = capture
        self.sequence = []
        self.client = httpx.Client(timeout=5, follow_redirects=False, trust_env=False)

    def __call__(self, label, method, params, earliest=1):
        if self.stop.is_set():
            raise ValueError("other lane stopped")
        n = len(self.sequence) + 1
        prefix = self.root / f"{self.source}-{n:02d}-{label}"
        request = _body(n, method, params)
        self.sequence.append((label, method, params))
        if self.capture:
            with RpcBudget(
                self.root / "original.sqlite3", BUDGETS, require_existing=True
            ) as budget:
                delay = budget.reserve(self.source, now_ms=time.time_ns() // 1_000_000)
                if delay:
                    time.sleep((delay + 1) / 1000)
                    if self.stop.is_set():
                        raise ValueError("other lane stopped during pacing")
                    if budget.reserve(self.source, now_ms=time.time_ns() // 1_000_000):
                        raise ValueError("pacing failed")
            _fetch(self.client, SOURCES[self.source], prefix, request)
        data, clock = _read(prefix, request, n, earliest)
        return data, clock, read(prefix.with_suffix(".meta"))

    def close(self):
        self.client.close()


def verify_origin(sim, state):
    if rpc_quantity(sim("chain", "eth_chainId", [])[0]) != 8453:
        raise ValueError("wrong simulation chain")
    if rpc_quantity(state("chain", "eth_chainId", [])[0]) != 8453:
        raise ValueError("wrong state chain")
    if rpc_quantity(sim("nonce", "eth_getTransactionCount", [SENDER, "latest"])[0]) != NONCE:
        raise ValueError("public actor nonce changed")


def run(root, capture):
    scope = read(root / "scope.json")
    source = root / "capture.py"
    if scope != plan(scope["deadline"], scope["frozen_utc"], scope["baseline_git"], source):
        raise ValueError("frozen scope changed")
    if capture and source.read_bytes() != Path(__file__).read_bytes():
        raise ValueError("capture source differs from the frozen plan")
    envs = scope["envelopes"]
    stop = Event()
    sim = Wire(root, "simulator", stop, capture)
    state = Wire(root, "state", stop, capture)
    with RpcBudget(
        root / "original.sqlite3", BUDGETS, require_existing=not capture, read_only=not capture
    ):
        pass
    heads = []
    stream = None
    rows = []
    failure = None
    fees = {}
    identity = None
    ws_counts = None
    state_queue = Queue(maxsize=len(POSITIONS) + 1)
    sim_queue = Queue(maxsize=len(POSITIONS) + 1)
    try:
        verify_origin(sim, state)
        header, _, meta = sim("runtime-header", "eth_getBlockByNumber", ["latest", False])
        block = _header(canonical_json({"jsonrpc": "2.0", "id": 1, "result": header}))
        runtime, _, runtime_meta = state(
            "runtime",
            "eth_getCode",
            [ROUTER, {"blockHash": block.block_hash, "requireCanonical": True}],
        )
        if sha256_hex(data_bytes(runtime)) != RUNTIME:
            raise ValueError("unknown router runtime")
        identity = {"header": meta, "runtime": runtime_meta, "block_hash": block.block_hash}

        def price_all():
            try:
                for row in iter(state_queue.get, None):
                    h = row["head"]
                    result, end, proof = state(
                        str(h["height"]),
                        "eth_call",
                        fee_request(h["block_hash"], envs),
                        row["received_monotonic_ns"],
                    )
                    (found,) = abi(("(bool,bytes)[]",), data_bytes(result))
                    sizes = sorted({unsigned_size(env) for env in envs.values()})
                    if len(found) != 2 + len(sizes) or any(not ok for ok, _ in found):
                        raise ValueError("incomplete fixed fee and balance")
                    op, balance, *priced = [abi(("uint256",), x[1])[0] for x in found]
                    l1 = dict(zip(sizes, priced, strict=True))
                    if balance < AMOUNT + CAP * GAS + op + max(priced):
                        raise ValueError("real sender balance is insufficient")
                    fees[h["height"]] = (l1, op, balance, end, proof)
            except Exception:
                stop.set()
                raise

        def simulate_all():
            try:
                for row in iter(sim_queue.get, None):
                    h = row["head"]
                    for key, env in envs.items():
                        result, end, proof = sim(
                            f"{h['height']}-{key}",
                            "eth_simulateV1",
                            sim_request(h["height"], env),
                            row["received_monotonic_ns"],
                        )
                        out = decode_base_simulation(
                            result,
                            parent_number=h["height"],
                            parent_hash=h["block_hash"],
                            recipient=SENDER,
                            gas_limit=GAS,
                        )
                        if out.base_fee_wei_per_gas > CAP:
                            raise ValueError("frozen maximum fee exceeded")
                        payments = None
                        call = result[0]["calls"][0]
                        if out.success:
                            payments = validate_payment(out, call["logs"], key)
                        if key == "minimum_output_control" and (
                            out.success
                            or call.get("error", {}).get("data")
                            != topic("InsufficientOutputAmount()")[:10]
                        ):
                            raise ValueError("minimum output control returned a different result")
                        rows.append(
                            {
                                "route": key,
                                "state": h["height"],
                                "state_hash": h["block_hash"],
                                "head_receive_ns": row["received_monotonic_ns"],
                                "simulation_reply_ns": end,
                                "success": out.success,
                                "paid_wei": AMOUNT if out.success else 0,
                                "received_weth_wei": out.weth_delta,
                                "gas_used": out.gas_used,
                                "l2_wei": out.gas_used * min(CAP, TIP + out.base_fee_wei_per_gas),
                                "payments": payments,
                                "simulation_proof": proof,
                            }
                        )
            except Exception:
                stop.set()
                raise

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = [workers.submit(task) for task in (price_all, simulate_all)]
            try:
                if capture:
                    stream = NewHeadsSession(root / "heads", max(POSITIONS), stop)
                    stream.start()
                    observed = (stream.next_head().to_wire() for _ in range(max(POSITIONS)))
                else:
                    saved = read(root / "heads.json")
                    directory = root / "heads"
                    references = saved["items"]
                    if len(references) != max(POSITIONS) or saved["schedule"] is not None:
                        raise ValueError("invalid FIFO observed head count")
                    expected_files = {
                        f"frame-{i:08d}.raw" for i in range(1, references[-1]["frame"] + 1)
                    }
                    if {p.name for p in directory.glob("frame-*.raw")} != expected_files:
                        raise ValueError("FIFO raw frame coverage is incomplete")
                    verified = [replay_head_observation(directory, r) for r in references]
                    for prior, current in pairwise(verified):
                        if (
                            current.head.height != prior.head.height + 1
                            or current.head.parent_hash != prior.head.block_hash
                        ):
                            raise ValueError("FIFO witness omits a direct parent or child")
                    observed = (item.to_wire() for item in verified)
                    with RpcBudget(
                        directory / "original-budget.sqlite3", POLICIES, read_only=True
                    ) as ledger:
                        ws_counts = ledger.counts()
                    if ws_counts != saved["counts"]:
                        raise ValueError("FIFO websocket reservation counts differ")
                for position, row in enumerate(observed, 1):
                    heads.append(row)
                    if position in POSITIONS:
                        state_queue.put(row)
                        sim_queue.put(row)
                if capture:
                    stream.check()
                    stream.close()
                    with RpcBudget(
                        root / "heads" / "original-budget.sqlite3", POLICIES, read_only=True
                    ) as ledger:
                        ws_counts = ledger.counts()
                    write(
                        root / "heads.json",
                        {
                            "items": heads,
                            "schedule": stream.schedule_sha256,
                            "counts": ws_counts,
                        },
                    )
                elif heads != saved["items"]:
                    raise ValueError("head sequence differs from canonical observations")
            finally:
                state_queue.put(None)
                sim_queue.put(None)
            for job in results:
                job.result()
    except (ValueError, OSError, RuntimeError, KeyError, OasisError, httpx.HTTPError) as error:
        failure = str(error)
    finally:
        if stream is not None:
            stream.close()
        sim.close()
        state.close()
    for row in rows:
        item = fees.get(row["state"])
        if item is None:
            continue
        l1, op, balance, end, proof = item
        l1 = l1[unsigned_size(envs[row["route"]])]
        cost = row["l2_wei"] + l1 + op
        row.update(
            fee_ready_ns=end,
            fee_proof=proof,
            l1_upper_wei=l1,
            operator_upper_wei=op,
            proven_sender_balance_wei=balance,
            network_fee_scenario_wei=cost,
            ready_ns=max(end, row["simulation_reply_ns"]),
            net_wei=row["received_weth_wei"] - row["paid_wei"] - cost,
        )
    with RpcBudget(root / "original.sqlite3", BUDGETS, read_only=True) as ledger:
        counts = ledger.counts()
    return {
        "schema": "oasis-base-prior-head-policy-v1",
        "status": "complete" if not failure else "incomplete",
        "stop_reason": failure,
        "scope_sha256": sha256_hex((root / "scope.json").read_bytes()),
        "scope": scope,
        "rpc_http": counts,
        "rpc_ws": ws_counts if heads is not None else None,
        "origin": identity,
        "heads": heads,
        "rows": rows,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("prepare", "capture", "replay"))
    p.add_argument("root", type=Path)
    args = p.parse_args()
    folder = args.root
    if args.action == "prepare":
        folder.mkdir()
        baseline = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        source = folder / "capture.py"
        durable_new(source, Path(__file__).read_bytes())
        write(folder / "scope.json", plan(int(time.time()) + 600, utc(), baseline, source))
        return
    result = run(folder, args.action == "capture")
    if args.action == "capture":
        write(folder / "report.json", result)
        write(folder / "STOP.json", {"reason": result["stop_reason"], "status": result["status"]})
    elif result != read(folder / "report.json") or read(folder / "STOP.json") != {
        "reason": result["stop_reason"],
        "status": result["status"],
    }:
        raise ValueError("retained Base outcome differs from replay")
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["status"] == "complete" else 2)


if __name__ == "__main__":
    main()
