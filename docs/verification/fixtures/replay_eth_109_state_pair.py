"""Compare the original historical transaction across a single predecessor state transition."""

import argparse
import json
from hashlib import sha256
from pathlib import Path

from replay_eth_109_motive import (
    EXECUTOR,
    FEED,
    HASH,
    INDEX,
    METRIC_ADAPTER,
    METRIC_POOL,
    ORACLE,
    PAYOUT,
    SENDER,
    TX,
    USDC,
    USDT,
    WETH,
    prove,
    selector,
)
from replay_eth_twenty_flow import load

from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes
from oasis_agent.evidence.research_traces import first_failure, nested_calls
from oasis_agent.evidence.swap_flow import TRANSFER
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget


def branch_at(tree, path):
    node = tree
    for i in path:
        node = node["calls"][i]
    return node


def movement(tree, path, token, payer, destination):
    logs = branch_at(tree, path)["logs"]
    expected = [TRANSFER, "0x" + "0" * 24 + payer[2:], "0x" + "0" * 24 + destination[2:]]
    matches = [log["data"] for log in logs if log["address"] == token and log["topics"] == expected]
    assert len(matches) == 1
    return abi(("uint256",), data_bytes(matches[0]))[0]


def amounts(tree, landed, env):
    frames = nested_calls(tree)
    assert tree.get("error") is None and tree["input"] == env["data"]
    assert (tree["to"], tree["from"], tree["value"]) == (EXECUTOR, SENDER, env["value"])
    path = tuple(landed["oracle_join"]["read_trace_path"])
    oracle = frames[path]
    assert first_failure(frames, path) is None and oracle.recipient == ORACLE
    assert oracle.data[:4] == selector("price(bytes32,address)")
    assert abi(("bytes32", "address"), oracle.data[4:]) == (data_bytes(FEED), METRIC_POOL)
    value = abi(("uint256", "uint256", "uint16", "uint256"), oracle.output)
    pool = path[:-2]
    incoming, outgoing = (*pool, 4, 0), (*pool, 2, 0)
    assert all(first_failure(frames, p) is None for p in (incoming, outgoing))
    sold = movement(tree, incoming, USDT, METRIC_ADAPTER, METRIC_POOL)
    bought = movement(tree, outgoing, USDC, METRIC_POOL, EXECUTOR)
    profits = []
    previous = None
    for i in range(1, 5):
        before, after = (frames[(i, n)] for n in (0, 2))
        for z in (before, after):
            assert z.kind == "STATICCALL" and z.sender == PAYOUT and z.recipient == WETH
            assert (
                z.data == selector("balanceOf(address)") + bytes(12) + data_bytes(PAYOUT)
                and z.error is None
            )
        start, end = (abi(("uint256",), z.output)[0] for z in (before, after))
        if previous is not None:
            assert start == previous
        previous = end
        paid = (i, 1, 3)
        assert first_failure(frames, paid) is None
        assert movement(tree, paid, WETH, EXECUTOR, PAYOUT) == end - start
        profits.append(end - start)
    fee = frames[(0,)]
    assert (
        first_failure(frames, (0,)) is None
        and fee.sender == EXECUTOR
        and fee.recipient == landed["fee_recipient"]
        and fee.value == landed["direct_fee_wei"]
    )
    changes = dict.fromkeys((SENDER, EXECUTOR, PAYOUT), 0)
    for p, z in frames.items():
        if z.kind != "CALL" or first_failure(frames, p) is not None:
            continue
        for a in changes:
            changes[a] += z.value * (int(z.recipient == a) - int(z.sender == a))
    assert changes == {SENDER: -fee.value, EXECUTOR: 0, PAYOUT: 0}
    gas = int(tree["gasUsed"], 16)
    charge = gas * landed["effective_gas_price_wei"]
    return {
        "oracle_return": list(value),
        "swap_usdt_in_raw": str(sold),
        "swap_usdc_out_raw": str(bought),
        "payout_by_batch_weth_wei": [str(n) for n in profits],
        "gross_weth_wei": str(sum(profits)),
        "gas_used": gas,
        "gas_charge_scenario_wei": str(charge),
        "direct_fee_wei": str(fee.value),
        "conditional_residual_wei": str(sum(profits) - fee.value - charge),
        "frames": len(frames),
    }


def replay(research):
    src = research / "eth-109-motive-20261127"
    root = research / "eth-109-state-pair-20261127"
    landed = prove(src, research / "eth-transfer-fingerprint-20261127/logs.json")
    header = json.loads((src / "header.response.json").read_bytes())["result"]
    original = json.loads((src / "trace.response.json").read_bytes())["result"]
    tx = header["transactions"][INDEX]
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
    scope = json.loads((root / "scope.json").read_bytes())
    assert (
        scope["envelope_sha256"] == digest
        and scope["positions"] == [INDEX, INDEX - 1]
        and scope["tx"] == TX
    )
    with RpcBudget(root / "original.sqlite3", (BudgetPolicy("trace", 2, 0),), read_only=True) as b:
        assert b.counts() == {"trace": 2}
    results = {}
    wires = {}
    for idx in (INDEX, INDEX - 1):
        config = {
            "tracer": "callTracer",
            "tracerConfig": {"withLog": True},
            "txIndex": hex(idx),
            "timeout": "10s",
        }
        tree = load(root, f"{idx}-trace", "debug_traceCall", [env, HASH, config])
        if idx == INDEX:
            assert tree == original
        results[idx] = amounts(tree, landed, env)
        meta = json.loads((root / f"{idx}-trace.meta.json").read_bytes())
        assert meta["source"] == scope["source"]["trace"]
        wires[idx] = {
            k: meta[k] for k in ("source", "completed_utc", "request_sha256", "response_sha256")
        }
    old, new = results[INDEX - 1], results[INDEX]
    assert old["payout_by_batch_weth_wei"][1:] == new["payout_by_batch_weth_wei"][1:]
    assert new["conditional_residual_wei"] == str(
        landed["visible_after_execution_and_direct_fee_if_one_owner_wei"]
    )
    return {
        "chain_id": 1,
        "block_hash": HASH,
        "tx": TX,
        "envelope_sha256": digest,
        "scope_sha256": sha256((root / "scope.json").read_bytes()).hexdigest(),
        "after_update_matches_landed_tree": True,
        "before_update": old,
        "after_update": new,
        "net_change_wei": str(
            int(new["conditional_residual_wei"]) - int(old["conditional_residual_wei"])
        ),
        "wires": wires,
    }


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("evidence verification requires assertions")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("research", type=Path)
    print(json.dumps(replay(parser.parse_args().research), indent=2, sort_keys=True))
