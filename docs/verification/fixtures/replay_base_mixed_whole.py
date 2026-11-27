"""Replay two fixed Base cycle rotations on hash-bound mainnet state, unsigned."""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import rlp
from eth_hash.auto import keccak
from replay_base_policy_natural import ROOT
from run_base_prior_head_policy import (
    ORACLE,
    USDC,
    WETH,
    indexed,
    read,
    write,
)
from run_base_prior_head_policy import (
    SENDER as ACTOR,
)

from oasis_agent.arbitrage.base_survival import decode_base_simulation
from oasis_agent.arbitrage.cycle_protocols import abi, calldata, data_bytes, topic
from oasis_agent.evidence.rpc_validation import rpc_quantity
from oasis_agent.fastpath.base_mixed_snapshot import _header
from oasis_agent.fastpath.multicall_universe import (
    MULTICALL3,
    UNIV3_FACTORY,
    _encode_aggregate3_calldata,
)
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts.measure_robinhood_quote_delivery import _body, _fetch, _read

DIR = ROOT / "reports/research/base-mixed-whole-20261128"
NATURAL = ROOT / "docs/verification/BASE_PRIOR_HEAD_POLICY_NATURAL_20261128.json"
SOURCE = Path(__file__).with_name("BaseMixedWholeProbe.sol")
ENDPOINTS = {
    "official": "https://mainnet.base.org",
    "state": "https://base-rpc.publicnode.com",
}
POLICIES = (BudgetPolicy("official", 10, 250), BudgetPolicy("state", 15, 250))
HEIGHT = 63043739
CHAIN = 8453
GAS = 550_000
TIP = 2_000_000
CAP = 250_000_000
HARNESS = "0x" + "00" * 18 + "a257"
UNI = "0xb4cb800910b228ed3d0834cf79d697127bbb00e5"
AERO = "0xb2cc224c1c9fee385f8ad6a55b4d94e92359dc59"
AERO_FACTORY = "0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a"
USDC_FIRST_BALANCE = (
    "0x" + keccak(bytes.fromhex(HARNESS[2:].rjust(64, "0") + format(9, "064x"))).hex()
)
AMOUNTS = {WETH: 67_280_448_940_895_129, USDC: 112_819_960}


def digest(path):
    return sha256_hex(path.read_bytes())


def envelope(token):
    amount = AMOUNTS[token]
    return {
        "from": ACTOR,
        "to": HARNESS,
        "value": hex(amount if token == WETH else 0),
        "nonce": "0x0",
        "gas": hex(GAS),
        "maxFeePerGas": hex(CAP),
        "maxPriorityFeePerGas": hex(TIP),
        "accessList": [],
        "data": calldata(
            "probe(bool,uint256,uint256)",
            ("bool", "uint256", "uint256"),
            (token == USDC, amount, 1),
        ),
    }


def simulation(number, runtime, token):
    overrides = {HARNESS: {"code": runtime}}
    if token == USDC:
        overrides[USDC] = {"stateDiff": {USDC_FIRST_BALANCE: f"0x{AMOUNTS[USDC]:064x}"}}
    return [
        {
            "blockStateCalls": [{"calls": [envelope(token)], "stateOverrides": overrides}],
            "traceTransfers": False,
            "validation": True,
        },
        hex(number),
    ]


def unsigned_size(env):
    return len(
        b"\x02"
        + rlp.encode(
            [
                CHAIN,
                0,
                TIP,
                CAP,
                GAS,
                data_bytes(HARNESS),
                rpc_quantity(env["value"]),
                data_bytes(env["data"]),
                [],
            ]
        )
    )


def state_params(pin):
    specs = [
        (MULTICALL3, "getChainId()", (), (), ("uint256",)),
        (MULTICALL3, "getEthBalance(address)", ("address",), (ACTOR,), ("uint256",)),
        (WETH, "balanceOf(address)", ("address",), (HARNESS,), ("uint256",)),
        (USDC, "balanceOf(address)", ("address",), (HARNESS,), ("uint256",)),
        (WETH, "decimals()", (), (), ("uint8",)),
        (USDC, "decimals()", (), (), ("uint8",)),
        (UNI, "token0()", (), (), ("address",)),
        (UNI, "token1()", (), (), ("address",)),
        (AERO, "token0()", (), (), ("address",)),
        (AERO, "token1()", (), (), ("address",)),
        (UNI, "factory()", (), (), ("address",)),
        (AERO, "factory()", (), (), ("address",)),
        (
            UNIV3_FACTORY,
            "getPool(address,address,uint24)",
            ("address", "address", "uint24"),
            (WETH, USDC, 100),
            ("address",),
        ),
        (
            AERO_FACTORY,
            "getPool(address,address,int24)",
            ("address", "address", "int24"),
            (WETH, USDC, 100),
            ("address",),
        ),
        (ORACLE, "getOperatorFee(uint256)", ("uint256",), (GAS,), ("uint256",)),
    ]
    specs.extend(
        (
            ORACLE,
            "getL1FeeUpperBound(uint256)",
            ("uint256",),
            (unsigned_size(envelope(token)),),
            ("uint256",),
        )
        for token in AMOUNTS
    )
    rows = [
        (to, False, data_bytes(calldata(signature, types, args)))
        for to, signature, types, args, _ in specs
    ]
    return [{"to": MULTICALL3, "data": _encode_aggregate3_calldata(rows)}, pin], [
        s[-1] for s in specs
    ]


def bound_state(raw, returns):
    (items,) = abi(("(bool,bytes)[]",), data_bytes(raw))
    vals = [abi(typ, data)[0] for (ok, data), typ in zip(items, returns, strict=True) if ok]
    chain, balance, weth, usdc, wd, ud, u0, u1, a0, a1, uf, af, up, ap, op, l1w, l1u = vals
    assert (chain, weth, usdc, wd, ud) == (CHAIN, 0, 0, 18, 6)
    assert (u0, u1, a0, a1, uf, af, up, ap) == (
        WETH,
        USDC,
        WETH,
        USDC,
        UNIV3_FACTORY,
        AERO_FACTORY,
        UNI,
        AERO,
    ), "fee tier, spacing or pool identity disagree"
    assert balance >= AMOUNTS[WETH] + CAP * GAS + op + max(l1w, l1u)
    return {
        "actor_eth_balance": balance,
        "operator_upper_wei": op,
        "l1_upper_wei": {WETH: l1w, USDC: l1u},
    }


def movement(logs, token, signature, addresses, amount):
    topics = [topic(signature), *(indexed(address) for address in addresses)]
    [log] = [log for log in logs if (log["address"], log["topics"]) == (token, topics)]
    assert (amount,) == abi(("uint256",), data_bytes(log["data"]))
    return rpc_quantity(log["logIndex"])


def settlement(raw, block, state, token, expected=None):
    out = decode_base_simulation(
        raw,
        parent_number=block.number,
        parent_hash=block.block_hash,
        recipient=ACTOR,
        gas_limit=GAS,
    )
    assert out.base_fee_wei_per_gas <= CAP
    paid_fee = out.gas_used * min(CAP, out.base_fee_wei_per_gas + TIP)
    network = paid_fee + state["l1_upper_wei"][token] + state["operator_upper_wei"]
    result = {
        "gas_used": out.gas_used,
        "simulated_base_fee": out.base_fee_wei_per_gas,
        "chain_fee_scenario_wei": network,
        "l2_wei": paid_fee,
    }
    amount = AMOUNTS[token]
    if not out.success:
        return result | {
            "status": "reverted",
            "revert_selector": out.return_data[:4].hex(),
            "settlement_delta_raw": 0,
            "conditional_net_raw": -network if token == WETH else None,
        }
    middle, received = abi(("uint256", "uint256"), out.return_data)
    if expected is not None:
        assert (middle, received) == tuple(expected), (
            "same-state whole call differs from both retained quotes"
        )
    other = USDC if token == WETH else WETH
    pool1, pool2 = (UNI, AERO) if token == WETH else (AERO, UNI)
    logs = raw[0]["calls"][0]["logs"]
    sig = "Transfer(address,address,uint256)"
    if token == WETH:
        assert out.weth_delta == received
        mint = [movement(logs, WETH, "Deposit(address,uint256)", (HARNESS,), amount)]
    else:
        assert out.weth_delta == 0
        mint = []
    flows = [
        movement(logs, other, sig, (pool1, HARNESS), middle),
        movement(logs, token, sig, (HARNESS, pool1), amount),
        movement(logs, token, sig, (pool2, HARNESS), received),
        movement(logs, other, sig, (HARNESS, pool2), middle),
        movement(logs, token, sig, (HARNESS, ACTOR), received),
    ]
    positions = mint + flows
    assert positions == sorted(set(positions)), "noncanonical callback delivery order"
    fee_converted = network if token == WETH else (network * amount + middle - 1) // middle
    return result | {
        "status": "settled",
        "principal_raw": amount,
        "middle_raw": middle,
        "final_received_raw": received,
        "settlement_delta_raw": received - amount,
        "conditional_net_raw": received - amount - fee_converted,
        "conversion_basis": "ETH and WETH par"
        if token == WETH
        else "USDC/WETH first-leg implied rate",
        "charged_fee_in_settlement_smallest_units": fee_converted,
        "payment_log_indices": positions,
    }


def build_plan(root, frozen):
    panel = read(NATURAL)
    row = next(item for item in panel["heads"] if item["block"] == HEIGHT)
    candidates = row["normal_ranked_candidates"]
    assert {q["base_token"]: int(q["input_raw"]) for q in candidates} == AMOUNTS
    runtime = data_bytes("0x" + (root / "BaseMixedWholeProbe.bin-runtime").read_text().strip())
    return {
        "schema": "oasis-base-mixed-whole-fixture-v1",
        "chain_id": CHAIN,
        "frozen_utc": frozen,
        "parent_heights": [HEIGHT, HEIGHT + 1, HEIGHT - 1],
        "h": row["block_hash"],
        "p": row["parent_hash"],
        "retained_natural_report_sha256": digest(NATURAL),
        "runtime_keccak": "0x" + keccak(runtime).hex(),
        "runtime_sha256": sha256_hex(runtime),
        "probe_source_sha256": digest(SOURCE),
        "driver_sha256": digest(Path(__file__)),
        "quoted_h": {q["base_token"]: q["retained_hop_outputs_raw"] for q in candidates},
        "envelopes": {token: envelope(token) for token in AMOUNTS},
        "providers": ENDPOINTS,
        "limits": {item.source: item.max_requests for item in POLICIES},
        "usd": 0,
        "code_override": HARNESS,
        "usdc_inventory_slot": USDC_FIRST_BALANCE,
    }


def run(root, capture):
    scope = read(root / "scope.json")
    assert scope == build_plan(root, scope["frozen_utc"])
    ledger = root / "original.sqlite3"
    if capture and ledger.exists():
        raise ValueError("already attempted original scope")
    runtime = "0x" + (root / "BaseMixedWholeProbe.bin-runtime").read_text().strip()
    rows, certs, refs = [], {}, {}
    failure = None
    number = 0
    with (
        RpcBudget(ledger, POLICIES, read_only=not capture) as budget,
        httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client,
    ):

        def ask(source, method, params):
            nonlocal number
            number += 1
            prefix = root / f"{number:02}-{source}"
            request = _body(number, method, params)
            if capture:
                wait = budget.reserve(source, now_ms=time.time_ns() // 1_000_000)
                if wait:
                    time.sleep((wait + 1) / 1000)
                    if budget.reserve(source, now_ms=time.time_ns() // 1_000_000):
                        raise ValueError("RPC pacing not resolved")
                _fetch(client, ENDPOINTS[source], prefix, request)
            result, _ = _read(prefix, request, number, 1)
            refs[prefix.name] = read(prefix.with_suffix(".meta"))
            return result

        try:
            for source in ENDPOINTS:
                assert rpc_quantity(ask(source, "eth_chainId", [])) == CHAIN
            origin = None
            for height in scope["parent_heights"]:
                headers = [
                    ask(source, "eth_getBlockByNumber", [hex(height), False])
                    for source in ENDPOINTS
                ]
                left, right = [
                    _header(canonical_json({"jsonrpc": "2.0", "id": 1, "result": hdr}))
                    for hdr in headers
                ]
                assert left == right and left.number == height
                if origin is None:
                    assert (left.block_hash, left.parent_hash) == (scope["h"], scope["p"])
                    origin = left
                elif height == HEIGHT + 1:
                    assert left.parent_hash == origin.block_hash
                else:
                    assert left.block_hash == origin.parent_hash
                pin = {"blockHash": left.block_hash, "requireCanonical": True}
                if height == HEIGHT:
                    for contract in (HARNESS, UNI, AERO, WETH, USDC):
                        code = data_bytes(ask("state", "eth_getCode", [contract, pin]))
                        assert bool(code) == (contract != HARNESS), (
                            "unexpected fixture/runtime code"
                        )
                        certs[contract] = "0x" + keccak(code).hex()
                    assert rpc_quantity(ask("state", "eth_getTransactionCount", [ACTOR, pin])) == 0
                params, returns = state_params(pin)
                state = bound_state(ask("state", "eth_call", params), returns)
                for token in AMOUNTS:
                    result = ask("official", "eth_simulateV1", simulation(height, runtime, token))
                    expected = scope["quoted_h"][token] if height == HEIGHT else None
                    observed = settlement(result, left, state, token, expected)
                    rows.append(
                        {
                            "height": height,
                            "parent_hash": left.block_hash,
                            "token": token,
                            **state,
                            **observed,
                        }
                    )
        except (AssertionError, ValueError, OSError, httpx.HTTPError) as exc:
            failure = {"at_request": number, "error_type": type(exc).__name__, "details": str(exc)}
            if number and (meta := prefix_failure(root, number)):
                failure["response_proof"] = meta
        counts = budget.counts()
    return {
        "scope": scope,
        "scope_sha256": digest(root / "scope.json"),
        "stopped": failure,
        "rows": rows,
        "canonical_runtime_hashes": certs,
        "exchanges": refs,
        "request_counts": counts,
        "execution_eligible": False,
        "actual_paid_wei": None,
    }


def prefix_failure(root, number):
    for source in ENDPOINTS:
        path = root / f"{number:02}-{source}.meta"
        if path.exists():
            return read(path)
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "capture", "replay"))
    parser.add_argument("--root", type=Path, default=DIR)
    args = parser.parse_args()
    if args.action == "prepare":
        write(args.root / "scope.json", build_plan(args.root, datetime.now(UTC).isoformat()))
    else:
        result = run(args.root, args.action == "capture")
        if args.action == "capture":
            write(args.root / "STOP.json", result["stopped"])
        print(json.dumps(result, indent=2, sort_keys=True))
