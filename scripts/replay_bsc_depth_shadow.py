"""Offline replay of the BSC Pancake v3 sequential-depth control. No RPC or signer."""

import argparse
import hashlib
import json
from datetime import datetime
from fractions import Fraction
from pathlib import Path

from eth_abi.abi import decode
from eth_hash.auto import keccak

DEFAULT = Path("reports/research/multichain-forward-20261122/bsc-router-depth")
SOURCE = "https://bsc-dataseed.bnbchain.org"
OBSERVER = "0x0000000000000000000000000000000a115de017"
WRAPPED = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
RUNTIME_HASH = "142de2926c05d2737c1bc876aade42d3c6b266715bb513181b16e8ffcfd7ecec"
ROUTER_HASH = "61002a11263b2c38ff8d5ded90a655987ed98f171a31840ee0014dd7052bccda"
AMOUNT = 10**16
DEPTHS = (3, 30, 102)


def require(ok, message):
    if not ok:
        raise ValueError(message)


def read_wire(folder, report):
    lines = [json.loads(row) for row in (folder / "ledger.jsonl").read_text().splitlines()]
    scope, *exchanges, stop = lines
    require(scope == report["scope"] and scope["endpoint"] == SOURCE, "source or scope mismatch")
    require(scope["chain"] == 56 and scope["cost_usd"] == "0", "chain or cost mismatch")
    require(report["status"] == stop["status"] == "complete", "incomplete control")
    count = stop["requests"]
    require(count == report["requests"] <= scope["max_calls"] <= 32, "budget mismatch")
    require(len(exchanges) == count * 2, "missing requests or receipts")
    wire = {}
    for idx in range(1, count + 1):
        paid, got = exchanges[2 * idx - 2 : 2 * idx]
        require(paid["action"] == "reserve" and got["action"] == "receive", "missing reservation")
        require(paid["usd"] == "0" and got["http"] == 200, "unexpected cost or HTTP failure")
        req = (folder / f"{idx:02}.request.json").read_bytes()
        res = (folder / f"{idx:02}.response.json").read_bytes()
        require(hashlib.sha256(req).hexdigest() == paid["request_sha256"], "request hash mismatch")
        require(hashlib.sha256(res).hexdigest() == got["response_sha256"], "reply hash mismatch")
        request, response = json.loads(req), json.loads(res)
        require(idx == paid["id"] == got["id"] == request["id"] == response["id"], "ID mismatch")
        require(request["method"] == paid["method"], "method mismatch")
        require(
            datetime.fromisoformat(paid["utc"]) <= datetime.fromisoformat(got["utc"]),
            "RPC received before reservation",
        )
        wire[idx] = (request, response["result"], got["utc"])
    require(wire[1][0]["method"] == "eth_chainId" and wire[1][1] == "0x38", "chain mismatch")
    require(
        wire[3][0]["method"] == "eth_getCode"
        and keccak(bytes.fromhex(wire[3][1][2:])).hex() == ROUTER_HASH,
        "router identity",
    )
    require(wire[4][0]["params"][0] == OBSERVER and wire[4][1] == "0x", "overlay collision")
    return wire


def verify_trial(trial, depth, wire):
    require(trial["depth"] == depth, "missing or extra depth arm")
    order = trial["order"]
    require(
        len(order) == 4 and len(set(order)) == 3 and order[0] == order[-1] == WRAPPED,
        "unexpected cycle",
    )
    cycle, product = b"", Fraction(1)
    for a, b, pool in zip(order[:-1], order[1:], trial["pools"], strict=True):
        require(sorted((a, b)) == [pool["t0"], pool["t1"]], "direction mismatch")
        require(pool["liq"] > 0 and 0 < pool["fee"] < 10**6, "pool liquidity or fee")
        price = Fraction(pool["sqrt"] ** 2, 2**192)
        product *= (price if a == pool["t0"] else 1 / price) * Fraction(10**6 - pool["fee"], 10**6)
        cycle += pool["fee"].to_bytes(3, "big") + bytes.fromhex(b[2:])
    expected_path = bytes.fromhex(WRAPPED[2:]) + cycle * (depth // 3)
    bound = int(AMOUNT * product ** (depth // 3)) - AMOUNT
    require(bound == int(trial["detection_before_gas_upper_wei"]), "marginal bound mismatch")

    original = None
    first = trial["at_detection"]["rpc_id"]
    price = int(wire[first - 2][1], 16)
    require(
        wire[first - 2][0]["method"] == "eth_gasPrice" and price == int(trial["gas_price_wei"]),
        "gas reference mismatch",
    )
    # Decode the pinned state batch and check the selected pool records against its original reply.
    state_request, state_response, _ = wire[first - 1]
    require(
        state_request["params"][1]
        == {"blockHash": trial["head"]["hash"], "requireCanonical": True},
        "state pin mismatch",
    )
    packed = bytes.fromhex(state_request["params"][0]["data"][2:])
    require(packed[:4] == keccak(b"aggregate3((address,bool,bytes)[])")[:4], "state selector")
    (queries,) = decode(["(address,bool,bytes)[]"], packed[4:])
    (answers,) = decode(["(bool,bytes)[]"], bytes.fromhex(state_response[2:]))
    values = {
        (target, selector): raw
        for (target, _, selector), (ok, raw) in zip(queries, answers, strict=True)
        if ok
    }
    for pool in trial["pools"]:
        for sig, key, abi_type in (
            ("token0()", "t0", "address"),
            ("token1()", "t1", "address"),
            ("fee()", "fee", "uint24"),
            ("liquidity()", "liq", "uint128"),
            ("slot0()", "sqrt", "uint160"),
        ):
            raw = values[pool["pool"], keccak(sig.encode())[:4]]
            require(decode([abi_type], raw)[0] == pool[key], "pool state mismatch")

    for stage in ("at_detection", "after_sim"):
        row = trial[stage]
        idx = row["rpc_id"]
        request, result, received = wire[idx]
        require(request["method"] == "eth_call", "expected read-only simulation")
        tx, pin, state = request["params"]
        header = row["head"]
        require(pin == {"blockHash": header["hash"], "requireCanonical": True}, "wrong EVM pin")
        head_idx = first - 3 if stage == "at_detection" else idx - 1
        source = wire[head_idx]
        require(
            source[0]["params"] == ["latest", False]
            and all(source[1][k] == v for k, v in header.items()),
            "head is not the observed latest",
        )
        if stage == "after_sim":
            require(wire[first][2] <= source[2] and idx == first + 2, "chronology mismatch")
        require(tx["to"] == OBSERVER and list(state) == [OBSERVER], "unexpected observer")
        overlay = state[OBSERVER]
        require(
            int(overlay["balance"], 16) == AMOUNT
            and keccak(bytes.fromhex(overlay["code"][2:])).hex() == RUNTIME_HASH,
            "wrong observer runtime or funding",
        )
        if original is None:
            original = (tx, state)
        require(original == (tx, state), "changed simulated transaction")
        calldata = bytes.fromhex(tx["data"][2:])
        require(calldata[:4] == keccak(b"run(bytes,uint256,uint256)")[:4], "selector mismatch")
        path, funded, deadline = decode(["bytes", "uint256", "uint256"], calldata[4:])
        require(
            path == expected_path and funded == AMOUNT and deadline >= int(header["timestamp"], 16),
            "incorrect path, amount or deadline",
        )
        intrinsic = 21000 + sum(4 if byte == 0 else 16 for byte in calldata)
        out, credited, gas = decode(["uint256", "uint256", "uint256"], bytes.fromhex(result[2:]))
        require(out == credited == int(row["output_wei"]) == int(row["credit_wei"]), "cash delta")
        require(gas == row["gas_interval"] and intrinsic == trial["intrinsic_legacy"], "gas")
        gross = out - AMOUNT
        require(
            gross == int(row["before_gas_wei"])
            and gross - (gas + intrinsic) * price == int(row["modeled_net_wei"]),
            "net mismatch",
        )
        require(
            datetime.fromisoformat(received) <= datetime.fromisoformat(row["completed_utc"]),
            "completed before RPC returned",
        )
        if stage == "at_detection":
            require(header == trial["head"] and gross <= bound, "initial bound violation")
        else:
            require(header["parentHash"] == trial["head"]["hash"], "expected observed child block")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=DEFAULT)
    args = parser.parse_args()
    report = json.loads((args.directory / "result.json").read_bytes())
    wire = read_wire(args.directory, report)
    for trial, depth in zip(report["trials"], DEPTHS, strict=True):
        verify_trial(trial, depth, wire)
    print("Verified 30 RPC exchanges and six whole-route simulations over 3, 30 and 102 hops.")


if __name__ == "__main__":
    main()
