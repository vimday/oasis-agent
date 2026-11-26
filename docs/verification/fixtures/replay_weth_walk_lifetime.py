"""Verify the two later-state Ethereum calls for the retained 12-swap WETH case."""

import json
from hashlib import sha256

import rlp
from eth_account import Account
from eth_hash.auto import keccak
from replay_eth_twenty_flow import load

from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes
from oasis_agent.evidence.research_traces import nested_calls
from oasis_agent.evidence.state_trie import prague_header
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget


def transfer_call(frames, branch, token, venue, incoming):
    rows = []
    for path, f in frames.items():
        if path[:3] != branch or f.kind != "CALL" or f.recipient != token or f.error:
            continue
        if f.data[:4] == bytes.fromhex("a9059cbb"):
            to, amount = abi(("address", "uint256"), f.data[4:])
            payer = f.sender
        elif f.data[:4] == bytes.fromhex("23b872dd"):
            payer, to, amount = abi(("address", "address", "uint256"), f.data[4:])
        else:
            continue
        if (to if incoming else payer) == venue:
            assert f.output in (b"", (1).to_bytes(32, "big"))
            rows.append((payer, to, amount))
    assert len(rows) == 1
    return rows[0]


def verify_lifetime(root, case):
    folder = root / "lifetime-105"
    height, idx, txid = case["height"], case["index"], case["tx"]
    assert idx == 105 and height == 26406955
    tx = load(folder, "envelope-header", "eth_getTransactionByHash", [txid])
    parent = load(root, f"{height}-header", "eth_getBlockByNumber", [hex(height), False])
    header, _ = prague_header(parent, height, case["hash"])
    child = load(folder, "child-header", "eth_getBlockByNumber", [hex(height + 1), False])
    successor, _ = prague_header(child, height + 1, child["hash"])
    assert successor.parent_hash == parent["hash"] == tx["blockHash"]
    assert int(tx["blockNumber"], 16) == height and int(tx["transactionIndex"], 16) == idx
    args = ("chainId", "nonce", "maxPriorityFeePerGas", "maxFeePerGas", "gas")
    prefix = [int(tx[k], 16) for k in args]
    assert int(tx["type"], 16) == 2 and prefix[0] == 1
    access = [
        [data_bytes(a["address"]), [data_bytes(k) for k in a["storageKeys"]]]
        for a in tx["accessList"]
    ]
    signed = b"\x02" + rlp.encode(
        [
            *prefix,
            data_bytes(tx["to"]),
            int(tx["value"], 16),
            data_bytes(tx["input"]),
            access,
            *[int(tx[k], 16) for k in ("yParity", "r", "s")],
        ]
    )
    assert tx["hash"] == txid == "0x" + keccak(signed).hex()
    assert Account.recover_transaction(signed).lower() == tx["from"] == case["sender"]
    assert tx["to"] == case["executor"]
    env = {k: tx[k] for k in (*args, "type", "from", "to", "value", "accessList")}
    env["data"] = tx["input"]
    config = {"tracer": "callTracer", "tracerConfig": {"withLog": True}, "timeout": "10s"}
    orig = load(root, txid[2:14] + "-trace", "debug_traceTransaction", [txid, config])
    end = load(folder, "included-end-trace", "debug_traceCall", [env, parent["hash"], config])
    for tree in (orig, end):
        assert (tree["from"], tree["to"], tree["value"], tree["input"]) == (
            tx["from"],
            tx["to"],
            tx["value"],
            tx["input"],
        )
    included, later = nested_calls(orig), nested_calls(end)
    amounts = []
    for leg in case["path"]:
        a = tuple(leg["frame"])
        b = (*a[:2], a[2] - 1, *a[3:])
        venue = leg["venue"][0]
        assert included[a].recipient == later[b].recipient == venue
        assert included[a].data[:4] == later[b].data[:4] and later[b].error is None
        observed = []
        for side, incoming in (("input", True), ("output", False)):
            token = leg[side + "_asset"]
            before = transfer_call(included, a[:3], token, venue, incoming)
            after = transfer_call(later, b[:3], token, venue, incoming)
            assert before[:2] == after[:2] and before[2] == int(leg[side]) and after[2] > 0
            observed.append(after[2])
        if amounts:
            assert amounts[-1] == observed[0]
        else:
            amounts.append(observed[0])
        amounts.append(observed[1])
    assert len(amounts) == 13 and amounts[0] == int(case["path"][0]["input"])
    assert case["path"][0]["input_asset"] == case["path"][-1]["output_asset"]
    assert end["error"] == "execution reverted" and end["revertReason"] == "CDE"
    failures = {p for p, f in later.items() if f.error}
    leaf = (0, 1, 59)
    assert failures == {leaf[:i] for i in range(4)}
    assert later[leaf].recipient == included[0, 1, 60].recipient
    assert later[leaf].data[:4] == included[0, 1, 60].data[:4]
    assert later[leaf].output == bytes.fromhex("d174508b")
    queue = [end]
    while queue:
        frame = queue.pop()
        assert not frame.get("logs")
        queue.extend(frame.get("calls", []))

    wires = {}
    for stem in ("envelope-header", "child-header", "included-end-trace", "child-end-trace"):
        meta = json.loads((folder / f"{stem}.meta.json").read_bytes())
        lane = stem.rsplit("-", 1)[1]
        assert meta["source"] == (
            "https://ethereum.publicnode.com" if lane == "header" else "https://eth.drpc.org"
        )
        assert meta["http"] == "200"
        for side in ("request", "response"):
            assert (
                sha256((folder / f"{stem}.{side}.json").read_bytes()).hexdigest()
                == meta[side + "_sha256"]
            )
        wires[stem] = {
            k: meta[k]
            for k in ("source", "request_sha256", "response_sha256", "completed_utc", "failure")
        }
    req = json.loads((folder / "child-end-trace.request.json").read_bytes())
    assert req == {
        "id": 1,
        "jsonrpc": "2.0",
        "method": "debug_traceCall",
        "params": [env, child["hash"], config],
    }
    denial = json.loads((folder / "child-end-trace.response.json").read_bytes())
    assert denial == {
        "id": 1,
        "jsonrpc": "2.0",
        "error": {"code": -32000, "message": "max fee per gas less than block base fee"},
    }
    assert (folder / "stop-trace.json").read_bytes() == (
        folder / "child-end-trace.meta.json"
    ).read_bytes()
    policies = (BudgetPolicy("header", 2, 0), BudgetPolicy("trace", 3, 0))
    with RpcBudget(folder / "original.sqlite3", policies, read_only=True) as budget:
        assert budget.counts() == {"header": 2, "trace": 2}
    cap, tip = prefix[3], prefix[2]
    base, next_base = (int(h["baseFeePerGas"], 16) for h in (parent, child))
    assert base <= cap < next_base
    gas = int(end["gasUsed"], 16)
    return {
        "envelope_sha256": sha256(
            json.dumps(env, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest(),
        "direct_child_hash": child["hash"],
        "gap_seconds": successor.ref.timestamp - header.ref.timestamp,
        "nonce": prefix[1],
        "fee_cap_wei": str(cap),
        "tip_cap_wei": str(tip),
        "base_fee_wei": [str(base), str(next_base)],
        "end_tentative_path_amounts_raw": [str(n) for n in amounts],
        "end_tentative_gross_weth_wei": str(amounts[-1] - amounts[0]),
        "end_root_revert": end["revertReason"],
        "end_deepest_error": "0x" + later[leaf].output.hex(),
        "end_gas_used": gas,
        "end_if_included_revert_fee_wei": str(gas * min(cap, base + tip)),
        "child_error": denial["error"],
        "indexed_counterfactual": "unattempted after RPC rejection",
        "wires": wires,
    }
