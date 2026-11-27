"""Reconstruct the retained Robinhood observation, including declined and censored states."""

import argparse
import json
from pathlib import Path

from eth_abi import encode
from eth_hash.auto import keccak

from oasis_agent.arbitrage.cycle_protocols import data_bytes
from oasis_agent.evidence.robinhood_nitro import FeedMessage
from oasis_agent.evidence.robinhood_quote import (
    assert_feed_header,
    parse_routes,
    quote_multicall,
    read_quotes,
)
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from oasis_agent.rpc.protocol import response_envelope
from scripts import capture_robinhood_nitro_feed as feed
from scripts import measure_robinhood_quote_delivery as quote

SENDER = "0x8f56032baada22cd9ad8f69fbec86261ea3256ea"
ROUTER = "0x8876789976decbfcbbbe364623c63652db8c0904"


def read(p):
    return json.loads(p.read_bytes())


def wire(root, label, number, method, params, *, suffixes=None, expected_status=None):
    rows, manifests = {}, {}
    body = quote._body(number, method, params)
    for source, suffix in (suffixes or {k: k for k in quote.SOURCES}).items():
        prefix = root / (label + suffix)
        req, resp, meta = (prefix.with_suffix(ext) for ext in (".request", ".response", ".meta"))
        raw, info = resp.read_bytes(), read(meta)
        assert req.read_bytes() == body and info["request_sha256"] == sha256_hex(body)
        assert info["response_sha256"] == sha256_hex(raw) and info["status"] == (
            expected_status or {}
        ).get(source, 200)
        assert 0 < info["sent_ns"] <= info["received_ns"] and info["retrieved_unix_ns"] > 0
        rows[source] = response_envelope(raw, number)
        manifests[source] = dict(source=quote.SOURCES[source], **info)
    return rows, manifests


def leg_quotes(results):
    return [
        {
            k: z[k]
            for k in (
                "route",
                "input_wei",
                "hops",
                "status",
                "gross_wei",
                "optimistic_net_wei_before_other_costs",
            )
            if k in z
        }
        for z in results
    ]


def audit_scope(root, sources, prior, *, fixed):
    plan = read(root / "plan.json")
    assert plan["routes_sha256"] == sha256_hex((root / "routes.json").read_bytes())
    routes = parse_routes(read(root / "routes.json"))
    assert {r.amount for r in routes} == {10**14, 10**15, 10**16}
    assert {len(r.pools) for r in routes} == {2, 20, 24}
    collection = read(root / "result.json")
    scope, stop, streams = feed.decode_pair_capture(root / "feeds", sources)
    pair = feed.summarize_pair_capture(scope, stop, streams)
    assert pair == read(root / "feeds-report.json") and pair["primary_internal_missing"] == 0
    messages = {r["sequence"]: r for r in streams["primary"]["messages"]}
    both = {r["sequence"]: r for r in pair["pairs"]}
    acc, logged = {}, {}

    def get(i, method, params):
        rows, logged[i] = wire(root, f"{i:02d}-", i, method, params)
        return rows

    def same(i, method, params):
        rows = get(i, method, params)
        assert rows["official"]["result"] == rows["publicnode"]["result"]
        return rows["official"]["result"]

    def settled(i):
        return max(m["received_ns"] for m in logged[i].values())

    runtime = quote._runtime_pins()
    runtime[quote.MULTICALL3] = prior["states"][0]["runtime_keccak"][quote.MULTICALL3]
    router_config = read(quote.CONFIGS / "route-probe.robinhood-stockhooks-eth.example.json")
    runtime[ROUTER] = router_config["expected_hashes"]["universal_router"][2:]
    assert runtime == plan["runtime_keccaks"]
    hash_probe = (
        b"".join(
            b"\x73" + data_bytes(a) + b"\x3f\x60" + bytes([32 * i]) + b"\x52"
            for i, a in enumerate(runtime)
        )
        + b"\x60\xa0\x60\x00\xf3"
    )
    states = []
    tx = read(root / "envelope.json")
    f = read(root / "frozen.json")
    assert f == collection["frozen"] and sha256_hex(canonical_json(tx)) == f["envelope_sha256"]
    route = next(r for r in routes if r.name == f["route"])
    for state, (head_i, code_i, price_i, call_i) in zip(
        collection["states"],
        ([(1, 5, 6, 7), (9, 10, 11, 12), (14, 15, 16, 17)] if fixed else [(1, 5, 6, 7)]),
        strict=True,
    ):
        n = state["n"]
        header = same(head_i, "eth_getBlockByNumber", [hex(n), False])
        hint = messages[n]
        verified, base = assert_feed_header(header, FeedMessage.from_row(hint))
        assert (
            verified.ref.block_hash,
            verified.state_root,
            verified.ref.timestamp,
            verified.parent_hash,
            base,
        ) == (state["h"], state["state"], state["ts"], state["parent"], state["fee"])
        assert both[n]["block_hash"] == state["h"] and both[n]["primary_receive_ns"] == state["t"]
        pin = {"blockHash": state["h"], "requireCanonical": True}
        if not states:
            assert same(2, "eth_getCode", [SENDER, pin]) == "0x"
            balance = int(same(3, "eth_getBalance", [SENDER, pin]), 16)
            nonce = same(4, "eth_getTransactionCount", [SENDER, pin])
            acc = {"balance_wei": balance, "nonce": int(nonce, 16)}
            assert balance >= route.amount + 4000000 * 100000000 and base <= 100000000
            assert state["h"] == f["discovery_state"] and f["deadline"] == state["ts"] + 120
            expected = {
                "to": ROUTER,
                "type": "0x2",
                "chainId": hex(4663),
                "gas": hex(4000000),
                "nonce": nonce,
                "value": hex(route.amount),
                "maxPriorityFeePerGas": "0x0",
                "maxFeePerGas": hex(100000000),
                "accessList": [],
            }
            assert {k: v for k, v in tx.items() if k not in {"from", "data"}} == expected and tx[
                "from"
            ] == SENDER
            if fixed:
                assert tx["data"] == route.shadow_router_data(f["minout_wei"], f["deadline"])
            assert sha256_hex(data_bytes(tx["data"])) == f["calldata_sha256"]
        hashes = same(
            code_i, "eth_call", [{"data": "0x" + hash_probe.hex(), "gas": "0x30000"}, pin]
        )
        assert data_bytes(hashes) == b"".join(bytes.fromhex(v) for v in runtime.values())
        raw = same(
            price_i,
            "eth_call",
            [
                {
                    "to": quote.MULTICALL3,
                    "data": quote_multicall(routes),
                    "gas": hex(quote.TRANSPORT_GAS),
                },
                pin,
            ],
        )
        qm = read_quotes(raw, routes, base)
        assert qm["quotes"] == state["quotes"] and settled(price_i) == state["quote_ns"]
        qs = qm["quotes"]
        assert all(z.get("optimistic_net_wei_before_other_costs", 0) <= 0 for z in qs)
        target = next(z for z in qs if z["route"] == route.name)
        if not states:
            best = min(
                (z for z in qs if z["hops"] == 2 and z["input_wei"] == 10**14),
                key=lambda z: (-z["optimistic_net_wei_before_other_costs"], z["route"]),
            )
            assert (
                target == best
                and f["kind"] == "negative_control"
                and f["minout_wei"] == route.amount
            )
            assert state["quote_ns"] <= f["frozen"]["monotonic_ns"]
        replies = get(call_i, "eth_call", [tx, pin])
        if not states:
            assert min(v["sent_ns"] for v in logged[call_i].values()) >= f["frozen"]["monotonic_ns"]
        est = None
        if fixed:
            assert all(v["result"] == "0x" for v in replies.values())
            estimates = get(call_i + 1, "eth_estimateGas", [tx, pin])
            est = [int(z["result"], 16) for z in estimates.values()]
            assert est == state["estimate_gas"] and settled(call_i + 1) == state["whole_return_ns"]
        else:
            assert all(
                v["error"]["code"] == 3 and v["error"]["data"] == "0x" for v in replies.values()
            )
            assert settled(call_i) == state["whole_return_ns"]
        states.append(
            {
                "stage": state["stage"],
                "number": n,
                "hash": state["h"],
                "state_root": state["state"],
                "parent": state["parent"],
                "base_fee_wei": base,
                "input_quotes": leg_quotes(qs),
                "pools": len(qm["pools"]),
                "feed_to_initial_stage_result_ms": (state["whole_return_ns"] - state["t"]) // 10**6,
                "quote_to_whole_ns": state["whole_return_ns"] - state["quote_ns"],
                "completion_ns": state["whole_return_ns"],
                "feed_utc": hint["received"]["utc"],
                "whole_status": "ok" if fixed else "evm_revert",
                "router_gas_estimates": est,
                "estimated_net_wei": target["gross_wei"] - max(est) * base if est else None,
                "sender_cash_delta_exact_envelope_wei": None,
            }
        )
    if fixed:
        selected = read(root / "return-head-selection.json")
        ts = states[0]["completion_ns"]
        eligible = [
            r
            for r in messages.values()
            if r["received"]["monotonic_ns"] <= ts
            and r["feed_block_hash"]
            and r["incoming_kind"] == 3
        ]
        head = max(eligible, key=lambda r: r["received"]["monotonic_ns"])
        assert selected["simulation_completed_ns"] == ts and selected["selected"] == [
            head["sequence"],
            head["feed_block_hash"],
            head["received"]["monotonic_ns"],
        ]
        assert (
            selected["next_seen"][2] > ts
            and states[2]["number"] == head["sequence"]
            and states[1]["parent"] == states[0]["hash"]
            and states[1]["number"] == states[0]["number"] + 1
        )
        last = get(
            19,
            "debug_traceCall",
            [tx, states[0]["hash"], {"tracer": "callTracer", "timeout": "3s"}],
        )
        assert all(z["error"]["code"] == -32601 for z in last.values())
    else:
        ts = states[0]["completion_ns"]
        head = max(
            (
                v
                for v in messages.values()
                if v["received"]["monotonic_ns"] <= ts
                and v["feed_block_hash"]
                and v["incoming_kind"] == 3
            ),
            key=lambda v: v["received"]["monotonic_ns"],
        )
        failure = get(8, "eth_getBlockByNumber", [hex(head["sequence"]), False])
        assert failure["official"]["result"] is None
        assert_feed_header(failure["publicnode"]["result"], FeedMessage.from_row(head))
    assert len(logged) == collection["stop"]["attempted_pair"]
    distances = sorted(r["delayed_minus_primary_ns"] for r in pair["pairs"])
    return {
        "paired_feeds": {
            k: pair[k]
            for k in (
                "matched_same_hash",
                "primary_sequences",
                "delayed_sequences",
                "primary_internal_missing",
                "matched_without_two_hashes",
            )
        }
        | {
            "median_backup_minus_primary_ns": (
                distances[(len(distances) - 1) // 2] + distances[len(distances) // 2]
            )
            // 2
        },
        "account": acc,
        "frozen": f,
        "scope_sha256": sha256_hex((root / "plan.json").read_bytes()),
        "feed_capture_sha256": pair["capture_sha256"],
        "states": states,
        "stop": collection["stop"],
        "wires": logged,
    }


def replay(root, sources):
    baseline = quote.run(root / "control", sources, None)
    control = [
        {
            "number": x["number"],
            "hash": x["hash"],
            "parent": x["parent_hash"],
            "base_fee_wei": x["base_fee_per_gas_wei"],
            "quotes": leg_quotes(x["quotes"]),
            "latency_ns": x["frame_to_recorded_quote_ns"],
        }
        for x in baseline["states"]
    ]
    old = audit_scope(root / "ladder", sources, baseline, fixed=False)
    new = audit_scope(root / "corrected", sources, baseline, fixed=True)
    verified = read(root / "sourcify-router.response")
    assert verified["chainId"] == "4663" and verified["address"].lower() == ROUTER
    code = read(quote.CONFIGS / "route-probe.robinhood-stockhooks-eth.example.json")[
        "expected_hashes"
    ]["universal_router"]
    assert keccak(data_bytes(verified["runtimeBytecode"]["onchainBytecode"])).hex() == code[2:]
    assert (
        "uint256[] minHopPriceX36;"
        in verified["sources"]["lib/v4-periphery/src/interfaces/IV4Router.sol"]["content"]
    )
    old_env = read(root / "ladder/envelope.json")
    routes = parse_routes(read(root / "corrected/routes.json"))
    route = next(r for r in routes if r.name == old["frozen"]["route"])
    amended = {
        **old_env,
        "data": route.shadow_router_data(old["frozen"]["minout_wei"], old["frozen"]["deadline"]),
    }
    old_pin = {"blockHash": old["states"][0]["hash"], "requireCanonical": True}
    assert amended != old_env
    result, abi_wires = wire(
        root / "abi-control",
        "",
        1,
        "eth_call",
        [amended, old_pin],
        expected_status={"publicnode": 403},
    )
    assert result["official"]["result"] == "0x" and result["publicnode"]["error"]["code"] == -32602
    tmp = root / "cash-control"
    cp = read(tmp / "scope.json")
    assert cp["source_sha256"] == sha256_hex(
        Path(__file__).with_name("RobinhoodNativeCashProbe.sol").read_bytes()
    )
    binary = bytes.fromhex((tmp / "RobinhoodNativeCashProbe.bin").read_text().strip())
    assert cp["bytecode_sha256"] == sha256_hex(binary)
    tx = read(root / "corrected/envelope.json")
    assert cp["envelope_sha256"] == new["frozen"]["envelope_sha256"]
    data = binary + encode(["bytes"], [data_bytes(tx["data"])])
    assert sha256_hex(data) == cp["new_data_sha256"]
    probe = {**tx, "data": "0x" + data.hex()}
    del probe["to"]
    probes = {}
    for i, state in enumerate([new["states"][0], new["states"][2]], 1):
        st = state["stage"]
        pin = {"blockHash": state["hash"], "requireCanonical": True}
        # This single-source probe preserves the nested router payload and refunds the EOA.
        # The source filenames use the state stage rather than the transport label.
        rows, wires = wire(tmp, "", i, "eth_call", [probe, pin], suffixes={"official": st})
        res = data_bytes(rows["official"]["result"])
        assert len(res) == 32
        amount = int.from_bytes(res)
        z = next(z for z in state["input_quotes"] if z["route"] == new["frozen"]["route"])
        assert amount - z["input_wei"] == z["gross_wei"]
        probes[st] = {
            "state_hash": state["hash"],
            "cash_probe_refund_wei": amount,
            "wire": wires["official"],
        }
    policy = tuple(BudgetPolicy(k, 30, 250) for k in quote.SOURCES)
    with RpcBudget(root / "ladder/original.sqlite3", policy, read_only=True) as b:
        counts = b.counts()
    assert counts == {"official": 30, "publicnode": 28}
    return {
        "schema": "oasis-robinhood-forward-router-v1",
        "chain_id": 4663,
        "baseline": control,
        "baseline_report_sha256": sha256_hex((root / "control/report.json").read_bytes()),
        "original_ladder": old,
        "corrected": new,
        "public_source_sha256": sha256_hex((root / "sourcify-router.response").read_bytes()),
        "sourcify_match_id": verified["matchId"],
        "same_old_state_corrected_abi": abi_wires,
        "native_refund_controls": probes,
        "request_counts": {
            "control": {"official": 12, "publicnode": 12},
            "ladder_and_followups": counts,
        },
        "execution_eligible": False,
    }


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("research replay requires assertions")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    p.add_argument("--sources-dir", type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(replay(a.root, a.sources_dir), sort_keys=True, indent=2))
