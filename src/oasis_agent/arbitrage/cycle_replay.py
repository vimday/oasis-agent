"""Offline review of exact case capture bytes and one historical block-end call."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict
from itertools import pairwise
from pathlib import Path
from typing import Any

from eth_hash.auto import keccak

from ..evidence.exact_capture import successful_results
from ..evidence.rpc_validation import rpc_quantity, strict_json_object, strict_list, strict_object
from ..models.common import canonical_json, sha256_hex
from ..rpc.transport import RpcExchange
from .cycle_case import review_case
from .cycle_evidence import header, load_exports, read_bounded
from .cycle_plan import Plan, hash32
from .cycle_protocols import abi, data_bytes

type Query = tuple[str, list[object]]


def _wire(
    directory: Path, queries: Sequence[Query]
) -> tuple[tuple[object, ...], list[dict[str, Any]], Counter[tuple[str, str]]]:
    paths = {
        f"{i:02}.{part}"
        for i in range(1, len(queries) + 1)
        for part in (
            "request",
            "response",
        )
    }
    if {item.name for item in directory.glob("*") if item.suffix in {".request", ".response"}} != (
        paths
    ):
        raise ValueError("exact capture has missing or extra wire files")
    exchanges = []
    hashes = []
    parsed: Counter[tuple[str, str]] = Counter()
    for i, (method, _) in enumerate(queries, 1):
        request = read_bounded(directory / f"{i:02}.request", 128 * 1024)
        response = read_bounded(directory / f"{i:02}.response", 1024 * 1024)
        exchanges.append(RpcExchange(method, i, request, response, 1))
        hashes.append(
            {
                "index": i,
                "method": method,
                "request_sha256": sha256_hex(request),
                "response_sha256": sha256_hex(response),
            }
        )
        parsed[
            sha256_hex(canonical_json(strict_json_object(request))),
            sha256_hex(canonical_json(strict_json_object(response))),
        ] += 1
    return successful_results(exchanges, queries), hashes, parsed


def _reserves(historical: dict[str, Any], token0: str, token1: str) -> dict[str, Any] | None:
    trigger = historical["trigger"]["decoded"]
    winner = historical["winner"]["decoded"]
    middle, last = trigger["sync_event"], winner["sync_event"]
    if middle is None or last is None or winner["solidly_input_fee"] is None:
        return None
    trigger_delta = {
        trigger["output_asset"]: -trigger["output_raw"],
        trigger["input_asset"]: trigger["input_raw"]
        - sum(item["raw"] for item in trigger["fee_transfers"]),
    }
    winner_delta = {
        trigger["output_asset"]: winner["legs"][2]["input_raw"]
        - winner["solidly_input_fee"]["raw"],
        trigger["input_asset"]: -winner["wrapped_output_raw"],
    }
    middle_raw = (middle["reserve0_raw"], middle["reserve1_raw"])
    tokens = (token0, token1)
    reconstructed = tuple(
        raw - trigger_delta[token] for raw, token in zip(middle_raw, tokens, strict=True)
    )
    expected_after = tuple(
        raw + winner_delta[token] for raw, token in zip(middle_raw, tokens, strict=True)
    )
    observed_after = (last["reserve0_raw"], last["reserve1_raw"])
    return {
        "token_order": tokens,
        "before_trigger_swap_inferred_raw": reconstructed if min(reconstructed) >= 0 else None,
        "trigger_sync_raw": middle_raw,
        "winner_sync_raw": observed_after,
        "net_reserve_transition_matches_receipts": expected_after == observed_after,
        "scope": "transaction_log_positions",
    }


def replay_case(
    plan: Plan,
    primary: Path,
    secondary: Path,
    block_end: Path,
) -> dict[str, Any]:
    case = plan.case
    if case is None or len(plan.sources) != 1:
        raise ValueError("a single-primary case plan is required")
    if case.trigger_block <= 0 or case.winner_block != case.trigger_block + 1:
        raise ValueError("case blocks are not consecutive")
    headers = [
        ("eth_getBlockByNumber", [hex(number), False])
        for number in range(case.trigger_block - 1, case.winner_block + 1)
    ]
    full = [
        ("eth_getBlockByNumber", [hex(number), True])
        for number in (case.trigger_block, case.winner_block)
    ]
    receipts: list[Query] = [
        ("eth_getTransactionReceipt", [tx_hash])
        for tx_hash in (case.trigger_hash, case.winner_hash)
    ]
    initial: Query = ("eth_chainId", [])
    a, a_wire, a_parsed = _wire(primary, (initial, *headers, *full, *receipts))
    b, b_wire, _ = _wire(secondary, (initial, *headers, *receipts))
    if (*a[:4], *a[6:]) != b or rpc_quantity(a[0]) != plan.chain_id:
        raise ValueError("retained sources disagree on chain, headers, or receipts")
    context = tuple(
        header(raw, plan.chain_id, number)
        for raw, number in zip(
            a[1:4],
            range(case.trigger_block - 1, case.winner_block + 1),
            strict=True,
        )
    )
    if any(right.parent_hash != left.ref.block_hash for left, right in pairwise(context)):
        raise ValueError("historical block ancestry mismatch")
    records = load_exports(((plan.sources[0], primary / "parsed.json"),), plan.sources)
    if a_parsed != Counter((r.request_sha256, r.response_sha256) for r in records):
        raise ValueError("primary parsed export diverges from original capture bytes")
    reviewed = review_case(plan, records)
    if reviewed is None or reviewed["status"] not in {
        "receipt_token_flows_reconciled",
        "receipt_path_reconciled",
    }:
        raise ValueError("case receipts do not reconcile")
    winner = reviewed["winner"]["decoded"]
    volatile = next(pool for pool in plan.pools if pool.name == case.route[2])
    winner_block = strict_object(a[5], "winner full block")
    transactions = strict_list(winner_block.get("transactions"), "transactions")
    tx = strict_object(transactions[case.winner_index], "winner transaction")
    if (
        tx.get("type") != "0x2"
        or tx.get("from") != case.sender
        or tx.get("to") != case.executor
        or tx.get("hash") != case.winner_hash
        or tx.get("value") != "0x0"
    ):
        raise ValueError("unsupported or mismatched historical call envelope")
    fields = ("from", "gas", "value", "maxFeePerGas", "maxPriorityFeePerGas", "accessList")
    if not set(fields) <= tx.keys():
        raise ValueError("historical call envelope is incomplete")
    call = {"to": case.executor, "data": tx.get("input"), **{field: tx[field] for field in fields}}
    calldata = data_bytes(call["data"])
    if len(calldata) < 4:
        raise ValueError("missing historical calldata")
    pin = {"blockHash": context[1].ref.block_hash, "requireCanonical": True}
    c, c_wire, _ = _wire(
        block_end,
        (
            initial,
            headers[1],
            ("eth_getCode", [case.executor, pin]),
            ("eth_getBalance", [case.executor, pin]),
            ("eth_call", [call, pin]),
            headers[1],
        ),
    )
    if c[0] != a[0] or c[1] != a[2] or c[5] != a[2]:
        raise ValueError("block-end call context changed")
    runtime = data_bytes(c[2])
    runtime_hash = "0x" + keccak(runtime).hex()
    if not runtime or case.executor_hash not in (None, runtime_hash):
        raise ValueError("block-end executor runtime mismatch")
    initial_balance = rpc_quantity(c[3])
    principal = winner["principal_native_raw"]
    (returned,) = abi(("uint256",), data_bytes(c[4]))
    if initial_balance < principal or returned != winner["wrapped_output_raw"] - principal:
        raise ValueError("block-end response disagrees with case principal or output")

    return {
        "schema": "oasis-cycle-case-wire-review-v1",
        "scenario": plan.scenario,
        "plan_sha256": sha256_hex(canonical_json(plan.model_dump(mode="json", by_alias=True))),
        "chain_id": plan.chain_id,
        "headers": [asdict(item) for item in context],
        "historical_case": reviewed,
        "matching_source_fields": ["chain_id", "three_headers", "two_receipts"],
        "source_infrastructure_independence": "unverified",
        "reserves": _reserves(reviewed, volatile.token0, volatile.token1),
        "block_end_atomic_call": {
            "status": "provider_reported_success",
            "context": asdict(context[1]),
            "boundary": "end_of_block",
            "executor": case.executor,
            "caller": case.sender,
            "transaction_hash_used_as_envelope_source": case.winner_hash,
            "calldata_sha256": sha256_hex(calldata),
            "gas_limit": rpc_quantity(tx["gas"]),
            "max_fee_per_gas_raw": rpc_quantity(tx["maxFeePerGas"]),
            "max_priority_fee_per_gas_raw": rpc_quantity(tx["maxPriorityFeePerGas"]),
            "transaction_value_raw": rpc_quantity(tx["value"]),
            "executor_runtime_keccak": runtime_hash,
            "executor_opening_native_raw": initial_balance,
            "executor_return_word_raw": returned,
            "return_matches_receipt_wrapped_output_minus_principal": True,
            "native_balance_delta": None,
            "simulated_gas_used": None,
        },
        "exact_winner_prestate": {
            "status": "unresolved",
            "parent_end": asdict(context[1]),
            "target_block": asdict(context[2]),
            "target_transaction_index": case.winner_index,
            "prefix": [
                {"index": i, "hash": hash32(item["hash"]), "type": rpc_quantity(item["type"])}
                for i, raw in enumerate(transactions[: case.winner_index])
                for item in (strict_object(raw, "prefix transaction"),)
            ],
            "reason": "native_prefix_reexecution_or_transaction_prestate_trace_required",
        },
        "wire_manifest": {"primary": a_wire, "secondary": b_wire, "block_end": c_wire},
        "execution_eligible": False,
    }
