"""Verify where the landed four-batch envelope fails after its public settlement."""

import argparse
import json
from hashlib import sha256
from pathlib import Path

from replay_eth_109_motive import EXECUTOR, HASH, HEIGHT, INDEX, MANAGER, SENDER, TX, WETH, prove
from replay_eth_109_state_pair import branch_at
from replay_eth_twenty_flow import load

from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes, topic
from oasis_agent.evidence.research_traces import nested_calls
from oasis_agent.evidence.state_trie import prague_header
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

CALLBACK = (1, 1, 0, 0)


def selector(signature):
    return data_bytes(topic(signature))[:4]


def repayment(frames, principal):
    balance = frames[(*CALLBACK, 61)]
    assert balance.kind == "STATICCALL" and balance.sender == EXECUTOR and balance.recipient == WETH
    assert balance.data == selector("balanceOf(address)") + bytes(12) + data_bytes(EXECUTOR)
    (start,) = abi(("uint256",), balance.output)
    # Inspect every call before the repayment following this observed balance. Changes here
    # are local to the attempted subtree even when the later repayment reverts its parent.
    credits = []
    for path, frame in sorted(frames.items()):
        if path[:4] != CALLBACK or len(path) < 5 or not 61 < path[4] < 67:
            continue
        assert frame.error is None
        if frame.recipient != WETH:
            continue
        if frame.kind == "STATICCALL":
            assert frame.data[:4] == selector("balanceOf(address)")
        elif frame.data[:4] == selector("transfer(address,uint256)"):
            recipient, amount = abi(("address", "uint256"), frame.data[4:])
            assert frame.sender != EXECUTOR and frame.output == (1).to_bytes(32)
            if recipient == EXECUTOR:
                credits.append({"path": path, "from": frame.sender, "weth_wei": str(amount)})
        else:
            assert frame.kind == "CALL" and frame.sender != EXECUTOR
            assert frame.data == b"" or frame.data[:4] == selector("withdraw(uint256)")
    due = frames[(*CALLBACK, 67)]
    assert due.sender == EXECUTOR and due.recipient == WETH and due.kind == "CALL"
    assert due.data[:4] == selector("transfer(address,uint256)")
    assert abi(("address", "uint256"), due.data[4:]) == (MANAGER, principal)
    available = start + sum(int(c["weth_wei"]) for c in credits)
    assert len(credits) == 2
    assert (available >= principal and due.output == (1).to_bytes(32) and due.error is None) or (
        available < principal and due.error == "execution reverted"
    )
    return {
        "observed_executor_balance_wei": str(start),
        "observed_balance_path": (*CALLBACK, 61),
        "subsequent_executor_credits": credits,
        "available_before_transfer_wei": str(available),
        "repayment_wei": str(principal),
        "repayment_path": (*CALLBACK, 67),
        "surplus_or_shortfall_wei": str(available - principal),
    }


def replay(research):
    root = research / "eth-109-post-settlement-20261127"
    src = research / "eth-109-motive-20261127"
    scope_file = root / "scope.json"
    scope = json.loads(scope_file.read_bytes())
    assert scope["source"] == {
        "header": "https://ethereum.publicnode.com",
        "trace": "https://eth.drpc.org",
    }
    assert scope["cap"] == {"header": 1, "trace": 2} and scope["max_usd"] == 0
    with RpcBudget(
        root / "original.sqlite3",
        (BudgetPolicy("header", 1, 0), BudgetPolicy("trace", 2, 0)),
        read_only=True,
    ) as b:
        assert b.counts() == scope["cap"]
    landed = prove(src, research / "eth-transfer-fingerprint-20261127/logs.json")
    parent = json.loads((src / "header.response.json").read_bytes())["result"]
    child = load(root, "child-header", "eth_getBlockByNumber", [hex(HEIGHT + 1), False])
    prague_header(child, HEIGHT + 1, child["hash"])
    assert child["parentHash"] == parent["hash"] == HASH == scope["hash"]
    assert int(child["timestamp"], 16) > int(parent["timestamp"], 16)
    tx = parent["transactions"][INDEX]
    assert tx["hash"] == TX
    env = {
        k: tx[k]
        for k in (
            "from",
            "to",
            "gas",
            "value",
            "type",
            "nonce",
            "chainId",
            "accessList",
            "maxFeePerGas",
            "maxPriorityFeePerGas",
        )
    }
    env["data"] = tx["input"]
    digest = sha256(json.dumps(env, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert digest == scope["envelope_sha256"]
    mined = json.loads((src / "trace.response.json").read_bytes())["result"]
    take = nested_calls(mined)[(*CALLBACK, 0)]
    assert (
        take.sender == EXECUTOR
        and take.recipient == MANAGER
        and take.data[:4] == selector("take(address,address,uint256)")
    )
    token, receiver, principal = abi(("address", "address", "uint256"), take.data[4:])
    assert token == WETH and receiver == EXECUTOR
    original = repayment(nested_calls(mined), principal)
    assert (
        int(original["surplus_or_shortfall_wei"]) == landed["funding_batches"][0]["paid_weth_wei"]
    )
    results = {}
    for name, head in (("landed-end", parent), ("child-end", child)):
        tree = load(
            root,
            name + "-trace",
            "debug_traceCall",
            [
                env,
                head["hash"],
                {"tracer": "callTracer", "tracerConfig": {"withLog": True}, "timeout": "10s"},
            ],
        )
        assert tree["input"] == env["data"] and tree["value"] == env["value"]
        assert (
            tree["from"] == SENDER
            and tree["to"] == EXECUTOR
            and tree["error"] == "execution reverted"
        )
        frames = nested_calls(tree)
        payment = frames[(0,)]
        assert (
            payment.kind == "CALL"
            and payment.sender == EXECUTOR
            and payment.recipient == head["miner"]
        )
        assert (
            payment.value == landed["direct_fee_wei"] and payment.error is None and not payment.data
        )
        assert len(tree["calls"]) == 2 and frames[(*CALLBACK, 0)].data == take.data
        failures = {p for p, f in frames.items() if f.error is not None}
        last = max(failures, key=len)
        assert failures == {last[:i] for i in range(len(last) + 1)}
        terminal = frames[last]
        gas = int(tree["gasUsed"], 16)
        base = int(head["baseFeePerGas"], 16)
        assert int(env["maxPriorityFeePerGas"], 16) == 0 and base <= int(env["maxFeePerGas"], 16)
        result = {
            "height": int(head["number"], 16),
            "block_hash": head["hash"],
            "block_timestamp": int(head["timestamp"], 16),
            "frames": len(frames),
            "root_status": "reverted",
            "gas_used": gas,
            "effective_fee_scenario_wei_per_gas": str(base),
            "conditional_loss_if_simulated_gas_charged_wei": str(gas * base),
            "attempted_direct_payment_wei": str(payment.value),
            "attempted_payee": payment.recipient,
            "committed_direct_payment_wei": "0",
            "committed_payout_weth_wei": "0",
            "terminal_failure": {
                "path": last,
                "recipient": terminal.recipient,
                "method_selector": "0x" + terminal.data[:4].hex(),
                "return": "0x" + terminal.output.hex(),
            },
        }
        if name == "landed-end":
            assert last == (*CALLBACK, 67)
            result["first_repayment"] = repayment(frames, principal)
        else:
            assert terminal.kind == "STATICCALL" and terminal.output == frames[
                ()
            ].output == bytes.fromhex("666a2814")
            assert not branch_at(tree, last).get("calls")
        results[name] = result
    wires = {}
    for name in ("child-header", "landed-end-trace", "child-end-trace"):
        meta = json.loads((root / (name + ".meta.json")).read_bytes())
        assert meta["source"] == scope["source"][name.rsplit("-", 1)[1]]
        wires[name] = {
            k: meta[k] for k in ("source", "request_sha256", "response_sha256", "completed_utc")
        }
    return {
        "schema": "oasis-109-post-settlement-v1",
        "chain_id": 1,
        "tx": TX,
        "envelope_sha256": digest,
        "scope_sha256": sha256(scope_file.read_bytes()).hexdigest(),
        "original_nonce": int(env["nonce"], 16),
        "original_first_repayment": original,
        "states": results,
        "wires": wires,
    }


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("evidence verification requires assertions")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("research", type=Path)
    print(json.dumps(replay(parser.parse_args().research), sort_keys=True, indent=2))
