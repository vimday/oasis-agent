"""Measure a bounded new Base window with exact-head priority and separate diagnostics."""

import argparse
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import httpx
import replay_base_mixed_whole as mixed
from eth_hash.auto import keccak
from run_base_prior_head_policy import read, utc, write

from oasis_agent.arbitrage.cycle_protocols import abi, calldata, data_bytes, topic
from oasis_agent.errors import OasisError
from oasis_agent.evidence.base_newheads import NewHeadsSession, replay_head_observation
from oasis_agent.evidence.rpc_validation import rpc_quantity
from oasis_agent.fastpath.base_mixed_snapshot import _header
from oasis_agent.fastpath.base_policy import (
    BasePolicyCase,
    BasePolicySimulation,
    BasePolicyWindow,
    PreviousQuote,
)
from oasis_agent.fastpath.base_realtime import BaseHeadQueue, BasePriorCache
from oasis_agent.fastpath.batch_shadow import BatchShadowHead
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts.capture_public_rpc import durable_new
from scripts.measure_robinhood_quote_delivery import _body, _fetch, _read

COUNT = 16
CONTROL_INDEX = COUNT // 2
ROUTE = "mixed_weth"
CONTROL = "raised_weth_floor"
AMOUNT = mixed.AMOUNTS[mixed.WETH]
SOURCES = {"simulator": mixed.ENDPOINTS["official"], "state": mixed.ENDPOINTS["state"]}
RUNTIME_NAME = "BaseMixedWholeProbe.bin-runtime"
PRIOR_CAPTURE = mixed.DIR / RUNTIME_NAME
BUDGETS = (BudgetPolicy("simulator", 4 + COUNT, 250), BudgetPolicy("state", 4 + COUNT, 250))
CAPTURE_SOURCE = Path(__file__).resolve()
MODULES = (
    "src/oasis_agent/fastpath/base_realtime.py",
    "src/oasis_agent/fastpath/base_policy.py",
    "src/oasis_agent/evidence/base_newheads.py",
    "docs/verification/fixtures/run_base_prior_head_policy.py",
    "docs/verification/fixtures/replay_base_mixed_whole.py",
    "docs/verification/fixtures/BaseMixedWholeProbe.sol",
    "scripts/measure_robinhood_quote_delivery.py",
)


def plan(deadline, frozen, baseline, source):
    return {
        "schema": "oasis.base-head-first-window.v1",
        "chain_id": 8453,
        "frozen_utc": frozen,
        "baseline_git": baseline,
        "deadline": deadline,
        "heads": COUNT,
        "control_index": CONTROL_INDEX,
        "route": mixed.envelope(mixed.WETH),
        "control": mixed.envelope(mixed.WETH)
        | {
            "data": calldata(
                "probe(bool,uint256,uint256)",
                ("bool", "uint256", "uint256"),
                (False, AMOUNT, 2 * AMOUNT),
            )
        },
        "runtime_sha256": sha256_hex(data_bytes(read_runtime(source.parent))),
        "shadow_override": mixed.HARNESS,
        "historical_scope_sha256": sha256_hex((mixed.DIR / "scope.json").read_bytes()),
        "sources": SOURCES,
        "budgets": {p.source: p.max_requests for p in BUDGETS},
        "minimum_interval_ms_per_http_source": 250,
        "price": "simulated gas times gas price, L1 and operator fee bounds",
        "primary_metric": "H protocol result and fee evidence both ready before direct child",
        "baseline": "exact P same-envelope result cached at the first observed H timestamp",
        "diagnostics": "after realtime worker join, price missing states plus fixed control",
        "acceptance": "contiguous canonical blocks and exact payments; refuse conflicts",
        "profit_threshold_raw": 1,
        "scope_usd": 0,
        "signatures": 0,
        "broadcasts": 0,
        "source_sha256": sha256_hex(source.read_bytes()),
        "modules": {name: sha256_hex(Path(name).read_bytes()) for name in MODULES},
    }


class Lane:
    def __init__(self, root, key, stopped, capture):
        self.root, self.key, self.stopped, self.capture = root, key, stopped, capture
        self.sequence = 0
        self.client = (
            httpx.Client(timeout=5, follow_redirects=False, trust_env=False) if capture else None
        )

    def rpc(self, label, method, params, earliest=1):
        if self.stopped.is_set():
            raise ValueError("the frozen scope stopped")
        self.sequence += 1
        prefix = self.root / f"{self.key}-{self.sequence:02d}-{label}"
        body = _body(self.sequence, method, params)
        if self.capture:
            with RpcBudget(
                self.root / "original.sqlite3", BUDGETS, require_existing=True
            ) as budget:
                delay = budget.reserve(self.key, now_ms=time.time_ns() // 1_000_000)
                if delay:
                    time.sleep((delay + 1) / 1000)
                    if self.stopped.is_set():
                        raise ValueError("the scope stopped during source pacing")
                    if budget.reserve(self.key, now_ms=time.time_ns() // 1_000_000):
                        raise ValueError("source pacing deadline did not advance")
            _fetch(self.client, SOURCES[self.key], prefix, body)
        value, _ = _read(prefix, body, self.sequence, earliest)
        return value, read(prefix.with_suffix(".meta"))

    def close(self):
        if self.client is not None:
            self.client.close()


def observed_head(row):
    return BatchShadowHead(
        height=row["head"]["height"],
        block_hash=row["head"]["block_hash"],
        parent_hash=row["head"]["parent_hash"],
        observed_ns=row["received_monotonic_ns"],
    )


def read_runtime(root):
    return "0x" + (root / RUNTIME_NAME).read_text().strip()


def check_origin(simulator, fees):
    for lane in (simulator, fees):
        if rpc_quantity(lane.rpc("chain", "eth_chainId", [])[0]) != mixed.CHAIN:
            raise ValueError("anonymous endpoint is on a different chain")
    header, header_proof = simulator.rpc(
        "runtime-header", "eth_getBlockByNumber", ["latest", False]
    )
    block = _header(canonical_json({"jsonrpc": "2.0", "id": 1, "result": header}))
    pin = {"blockHash": block.block_hash, "requireCanonical": True}
    if rpc_quantity(simulator.rpc("nonce", "eth_getTransactionCount", [mixed.ACTOR, pin])[0]) != 0:
        raise ValueError("public actor nonce changed")
    identity = {"block_hash": block.block_hash, "header": header_proof}
    preflight = read(mixed.DIR / "stdout.json")
    historical = preflight["canonical_runtime_hashes"]
    for label, address in (("harness", mixed.HARNESS), ("uni", mixed.UNI), ("aero", mixed.AERO)):
        runtime, proof = fees.rpc(label, "eth_getCode", [address, pin])
        raw = data_bytes(runtime)
        if address == mixed.HARNESS:
            if raw:
                raise ValueError("shadow harness already has deployed code")
        elif ("0x" + keccak(raw).hex()) != historical[address]:
            raise ValueError("pool source changed since historical settlement")
        identity[label] = {"proof": proof, "code_sha256": sha256_hex(raw)}
    return identity


def get_fee(lane, h):
    params, returns = mixed.state_params({"blockHash": h.block_hash, "requireCanonical": True})
    raw, proof = lane.rpc(str(h.height), "eth_call", params, h.observed_ns)
    return mixed.bound_state(raw, returns) | {"proof": proof}


def get_raw_simulation(lane, h, e, key, runtime):
    numbered = mixed.simulation(h.height, runtime, mixed.WETH)
    numbered[0]["blockStateCalls"][0]["calls"] = [e]
    return lane.rpc(f"{h.height}-{key}", "eth_simulateV1", numbered, h.observed_ns)


def control_fields(raw, status):
    call = raw[0]["calls"][0]
    body = data_bytes(call.get("error", {}).get("data"))
    returned = data_bytes(call["returnData"])
    selector = topic("MinimumOutput(uint256,uint256)")[2:10]
    if status != "reverted" or body[:4].hex() != selector or returned not in (b"", body):
        raise ValueError("unexpected negative-control outcome")
    got, floor = abi(("uint256", "uint256"), body[4:])
    if floor != 2 * AMOUNT or got >= floor:
        raise ValueError("invalid minimum output error")
    return {"revert_selector": selector, "control_guard_actual": got, "control_guard_floor": floor}


def process(scope, held, heads, sim, fee, pool, capture, ready_ns=None):
    key = held["kind"]
    e = scope["control" if key == CONTROL else "route"]
    h = heads[held["frame"]]
    request = pool.submit(get_raw_simulation, sim, h, e, key, read_runtime(sim.root))
    fee_request = pool.submit(get_fee, fee, h) if key != CONTROL else None
    raw, proof = request.result()
    if fee_request is not None:
        fees = fee_request.result()
    else:
        if mixed.unsigned_size(scope["route"]) != mixed.unsigned_size(e):
            raise ValueError("negative control needs an independently sized L1 bound")
        fees = held["route_fee"]
    pin = SimpleNamespace(number=h.height, block_hash=h.block_hash)
    settlement = mixed.settlement(raw, pin, fees, mixed.WETH)
    if key == CONTROL:
        settlement.update(control_fields(raw, settlement["status"]))
    ready = time.monotonic_ns() if capture else ready_ns
    if max(proof["received_ns"], fees["proof"]["received_ns"]) > ready:
        raise ValueError("decision timestamp precedes complete evidence")
    simulation = BasePolicySimulation(
        head=h,
        envelope_sha256=sha256_hex(canonical_json(e)),
        available_ns=ready,
        disposition=settlement["status"],
        settlement_delta_wei=settlement["settlement_delta_raw"],
        modeled_network_fee_wei=settlement["chain_fee_scenario_wei"],
    )
    row = {
        "head": h.height,
        "simulation": settlement | {"proof": proof},
        "fee": fees,
        "observation": simulation.model_dump(mode="json"),
        "network_fee_scenario_net_wei": simulation.modeled_net_wei,
    }
    return row, simulation


def load_observations(root, manifest):
    references = manifest["heads"]
    if len(references) != COUNT:
        raise ValueError("replay needs the frozen number of heads")
    frames = {p.name for p in (root / "heads").glob("frame-*.raw")}
    if frames != {f"frame-{i:08d}.raw" for i in range(1, references[-1]["frame"] + 1)}:
        raise ValueError("raw head frame coverage changed")
    verified = [replay_head_observation(root / "heads", row).to_wire() for row in references]
    if verified != references:
        raise ValueError("raw head lineage changed")
    return [observed_head(item) for item in verified]


def simulate_manifest(scope, roots, clockwork, heads, sim, fee):
    done = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        for retained in clockwork["work"]:
            event = retained
            i = event["frame"]
            if event["kind"] == CONTROL:
                event = dict(event, route_fee=done[i]["fee"])
            row, _ = process(scope, event, heads, sim, fee, pool, False, event["ready_ns"])
            if event["kind"] == CONTROL:
                control = row
            else:
                done[i] = row
    return complete_report(scope, roots, clockwork, heads, done, control)


def complete_report(scope, roots, clockwork, heads, rows, control):
    cutoff = clockwork["diagnostics_open_ns"]
    live_work = {e["frame"]: e for e in clockwork["work"] if e["lane"] == "live"}
    seen = set()
    passed = cutoff
    for event in clockwork["work"]:
        clock = event["ready_ns"]
        lane = event["lane"]
        is_control = event["kind"] == CONTROL
        row = control if is_control else rows[event["frame"]]
        sent = row["simulation"]["proof"]["sent_ns"]
        if lane == "live":
            if not sent <= clock <= event["published_ns"] <= cutoff or is_control:
                raise ValueError("a realtime slot has diagnostic work")
        elif not passed <= sent <= clock:
            raise ValueError("diagnostic work preceded realtime completion")
        if lane == "diagnostic":
            passed = clock
        witness = (event["frame"], event["kind"])
        if witness in seen:
            raise ValueError("duplicate measured work")
        seen.add(witness)
    if set(rows) != set(range(COUNT)) or control["head"] != heads[CONTROL_INDEX].height:
        raise ValueError("incomplete simulation set")
    digest = sha256_hex(canonical_json(scope["route"]))
    cases = []
    h_rows = []
    for i, (head, child) in enumerate(pairwise(heads)):
        previous = rows[i - 1] if i > 0 and i - 1 in live_work else None
        prior = None
        cache_hit = clockwork["cache_hits"][i]
        if previous is not None:
            evidence = BasePolicySimulation.model_validate(previous["observation"])
            publication = live_work[i - 1]["published_ns"]
            if cache_hit is not None:
                if cache_hit != evidence.head.height or evidence.available_ns > head.observed_ns:
                    raise ValueError("cached result did not belong to the exact available parent")
                prior = PreviousQuote(
                    head=evidence.head,
                    envelope_sha256=digest,
                    available_ns=evidence.available_ns,
                    meets_minimum_output=evidence.disposition == "settled",
                    modeled_net_wei=evidence.modeled_net_wei,
                )
            elif publication < head.observed_ns:
                raise ValueError("known available direct parent was omitted by the cache")
        elif cache_hit is not None:
            raise ValueError("cache hit has no realtime parent evidence")
        h = BasePolicySimulation.model_validate(rows[i]["observation"])
        c = BasePolicySimulation.model_validate(rows[i + 1]["observation"])
        cases.append(
            BasePolicyCase(
                kind="fixed_route",
                envelope_sha256=digest,
                principal_wei=AMOUNT,
                head=head,
                child=child,
                previous_quote=prior,
                head_simulation=h,
                child_simulation=c,
            )
        )
        proof = rows[i]["simulation"]["proof"]
        h_rows.append(
            {
                "head": head.height,
                "diagnostic_only": i not in live_work,
                "prior_ready_at_h": prior is not None,
                "request_sent_before_child": proof["sent_ns"] < child.observed_ns,
                "reply_before_child": h.available_ns < child.observed_ns,
                "child_ms_since_h": (child.observed_ns - head.observed_ns) / 1e6,
                "sent_ms_since_h": (proof["sent_ns"] - head.observed_ns) / 1e6,
                "network_ms": (proof["received_ns"] - proof["sent_ns"]) / 1e6,
                "ready_ms_since_h": (h.available_ns - head.observed_ns) / 1e6,
                "h_net_wei": h.modeled_net_wei,
                "c_net_wei": c.modeled_net_wei,
            }
        )
    return {
        "schema": "oasis.base-realtime-scheduler-holdout.v1",
        "scope": scope,
        "scope_sha256": sha256_hex(canonical_json(scope)),
        "origin": roots,
        "clockwork": clockwork,
        "head_rows": h_rows,
        "all_evidence": [rows[i] for i in range(COUNT)],
        "control": control,
        "policy": BasePolicyWindow(
            schema_version="oasis.base-shadow-policy-input.v1", cases=tuple(cases)
        ).report(),
    }


def capture(scope, root, origin, sim, fee, stopped):
    heads = []
    references = []
    online = {}
    work = []
    discarded = []
    cache_hits = []
    errors = []
    queue = BaseHeadQueue[int]()
    cache = BasePriorCache()
    digest = sha256_hex(canonical_json(scope["route"]))
    stream = NewHeadsSession(root / "heads", COUNT, stopped)

    def realtime():
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                while pending := queue.take():
                    h, i = pending
                    if stopped.is_set():
                        return
                    if heads[-1].height != h.height:
                        discarded.append(i)
                        continue
                    event = {"frame": i, "kind": ROUTE, "lane": "live"}
                    row, audit = process(scope, event, heads, sim, fee, pool, True)
                    cache.record(audit)
                    published = time.monotonic_ns()
                    online[i] = row
                    work.append(dict(event, ready_ns=audit.available_ns, published_ns=published))
        except (
            OSError,
            ValueError,
            TypeError,
            RuntimeError,
            KeyError,
            OasisError,
            httpx.HTTPError,
        ) as exc:
            errors.append(exc)
            stopped.set()

    worker = Thread(target=realtime)
    worker.start()
    try:
        stream.start()
        for i in range(COUNT):
            ref = stream.next_head().to_wire()
            h = observed_head(ref)
            references.append(ref)
            heads.append(h)
            cached = cache.lookup(h, digest, as_of_ns=h.observed_ns)
            cache_hits.append(cached.head.height if cached is not None else None)
            if i + 1 < COUNT and (superseded := queue.offer(h, i)) is not None:
                discarded.append(superseded[1])
        stream.check()
    finally:
        if (dropped := queue.close(discard_pending=True)) is not None:
            discarded.append(dropped[1])
        worker.join(timeout=20)
        stream.close()
        if worker.is_alive():
            raise TimeoutError("realtime worker did not stop")
        if errors:
            raise errors[0]
    diagnostics_open_ns = time.monotonic_ns()
    clockwork = {
        "heads": references,
        "work": work,
        "discarded": sorted(discarded),
        "cache_hits": cache_hits,
        "diagnostics_open_ns": diagnostics_open_ns,
        "ws_reservations": stream.counts(),
    }
    write(root / "realtime.json", clockwork)
    with ThreadPoolExecutor(max_workers=2) as pool:
        for i in range(COUNT):
            if i in online:
                continue
            event = {"frame": i, "kind": ROUTE, "lane": "diagnostic"}
            row, audit = process(scope, event, heads, sim, fee, pool, True)
            online[i] = row
            work.append(dict(event, ready_ns=audit.available_ns))
        event = {"frame": CONTROL_INDEX, "kind": CONTROL, "lane": "diagnostic"}
        control, audit = process(
            scope, dict(event, route_fee=online[CONTROL_INDEX]["fee"]), heads, sim, fee, pool, True
        )
        work.append(dict(event, ready_ns=audit.available_ns))
    write(root / "clockwork.json", clockwork)
    return complete_report(scope, origin, clockwork, heads, online, control)


def run(root, action):
    source = root / "capture.py"
    scope = read(root / "scope.json")
    if scope != plan(scope["deadline"], scope["frozen_utc"], scope["baseline_git"], source):
        raise ValueError("frozen scope, source or dependencies changed")
    online = action == "capture"
    if online and scope["deadline"] <= int(time.time()):
        raise ValueError("frozen capture validity expired")
    if online and source.read_bytes() != CAPTURE_SOURCE.read_bytes():
        raise ValueError("active driver differs from frozen scope")
    stopped = Event()
    sim = Lane(root, "simulator", stopped, online)
    fee = Lane(root, "state", stopped, online)
    try:
        with RpcBudget(root / "original.sqlite3", BUDGETS, read_only=not online):
            pass
        origin = check_origin(sim, fee)
        if online:
            result = capture(scope, root, origin, sim, fee, stopped)
        else:
            clockwork = read(root / "clockwork.json")
            result = simulate_manifest(
                scope, origin, clockwork, load_observations(root, clockwork), sim, fee
            )
        with RpcBudget(root / "original.sqlite3", BUDGETS, read_only=True) as ledger:
            counts = ledger.counts()
        if counts != {lane.key: lane.sequence for lane in (sim, fee)}:
            raise ValueError("measured RPC coverage differs from original source ledger")
        result["rpc"] = dict(counts, **result["clockwork"]["ws_reservations"])
        return result
    finally:
        sim.close()
        fee.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("prepare", "capture", "replay"))
    p.add_argument("root", type=Path)
    args = p.parse_args()
    root = args.root
    if args.action == "prepare":
        root.mkdir()
        source = root / "capture.py"
        durable_new(source, CAPTURE_SOURCE.read_bytes())
        durable_new(root / RUNTIME_NAME, PRIOR_CAPTURE.read_bytes())
        baseline = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        scope = plan(int(time.time()) + 600, utc(), baseline, source)
        write(root / "scope.json", scope)
        print(sha256_hex(canonical_json(scope)))
        return
    if args.action == "capture":
        try:
            result = run(root, args.action)
        except Exception as exc:
            write(root / "STOP.json", {"status": "incomplete", "reason": str(exc)})
            raise
        write(root / "report.json", result)
        write(root / "STOP.json", {"status": "complete", "reason": None})
    else:
        result = run(root, args.action)
        if result != read(root / "report.json"):
            raise ValueError("captured and replayed results differ")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
