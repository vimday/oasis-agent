"""Replay the retained Ethereum July four-pool opportunity at four historical positions."""

import json
from hashlib import sha256

from replay_eth_109_state_pair import movement
from replay_eth_hundred_census import receipts_proof
from replay_eth_hundred_holdout import envelope_cost
from replay_y2026_eth_quarters import JULY_WITNESS, ROOT, SCOPE
from replay_y2026_inventory import decoded, digest, read

from oasis_agent.evidence.public_receipts import decode_receipt, quantity
from oasis_agent.evidence.research_traces import first_failure, nested_calls
from oasis_agent.evidence.swap_flow_trace import _calldata, _static_then_bytes
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

STUDY = ROOT.parent / "y2026-july-state-20261127"
PLAN = "5bf774694f243e7beb66ad1abae95d530301c528635b8fb70f9c24a54e574601"
SOURCE = "https://eth.drpc.org"
WBTC = "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"


def economics(tree, env, header):
    frames = nested_calls(tree)
    sender, target = env["from"], env["to"]
    root = frames[()]
    assert (root.sender, root.recipient, root.value, root.data.hex()) == (
        sender,
        target,
        quantity(env["value"]),
        env["data"][2:],
    )
    base = quantity(header["baseFeePerGas"])
    cap = quantity(env["maxFeePerGas"])
    assert cap >= base
    price = min(cap, base + quantity(env["maxPriorityFeePerGas"]))
    gas = quantity(tree["gasUsed"])
    assert gas <= quantity(env["gas"])
    charge = gas * price
    out = {
        "frames": len(frames),
        "root_error": root.error,
        "gas": gas,
        "wei_per_gas": price,
        "gas_charge_scenario_wei": charge,
        "sender_eth_change_after_gas_scenario_wei": -charge,
        "wrapped_proceeds_wei": None,
        "borrowed_wbtc_raw": None,
        "repaid_wbtc_raw": None,
        "direct_payment_wei": 0,
        "wbtc_excess_sold_raw": None,
    }
    if root.error:
        assert all(first_failure(frames, path) is not None for path in frames)
        return out
    loan = frames[9, 0]
    types = ("address", "uint256", "bytes")
    token, amount, _ = _static_then_bytes(types, _calldata(loan, "flashLoan", types))
    assert token == WBTC and loan.recipient == "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb"
    lender, borrower = loan.recipient, loan.sender
    assert movement(tree, (9, 0, 0), token, lender, borrower) == amount
    assert movement(tree, (9, 0, 2), token, borrower, lender) == amount
    excess = movement(tree, (10, 1), token, borrower, target)
    venue = frames[22,].recipient
    assert movement(tree, (22, 2, 0), token, target, venue) == excess
    wrapped = movement(tree, (22, 0), WETH, venue, target)
    natives = {
        p: f
        for p, f in frames.items()
        if f.kind == "CALL" and f.value and first_failure(frames, p) is None
    }
    assert set(natives) == {(), (24, 0), (25,), (26,)}
    assert (natives[24, 0].sender, natives[24, 0].recipient, natives[24, 0].value) == (
        WETH,
        target,
        wrapped,
    )
    direct, payout = natives[25,], natives[26,]
    assert (direct.sender, direct.recipient, payout.sender, payout.recipient) == (
        target,
        header["miner"],
        target,
        sender,
    )
    assert wrapped + root.value == direct.value + payout.value
    out.update(
        borrowed_wbtc_raw=amount,
        repaid_wbtc_raw=amount,
        wbtc_excess_sold_raw=excess,
        wrapped_proceeds_wei=wrapped,
        direct_payment_wei=direct.value,
        sender_eth_change_after_gas_scenario_wei=payout.value - root.value - charge,
    )
    return out


def main():
    scope, stop = read(STUDY / "scope.json"), read(STUDY / "STOP.json")
    assert digest(STUDY / "scope.json") == PLAN == stop["scope"]
    assert scope["source"] == SOURCE and scope["expected_tx"] == JULY_WITNESS
    assert scope["driver"] == digest(STUDY / "capture.py") and scope[
        "root_scope"
    ] == SCOPE == digest(ROOT / "scope.json")
    with RpcBudget(
        STUDY / "original.sqlite3", (BudgetPolicy("trace", 5, 150),), read_only=True
    ) as budget:
        assert budget.counts() == stop["usage"] == {"trace": 5} and stop["error"] is None
    chain, _ = decoded(STUDY, "chain", "eth_chainId", [], SOURCE)
    assert quantity(chain) == 1
    headers = []
    for n in (25584104, 25584105):
        h, _ = decoded(
            ROOT / "panels", f"{n}-head", "eth_getBlockByNumber", [hex(n), True], "header"
        )
        rows, _ = decoded(
            ROOT / "panels", f"{n}-rows", "eth_getBlockReceipts", [h["hash"]], "receipts"
        )
        block, _ = receipts_proof(h, rows, n, h["hash"])
        if not headers:
            tx = h["transactions"][122]
            rec = decode_receipt(rows[122], block.ref, JULY_WITNESS, 122)
            assert tx["hash"] == JULY_WITNESS
            fee = envelope_cost(tx, rec, quantity(h["baseFeePerGas"]))
        headers.append(h)
    parent, child = headers
    assert child["parentHash"] == parent["hash"]
    env = {
        k: tx[k]
        for k in (
            "chainId",
            "type",
            "nonce",
            "from",
            "to",
            "value",
            "gas",
            "maxFeePerGas",
            "maxPriorityFeePerGas",
            "accessList",
        )
    }
    env["data"] = tx["input"]
    assert sha256(canonical_json(env)).hexdigest() == scope["envelope"]
    trace_cfg = {"tracer": "callTracer", "tracerConfig": {"withLog": True}}
    landed, _ = decoded(
        ROOT / "trace", JULY_WITNESS, "debug_traceTransaction", [JULY_WITNESS, trace_cfg], "trace"
    )
    states = {}
    plan = [
        ("landed-pre", parent, 122),
        ("one-earlier", parent, 121),
        ("block-end", parent, None),
        ("child-end", child, None),
    ]
    assert scope["states"] == [[name, h["hash"], idx] for name, h, idx in plan]
    for name, h, idx in plan:
        cfg = trace_cfg | {"timeout": "10s"}
        if idx is not None:
            cfg["txIndex"] = hex(idx)
        tree, meta = decoded(STUDY, name, "debug_traceCall", [env, h["hash"], cfg], SOURCE)
        same = tree == landed
        outcome = economics(tree, env, h)
        if name == "landed-pre":
            assert same and outcome["gas_charge_scenario_wei"] == fee["gas_wei"]
        states[name] = {
            "index": idx,
            "height": quantity(h["number"]),
            "hash": h["hash"],
            "identical_landed_tree": same,
            "original_nonce_already_consumed": idx is None,
            "wire": meta,
            **outcome,
        }
    return {
        "schema": "oasis.y2026.july-historical-states.v1",
        "scope_sha256": PLAN,
        "envelope_sha256": scope["envelope"],
        "tx": JULY_WITNESS,
        "source": SOURCE,
        "stop": stop,
        "states": states,
        "original_nonce": quantity(env["nonce"]),
        "usd": scope["usd"],
        "child_time_delta_seconds": quantity(child["timestamp"]) - quantity(parent["timestamp"]),
    }


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("run without -O")
    print(json.dumps(main(), indent=2, sort_keys=True))
