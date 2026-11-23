"""Compare fixed-size V2 route policies using disposable loopback Anvil receipts."""

import argparse
import json
import shutil
import socket
import subprocess  # nosec B404 - fixed loopback Anvil invocation
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from eth_abi import decode, encode
from eth_utils import to_checksum_address

from oasis_agent.fastpath.v2_batch import V2BatchComparison, V2BatchOrder, compare_v2_batch
from oasis_agent.fastpath.v2_sizing import V2Leg, V2Pool, V2Route
from scripts.verify_atomic_vault_anvil import (
    DEFAULT_CONTRACTS_OUT_DIR,
    _AnvilRpc,
    _AnvilRpcError,
    _encode_call,
    _require,
    _wait_for_anvil_ready,
    _write_immutable_json,
)

UNIT = 10**18
LOCAL_CHAIN_ID = 31337
CAPITAL = 6 * UNIT
AMOUNT = 5 * UNIT
PAYMENT = 2 * 10**12
GAS_PRICE = 10**9
FEE_BPS = 30
SHIFT = 40 * UNIT
LOSS_DATA = "0x" + _encode_call("Error(string)", ("string",), ("LOSS",)).hex()


def _send(rpc: _AnvilRpc, call: dict[str, str]) -> dict[str, Any]:
    tx_hash = rpc.call("eth_sendTransaction", [call])
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        receipt = rpc.call("eth_getTransactionReceipt", [tx_hash])
        if receipt is not None:
            _require(receipt["transactionHash"] == tx_hash, "unexpected receipt transaction")
            _require(receipt["blockHash"] == rpc.latest_block()["hash"], "unexpected receipt block")
            return receipt
        time.sleep(0.002)
    raise RuntimeError("local Anvil receipt timed out")


def _call(sender: str, target: str | None, data: bytes, value: int = 0) -> dict[str, str]:
    call = {
        "from": sender,
        "data": "0x" + data.hex(),
        "value": hex(value),
        "gas": hex(25_000_000),
        "gasPrice": hex(GAS_PRICE),
    }
    if target is not None:
        call["to"] = target
    return call


def _setup_tx(rpc: _AnvilRpc, sender: str, target: str | None, data: bytes) -> dict[str, Any]:
    receipt = _send(rpc, _call(sender, target, data))
    _require(receipt["status"] == "0x1", "fixture setup reverted")
    return receipt


def _deploy(rpc: _AnvilRpc, sender: str, name: str, source: str, args: bytes) -> str:
    artifact = DEFAULT_CONTRACTS_OUT_DIR / source / f"{name}.json"
    creation = json.loads(artifact.read_text(encoding="utf-8"))["bytecode"]["object"]
    receipt = _setup_tx(rpc, sender, None, bytes.fromhex(creation[2:]) + args)
    return to_checksum_address(receipt["contractAddress"])


def _mint(rpc: _AnvilRpc, sender: str, token: str, holder: str, amount: int) -> None:
    data = _encode_call("mint(address,uint256)", ("address", "uint256"), (holder, amount))
    _setup_tx(rpc, sender, token, data)


def _read_pools(
    rpc: _AnvilRpc, paths: dict[str, list[str]], tokens: tuple[str, str]
) -> dict[str, V2Pool]:
    pools = {}
    for pool in dict.fromkeys(pool for path in paths.values() for pool in path):
        reserve_0, reserve_1, _ = rpc.call_abi(
            pool, "getReserves()", (), (), ("uint112", "uint112", "uint32")
        )
        pools[pool] = V2Pool(pool, *tokens, reserve_0, reserve_1, 10_000 - FEE_BPS, 10_000)
    return pools


def _quote(
    paths: list[list[str]], pools: dict[str, V2Pool], tokens: tuple[str, str], capital: int
) -> V2BatchComparison:
    orders = []
    for index, path in enumerate(paths):
        route = V2Route(tuple(V2Leg(pools[pool], tokens[hop % 2]) for hop, pool in enumerate(path)))
        orders.append(V2BatchOrder(str(index), route, AMOUNT, 0, 0))
    return compare_v2_batch(orders, anchor_token=tokens[0], initial_capital=capital)


def _attempt(
    rpc: _AnvilRpc,
    sender: str,
    beneficiary: str,
    runner: str,
    tokens: tuple[str, str],
    paths: list[list[str]],
    pools: dict[str, V2Pool],
) -> dict[str, Any]:
    data = _encode_call("execute(address[][],uint256)", ("address[][]", "uint256"), (paths, AMOUNT))
    call = _call(sender, runner, data, PAYMENT)
    block = rpc.latest_block()
    before = rpc.erc20_balance(tokens[0], runner)
    native_before = rpc.balance(sender)
    beneficiary_before = rpc.balance(beneficiary)
    started = time.monotonic_ns()
    model = _quote(paths, pools, tokens, before)
    discovered = time.monotonic_ns()
    model_can_execute = model.ordered.success_count == len(paths)
    try:
        result = rpc.call(
            "eth_call", [call, {"blockHash": block["hash"], "requireCanonical": True}]
        )
        (simulated,) = decode(["uint256"], bytes.fromhex(result[2:]))
        simulation_error = None
    except _AnvilRpcError as exc:
        _require(exc.data == LOSS_DATA, "unexpected pinned simulation error")
        simulated, simulation_error = None, exc.data
    completed = time.monotonic_ns()
    receipt = _send(rpc, call)
    receipt_observed = time.monotonic_ns()
    execution_block = rpc.latest_block()
    success = receipt["status"] == "0x1"
    _require(execution_block["parentHash"] == block["hash"], "execution is not the direct child")
    _require(success == model_can_execute, "model disposition mismatch")
    _require(success == (simulated is not None), "simulation disposition mismatch")
    gross = rpc.erc20_balance(tokens[0], runner) - before
    _require(gross == (simulated if success else 0), "simulated and settled balance delta differs")
    _require(not success or gross == model.ordered.gross_profit, "model and EVM gross differs")
    _require(rpc.erc20_balance(tokens[1], runner) == 0, "unexpected residual intermediate token")
    paid = rpc.balance(beneficiary) - beneficiary_before
    _require(paid == (PAYMENT if success else 0), "payment failed to reconcile")
    gas_used = int(receipt["gasUsed"], 16)
    gas_cost = gas_used * int(receipt["effectiveGasPrice"], 16)
    _require(native_before - rpc.balance(sender) == gas_cost + paid, "native spend differs")
    expected_pools = {pool.pool_id: pool for pool in model.ordered.final_pools} if success else {}
    actual_pools = _read_pools(rpc, {str(i): path for i, path in enumerate(paths)}, tokens)
    for address, actual in actual_pools.items():
        expected = expected_pools.get(address.lower(), pools[address])
        _require(actual == expected, "final reserves differ from model or rollback")
    return {
        "state": {key: block[key] for key in ("hash", "parentHash", "number", "stateRoot")},
        "envelope": call,
        "receipt": receipt,
        "initial_anchor": before,
        "hops": [len(path) for path in paths],
        "model_legs_by_route": [
            entry.quote.amounts if entry.quote else None for entry in model.ordered.results
        ],
        "reset_model_gross": model.independent.gross_profit,
        "discovery_net_at_gas_ceiling": (
            model.ordered.gross_profit - int(call["gas"], 16) * GAS_PRICE - PAYMENT
            if model_can_execute
            else None
        ),
        "simulated_gross": simulated,
        "simulation_revert_data": simulation_error,
        "realized_gross": gross,
        "gas_used": gas_used,
        "gas_cost": gas_cost,
        "payment": paid,
        "net": gross - paid - gas_cost,
        "quote_ns": discovered - started,
        "until_pinned_simulation_done_ns": completed - started,
        "until_local_receipt_ns": receipt_observed - started,
    }


def _experiment(rpc: _AnvilRpc) -> dict[str, Any]:
    version = rpc.verify_local_anvil(expected_chain_id=LOCAL_CHAIN_ID)
    sender, beneficiary, other, *_ = map(to_checksum_address, rpc.call("eth_accounts", []))
    mocks = "OasisAtomicExecutor.t.sol"
    token_a, token_b = (
        _deploy(rpc, sender, "MockAtomicERC20", mocks, encode(["string", "string"], [name, name]))
        for name in ("A", "B")
    )
    tokens = (token_a, token_b)
    runner = _deploy(
        rpc,
        sender,
        "V2OrderedRouteFixture",
        "V2OrderedRouteFixture.sol",
        encode(["address", "address"], [tokens[0], beneficiary]),
    )
    _mint(rpc, sender, tokens[0], runner, CAPITAL)

    def pair(reserve_a: int, reserve_b: int) -> str:
        pool = _deploy(
            rpc,
            sender,
            "MockV2Pair",
            mocks,
            encode(["address", "address", "uint24"], [*tokens, FEE_BPS]),
        )
        _mint(rpc, sender, tokens[0], pool, reserve_a * UNIT)
        _mint(rpc, sender, tokens[1], pool, reserve_b * UNIT)
        _setup_tx(rpc, sender, pool, _encode_call("syncReserves()"))
        return pool

    shared = pair(1_000, 1_025)
    left, right = [shared, pair(1_010, 1_000)], [shared, pair(1_005, 1_000)]
    long_route = [shared, *(pair(1_000_000, 1_000_000) for _ in range(98)), pair(1_350, 1_000)]
    paths = {"left": left, "right": right, "long": long_route}
    arms = {
        "left": [[left]],
        "right": [[right]],
        "left_then_right": [[left, right]],
        "right_then_left": [[right, left]],
        "left_then_left": [[left, left]],
        "separate": [[left], [right]],
        "long": [[long_route]],
    }
    states = {}
    for phase in ("parent", "child"):
        state = rpc.latest_block()
        results = {}
        base = _read_pools(rpc, paths, tokens)
        for arm, transactions in arms.items():
            snapshot = rpc.call("evm_snapshot", [])
            attempts = []
            pools = base
            for transaction in transactions:
                if attempts:
                    pools = _read_pools(rpc, paths, tokens)
                attempts.append(
                    _attempt(rpc, sender, beneficiary, runner, tokens, transaction, pools)
                )
            results[arm] = {"attempts": attempts, "net": sum(row["net"] for row in attempts)}
            _require(rpc.call("evm_revert", [snapshot]) is True, "branch restore failed")
            _require(rpc.latest_block()["hash"] == state["hash"], "branch restored wrong state")
        states[phase] = {
            "header": state,
            "arms": results,
            "reserves": {address: [pool.reserve0, pool.reserve1] for address, pool in base.items()},
        }
        if phase == "parent":
            shift = _encode_call(
                "shiftReserve(address,uint256)", ("address", "uint256"), (shared, SHIFT)
            )
            _setup_tx(rpc, other, runner, shift)
            _require(
                rpc.latest_block()["parentHash"] == state["hash"],
                "repricing state is not the direct child",
            )
    for arm in arms:
        first = states["parent"]["arms"][arm]["attempts"]
        second = states["child"]["arms"][arm]["attempts"]
        _require(
            [row["envelope"] for row in first] == [row["envelope"] for row in second],
            "envelope changed",
        )
    parent = states["parent"]["arms"]
    child = states["child"]["arms"]
    control = parent["left_then_left"]["attempts"][0]
    first_quote, second_quote = control["model_legs_by_route"]
    _require(first_quote[-1] > AMOUNT > second_quote[-1], "rollback control lost its sign flip")
    _require(control["realized_gross"] == 0 and control["net"] < 0, "rollback control settled")
    _require(parent["left_then_right"]["net"] > parent["separate"]["net"], "batch lost gas edge")
    _require(all(result["net"] < 0 for result in child.values()), "a child arm remained positive")
    return {
        "schema": "oasis-local-v2-ordered-route-1",
        "client": version,
        "chain_id": LOCAL_CHAIN_ID,
        "capital": CAPITAL,
        "fixed_input": AMOUNT,
        "native_to_anchor_ratio": [1, 1],
        "anchor": tokens[0],
        "intermediate": tokens[1],
        "shared_pool": shared,
        "child_shift": SHIFT,
        "states": states,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    anvil = shutil.which("anvil")
    _require(anvil is not None, "Anvil is required")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(  # nosec B603 - fixed local arguments, no fork or external RPC
        [
            str(anvil),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--chain-id",
            str(LOCAL_CHAIN_ID),
            "--silent",
            "--hardfork",
            "cancun",
            "--block-base-fee-per-gas",
            "0",
            "--gas-price",
            str(GAS_PRICE),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with _wait_for_anvil_ready(
            f"http://127.0.0.1:{port}", timeout_s=5, expected_chain_id=LOCAL_CHAIN_ID
        ) as rpc:
            _write_immutable_json(args.out, _experiment(rpc))
    finally:
        process.terminate()
        process.wait(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
