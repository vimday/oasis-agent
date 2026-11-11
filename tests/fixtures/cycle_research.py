"""Generate synthetic RPC exports with an explicit three-block counterfactual."""

import argparse
import json
from pathlib import Path

from eth_abi.abi import encode
from eth_utils import keccak


def address(n):
    return f"0x{n:040x}"


def block_hash(n):
    return f"0x{n:064x}"


def data(types, values):
    return "0x" + encode(types, values).hex()


def topic(signature):
    return "0x" + keccak(text=signature).hex()


def call(signature, types=(), values=()):
    return topic(signature)[:10] + data(types, values)[2:]


NATIVE, USDC, WRAPPED, KNTQ = map(address, range(4))
MANAGER, V3, SOLID, FACTORY3, FACTORY_SOLID, QUOTER4, QUOTER3, ROUTER, EXECUTOR, USER, FEE_VAULT = (
    map(address, range(10, 21))
)
RUNTIME = "0x600160005260206000f3"
RUNTIME_HASH = "0x" + keccak(hexstr=RUNTIME).hex()
POOL_KEY = (NATIVE, KNTQ, 3000, 60, NATIVE)
KEY_ABI = "(address,address,uint24,int24,address)"
POOL_ID = "0x" + keccak(encode([KEY_ABI], [POOL_KEY])).hex()
CHAIN = 87654321
HEIGHTS = (100, 101, 102)
TIMESTAMP = 1767225600
DAYS = ["2026-01-01T00:00:01Z", "2026-01-01T00:00:02Z", "2026-01-01T00:00:03Z"]
SIZES = (1000, 2000, 10000)
TRIGGER, WINNER = block_hash(1001), block_hash(1002)
COSTS = {"gas": 20, "builder": 10, "protocol": 7, "financing": 3}
TRANSFER = "Transfer(address,address,uint256)"
SOLIDLY_SWAP = "Swap(address,uint256,uint256,uint256,uint256,address)"
SOLIDLY_SYNC = "Sync(uint256,uint256)"


def plan():
    def asset(addr, name, decimals):
        return {
            "address": addr,
            "symbol": name,
            "decimals": decimals,
            "runtime_hash": RUNTIME_HASH if addr else None,
        }

    return {
        "schema": "oasis-cycle-plan-v1",
        "scenario": "synthetic",
        "chain_id": CHAIN,
        "sources": ["synthetic"],
        "assets": [
            asset(None, "HYPE", 18),
            asset(KNTQ, "KNTQ", 18),
            asset(USDC, "USDC", 6),
            asset(WRAPPED, "WHYPE", 18),
        ],
        "wrapped_native": WRAPPED,
        "pools": [
            {
                "name": "first",
                "protocol": "v4",
                "address": MANAGER,
                "token0": NATIVE,
                "token1": KNTQ,
                "pool_id": POOL_ID,
                "fee": 3000,
                "tick_spacing": 60,
                "hooks": NATIVE,
                "quoter": QUOTER4,
                "quoter_hash": RUNTIME_HASH,
                "runtime_hash": RUNTIME_HASH,
            },
            {
                "name": "second",
                "protocol": "v3",
                "address": V3,
                "token0": USDC,
                "token1": KNTQ,
                "fee": 3000,
                "quoter": QUOTER3,
                "quoter_hash": RUNTIME_HASH,
                "factory": FACTORY3,
                "factory_hash": RUNTIME_HASH,
                "runtime_hash": RUNTIME_HASH,
            },
            {
                "name": "third",
                "protocol": "solidly_volatile",
                "address": SOLID,
                "token0": USDC,
                "token1": WRAPPED,
                "factory": FACTORY_SOLID,
                "factory_hash": RUNTIME_HASH,
                "runtime_hash": RUNTIME_HASH,
            },
        ],
        "windows": [
            {
                "name": name,
                "block": height,
                "expected_hash": block_hash(height),
                "cutoff_utc": cutoff,
            }
            for name, height, cutoff in zip(
                ("before", "imbalance", "after_competitor"),
                HEIGHTS,
                DAYS,
                strict=True,
            )
        ],
        "sizes_raw": list(SIZES),
        "inventory_raw": 20_000,
        "gas_reserve_raw": 40,
        "commitments_raw": 100,
        "anomaly_ratio_bps": 20_000,
        "max_points": 16,
        "max_queries_per_window": 96,
        "costs": [
            {"kind": kind, "raw": raw, "status": "estimate", "source": "synthetic_cost_rule"}
            for kind, raw in COSTS.items()
        ],
        "case": {
            "trigger_hash": TRIGGER,
            "trigger_block": 101,
            "trigger_index": 0,
            "router": ROUTER,
            "router_hash": RUNTIME_HASH,
            "winner_hash": WINNER,
            "winner_block": 102,
            "winner_index": 1,
            "sender": USER,
            "executor": EXECUTOR,
            "executor_hash": RUNTIME_HASH,
            "route": ["first", "second", "third"],
            "prefix_system_type": 106,
        },
    }


def _volatile(amount, reserve_in, reserve_out):
    # Fixture hypothesis uses a classic volatile curve with fee removed from the input.
    fee = amount * 30 // 10_000
    net = amount - fee
    return net * reserve_out // (reserve_in + net), fee


BEFORE = (100_000, 10_000)
TRIGGER_INPUT = 90_000
TRIGGER_OUTPUT, TRIGGER_FEE = _volatile(TRIGGER_INPUT, BEFORE[1], BEFORE[0])
IMBALANCE = (BEFORE[0] - TRIGGER_OUTPUT, BEFORE[1] + TRIGGER_INPUT - TRIGGER_FEE)
WINNER_PRINCIPAL = 50_000
WINNER_OUTPUT, WINNER_FEE = _volatile(WINNER_PRINCIPAL * 3, *IMBALANCE)
AFTER = (IMBALANCE[0] + WINNER_PRINCIPAL * 3 - WINNER_FEE, IMBALANCE[1] - WINNER_OUTPUT)


def _header(height):
    return {
        "number": hex(height),
        "hash": block_hash(height),
        "parentHash": block_hash(height - 1),
        "timestamp": hex(TIMESTAMP),
    }


def _position(height, index):
    return {
        "blockNumber": hex(height),
        "blockHash": block_hash(height),
        "transactionIndex": hex(index),
    }


def _transaction(height, index, tx_hash, sender, to, input_data="0x", tx_type=2):
    return {
        "hash": tx_hash,
        **_position(height, index),
        "from": sender,
        "to": to,
        "input": input_data,
        "value": "0x0",
        "chainId": hex(CHAIN),
        "type": hex(tx_type),
    }


def _event(target, signature, indexed, types, values):
    return {"address": target, "topics": [topic(signature), *indexed], "data": data(types, values)}


def _addresses(*addresses):
    return tuple(data(["address"], [addr]) for addr in addresses)


def _transfer(token, sender, recipient, amount):
    return _event(token, TRANSFER, _addresses(sender, recipient), ["uint256"], [amount])


def _receipt(tx, events):
    position = {key: tx[key] for key in ("blockHash", "blockNumber", "transactionIndex")}
    return {
        **position,
        "transactionHash": tx["hash"],
        "status": "0x1",
        "from": tx["from"],
        "to": tx["to"],
        "gasUsed": "0x7",
        "effectiveGasPrice": "0xb",
        "logs": [
            {**position, "transactionHash": tx["hash"], "logIndex": hex(index), **event}
            for index, event in enumerate(events)
        ],
    }


def _trigger():
    min_out = TRIGGER_OUTPUT - 150
    path = [(WRAPPED, USDC, False)]
    tx = _transaction(
        101,
        0,
        TRIGGER,
        USER,
        ROUTER,
        call(
            "swapExactTokensForTokens(uint256,uint256,(address,address,bool)[],address,uint256)",
            ("uint256", "uint256", "(address,address,bool)[]", "address", "uint256"),
            (TRIGGER_INPUT, min_out, path, USER, TIMESTAMP + 1),
        ),
    )
    return tx, _receipt(
        tx,
        [
            _transfer(WRAPPED, USER, SOLID, TRIGGER_INPUT),
            _transfer(USDC, SOLID, USER, TRIGGER_OUTPUT),
            _transfer(WRAPPED, SOLID, FEE_VAULT, TRIGGER_FEE),
            _event(
                SOLID,
                "Fees(address,uint256,uint256)",
                _addresses(ROUTER),
                ["uint256"] * 2,
                [0, TRIGGER_FEE],
            ),
            _event(SOLID, SOLIDLY_SYNC, [], ["uint256"] * 2, IMBALANCE),
            _event(
                SOLID,
                SOLIDLY_SWAP,
                _addresses(ROUTER, USER),
                ["uint256"] * 4,
                [0, TRIGGER_INPUT, TRIGGER_OUTPUT, 0],
            ),
        ],
    )


def _winner():
    amount1, amount2 = WINNER_PRINCIPAL * 2, WINNER_PRINCIPAL * 3
    tx = _transaction(102, 1, WINNER, USER, EXECUTOR)
    return tx, _receipt(
        tx,
        [
            _event(
                MANAGER,
                "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)",
                (POOL_ID, *_addresses(EXECUTOR)),
                ["int128", "int128", "uint160", "uint128", "int24", "uint24"],
                [-WINNER_PRINCIPAL, amount1, 1 << 96, 123, 0, 3000],
            ),
            _transfer(KNTQ, MANAGER, EXECUTOR, amount1),
            _transfer(KNTQ, EXECUTOR, V3, amount1),
            _transfer(USDC, V3, EXECUTOR, amount2),
            _event(
                V3,
                "Swap(address,address,int256,int256,uint160,uint128,int24)",
                _addresses(EXECUTOR, EXECUTOR),
                ["int256", "int256", "uint160", "uint128", "int24"],
                [-amount2, amount1, 1 << 96, 111, 0],
            ),
            _transfer(USDC, EXECUTOR, SOLID, amount2),
            _transfer(WRAPPED, SOLID, EXECUTOR, WINNER_OUTPUT),
            _transfer(USDC, SOLID, FEE_VAULT, WINNER_FEE),
            _event(
                SOLID,
                "Fees(address,uint256,uint256)",
                _addresses(EXECUTOR),
                ["uint256"] * 2,
                [WINNER_FEE, 0],
            ),
            _event(SOLID, SOLIDLY_SYNC, [], ["uint256"] * 2, AFTER),
            _event(
                SOLID,
                SOLIDLY_SWAP,
                _addresses(EXECUTOR, EXECUTOR),
                ["uint256"] * 4,
                [amount2, 0, 0, WINNER_OUTPUT],
            ),
            _event(
                WRAPPED,
                "Withdrawal(address,uint256)",
                _addresses(EXECUTOR),
                ["uint256"],
                [WINNER_OUTPUT],
            ),
        ],
    )


def records():
    result = []
    for height, state, fetched in zip(HEIGHTS, (BEFORE, IMBALANCE, AFTER), DAYS, strict=True):
        pin = {"blockHash": block_hash(height), "requireCanonical": True}

        def append(method, params, reply, fetched=fetched):
            req_id = len(result) + 1
            result.append(
                {
                    "endpoint": "synthetic.invalid",
                    "fetched_utc": fetched,
                    "request": {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params},
                    "response": {"jsonrpc": "2.0", "id": req_id, "result": reply},
                }
            )

        def contract(target, query, types, values, pin=pin, append=append):
            append("eth_call", [{"to": target, "data": query}, pin], data(types, values))

        if height == HEIGHTS[0]:
            append("eth_chainId", [], hex(CHAIN))
        append("eth_getBlockByNumber", [hex(height), False], _header(height))
        for target in (
            USDC,
            KNTQ,
            WRAPPED,
            MANAGER,
            V3,
            SOLID,
            FACTORY3,
            FACTORY_SOLID,
            QUOTER3,
            QUOTER4,
            ROUTER,
            EXECUTOR,
        ):
            append("eth_getCode", [target, pin], RUNTIME)
        for token, decimals in ((USDC, 6), (KNTQ, 18), (WRAPPED, 18)):
            contract(token, call("decimals()"), ["uint8"], [decimals])
        for pool, token1, factory in ((V3, KNTQ, FACTORY3), (SOLID, WRAPPED, FACTORY_SOLID)):
            for getter, value in (("token0()", USDC), ("token1()", token1), ("factory()", factory)):
                contract(pool, call(getter), ["address"], [value])
        contract(V3, call("fee()"), ["uint24"], [3000])
        contract(
            FACTORY3,
            call(
                "getPool(address,address,uint24)",
                ("address", "address", "uint24"),
                (USDC, KNTQ, 3000),
            ),
            ["address"],
            [V3],
        )
        contract(QUOTER3, call("factory()"), ["address"], [FACTORY3])
        contract(QUOTER4, call("poolManager()"), ["address"], [MANAGER])
        contract(SOLID, call("stable()"), ["bool"], [False])
        contract(SOLID, call("fees()"), ["address"], [FEE_VAULT])
        contract(SOLID, call("getReserves()"), ["uint256"] * 3, [*state, TIMESTAMP])
        for q in SIZES:
            v4_call = call(
                "quoteExactInputSingle(((address,address,uint24,int24,address),bool,"
                "uint128,bytes))",
                (f"({KEY_ABI},bool,uint128,bytes)",),
                ((POOL_KEY, True, q, b""),),
            )
            contract(QUOTER4, v4_call, ["uint256"] * 2, [q * 2, 180])
            v3_call = call(
                "quoteExactInputSingle((address,address,uint256,uint24,uint160))",
                ("(address,address,uint256,uint24,uint160)",),
                ((KNTQ, USDC, q * 2, 3000, 0),),
            )
            contract(
                QUOTER3,
                v3_call,
                ["uint256", "uint160", "uint32", "uint256"],
                [q * 3, 1 << 96, 0, 220],
            )
            solid = call("getAmountOut(uint256,address)", ("uint256", "address"), (q * 3, USDC))
            contract(SOLID, solid, ["uint256"], [_volatile(q * 3, *state)[0]])
        if height in (101, 102):
            tx, receipt = _trigger() if height == 101 else _winner()
            prefix = (
                []
                if height == 101
                else [
                    _transaction(102, 0, block_hash(106), USER, EXECUTOR, tx_type=106),
                ]
            )
            append(
                "eth_getBlockByNumber",
                [hex(height), True],
                {**_header(height), "transactions": [*prefix, tx]},
            )
            append("eth_getTransactionReceipt", [tx["hash"]], receipt)
    return result


def write_bundle(path: Path, entries):
    path.write_text(json.dumps({"schema": "oasis-parsed-rpc-bundle-v1", "records": entries}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    (args.directory / "plan.json").write_text(json.dumps(plan(), indent=2))
    write_bundle(args.directory / "synthetic.json", records())


if __name__ == "__main__":
    main()
