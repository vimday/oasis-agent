"""Invented two-venue RPC vectors. No real deployment or market observations."""

import json
import sys
from pathlib import Path

from eth_hash.auto import keccak

from tests.fixtures.cycle_research import RUNTIME, RUNTIME_HASH, address, block_hash, call, data

USD, ETH, MP, MF, MQ, SENDER, RECIPIENT, VP, VF, VQ = map(address, range(1, 11))
IMMUTABLES = [
    MF,
    ETH,
    USD,
    1,
    10**12,
    1,
    1,
    1,
    address(20),
    -100,
    100,
    *([address(0)] * 7),
    *([0] * 6),
]
IMMUTABLE_ABI = [
    "address",
    "address",
    "address",
    "uint256",
    "uint256",
    "uint256",
    "uint256",
    "uint256",
    "address",
    "int256",
    "int256",
    *(["address"] * 7),
    *(["uint256"] * 6),
]
IMMUTABLE_RAW = data(IMMUTABLE_ABI, IMMUTABLES)
METRIC_SIG = "quoteLiveExactInSingle(address,address,address,bool,uint128,uint128,bytes)"
V3_SIG = "quoteExactInputSingle((address,address,uint256,uint24,uint160))"


def plan_data():
    return {
        "schema": "oasis-metric-v3-plan-v1",
        "scenario": "synthetic",
        "chain_id": 1,
        "sources": ["alpha", "beta"],
        "assets": [
            {"address": a, "symbol": s, "decimals": d, "runtime_hash": RUNTIME_HASH}
            for a, s, d in ((ETH, "SYNETH", 18), (USD, "SYNUSD", 6))
        ],
        "metric": {
            "pool": MP,
            "pool_hash": RUNTIME_HASH,
            "factory": MF,
            "factory_hash": RUNTIME_HASH,
            "quoter": MQ,
            "quoter_hash": RUNTIME_HASH,
            "token0": ETH,
            "token1": USD,
            "immutables_hash": "0x" + keccak(bytes.fromhex(IMMUTABLE_RAW[2:])).hex(),
            "sender": SENDER,
            "sender_hash": RUNTIME_HASH,
            "recipient": RECIPIENT,
            "extension_data": "0x1234",
        },
        "v3": {
            "name": "v3",
            "protocol": "v3",
            "address": VP,
            "runtime_hash": RUNTIME_HASH,
            "token0": USD,
            "token1": ETH,
            "factory": VF,
            "factory_hash": RUNTIME_HASH,
            "quoter": VQ,
            "quoter_hash": RUNTIME_HASH,
            "fee": 3000,
        },
        "starts": [
            {
                "token": a,
                "sizes_raw": sizes,
                "capital_raw": 1000 * sizes[0],
                "commitments_raw": 0,
                "reserve_raw": 0,
                "costs": [
                    {"kind": k, "raw": 1}
                    for k in ("gas", "builder", "financing", "failure_buffer", "opportunity")
                ],
            }
            for a, sizes in ((ETH, [10**18, 2 * 10**18]), (USD, [2000 * 10**6, 4000 * 10**6]))
        ],
        "window": {
            "name": "block_end",
            "block": 100,
            "expected_hash": block_hash(100),
            "cutoff_utc": "2026-01-01T00:00:30Z",
        },
        "max_queries": 128,
    }


def bundle(source):
    p = plan_data()
    records = []
    seen = set()
    pin = {"blockHash": block_hash(100), "requireCanonical": True}

    def add(method, params, result):
        key = json.dumps([method, params], sort_keys=True)
        if key in seen:
            return
        seen.add(key)
        records.append(
            {
                "endpoint": f"fixture://{source}",
                "fetched_utc": "2026-01-01T00:00:10Z",
                "request": {
                    "jsonrpc": "2.0",
                    "id": len(records),
                    "method": method,
                    "params": params,
                },
                "response": {"jsonrpc": "2.0", "id": len(records), "result": result},
            }
        )

    def getter(target, signature, types, values):
        add("eth_call", [{"to": target, "data": call(signature)}, pin], data(types, values))

    add("eth_chainId", [], "0x1")
    add(
        "eth_getBlockByNumber",
        ["0x64", False],
        {
            "number": "0x64",
            "hash": block_hash(100),
            "parentHash": block_hash(99),
            "timestamp": hex(1767225600),
        },
    )
    for target in (USD, ETH, MP, MF, MQ, SENDER, VP, VF, VQ):
        add("eth_getCode", [target, pin], RUNTIME)
    for target, d in ((ETH, 18), (USD, 6)):
        getter(target, "decimals()", ["uint8"], [d])
    getter(MP, "getImmutables()", IMMUTABLE_ABI, IMMUTABLES)
    for sig, t, v in (
        ("token0()", "address", USD),
        ("token1()", "address", ETH),
        ("factory()", "address", VF),
        ("fee()", "uint24", 3000),
    ):
        getter(VP, sig, [t], [v])
    getter(VQ, "factory()", ["address"], [VF])
    add(
        "eth_call",
        [
            {
                "to": VF,
                "data": call(
                    "getPool(address,address,uint24)",
                    ["address", "address", "uint24"],
                    [USD, ETH, 3000],
                ),
            },
            pin,
        ],
        data(["address"], [VP]),
    )
    for start in p["starts"]:
        for size in start["sizes_raw"]:
            for venues in (("metric", "v3"), ("v3", "metric")):
                amount, token = size, start["token"]
                for venue in venues:
                    other = USD if token == ETH else ETH
                    bid, ask = (2100, 2101) if venue == "metric" else (2000, 2001)
                    output = amount * bid // 10**12 if token == ETH else amount * 10**12 // ask
                    if venue == "metric":
                        target = MQ
                        payload = call(
                            METRIC_SIG,
                            [
                                "address",
                                "address",
                                "address",
                                "bool",
                                "uint128",
                                "uint128",
                                "bytes",
                            ],
                            [MP, SENDER, RECIPIENT, token == ETH, amount, 0, b"\x12\x34"],
                        )
                        result = data(["uint256", "uint256"], [amount, output])
                    else:
                        target = VQ
                        payload = call(
                            V3_SIG,
                            ["(address,address,uint256,uint24,uint160)"],
                            [(token, other, amount, 3000, 0)],
                        )
                        result = data(
                            ["uint256", "uint160", "uint32", "uint256"], [output, 2**96, 2, 100_000]
                        )
                    add("eth_call", [{"to": target, "data": payload}, pin], result)
                    amount, token = output, other
    return {"schema": "oasis-parsed-rpc-bundle-v1", "records": records}


def write(directory: Path):
    directory.mkdir()
    (directory / "plan.json").write_text(json.dumps(plan_data(), indent=2) + "\n")
    for source in ("alpha", "beta"):
        (directory / f"{source}.json").write_text(json.dumps(bundle(source), indent=2) + "\n")


if __name__ == "__main__":
    write(Path(sys.argv[1]))
