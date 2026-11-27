"""Replay twelve independently rooted historical Ethereum blocks without network I/O."""

import argparse
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from replay_eth_hundred_census import receipts_proof, strict_paths
from replay_eth_hundred_holdout import envelope_cost, source_bytes

from oasis_agent.evidence.public_receipts import decode_receipt, quantity
from oasis_agent.evidence.research_traces import first_failure, nested_calls
from oasis_agent.evidence.swap_flow import profile_swap_receipt
from oasis_agent.evidence.swap_flow_trace import _receipt_positions, replay_trace_funding
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts.replay_eth_tail_case import _settlement

HEIGHT = 26_463_200
CAPS = {"header": 12, "receipts": 12, "trace": 1}
POLICIES = tuple(BudgetPolicy(lane, cap, 0) for lane, cap in CAPS.items())
SOURCES = {
    "header": "https://ethereum.publicnode.com",
    "receipts": "https://eth.nodely.dev",
    "trace": "https://eth.drpc.org",
}
DAY = "2026-11-21"


def make_scope(root):
    declined = root.parent / "eth-unseen-date-20261127/STOP.json"
    return {
        "schema": "eth-nov21-contiguous-closure-control-v1",
        "usd": 0,
        "chain_id": 1,
        "day_utc": DAY,
        "first_height": HEIGHT,
        "last_height": HEIGHT + CAPS["header"] - 1,
        "max_rpc": sum(CAPS.values()),
        "caps": CAPS,
        "sources": SOURCES,
        "retry": False,
        "fixed_qualification": "at least five eight-signature events and not previously audited",
        "score": "longest unique equal-amount distinct-pool chain; ties by earliest position",
        "control": "strict full-amount shortest/longest closed components in same selected receipt",
        "input_sha256": {
            name: sha256_hex((root / name).read_bytes()) for name in ("novelty.json", "topics.json")
        },
        "failed_prev_object_sha256": sha256_hex(declined.read_bytes()),
        "code_sha256": sha256_hex((root / "measure_eth_nov21_window.py").read_bytes()),
    }


def scope_io(root):
    def get(lane, label, method, params):
        raw, request, source = source_bytes(root, f"{label}-{lane}")
        assert source["source"] == SOURCES[lane]
        want = json.loads(request)
        assert want["method"] == method and want["params"] == params
        return json.loads(raw)["result"], source

    return get


def amount(k, raw):
    if not raw or len(raw) % 64:
        return None
    words = [int(raw[i : i + 64], 16) for i in range(0, len(raw), 64)]
    if (
        k == "v2"
        and len(words) == 4
        and bool(words[0]) != bool(words[1])
        and bool(words[2]) != bool(words[3])
    ):
        x, y = sum(words[:2]), sum(words[2:])
    elif k in {"v3", "pancake_v3_shape", "v4"} and len(words) >= 2:
        a, b = [i - 2**256 if i >= 2**255 else i for i in words[:2]]
        if a * b >= 0:
            return None
        x, y = (max(a, b), -min(a, b)) if k != "v4" else (-min(a, b), max(a, b))
    elif k in {"balancer_v2", "bancor_conversion"} and len(words) >= 2:
        x, y = words[:2]
    elif k in {"curve", "curve2"} and len(words) >= 4:
        x, y = words[1], words[3]
    else:
        return None
    if min(x, y) > 0:
        return x, y
    return None


def rank(logs, topics):
    inputs, outputs = defaultdict(list), defaultdict(list)
    pools = []
    pairs = []
    for i, log in enumerate(logs):
        kind = topics[log["topics"][0]]
        pools.append(
            (
                log["address"],
                log["topics"][1]
                if kind in {"v4", "balancer_v2"} and len(log["topics"]) > 1
                else None,
            )
        )
        pair = amount(kind, log["data"][2:])
        pairs.append(pair)
        if pair:
            inputs[pair[0]].append(i)
            outputs[pair[1]].append(i)
    links = {
        outs[0]: inputs[key][0]
        for key, outs in outputs.items()
        if len(outs) == len(inputs[key]) == 1 and outs[0] != inputs[key][0]
    }
    best = []
    for start, pair in enumerate(pairs):
        if not pair:
            continue
        path = []
        seen = set()
        i = start
        while i is not None and pools[i] not in seen:
            seen.add(pools[i])
            path.append(i)
            i = links.get(i)
        if len(path) > len(best):
            best = path
    return len(set(pools)), [quantity(logs[i]["logIndex"]) for i in best]


def evaluate(root, get):
    topics = json.loads((root / "topics.json").read_bytes())
    exclusions = set(json.loads((root / "novelty.json").read_bytes())["exclude"])
    hits = Counter()
    blocks = []
    candidates = []
    registry = {}
    previous = None
    for height in range(HEIGHT, HEIGHT + CAPS["header"]):
        header, head_source = get(
            "header", str(height), "eth_getBlockByNumber", [hex(height), True]
        )
        stamp = datetime.fromtimestamp(quantity(header["timestamp"]), UTC)
        if stamp.date().isoformat() != DAY or previous not in (None, header["parentHash"]):
            raise ValueError("block timestamp or continuity mismatch")
        block_hash = header["hash"]
        receipts, receipt_source = get(
            "receipts", str(height), "eth_getBlockReceipts", [block_hash]
        )
        block, root_hash = receipts_proof(header, receipts, height, block_hash)
        previous = block_hash
        blocks.append(
            {
                "height": height,
                "hash": block_hash,
                "parent": header["parentHash"],
                "root": root_hash,
                "timestamp_utc": stamp.isoformat(),
                "transactions": len(receipts),
                "receipt_logs": sum(len(r["logs"]) for r in receipts),
                "sources": [head_source, receipt_source],
            }
        )
        for i, row in enumerate(receipts):
            filtered = [log for log in row["logs"] if log["topics"] and log["topics"][0] in topics]
            if filtered:
                hits[len(filtered)] += 1
            if len(filtered) < 5 or row["transactionHash"] in exclusions:
                continue
            n_pools, path = rank(filtered, topics)
            tx = row["transactionHash"]
            candidate = {
                "hash": tx,
                "height": height,
                "index": i,
                "events": len(filtered),
                "pool_keys": n_pools,
                "equal_amount_proxy_depth": len(path),
                "proxy_events": path,
            }
            candidates.append(candidate)
            registry[tx] = (block, header, row)
    ordered = sorted(
        candidates,
        key=lambda br: (-br["equal_amount_proxy_depth"], br["height"], br["index"], br["hash"]),
    )
    derived = {
        "complete_blocks": blocks,
        "tx_total": sum(x["transactions"] for x in blocks),
        "all_events": sum(k * v for k, v in hits.items()),
        "matched_event_hist": dict(hits),
        "thresholds": {
            str(k): sum(v for i, v in hits.items() if i >= k) for k in (1, 5, 20, 50, 100, 200)
        },
        "ranking_population": len(ordered),
        "ranked_top": ordered[:10],
    }
    return derived, registry


def audit(chosen, registry, get):
    tx, i = chosen["hash"], chosen["index"]
    block, header, row = registry[tx]
    receipt = decode_receipt(row, block.ref, tx, i)
    traced, source = get(
        "trace",
        tx[2:14],
        "debug_traceTransaction",
        [tx, {"tracer": "callTracer", "timeout": "10s", "tracerConfig": {"withLog": True}}],
    )
    frames = nested_calls(traced)
    _receipt_positions(traced, frames, receipt)
    assert (frames[()].sender, frames[()].recipient) == (receipt.sender, receipt.recipient)
    envelope = header["transactions"][i]
    assert quantity(envelope["value"]) == frames[()].value
    if quantity(envelope["type"]) == 2:
        fees = envelope_cost(envelope, receipt, quantity(header["baseFeePerGas"]))
        fee_check = "signed type 2 and rooted gas"
    else:
        fees = {
            "gas_used": receipt.gas_used,
            "gas_wei": receipt.gas_used * receipt.effective_gas_price,
        }
        fee_check = "rooted gas; receipt source effective price"
    audit = {
        "source": source,
        "call_frames": len(frames),
        "matched_receipt_logs": len(receipt.logs),
        "compatible_shape_events": len(profile_swap_receipt(receipt).events),
        "funding": strict_paths(replay_trace_funding(receipt, traced)),
        "fees": fees,
        "fee_check": fee_check,
        "explicit_native_fee_calls": [
            {"value": f.value, "from": f.sender, "call": path}
            for path, f in sorted(frames.items())
            if f.kind == "CALL"
            and f.value
            and f.recipient == header["miner"]
            and first_failure(frames, path) is None
        ],
        "settlement": _settlement(receipt, traced, header["miner"]),
    }
    audit["settlement"].pop("native_calls_touching_roles")
    return audit


def main():
    if not __debug__:
        raise SystemExit("run this verifier without Python optimization")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("operation", choices=("replay",))
    p.add_argument("root", type=Path)
    args = p.parse_args()
    root = args.root
    plan = make_scope(root)
    frozen = root / "scope.json"
    scope_bytes = frozen.read_bytes()
    reference = json.loads(scope_bytes)
    if any(reference[key] != value for key, value in plan.items()):
        raise ValueError("retained Ethereum scope or original capture driver changed")
    if json.loads((root / "STOP.json").read_bytes()).get("complete") is not True:
        raise ValueError("Ethereum frozen window is incomplete")
    get = scope_io(root)
    result, registry = evaluate(root, get)
    result["decision"] = "selected" if result["ranked_top"] else "no_new_qualified_transactions"
    chosen = result["ranked_top"][0] if result["ranked_top"] else None
    selection = canonical_json(
        {"chosen": chosen, "result_sha256": sha256_hex(canonical_json(result))}
    )
    if (root / "selection.json").read_bytes() != selection:
        raise ValueError("retained Ethereum target selection changed")
    result["audit"] = audit(chosen, registry, get) if chosen else None
    with RpcBudget(root / "original.sqlite3", POLICIES, read_only=True) as ledger:
        result["counts"] = ledger.counts()
    result["scope_sha256"] = sha256_hex(scope_bytes)
    print(canonical_json(result).decode())


if __name__ == "__main__":
    main()
