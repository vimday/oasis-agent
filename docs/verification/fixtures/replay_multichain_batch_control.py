"""Replay retained Base and BSC batch comparison without network I/O."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path
from typing import Any

from eth_hash.auto import keccak

from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes, topic
from oasis_agent.evidence.public_receipts import decode_receipt, quantity, receipt_header
from oasis_agent.evidence.research_traces import first_failure, nested_calls
from oasis_agent.evidence.swap_flow import analyze_swap_funding, profile_swap_receipt
from oasis_agent.evidence.swap_flow_trace import (
    _receipt_positions,
    _transfers,
    replay_trace_funding,
)

type Row = dict[str, Any]
BASE_MANAGER = "0x498581ff718922c3f8e6a244956af099b2652b2b"
BASE_WETH = "0x4200000000000000000000000000000000000006"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
BSC_USDT = "0x55d398326f99059ff775485246999027b3197955"


def _read(path: Path) -> Any:
    return json.loads(path.read_bytes())


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _base(archive: Path, sample: int) -> Row:
    files = {
        "header": archive / f"base-tail-{sample}-publicnode/block.res",
        "receipt": archive / f"base-tail-{sample}-publicnode/receipt.res",
        "trace": archive / f"base-tail-{sample}-drpc/trace.res",
    }
    h, r, trace = (_read(path)["result"] for path in files.values())
    header = receipt_header(h, 8453, quantity(r["blockNumber"]), (r["transactionHash"],))
    receipt = decode_receipt(r, header.ref, r["transactionHash"], quantity(r["transactionIndex"]))
    frames = nested_calls(trace)
    trace_paths = _receipt_positions(trace, frames, receipt)
    transfers = _transfers(receipt, trace_paths)
    logs = {log.log_index: log for log in receipt.logs}
    profile = profile_swap_receipt(receipt)
    traced = replay_trace_funding(receipt, trace)
    funded = analyze_swap_funding([proof.leg for proof in traced.resolved_swaps], traced.links)
    parties = {receipt.sender, receipt.recipient}
    net = Counter()
    for tx in transfers:
        net[tx.token] += tx.amount * (int(tx.recipient in parties) - int(tx.sender in parties))
    wraps = Counter()
    for log in receipt.logs:
        if log.address != BASE_WETH or len(log.topics) != 2:
            continue
        if log.topics[0] not in (
            topic("Deposit(address,uint256)"),
            topic("Withdrawal(address,uint256)"),
        ):
            continue
        (recipient,) = abi(("address",), data_bytes(log.topics[1]))
        if recipient in parties:
            (amount,) = abi(("uint256",), data_bytes(log.data))
            wraps[log.topics[0]] += amount
            net[BASE_WETH] += (
                amount if log.topics[0] == topic("Deposit(address,uint256)") else -amount
            )
    native = [
        (path, frame.sender, frame.recipient, frame.value)
        for path, frame in frames.items()
        if frame.kind == "CALL" and frame.value and first_failure(frames, path) is None
    ]
    unlock_signature = keccak(b"unlock(bytes)")[:4]
    unlocks = {
        path
        for path, frame in frames.items()
        if frame.recipient == BASE_MANAGER
        and frame.data[:4] == unlock_signature
        and first_failure(frames, path) is None
    }
    swap_types = ("(address,address,uint24,int24,address)", "(bool,int256,uint160)", "bytes")
    selector = keccak(("swap(" + ",".join(swap_types) + ")").encode())[:4]
    v4_rows = {}
    v4_by_unlock = defaultdict(list)
    for event in profile.events:
        if event.shape != "v4":
            continue
        index = event.log_index
        path = trace_paths[index]
        frame = frames[path]
        assert (
            frame.recipient == BASE_MANAGER
            and frame.data[:4] == selector
            and frame.output is not None
        )
        key, params, _ = abi(swap_types, frame.data[4:])
        assert "0x" + keccak(frame.data[4:164]).hex() == event.pool[1]
        (packed,) = abi(("uint256",), frame.output)
        returned = tuple(
            value if value < (1 << 127) else value - (1 << 128)
            for value in divmod(packed, 1 << 128)
        )
        event_delta = abi(
            ("int128", "int128", "uint160", "uint128", "int24", "uint24"),
            data_bytes(logs[index].data),
        )[:2]
        scope = max((u for u in unlocks if path[: len(u)] == u), key=len)
        v4_rows[index] = (key, params, event_delta, returned)
        v4_by_unlock[scope].append(index)
    cross_unlock = []
    if sample == 3:
        by_transfer = {move.index: move for move in transfers}
        address_swaps = {proof.leg.event.log_index: proof for proof in traced.resolved_swaps}
        for src, dst, pay_hook, give_fee, take, settle in (
            (72, 95, 70, 74, 85, 93),
            (79, 96, 77, 81, 86, 94),
        ):
            gross, fee, custody, paid = (by_transfer[i] for i in (pay_hook, give_fee, take, settle))
            assert (gross.sender, gross.amount, gross.recipient) == (
                address_swaps[src].leg.event.pool[0],
                address_swaps[src].leg.amount_out,
                BASE_MANAGER,
            )
            assert (
                gross.token
                == fee.token
                == custody.token
                == paid.token
                == address_swaps[src].leg.token_out
            )
            assert gross.amount - fee.amount == custody.amount == paid.amount
            assert (fee.sender, custody.sender, custody.recipient, paid.sender, paid.recipient) == (
                BASE_MANAGER,
                BASE_MANAGER,
                receipt.recipient,
                receipt.recipient,
                BASE_MANAGER,
            )
            assert not any(
                take < row.index < settle
                and row.token == gross.token
                and receipt.recipient in (row.sender, row.recipient)
                for row in transfers
            )
            key, params, event_delta, returned = v4_rows[dst]
            assert key[0] == gross.token and params[0] and -params[1] == paid.amount == -returned[0]
            assert key[4] == "0x" + "00" * 20 and event_delta == returned
            assert custody.path[:1] == (7,) and paid.path[:1] == (11,)
            cross_unlock.append(
                {
                    "src_v3_event": src,
                    "dst_v4_event": dst,
                    "asset": gross.token,
                    "gross_pool_output_raw": str(gross.amount),
                    "fee_transfer_raw": str(fee.amount),
                    "funded_v4_input_raw": str(paid.amount),
                    "transfer_logs": [pay_hook, give_fee, take, settle],
                    "fee_recipient": fee.recipient,
                    "take_trace_path": custody.path,
                    "settle_transfer_trace_path": paid.path,
                }
            )
    next_native = sum(value * (int(y in parties) - int(x in parties)) for _, x, y, value in native)
    fee = receipt.gas_used * receipt.effective_gas_price
    return {
        "chain_id": 8453,
        "tx": r["transactionHash"],
        "block": header.ref.number,
        "block_hash": header.ref.block_hash,
        "transaction_index": quantity(r["transactionIndex"]),
        "tx_receipt_root_rebuilt": False,
        "header_receipts_root": h["receiptsRoot"],
        "receipt_trace_logs_matched": len(receipt.logs),
        "trace_frames": len(frames),
        "failed_frames": sum(frame.error is not None for frame in frames.values()),
        "swap_shape_count": len(profile.events),
        "pool_count": profile.distinct_pools,
        "top_level_swap_branches": dict(
            Counter(trace_paths[event.log_index][0] for event in profile.events)
        ),
        "v4_unlocks": [{"path": path, "v4_logs": v4_by_unlock[path]} for path in sorted(unlocks)],
        "v4_zero_core_delta_logs": [
            idx for idx, (_, _, event, _) in v4_rows.items() if event == (0, 0)
        ],
        "v4_return_differs_from_core_logs": [
            idx for idx, (_, _, event, returned) in v4_rows.items() if event != returned
        ],
        "generic_funded_path": funded.longest_witnessed_path,
        "two_cross_unlock_funding_legs": cross_unlock,
        "gas_used": receipt.gas_used,
        "l2_execution_fee_wei": str(fee),
        "l1_data_fee_wei": str(quantity(r["l1Fee"])),
        "observable_l2_plus_l1_wei": str(fee + quantity(r["l1Fee"])),
        "operator_fee_wei": None
        if r.get("operatorFee") is None
        else str(quantity(r["operatorFee"])),
        "party_erc20_deltas_including_weth_wraps": {
            token: str(amount) for token, amount in net.items() if amount
        },
        "party_native_delta_before_costs_wei": str(next_native),
        "wraps_by_event_topic": {token: str(amount) for token, amount in wraps.items()},
        "raw_sha256": {role: _digest(path) for role, path in files.items()},
    }


def _bsc(root: Path, control: Path) -> tuple[list[Row], Row]:
    inputs = [
        (root / "bsc-eight-address-holdout-20261124/verified.json", True),
        (root / "bsc-66-swap-forensics-20261124/verified.json", True),
        (control / "a578-trace/verified.json", False),
    ]
    rows = []
    for path, is_list in inputs:
        data = _read(path)
        for row in data if is_list else [data]:
            path_sizes = Counter(len(v["swaps"]) for v in row["matched_transfer_funding_paths"])
            rows.append(
                {
                    "chain_id": 56,
                    "tx": row["tx"],
                    "block": row["height"],
                    "block_hash": row["block_hash"],
                    "transaction_index": row["index"],
                    "executor": row["executor"],
                    "receipt_root": row["receipt_root"],
                    "rooted_receipts": row["receipts_verified"],
                    "trace_matched_receipt_logs": row["matched_receipt_logs"],
                    "frames": row["root_frames"],
                    "swap_count": row["seen_swaps"],
                    "logical_pools": row["logical_pools"],
                    "top_level_swap_branch_sizes": list(
                        row["first_root_swap_branch_hist"].values()
                    ),
                    "funding_path_sizes": dict(path_sizes),
                    "exact_funding_principal_repayments": row["exact_funding_principal_repayments"],
                    "gas_used": row["gas_used"],
                    "gas_paid_wei": str(row["gas_spend_wei"]),
                    "third_party_native_payments_wei": [
                        str(q) for _, _, q in row["direct_third_party_native_payment_candidates"]
                    ],
                    "visible_anchor_net_after_exec_costs_wei": str(
                        row["visible_anchor_pnl_after_exec_costs"]
                    ),
                    "other_asset_deltas": {
                        a: str(n) for a, n in row["nonanchor_visible_deltas"].items()
                    },
                    "source_verification_sha256": _digest(path),
                }
            )
    new = _read(control / "a578-trace/verified.json")
    routes = [v for v in new["matched_transfer_funding_paths"] if len(v["swaps"]) >= 3]
    assert len(routes) == 6 and all(
        v["before"][0] == v["after"][0] == BSC_USDT
        or (v["before"][0] == WBNB and v["after"][0] == "0x" + "00" * 20)
        for v in routes
    )
    assert (
        len(new["wrapped_deposits"]) == 1
        and next(v["after"][1] for v in routes if v["before"][0] == WBNB)
        == new["wrapped_deposits"][0][1]
    )
    principal = Counter()
    for v in routes:
        principal[v["before"][0]] += v["before"][1]
    return rows, {
        "raw_source_sha256": {
            "scope": _digest(control / "a578-trace/scope.json"),
            "trace_request": _digest(control / "a578-trace/trace.request.json"),
            "trace_response": _digest(control / "a578-trace/trace.response.json"),
            "stop": _digest(control / "a578-trace/STOP.json"),
        },
        "proven_closure_principal_sums_raw": {a: str(n) for a, n in principal.items()},
        "proven_closed_routes": routes,
        "break_even_output_denominator_usdt_raw": str(new["nonanchor_visible_deltas"][BSC_USDT]),
        "break_even_min_bnb_wei_before_extra_liquidation_cost": str(
            -new["visible_anchor_pnl_after_exec_costs"]
        ),
    }


def main() -> None:
    if not __debug__:
        raise RuntimeError("forensic replay requires Python assertions enabled")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("research_root", type=Path)
    args = parser.parse_args()
    root = args.research_root
    control = root / "multichain-batch-control-20261127"
    failed = _read(control / "receipts.response.json")
    assert failed == {"jsonrpc": "2.0", "id": 1, "result": []}
    bsc, new = _bsc(root, control)
    before = root / "bsc-eight-address-holdout-20261124"
    ranking = _read(before / "ranked.json")
    result = {
        "schema": "oasis.multichain-batch-control.v1",
        "base": [_base(root / "pretrade-tail-20261123", case) for case in (1, 3)],
        "bsc": bsc,
        "bsc_new_case": new,
        "bsc_archived_screen": {
            "accepted_log_count": ranking["accepted_log_count"],
            "matching_blocks": ranking["matching_blocks"],
            "matching_transactions": ranking["matching_transactions"],
            "threshold_counts": ranking["threshold_counts"],
        },
        "failed_first_bsc_scope": {
            "selection": _read(control / "scope.json")["case"]["tx"],
            "archived_matched_logs": len(_read(control / "scope.json")["case"]["matches"]),
            "empty_result": True,
            "response_sha256": _digest(control / "receipts.response.json"),
            "stop": _read(control / "STOP.json"),
        },
    }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
