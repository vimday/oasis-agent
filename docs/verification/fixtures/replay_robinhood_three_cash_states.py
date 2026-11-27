"""Verify the old child refusal and a forward dual-provider three-state refund window."""

import argparse
import json
from collections import Counter
from pathlib import Path

from eth_abi import decode, encode
from eth_hash.auto import keccak
from replay_robinhood_shadow_window import ROUTER, SENDER, leg_quotes, read, wire

from oasis_agent.arbitrage.cycle_protocols import data_bytes
from oasis_agent.evidence.robinhood_nitro import FeedMessage
from oasis_agent.evidence.robinhood_quote import (
    _pool_ids,
    assert_feed_header,
    parse_routes,
    quote_multicall,
    read_quotes,
)
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts import capture_robinhood_nitro_feed as feed
from scripts import measure_robinhood_quote_delivery as quote


def historical(root, previous, previous_record):
    child = next(s for s in previous_record["corrected"]["states"] if s["stage"] == "direct_child")
    request = read(previous / "cash-control/discovery.request")
    request["params"][1] = {"blockHash": child["hash"], "requireCanonical": True}
    plan = read(root / "old-plan.json")
    raw = (root / "old-triport.response").read_bytes()
    meta = read(root / "old-triport.meta")
    assert canonical_json(request) == (root / "old-triport.request").read_bytes()
    assert sha256_hex(canonical_json(request)) == meta["request_sha256"] == plan["raw_body_sha256"]
    assert sha256_hex(raw) == meta["response_sha256"] and meta["status"] == 400
    reply = json.loads(raw)
    assert reply["id"] == 1 and reply["error"]["code"] == -32602
    assert (
        plan["state_hash"] == child["hash"]
        and plan["source"] == "https://triport.io/public/robinhood"
    )
    with RpcBudget(
        root / "historical-triport-original.sqlite3",
        (BudgetPolicy("triport", 1, 0),),
        read_only=True,
    ) as b:
        assert b.counts() == read(root / "old-STOP.json")["counts"] == {"triport": 1}
    return {
        "number": child["number"],
        "hash": child["hash"],
        "source": plan["source"],
        "error": reply["error"],
        "http": meta["status"],
        "cash_wei": None,
        "scope_sha256": sha256_hex((root / "old-plan.json").read_bytes()),
        "wire": meta,
    }


def failed_leg(row, route):
    raw = data_bytes(row["revert_data"])
    assert raw[:4] == keccak(b"UnexpectedRevertBytes(bytes)")[:4]
    (inner,) = decode(("bytes",), raw[4:])
    assert inner[:4] == keccak(b"NotEnoughLiquidity(bytes32)")[:4] and len(inner) == 36
    pool = inner[4:]
    return {
        "leg": list(_pool_ids((route,))).index(pool) + 1,
        "pool_id": "0x" + pool.hex(),
        "revert_data": row["revert_data"],
    }


def replay(root, sources):
    prev = root.parent / "robinhood-shadow-nov27"
    prior_path = Path(__file__).resolve().parents[1] / "ROBINHOOD_ROUTER_SHADOW_20261127.json"
    assert (
        sha256_hex(prior_path.read_bytes()) == read(root / "old-plan.json")["source_report_sha256"]
    )
    refused = historical(root, prev, read(prior_path))
    run = root / "forward"
    scope = read(run / "cash-plan.json")
    plan = read(run / "plan.json")
    original = read(run / "result.json")
    f = read(run / "frozen.json")
    env = read(run / "envelope.json")
    route_bytes = (run / "routes.json").read_bytes()
    routes = parse_routes(json.loads(route_bytes))
    assert sha256_hex(route_bytes) == scope["route_sha256"] == plan["routes_sha256"]
    assert (
        scope["sources"] == quote.SOURCES
        and scope["maximum_per_source"] == 30
        and scope["max_usd"] == 0
    )
    assert (
        "stop" not in original
        and f == original["frozen"]
        and sha256_hex(canonical_json(env)) == f["envelope_sha256"]
    )
    assert scope["created"]["monotonic_ns"] < f["quote_received_ns"] <= f["frozen"]["monotonic_ns"]
    ps, sp, streams = feed.decode_pair_capture(run / "feeds", sources)
    compared = feed.summarize_pair_capture(ps, sp, streams)
    assert compared == read(run / "feeds-report.json") and compared["primary_internal_missing"] == 0
    assert ps["started_ns"] > scope["created"]["monotonic_ns"]
    primary = {m["sequence"]: m for m in streams["primary"]["messages"]}
    both = {p["sequence"]: p for p in compared["pairs"]}
    sel = read(run / "return-head-selection.json")
    ordered = [m for m in primary.values() if m["feed_block_hash"] and m["incoming_kind"] == 3]
    earlier = [
        m for m in ordered if m["received"]["monotonic_ns"] <= sel["simulation_completed_ns"]
    ]
    later = [m for m in ordered if m["received"]["monotonic_ns"] > sel["simulation_completed_ns"]]

    def ident(m):
        return [m["sequence"], m["feed_block_hash"], m["received"]["monotonic_ns"]]

    assert sel["selected"] == ident(earlier[-1]) and sel["next_seen"] == ident(later[0])
    pins = quote._runtime_pins()
    pins[quote.MULTICALL3] = read(prev / "control/report.json")["states"][0]["runtime_keccak"][
        quote.MULTICALL3
    ]
    pins[ROUTER] = read(quote.CONFIGS / "route-probe.robinhood-stockhooks-eth.example.json")[
        "expected_hashes"
    ]["universal_router"][2:]
    assert pins == plan["runtime_keccaks"]
    probe = (
        b"".join(
            b"\x73" + data_bytes(addr) + b"\x3f\x60" + bytes([i * 32]) + b"\x52"
            for i, addr in enumerate(pins)
        )
        + b"\x60\xa0\x60\x00\xf3"
    )
    binary = bytes.fromhex((prev / "cash-control/RobinhoodNativeCashProbe.bin").read_text().strip())
    assert (
        sha256_hex(binary)
        == scope["cash_probe_bytecode_sha256"]
        == read(prev / "cash-control/scope.json")["bytecode_sha256"]
    )
    cash_env = {k: v for k, v in env.items() if k not in ("to", "data")}
    cash_env["data"] = "0x" + (binary + encode(["bytes"], [data_bytes(env["data"])])).hex()
    receipts = {}

    def pair(i, method, params):
        reply, refs = wire(run, f"{i:02}-", i, method, params)
        assert i not in receipts
        receipts[i] = refs
        vals = [v["result"] for v in reply.values()]
        assert vals[0] == vals[1]
        return vals[0]

    def end(i):
        return max(meta["received_ns"] for meta in receipts[i].values())

    control = next(r for r in routes if r.name == f["route"])
    long_route = next(r for r in routes if len(r.pools) == 24)
    output = {}
    schedule = (
        (1, 5, 6, 7, 8, 2, 3, 4, 25),
        (9, 10, 11, 12, 13, None, 20, 19, 23),
        (14, 15, 16, 17, 18, None, 22, 21, 24),
    )
    for state, ids in zip(original["states"], schedule, strict=True):
        head, code, market, call, estimate, identity, balance, nonce, cash = ids
        n, h = state["n"], state["h"]
        tag = {"blockHash": h, "requireCanonical": True}
        header, base = assert_feed_header(
            pair(head, "eth_getBlockByNumber", [hex(n), False]), FeedMessage.from_row(primary[n])
        )
        assert (
            both[n]["block_hash"] == header.ref.block_hash == h
            and header.state_root == state["state"]
        )
        assert (
            base == state["fee"]
            and header.parent_hash == state["parent"]
            and header.ref.timestamp == state["ts"]
        )
        if identity:
            assert (
                n == ordered[0]["sequence"] and pair(identity, "eth_getCode", [SENDER, tag]) == "0x"
            )
        bal = int(pair(balance, "eth_getBalance", [SENDER, tag]), 16)
        nonce_value = int(pair(nonce, "eth_getTransactionCount", [SENDER, tag]), 16)
        assert bal == state["sender_balance_at_state"] >= control.amount + 4000000 * 100000000
        assert nonce_value == state["sender_nonce_at_state"] == int(env["nonce"], 16)
        assert header.ref.timestamp <= f["deadline"] and base <= int(env["maxFeePerGas"], 16)
        assert data_bytes(
            pair(code, "eth_call", [{"data": "0x" + probe.hex(), "gas": "0x30000"}, tag])
        ) == b"".join(bytes.fromhex(s) for s in pins.values())
        raw = pair(
            market,
            "eth_call",
            [
                {
                    "to": quote.MULTICALL3,
                    "data": quote_multicall(routes),
                    "gas": hex(quote.TRANSPORT_GAS),
                },
                tag,
            ],
        )
        qm = read_quotes(raw, routes, base)
        assert state["quotes"] == qm["quotes"] and state["quote_ns"] == end(market)
        eligible = [
            z for z in qm["quotes"] if z.get("optimistic_net_wei_before_other_costs", 0) > 0
        ]
        if head == 1:
            candidates = eligible or [
                z
                for z in qm["quotes"]
                if z["status"] == "quoted" and z["hops"] == 2 and z["input_wei"] == 10**14
            ]
            selected = min(
                candidates, key=lambda z: (-z["optimistic_net_wei_before_other_costs"], z["route"])
            )
            minimum = selected["output_wei"] if eligible else control.amount
            assert selected["route"] == control.name and minimum == f["minout_wei"]
            assert (
                f["frozen"]["monotonic_ns"] >= end(market)
                and f["deadline"] == header.ref.timestamp + 120
            )
            assert f["kind"] == ("positive_floor" if eligible else "negative_control")
            assert env == {
                "from": SENDER,
                "to": ROUTER,
                "nonce": hex(nonce_value),
                "type": "0x2",
                "value": hex(control.amount),
                "gas": hex(4000000),
                "chainId": hex(4663),
                "maxFeePerGas": hex(100000000),
                "maxPriorityFeePerGas": "0x0",
                "accessList": [],
                "data": control.shadow_router_data(minimum, f["deadline"]),
            }
        assert pair(call, "eth_call", [env, tag]) == state["whole_return"] == "0x"
        gas = int(pair(estimate, "eth_estimateGas", [env, tag]), 16)
        assert state["estimate_gas"] == [gas, gas] and state["whole_return_ns"] == end(estimate)
        if head == 1:
            assert end(estimate) == sel["simulation_completed_ns"]
            assert all(m["sent_ns"] >= f["frozen"]["monotonic_ns"] for m in receipts[call].values())
        refund_raw = data_bytes(pair(cash, "eth_call", [cash_env, tag]))
        assert len(refund_raw) == 32
        refund = int.from_bytes(refund_raw)
        assert (
            refund == state["cash"]["refund_wei"] and end(cash) == state["cash_probe_received_ns"]
        )
        chosen = next(r for r in qm["quotes"] if r["route"] == control.name)
        assert refund == chosen["output_wei"] and refund - control.amount == chosen["gross_wei"]
        failed = failed_leg(
            next(r for r in qm["quotes"] if r["route"] == long_route.name), long_route
        )
        failed["pool_state"] = next(p for p in qm["pools"] if p["pool_id"] == failed["pool_id"])
        output[state["stage"]] = {
            "number": n,
            "hash": h,
            "state_root": header.state_root,
            "parent_hash": header.parent_hash,
            "utc_feed_received": primary[n]["received"]["utc"],
            "feed_to_initial_full_result_ns": end(estimate) - both[n]["primary_receive_ns"],
            "quotes_to_initial_full_result_ns": end(estimate) - end(market),
            "sender_nonce": nonce_value,
            "sender_available_wei": str(bal),
            "estimated_gas": gas,
            "base_fee_wei": str(base),
            "refund_probe_wei": str(refund),
            "probe_gross_wei": str(refund - control.amount),
            "estimated_network_cost_wei": str(gas * base),
            "probe_net_after_estimated_network_wei": str(refund - control.amount - gas * base),
            "direct_eoa_balance_delta": None,
            "positive_screen": len(eligible),
            "quotes": leg_quotes(qm["quotes"]),
            "failed_long_route": failed,
            "checked_pools": len(qm["pools"]),
        }
    assert (
        output["direct_child"]["parent_hash"] == output["discovery"]["hash"]
        and output["direct_child"]["number"] == output["discovery"]["number"] + 1
    )
    assert output["return_head"]["number"] == sel["selected"][0]
    with RpcBudget(
        run / "original.sqlite3",
        tuple(BudgetPolicy(k, 30, 250) for k in quote.SOURCES),
        read_only=True,
    ) as budget:
        # Later experiments append to the same capped ledger. Authenticate the original cut.
        budget.counts()
        cut = len(receipts) * len(quote.SOURCES)
        prefix = budget.db.execute(
            "SELECT payload FROM rpc_usage WHERE seq<=? ORDER BY seq", (cut,)
        )
        counts = Counter(json.loads(raw)["source"] for (raw,) in prefix)
        assert dict(counts) == original["counts"] == dict.fromkeys(quote.SOURCES, len(receipts))
    prior = read(prior_path)["corrected"]["states"][0]
    assert (
        prior["stage"] == "discovery"
        and (prev / "corrected/routes.json").read_bytes() == route_bytes
    )
    previous_quotes, _ = wire(
        prev / "corrected",
        "06-",
        6,
        "eth_call",
        [
            {
                "to": quote.MULTICALL3,
                "data": quote_multicall(routes),
                "gas": hex(quote.TRANSPORT_GAS),
            },
            {"blockHash": prior["hash"], "requireCanonical": True},
        ],
    )
    raw = previous_quotes["official"]["result"]
    assert raw == previous_quotes["publicnode"]["result"]
    earlier = read_quotes(raw, routes, prior["base_fee_wei"])
    before = next(r for r in earlier["quotes"] if r["route"] == long_route.name)
    current = output["discovery"]["failed_long_route"]["pool_state"]
    prior_pool = next(p for p in earlier["pools"] if p["pool_id"] == current["pool_id"])
    assert before["status"] == "quoted" and prior_pool["liquidity"] == current["liquidity"]
    assert prior_pool["tick"] != current["tick"]
    return {
        "schema": "oasis-robinhood-three-refund-states-v1",
        "chain_id": 4663,
        "historical_child": refused,
        "previous_successful_long_route": {
            "hash": prior["hash"],
            "pool_state": prior_pool,
            "quote": before,
        },
        "scope_sha256": sha256_hex((run / "cash-plan.json").read_bytes()),
        "original_routes_sha256": scope["route_sha256"],
        "probe_bytecode_sha256": scope["cash_probe_bytecode_sha256"],
        "comparison_scope_sha256": compared["scope_sha256"],
        "dual_feeds": {
            k: compared[k]
            for k in (
                "capture_sha256",
                "primary_sequences",
                "delayed_sequences",
                "matched_same_hash",
                "primary_internal_missing",
                "reasons",
            )
        },
        "selection": f,
        "return_head_bracket": sel,
        "counts": original["counts"],
        "states": output,
        "wires": receipts,
        "execution_eligible": False,
    }


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("evidence replay requires assertions")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    p.add_argument("--sources-dir", type=Path, required=True)
    x = p.parse_args()
    print(json.dumps(replay(x.root, x.sources_dir), indent=2, sort_keys=True))
