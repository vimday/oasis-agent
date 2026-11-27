"""Recheck the latest-state control, receipt census, and funded routes from the BSC window."""

import argparse
import json
import sys
from collections import Counter
from hashlib import sha256
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import rlp
from eth_account import Account
from eth_hash.auto import keccak
from replay_eth_twenty_flow import receipt_leaf

from oasis_agent.arbitrage.cycle_protocols import abi, calldata, data_bytes
from oasis_agent.evidence.nitro_block import indexed_trie_root
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts.replay_swap_flow import analyze


def read(path):
    return json.loads(path.read_bytes())


def sha(path):
    return sha256(path.read_bytes()).hexdigest()


def module(name, path):
    spec = spec_from_file_location(name, path)
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def amounts(tx):
    def qty(k):
        return int(tx[k], 16)

    assert qty("chainId") == 56
    tail = [qty("gas"), data_bytes(tx["to"]), qty("value"), data_bytes(tx["input"])]
    if tx["type"] == "0x2":
        assert not tx["accessList"] and qty("v") == qty("yParity")
        fields = [
            56,
            qty("nonce"),
            qty("maxPriorityFeePerGas"),
            qty("maxFeePerGas"),
            *tail,
            [],
            qty("yParity"),
        ]
        encoded = b"\x02" + rlp.encode([*fields, qty("r"), qty("s")])
        assert qty("maxFeePerGas") == 0
    else:
        assert tx["type"] == "0x0" and (qty("v") - 35) // 2 == 56
        encoded = rlp.encode([qty("nonce"), qty("gasPrice"), *tail, qty("v"), qty("r"), qty("s")])
    assert "0x" + keccak(encoded).hex() == tx["hash"]
    assert Account.recover_transaction(encoded).lower() == tx["from"] and qty("gasPrice") == 0


def wires(root, official=False):
    records = {}
    for p in root.glob("*.meta.json"):
        name = p.name.split(".")[0]
        raw = root / (name + (".request" if official else ".request.json"))
        wire = root / (name + (".response" if official else ".response.json"))
        m = read(p)
        assert str(m["http"]) == "200" and m.get("failure") is None
        assert sha(raw) == m["request_sha256"] and sha(wire) == m.get(
            "response_sha256", m.get("sha256")
        )
        req, res = read(raw), read(wire)
        assert (
            req["jsonrpc"] == res["jsonrpc"] == "2.0"
            and req["id"] == res["id"]
            and "error" not in res
        )
        records[name] = (req, res["result"], m)
    return records


def live(r):
    base = r / "bsc-wide-forward-20261123"
    folder = r / "bsc-frozen-lifetime-20261127"
    assert sha(folder / "scope.json") == (
        "98a1d9f76a59fd6cdaf78773b82abb9808f9cd5f8b5f11acb4b8c1cf79d25f4a"
    )
    scope = read(folder / "scope.json")
    for f in (
        base / "forward.py",
        base / "universe.json",
        base / "BscV2WideShadowProbe.bin-runtime",
        r / "bsc-recent-arbitrage-20261123/collect.py",
        folder / "run.py",
    ):
        assert sha(f) == scope["hashes"][f.name]
    legacy = module("bsc_original_pricing", base / "forward.py")
    uni = read(base / "universe.json")
    allpaths = legacy.paired_paths(uni)
    plain = tuple(
        row for row in allpaths if "0xfb5b838b6cfeedc2873ab27866079ac55363d37e" not in row[0]
    )
    file = folder / "observations.jsonl"
    obs = [json.loads(s) for s in file.read_text().splitlines()]
    allwire = wires(folder / "wire", True)

    def fetch(seq, method):
        req, res, meta = allwire[f"{seq:02}"]
        assert req["method"] == method and meta["source"] == scope["source"]
        return req["params"], res, meta

    price = int(fetch(2, "eth_gasPrice")[1], 16)
    assert int(fetch(1, "eth_chainId")[1], 16) == 56
    headers = {
        int(v["number"], 16): v
        for req, v, _ in allwire.values()
        if req["method"] == "eth_getBlockByNumber"
    }
    counts = {"whole_registry": Counter(), "standard_subset": Counter()}
    for row in obs:
        head = row["head"]
        hp, h, hm = fetch(head["rpc_seq"], "eth_getBlockByNumber")
        assert hp == ["latest", False] and (h["hash"], h["timestamp"]) == (
            head["hash"],
            head["timestamp"],
        )
        params, out, meta = fetch(row["state_rpc"], "eth_call")
        assert params[1] == {"blockHash": head["hash"], "requireCanonical": True}
        mc = legacy.rpc.MULTICALL
        input_calls = [
            (mc, False, data_bytes(calldata("getChainId()"))),
            (mc, False, data_bytes(calldata("getBlockNumber()"))),
        ]
        input_calls += [(u["pool"], False, data_bytes(calldata("getReserves()"))) for u in uni]
        assert params[0] == {
            "to": mc,
            "data": calldata(
                "aggregate3((address,bool,bytes)[])", ("(address,bool,bytes)[]",), (input_calls,)
            ),
        }
        (items,) = abi(("(bool,bytes)[]",), data_bytes(out))
        assert len(items) == len(input_calls) and all(ok for ok, _ in items)
        assert tuple(int.from_bytes(v) for _, v in items[:2]) == (56, int(head["number"], 16))
        reserves = [abi(("uint112", "uint112", "uint32"), v) for _, v in items[2:]]
        assert (
            hm["received"]["mono_ns"]
            <= meta["before"]["mono_ns"]
            <= meta["received"]["mono_ns"]
            <= row["ready"]["mono_ns"]
            <= row["discovered"]["mono_ns"]
        )
        for paths, field, label in [
            (allpaths, "all_diagnostic", "whole_registry"),
            (plain, "active_economic", "standard_subset"),
        ]:
            rec, selected = legacy.score(uni, reserves, paths, price)
            assert json.loads(canonical_json(rec)) == row[field] and selected is None
            for band in rec["by_hops"].values():
                counts[label].update(band)
    assert (
        len(obs) == 12
        and len(allwire) == read(folder / "complete.json")["used"]["bnb_official_free"]
    )
    with RpcBudget(
        folder / "original.sqlite3", (BudgetPolicy("bnb_official_free", 100, 65),), read_only=True
    ) as budget:
        assert budget.counts() == {"bnb_official_free": len(allwire)}
    lock = read(folder / "control-freeze.json")
    heads = read(folder / "control-heads.json")
    stages = [json.loads(s) for s in (folder / "control-stages.jsonl").read_text().splitlines()]
    assert [s["stage"] for s in stages] == ["discovery", "after_first_return", "direct_child"]
    starting = stages[0]
    same = heads["after_return"]
    tag, seen, clock = fetch(starting["rpc"] + 1, "eth_getBlockByNumber")
    assert (
        tag == ["latest", False]
        and same["hash"] == seen["hash"]
        and same["rpc_seq"] == starting["rpc"] + 1
    )
    assert (
        starting["returned"]["mono_ns"] <= clock["before"]["mono_ns"]
        and lock["frozen"]["mono_ns"] <= fetch(starting["rpc"], "eth_call")[2]["before"]["mono_ns"]
    )
    assert (
        heads["child"]["parentHash"] == heads["discovery"]["hash"]
        and int(heads["child"]["number"], 16) == int(heads["discovery"]["number"], 16) + 1
    )
    control = scope["control"]
    assert lock["selection"] == control
    model = lock["model"]
    p0 = obs[0]
    __, values0, _ = fetch(p0["state_rpc"], "eth_call")
    reserves = [
        abi(("uint112", "uint112", "uint32"), v)
        for _, v in abi(("(bool,bytes)[]",), data_bytes(values0))[0][2:]
    ]
    assert (
        list(legacy.predict(uni, reserves, control).amounts) == model
        and lock["principal_wei"] == model[0]
    )
    masked = calldata(
        "observe(address[],address[],uint256)",
        ("address[]", "address[]", "uint256"),
        (control["tokens"], control["pools"], 1),
    )
    assert lock["transaction"] == {
        "from": legacy.CALLER,
        "to": legacy.PROBE,
        "chainId": "0x38",
        "gas": hex(1500000),
        "gasPrice": hex(price),
        "value": hex(model[0]),
        "data": masked,
        "nonce": fetch(6, "eth_getTransactionCount")[1],
    }
    summaries = []
    for row, head in zip(
        stages, (heads["discovery"], heads["after_return"], heads["child"]), strict=True
    ):
        params, result, meta = fetch(row["rpc"], "eth_call")
        tx, pin, over = params
        assert (
            pin == {"blockHash": head["hash"], "requireCanonical": True}
            and tx == lock["transaction"]
        )
        assert over == {
            legacy.CALLER: {"balance": hex(10**19)},
            legacy.PROBE: {
                "code": "0x" + (base / "BscV2WideShadowProbe.bin-runtime").read_text().strip(),
                "balance": "0x0",
            },
        }
        assert (
            sha256(
                canonical_json({"chain_id": 56, "transaction": tx, "state_overrides": over})
            ).hexdigest()
            == lock["digest"]
            == row["digest"]
        )
        begin, end, body = abi(("uint256", "uint256", "uint256"), data_bytes(result))
        z = end - begin - model[0]
        intrinsic = 21000 + sum(4 if c == 0 else 16 for c in data_bytes(tx["data"]))
        assert (begin, end, body, z, intrinsic) == (
            row["sim_start_weth"],
            row["sim_end_weth"],
            row["body_gas"],
            row["excess_wei"],
            row["intrinsic"],
        )
        gross_gas = body + intrinsic
        summaries.append(
            {
                "stage": row["stage"],
                "block": row["height"],
                "block_hash": head["hash"],
                "shared_call": row.get("shared", False),
                "output_wei": str(end - begin),
                "gross_wei": str(z),
                "body_gas": body,
                "intrinsic": intrinsic,
                "full_gas_known": False,
                "net_lower_wei": str(z - 1500000 * price),
                "net_upper_wei": str(z - (gross_gas - gross_gas // 5) * price),
            }
        )
    return headers, {
        "free_calls": len(allwire),
        "source": scope["source"],
        "frozen_utc": scope["frozen_utc"],
        "completed_utc": read(folder / "complete.json")["utc"],
        "scope_sha256": sha(folder / "scope.json"),
        "observations_sha256": sha(file),
        "pools": len(uni),
        "heads": len(obs),
        "first": int(obs[0]["head"]["number"], 16),
        "last": int(obs[-1]["head"]["number"], 16),
        "route_counts": counts,
        "max_gross_upper_wei": {
            key: str(max(o[key]["top_all_input_upper"]["amount_free_gross_upper"] for o in obs))
            for key in ("all_diagnostic", "active_economic")
        },
        "eligible_frozen": 0,
        "control_principal_wei": str(model[0]),
        "envelope_sha256": lock["digest"],
        "gas_price_wei": str(price),
        "control": summaries,
        "first_return_to_latest_request_ns": clock["before"]["mono_ns"]
        - starting["returned"]["mono_ns"],
        "first_return_to_latest_response_ns": clock["received"]["mono_ns"]
        - starting["returned"]["mono_ns"],
    }


def landed(r, headers):
    folders = r / "bsc-window-audits-20261127"
    base = folders / "cohort"
    extra = folders / "commitments"
    unknown = folders / "fees"
    cases = wires(base)
    fill = wires(extra)
    picked = read(base / "ranking.json")["selected"]
    scope = read(base / "scope.json")
    topics = set(scope["signatures"])
    txs = {}
    for req, obj, _meta in (*cases.values(), *fill.values()):
        if req["method"] == "eth_getBlockByNumber":
            assert req["params"] == [obj["number"], False]
            height = int(obj["number"], 16)
            if height in headers:
                assert (
                    headers[height]["hash"] == obj["hash"]
                    and headers[height]["receiptsRoot"] == obj["receiptsRoot"]
                )
            headers[height] = obj
        elif req["method"] == "eth_getTransactionByHash":
            assert req["params"] == [obj["hash"]]
            amounts(obj)
            txs[obj["hash"]] = obj
    previous = None
    hist = Counter()
    attributed = []
    roots = []
    named = {row["pool"] for row in read(r / "bsc-wide-forward-20261123/universe.json")}
    # Existing forensic decoder for Pancake Infinity and BSC v4 was retained with the raw cases.
    old = r / "bsc-eight-address-holdout-20261124"
    trie_path = r / "eth-cohort-cost-outlier-20261123" / "verify.py"
    for path, digest in (
        (old / "capture.py", "2f9a9130f1dd40a0bea70a049c49560a6001331c5ed714a1c70e48037eb0c570"),
        (old / "replay.py", "a49b25c9a462a11ef67f19028834f00e1947ce486bb129eef66451cae776464a"),
        (trie_path, "b9e563da64fb5b570f35cac81313cef56051de6a3321009b4ad6f831bd9ee4e8"),
    ):
        assert sha(path) == digest
    previous_capture = sys.modules.get("capture")
    sys.modules["capture"] = module("capture", old / "capture.py")
    try:
        original = module("bsc_funded_closures", old / "replay.py")
    finally:
        if previous_capture is None:
            del sys.modules["capture"]
        else:
            sys.modules["capture"] = previous_capture
    historic_trie = module("retained_receipts_trie", trie_path)
    for height in range(scope["begin"], scope["end"] + 1):
        req, rs, m = cases[str(height)]
        h = headers[height]
        assert req["params"] == [hex(height)] and req["method"] == "eth_getBlockReceipts"
        assert previous is None or h["parentHash"] == previous
        previous = h["hash"]
        assert (
            "0x" + indexed_trie_root([receipt_leaf(row) for row in rs]).hex() == h["receiptsRoot"]
        )
        assert [row["transactionHash"] for row in rs] == h["transactions"]
        gas = log_idx = 0
        for idx, row in enumerate(rs):
            assert (
                row["blockHash"] == h["hash"]
                and int(row["transactionIndex"], 16) == idx
                and int(row["blockNumber"], 16) == height
            )
            gas += int(row["gasUsed"], 16)
            assert int(row["cumulativeGasUsed"], 16) == gas
            for log in row["logs"]:
                assert (
                    int(log["logIndex"], 16) == log_idx
                    and row["transactionHash"] == log["transactionHash"]
                    and log["blockHash"] == h["hash"]
                )
                log_idx += 1
            hist[
                sum(bool(log["topics"]) and log["topics"][0] in topics for log in row["logs"])
            ] += 1
        assert int(h["gasUsed"], 16) == gas
        roots.append([height, h["hash"], h["receiptsRoot"], len(rs), log_idx])
    assert dict(hist) == {int(k): v for k, v in read(base / "ranking.json")["hist"].items()}
    for t in picked:
        n, tx, idx = t["block"], t["tx"], t["index"]
        txobj = txs[tx]
        headfile = base / (str(n) + "-head.response.json")
        rs = cases[str(n)][1]
        rc = rs[idx]
        stem = tx[2:14] + "-trace"
        req, tr, meta = cases[stem]
        assert req["params"][0] == tx and headers[n]["hash"] == txobj["blockHash"] == t["hash"]
        assert (
            int(txobj["transactionIndex"], 16) == idx
            and txobj["input"] == tr["input"]
            and txobj["from"] == tr["from"] == rc["from"]
        )
        out = analyze(
            chain_id=56,
            block_raw=headfile.read_bytes(),
            receipt_raw=canonical_json({"result": rc}),
            funding_raw=None,
            call_trace_request=(base / (stem + ".request.json")).read_bytes(),
            call_trace_raw=(base / (stem + ".response.json")).read_bytes(),
            transfer_splits=True,
        )
        assigned = original.one_case(
            {"tx": tx, "height": n, "index": idx, "hash": t["hash"], "matches": []},
            headers[n],
            rs,
            tr,
            True,
            historic_trie,
        )
        legs = assigned["matched_transfer_funding_paths"]
        routes = [
            (v["swaps"], v["before"][1], v["after"][1])
            for v in legs
            if v["before"][0] == v["after"][0] == original.WBNB
        ]
        split = out["token_retained_funding"]["splits"]
        for note in split:
            (a,) = [x for x in legs if x["swaps"][-1] == note["source_swap_log"]]
            (b,) = [x for x in legs if x["swaps"][0] == note["target_swap_log"]]
            assert (
                a["before"][0] == b["after"][0] == original.WBNB
                and a["after"] == [note["token"], note["gross"]]
                and b["before"] == [note["token"], note["credited"]]
            )
            routes.append((a["swaps"] + b["swaps"], a["before"][1], b["after"][1]))
        flattened = [j for swaps, _, _ in routes for j in swaps]
        assert len(flattened) == len(set(flattened)) == t["events"] == assigned["seen_swaps"]
        assert (
            txobj["to"] == rc["to"]
            and int(txobj["gasPrice"], 16) == int(rc["effectiveGasPrice"], 16) == 0
        )
        gross = sum(b - a for _, a, b in routes)
        payments = assigned["direct_third_party_native_payment_candidates"]
        external_wbnb = []
        for log in rc["logs"]:
            ts = log["topics"]
            if (
                log["address"] == original.WBNB
                and len(ts) == 3
                and ts[0] == original.T
                and original.addr(ts[1]) == rc["to"]
            ) and original.addr(ts[2]) not in {leg["settler"] for leg in assigned["legs"]}:
                external_wbnb.append(
                    (int(log["logIndex"], 16), original.addr(ts[2]), int(log["data"], 16))
                )
        assert gross == sum(q for _, _, q in payments + external_wbnb)
        assert (
            assigned["visible_anchor_pnl_after_exec_costs"] == 0
            and not assigned["nonanchor_visible_deltas"]
        )
        attributed.append(
            {
                "tx": tx,
                "block_hash": headers[n]["hash"],
                "index": idx,
                "events": t["events"],
                "rooted_receipts": len(rs),
                "frames": assigned["root_frames"],
                "logs": len(rc["logs"]),
                "core_registry_events": sum(
                    bool(log["topics"]) and log["topics"][0] in topics and log["address"] in named
                    for log in rc["logs"]
                ),
                "routes": [
                    {"swap_logs": ids, "in_wbnb_wei": str(a), "out_wbnb_wei": str(b)}
                    for ids, a, b in routes
                ],
                "split_proofs": split,
                "gross_wbnb_wei": str(gross),
                "gas_used": assigned["gas_used"],
                "signed_price_wei": 0,
                "native_outflows": payments,
                "wbnb_outflows": external_wbnb,
                "visible_sender_executor_delta_wei": "0",
                "receipt_root": headers[n]["receiptsRoot"],
                "fee_recipient": headers[n]["miner"],
                "trace_source": meta["source"],
                "trace_sha256": meta["response_sha256"],
            }
        )
    refused = read(unknown / "STOP.json")
    ref = next(unknown.glob("*.meta.json"))
    m = read(ref)
    assert (
        m["http"] == "403"
        and sha(ref.with_name(ref.name.replace(".meta.", ".response."))) == m["response_sha256"]
    )
    for path, pol in [
        (base, scope["caps"]),
        (extra, {"publicnode": len(fill)}),
        (unknown, {"triport": 2}),
    ]:
        expected = Counter(
            m["source"]
            for _, _, m in (cases if path == base else fill if path == extra else {}).values()
        )
        with RpcBudget(
            path / "original.sqlite3",
            tuple(BudgetPolicy(k, v, 0) for k, v in pol.items()),
            read_only=True,
        ) as b:
            if path == unknown:
                assert b.counts() == {"triport": 1}
            else:
                assert b.counts() == {name: expected[scope["source"][name]] for name in pol}
    return {
        "candidate_thresholds": {
            str(k): sum(v for n, v in hist.items() if n >= k) for k in (5, 20, 50, 100, 200)
        },
        "emitting_and_silent_transactions": sum(hist.values()),
        "roots": roots,
        "cases": attributed,
        "receipt_and_trace_reads": len(cases),
        "missing_headers_and_signed_fee_reads": len(fill),
        "independent_gas_reads": 1,
        "independent_gas_stopped": {
            "reason": refused["reason"],
            "http": m["http"],
            "response_sha256": m["response_sha256"],
        },
        "inputs": {
            "cohort_plan": sha(base / "scope.json"),
            "commitment_plan": sha(extra / "scope.json"),
            "fees_plan": sha(unknown / "scope.json"),
            "ranking": sha(base / "ranking.json"),
            "historic_case_decoder": sha(old / "replay.py"),
        },
    }


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("replay requires Python assertions")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("research", type=Path)
    root = p.parse_args().research
    hh, report = live(root)
    print(
        json.dumps(
            {"chain_id": 56, "prospective": report, "landed": landed(root, hh)},
            sort_keys=True,
            indent=2,
        )
    )
