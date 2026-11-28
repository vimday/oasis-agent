"""Measure Base fixed-principal discovery and whole execution across witnessed H/K/C states."""

import argparse
import json
import time
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from threading import Event

import httpx
import replay_base_mixed_whole as mixed
from run_base_head_first_window import check_origin, read_runtime

from oasis_agent.arbitrage.cycle_protocols import abi, calldata, data_bytes
from oasis_agent.errors import OasisError
from oasis_agent.evidence.base_newheads import POLICIES as WS_BUDGETS
from oasis_agent.evidence.base_newheads import NewHeadsSession, replay_head_observation
from oasis_agent.evidence.rpc_validation import rpc_quantity
from oasis_agent.fastpath.base_mixed_snapshot import _header
from oasis_agent.fastpath.multicall_universe import UNIV3_QUOTER_V2
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from oasis_agent.sniping.venues import SLIPSTREAM
from scripts.measure_robinhood_quote_delivery import _body, _fetch, _read

CHAIN = mixed.CHAIN
AMOUNT = mixed.AMOUNTS[mixed.WETH]
ANCHORS = (0, 12, 24, 36, 48)
FRAMES = 62
SOURCES = {
    "sim1": mixed.ENDPOINTS["official"],
    "sim2": "https://base.drpc.org",
    "state": mixed.ENDPOINTS["state"],
}
BUDGETS = tuple(
    BudgetPolicy(key, limit, 250) for key, limit in (("sim1", 18), ("sim2", 16), ("state", 29))
)
FEE_STATE = "same hash, ETH/WETH at par, L1 and operator upper bounds"
BASE_FEE_VAULT = "0x4200000000000000000000000000000000000011"
GAS_REPORT = "docs/verification/BASE_SIMULATOR_PAIR_20261128.json"
QUOTERS = (
    (UNIV3_QUOTER_V2, mixed.WETH, mixed.USDC, "uint24"),
    (SLIPSTREAM[mixed.AERO_FACTORY][0], mixed.USDC, mixed.WETH, "int24"),
)


class Lane:
    def __init__(self, root, source, stop, client):
        self.root, self.source, self.stop, self.client = root, source, stop, client
        self.i = 0

    def rpc(self, label, method, params, earliest=1):
        try:
            if self.stop.is_set():
                raise ValueError("the frozen window stopped")
            self.i += 1
            path = self.root / f"{self.source}-{self.i:02d}-{label}"
            body = _body(self.i, method, params)
            if self.client is not None:
                with RpcBudget(
                    self.root / "original.sqlite3", BUDGETS, require_existing=True
                ) as budget:
                    if delay := budget.reserve(self.source, now_ms=time.time_ns() // 1_000_000):
                        time.sleep((delay + 1) / 1000)
                        if self.stop.is_set():
                            raise ValueError("the frozen window stopped")
                        if budget.reserve(self.source, now_ms=time.time_ns() // 1_000_000):
                            raise ValueError("source pacing did not advance")
                _fetch(self.client, SOURCES[self.source], path, body)
            parsed, _ = _read(path, body, self.i, earliest)
            return parsed, mixed.read(path.with_suffix(".meta"))
        except OSError, ValueError, httpx.HTTPError:
            self.stop.set()
            raise


def plan(root, utc):
    baseline = mixed.read(Path(GAS_REPORT))
    gas = max(row["lanes"]["official"]["whole"]["gas_used"] for row in baseline["rows"])
    runtime_hash = sha256_hex(data_bytes(read_runtime(root)))
    assert runtime_hash == mixed.read(mixed.DIR / "scope.json")["runtime_sha256"]
    return {
        "schema": "oasis.base-fixed-hkc.v1",
        "frozen_utc": utc,
        "chain_id": CHAIN,
        "scope_usd": 0,
        "driver_sha256": mixed.digest(Path(__file__)),
        "mixed_source_sha256": mixed.digest(Path(mixed.__file__)),
        "sources": SOURCES,
        "route": mixed.envelope(mixed.WETH),
        "runtime_sha256": runtime_hash,
        "anchors_zero_based": list(ANCHORS),
        "heads": FRAMES,
        "quote_gas_units": gas,
        "quote_gas_evidence_sha256": mixed.digest(Path(GAS_REPORT)),
        "budgets": {item.source: item.max_requests for item in BUDGETS},
        "fee_state": FEE_STATE,
        "quoter_schedule": [list(leg) for leg in QUOTERS],
        "discovery": "two dependent fixed-H Quoter calls and same-H fee state",
        "return_head": "latest witnessed head at locally validated dual whole return",
        "direct_child": "verified number H+1 and parent hash H",
        "simulated_context": "Denim next whole second, Base sequencer fee vault and child base fee",
    }


def market(root, block, wire):
    # Reopen the streamed leaf containing fee and Denim time. The head reader verifies its hash.
    raw = mixed.read(root / "heads" / f"frame-{wire.frame:08d}.raw")["params"]["result"]
    parsed = _header(canonical_json({"jsonrpc": "2.0", "id": 1, "result": raw}))
    assert (parsed.number, parsed.block_hash, parsed.parent_hash) == (
        block.height,
        block.block_hash,
        block.parent_hash,
    )
    return parsed


def fees(lane, header, earliest):
    options, parsed = mixed.state_params({"blockHash": header.block_hash, "requireCanonical": True})
    raw, proof = lane.rpc(f"{header.number}-fee", "eth_call", options, earliest)
    return mixed.bound_state(raw, parsed), proof


def quote(lane, header, earliest):
    pin = {"blockHash": header.block_hash, "requireCanonical": True}
    amount = AMOUNT
    outputs, proofs = [], []
    for i, (quoter, token_in, token_out, abi_kind) in enumerate(QUOTERS, 1):
        tup = f"(address,address,uint256,{abi_kind},uint160)"
        data = calldata(
            f"quoteExactInputSingle({tup})", (tup,), ((token_in, token_out, amount, 100, 0),)
        )
        raw, proof = lane.rpc(
            f"{header.number}-q{i}", "eth_call", [{"to": quoter, "data": data}, pin], earliest
        )
        amount, *_ = abi(("uint256", "uint160", "uint32", "uint256"), data_bytes(raw))
        assert amount > 0, "zero fixed-principal output"
        outputs.append(amount)
        proofs.append(proof)
    return outputs, proofs


def whole(executor, lanes, header, charges, runtime, earliest, estimated_outputs=None):
    params = mixed.simulation(header.number, runtime, mixed.WETH)
    pending = {
        key: executor.submit(
            lanes[key].rpc, f"{header.number}-whole", "eth_simulateV1", params, earliest
        )
        for key in ("sim1", "sim2")
    }
    results = {}
    for key, f in pending.items():
        raw, proof = f.result()
        [simulated] = raw
        seconds = rpc_quantity(simulated["timestamp"])
        if seconds != (header.timestamp_ms + 200) // 1000 or simulated["miner"] != BASE_FEE_VAULT:
            raise ValueError(f"whole-transaction child context differs at {header.number}")
        fields = mixed.settlement(raw, header, charges, mixed.WETH, estimated_outputs)
        results[key] = fields | {"simulated_timestamp": seconds}, proof
    if results["sim1"][0] != results["sim2"][0]:
        raise ValueError(f"whole-transaction sources disagree at {header.number}")
    return results["sim1"][0] | {"proof": {k: proof for k, (_, proof) in results.items()}}


def live_stage(root, scope, observation, executor, lanes, runtime, clock=None):
    start = observation.received_monotonic_ns
    header = market(root, observation.head, observation)
    outputs, quote_proofs = quote(lanes["state"], header, start)
    cost, fee_proof = fees(lanes["state"], header, start)
    discovery_ns = time.monotonic_ns() if clock is None else clock["discovery_ns"]
    assert max(fee_proof["received_ns"], quote_proofs[-1]["received_ns"]) <= discovery_ns
    l2 = scope["quote_gas_units"] * min(mixed.CAP, header.base_fee + mixed.TIP)
    fee = l2 + cost["l1_upper_wei"][mixed.WETH] + cost["operator_upper_wei"]
    result = whole(executor, lanes, header, cost, runtime, discovery_ns, estimated_outputs=outputs)
    return_ns = time.monotonic_ns() if clock is None else clock["return_ns"]
    assert max(proof["received_ns"] for proof in result["proof"].values()) <= return_ns
    return {
        "h": header.number,
        "principal_wei": AMOUNT,
        "discovery_ns": discovery_ns,
        "return_ns": return_ns,
        "discovery": {
            "funding_outputs_raw": outputs,
            "network_cost_estimate_wei": fee,
            "net_wei": outputs[-1] - AMOUNT - fee,
            "quotes": quote_proofs,
            "fee": fee_proof,
        },
        "whole_on_h": result,
    }


def select_window(heads, rows):
    arrival = [item.received_monotonic_ns for item in heads]
    indexes = {item.head.height: i for i, item in enumerate(heads)}
    result = {}
    for sample in rows:
        i = indexes[sample["h"]]
        d = bisect_right(arrival, sample["discovery_ns"]) - 1
        k = bisect_right(arrival, sample["return_ns"]) - 1
        if not i <= d <= k < len(heads) - 1:
            raise ValueError("receive clock has no later witness")
        result[sample["h"]] = (heads[i], heads[d], heads[k], heads[i + 1])
    return result


def pipeline(root, plan, lanes, stop, capture):
    identity = check_origin(lanes["sim1"], lanes["state"])
    chain, _ = lanes["sim2"].rpc("chain", "eth_chainId", [])
    assert chain == hex(CHAIN)
    runtime = read_runtime(root)
    with ThreadPoolExecutor(max_workers=2) as executor:
        if capture:
            stream = NewHeadsSession(root / "heads", FRAMES, stop)
            heads, live = [], []
            with ThreadPoolExecutor(max_workers=1) as quotes:
                stream.start()
                try:
                    for i in range(FRAMES):
                        item = stream.next_head()
                        heads.append(item)
                        if i in ANCHORS:
                            live.append(
                                quotes.submit(
                                    live_stage, root, plan, item, executor, lanes, runtime
                                )
                            )
                    stream.check()
                finally:
                    stream.close()
                    mixed.write(root / "heads.json", [obs.to_wire() for obs in heads])
                live_rows = [task.result() for task in live]
                mixed.write(
                    root / "clock.json",
                    [{key: row[key] for key in ("discovery_ns", "return_ns")} for row in live_rows],
                )
        else:
            heads = [
                replay_head_observation(root / "heads", item)
                for item in mixed.read(root / "heads.json")
            ]
            clocks = mixed.read(root / "clock.json")
            live_rows = [
                live_stage(root, plan, heads[i], executor, lanes, runtime, clock)
                for i, clock in zip(ANCHORS, clocks, strict=True)
            ]
        assert len(heads) == FRAMES
        for left, right in pairwise(heads):
            assert right.head.height == left.head.height + 1
            assert right.head.parent_hash == left.head.block_hash
            assert left.received_monotonic_ns < right.received_monotonic_ns
        window = select_window(heads, live_rows)
        settled = {row["h"]: row["whole_on_h"] for row in live_rows}
        for h, _d, k, c in window.values():
            for point in (k, c):
                if point.head.height in settled:
                    continue
                header = market(root, point.head, point)
                after = max(h.received_monotonic_ns, point.received_monotonic_ns)
                cost, proof = fees(lanes["state"], header, after)
                result = whole(executor, lanes, header, cost, runtime, after)
                settled[point.head.height] = result | {"fee": proof}
        for row in live_rows:
            h, d, k, c = window[row["h"]]
            row.update(
                {
                    "discovery_latest_seen": d.head.height,
                    "child_seen_ns": c.received_monotonic_ns,
                    "h_seen_ns": h.received_monotonic_ns,
                    "hash": h.head.block_hash,
                    "whole_return_before_child": row["return_ns"] < c.received_monotonic_ns,
                    "k": k.head.height,
                    "k_hash": k.head.block_hash,
                    "whole_on_k": settled[k.head.height],
                    "c": c.head.height,
                    "c_hash": c.head.block_hash,
                    "whole_on_c": settled[c.head.height],
                }
            )
        # A witnessed direct child independently derives the protocol's next base fee.
        head_by_height = {obs.head.height: obs for obs in heads}
        for height, receipt in settled.items():
            successor = head_by_height[height + 1]
            child = market(root, successor.head, successor)
            if (
                receipt["simulated_base_fee"] != child.base_fee
                or receipt["simulated_timestamp"] != child.timestamp_ms // 1000
            ):
                raise ValueError(f"simulated fee or time conflicts with child at {height}")
        return identity, live_rows


def run(root, active, capture):
    stop = Event()
    if capture:
        with RpcBudget(root / "original.sqlite3", BUDGETS):
            pass
    try:
        with ExitStack() as stack:
            lanes = {
                source: Lane(
                    root,
                    source,
                    stop,
                    stack.enter_context(
                        httpx.Client(timeout=5, follow_redirects=False, trust_env=False)
                    )
                    if capture
                    else None,
                )
                for source in SOURCES
            }
            identity, rows = pipeline(root, active, lanes, stop, capture)
    except (AssertionError, KeyError, ValueError, OSError, OasisError, httpx.HTTPError) as exc:
        if capture:
            mixed.write(root / "STOP.json", {"fault": str(exc) or "experiment assertion failed"})
        raise
    with RpcBudget(root / "original.sqlite3", BUDGETS, read_only=True) as ledger:
        counts = ledger.counts()
    assert counts == {key: lane.i for key, lane in lanes.items()}
    with RpcBudget(root / "heads/original-budget.sqlite3", WS_BUDGETS, read_only=True) as ledger:
        ws = ledger.counts()
    if capture:
        mixed.write(root / "STOP.json", {"fault": None})
    assert mixed.read(root / "STOP.json") == {"fault": None}
    return {
        "scope_sha256": mixed.digest(root / "scope.json"),
        "rpc_reservations": counts | ws,
        "origin": identity,
        "first_and_last_heads": [mixed.read(root / "heads.json")[i]["head"] for i in (0, -1)],
        "rows": rows,
        "actual_spend_usd": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "capture", "replay"))
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root
    if args.action == "prepare":
        mixed.write(root / "scope.json", plan(root, datetime.now(UTC).isoformat()))
        return
    active = mixed.read(root / "scope.json")
    assert active == plan(root, active["frozen_utc"])
    print(json.dumps(run(root, active, args.action == "capture"), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
