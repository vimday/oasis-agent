"""Reconcile three preselected BSC receipt flows, including native and taxed-token boundaries."""

import hashlib
import json
from collections import Counter
from itertools import pairwise
from pathlib import Path

from eth_abi import encode
from eth_hash.auto import keccak

from docs.verification.fixtures.replay_bsc_pool_expansion import receipts_summary
from oasis_agent.arbitrage.cycle_protocols import topic
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

ROOT = Path(__file__).resolve().parents[3]
SCREEN = ROOT / "docs/verification/BSC_GOLD_WINDOW_SCREENED_20261127.json"
FOLLOWUP = ROOT / "reports/research/gold-book-20261127"
ZERO = "0x" + "0" * 40
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
USDT = "0x55d398326f99059ff775485246999027b3197955"
USDC = "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"
TOKEN_61 = "0x61a10e8556bed032ea176330e7f17d6a12a10000"
INFINITY = "0xa0ffb9c1ce1fe56963b0321b32e7a0302114058b"
VAULT = "0x238a358808379702088667322f80ac48bad5e6c4"
V4 = "0x28e2ea090877bf75740558f6bfb36a5ffee9e9df"
TRANSFER = topic("Transfer(address,address,uint256)")
WITHDRAW = topic("Withdrawal(address,uint256)")
V2_SWAP = topic("Swap(address,uint256,uint256,uint256,uint256,address)")
V3_SWAP = topic("Swap(address,address,int256,int256,uint160,uint128,int24)")
PANCAKE_SWAP = topic("Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)")
MANAGER_SWAP = topic("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
CL_SWAP = topic("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24,uint16)")
# Each case lists the swap in funding order, the asset paid into custody and all pool output logs.
LEGS = {
    33: [(125, None, [126]), (127, 129, [128]), (130, 132, [131]), (135, 134, [133])],
    34: [(150, 149, [139]), (142, 141, [140]), (143, 145, [144]), (148, 147, [146])],
    39: [
        (190, 183, [187]),
        (198, 196, [195]),
        (202, 201, [203]),
        (208, 207, [206]),
        (217, 210, [211, 213]),
    ],
}
LINKS = {
    33: [[126, 129], [128, 132], [131, 134]],
    34: [[139, 141], [140, 145], [144, 147]],
    39: [[187, 191, 193, 196], [195, 200, 201], [203, 204, 205, 207], [206, 210]],
}
KEYS = {
    125: (ZERO, TOKEN_61, 34, 1),
    127: (USDT, TOKEN_61, 7, 1),
    130: (USDT, USDC, 2, 1),
    143: (USDT, USDC, 2, 1),
    202: (USDT, USDC, 2, 9),
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def addr(word):
    assert len(word) == 66 and int(word[2:26], 16) == 0
    return "0x" + word[-40:].lower()


def read_inputs():
    screen = json.loads(SCREEN.read_bytes())
    select_raw = (FOLLOWUP / "bsc-followup-selection.json").read_bytes()
    selection = json.loads(select_raw)
    assert selection["source_screen"] == sha(SCREEN.read_bytes())
    scope = json.loads((FOLLOWUP / "bsc-followup-scope.json").read_bytes())
    assert scope["usd"] == 0 and scope["selection_sha256"] == sha(select_raw)
    trace_lane = FOLLOWUP / "bsc-followup-wire/triport"
    stop = json.loads((trace_lane / "stop.json").read_bytes())
    with RpcBudget(
        trace_lane / "original.sqlite3", (BudgetPolicy("triport", 5, 300),), read_only=True
    ) as ledger:
        assert ledger.counts()["triport"] == stop["reservations"] == 2
    error = (trace_lane / "header.response.json").read_bytes()
    meta = json.loads((trace_lane / "header.meta.json").read_bytes())
    assert meta["http"] == 403 and sha(error) == meta["response_sha256"]
    assert json.loads(error)["method"] == "eth_getBlockByHash"
    assert not list(trace_lane.glob("*trace.request.json"))
    values = []
    for name, method in [
        ("raw_header", "eth_getBlockByNumber"),
        ("raw_response", "eth_getBlockReceipts"),
    ]:
        p = ROOT / screen[name]
        raw = p.read_bytes()
        m = json.loads(p.with_suffix(".meta.json").read_bytes())
        req = p.with_suffix(".request").read_bytes()
        assert m["sha256"] == sha(raw) == screen[name + "_sha256"]
        assert m["request_sha256"] == sha(req) and m["http"] == 200
        request, data = json.loads(req), json.loads(raw)
        assert request["method"] == method and request["id"] == data["id"]
        if name == "raw_response":
            assert request["params"] == [screen["block_hash"]]
        values.append(data["result"])
    header, receipts = values
    checked = receipts_summary(
        header,
        receipts,
        set(),
        {"pools": set(), "managers": set()},
        set(screen["matched_log_topic0"]),
    )
    assert header["hash"] == screen["block_hash"] == selection["head_hash"]
    assert checked["receipts_root"] == screen["receipts_root"] and checked["all_receipts"] == 143
    assert selection["tx_hashes"] == [receipts[i]["transactionHash"] for i in LEGS]
    return screen, receipts


def pool_key(log, idx):
    a, b, fee, spacing = KEYS[idx]
    manager = log["address"].lower()
    assert a < b
    if manager == INFINITY:
        types = ["address"] * 4 + ["uint24", "bytes32"]
        values = [a, b, ZERO, manager, fee, (spacing << 16).to_bytes(32)]
    else:
        assert manager == V4
        types = ["address", "address", "uint24", "int24", "address"]
        values = [a, b, fee, spacing, ZERO]
    pid = "0x" + keccak(encode(types, values)).hex()
    assert log["topics"][1] == pid
    return {
        "pool_id": pid,
        "currency0": a,
        "currency1": b,
        "fee_pips_in_key": fee,
        "tick_spacing": spacing,
        "hooks": ZERO,
    }


def assets_at_legs(idx, logs, moves):
    legs = []
    for swap, pay, outs in LEGS[idx]:
        log = logs[swap]
        sig = log["topics"][0]
        data = bytes.fromhex(log["data"][2:])
        custodian = VAULT if log["address"] == INFINITY else log["address"]
        output = moves[outs[0]][0]
        assert all(moves[o][0] == output and moves[o][1] == custodian for o in outs)
        payout = sum(moves[o][3] for o in outs)
        if pay is not None:
            incoming, _, dest, cost = moves[pay]
            assert dest == custodian
        else:
            assert swap == 125
            incoming = ZERO
        key = None
        if swap in KEYS:
            key = pool_key(log, swap)
            assert sig == (CL_SWAP if log["address"] == INFINITY else MANAGER_SWAP)
            assert len(log["topics"]) == 3 and len(data) == (224 if sig == CL_SWAP else 192)
            amounts = [int.from_bytes(data[i : i + 32], signed=True) for i in (0, 32)]
            tokens = [key["currency0"], key["currency1"]]
            assert {incoming, output} == set(tokens)
            assert amounts[tokens.index(output)] == payout
            amt = -amounts[tokens.index(incoming)]
        elif sig == V2_SWAP:
            assert len(data) == 128 and len(log["topics"]) == 3
            d = [int.from_bytes(data[i : i + 32]) for i in range(0, 128, 32)]
            assert (d[0] > 0 and d[3] > 0 and d[1] == d[2] == 0) or (
                d[1] > 0 and d[2] > 0 and d[0] == d[3] == 0
            )
            amt = d[0] + d[1]
            assert d[2] + d[3] == payout
        else:
            assert sig in (V3_SWAP, PANCAKE_SWAP) and len(log["topics"]) == 3
            assert len(data) == (160 if sig == V3_SWAP else 224)
            a, b = (int.from_bytes(data[i : i + 32], signed=True) for i in (0, 32))
            assert a * b < 0 and -min(a, b) == payout
            amt = max(a, b)
        assert amt > 0 and (pay is None or cost == amt)
        legs.append(
            {
                "swap_log": swap,
                "market": log["address"],
                "custodian": custodian,
                "pool_key": key,
                "token_in": incoming,
                "input_raw": amt,
                "input_transfer_log": pay,
                "token_out": output,
                "output_raw": payout,
                "output_transfer_logs": outs,
            }
        )
    return legs


def reconcile(idx, receipt):
    payer, executor = receipt["from"], receipt["to"]
    logs = {int(log["logIndex"], 16): log for log in receipt["logs"]}
    moves = {}
    withdrawals = {}
    balances = Counter()
    for i, log in sorted(logs.items()):
        topics = log["topics"]
        token = log["address"]
        if topics[0] == TRANSFER and len(topics) == 3 and len(log["data"]) == 66:
            a, b = map(addr, topics[1:])
            v = int(log["data"], 16)
            moves[i] = (token, a, b, v)
            balances[a, token] -= v
            balances[b, token] += v
        if token == WBNB and topics[0] == WITHDRAW:
            assert len(topics) == 2 and len(log["data"]) == 66 and addr(topics[1]) == executor
            withdrawals[i] = int(log["data"], 16)
            balances[executor, WBNB] -= withdrawals[i]
    legs = assets_at_legs(idx, logs, moves)
    links = []
    for (a, b), path in zip(pairwise(legs), LINKS[idx], strict=True):
        t, v = a["token_out"], a["output_raw"]
        assert (t, v) == (b["token_in"], b["input_raw"])
        assert path[0] in a["output_transfer_logs"] and path[-1] == b["input_transfer_log"]
        for left, right in pairwise(path):
            A, B = moves[left], moves[right]
            holder = A[2]
            assert left < right and holder == B[1] and A[0] == B[0] == t and A[3] == B[3] == v
            assert not any(
                left < i < right and m[0] == t and holder in m[1:3] for i, m in moves.items()
            )
        links.append(
            {
                "swaps": [a["swap_log"], b["swap_log"]],
                "transfer_logs": path,
                "token": t,
                "amount_raw": v,
            }
        )
    gas, price = (int(receipt[k], 16) for k in ("gasUsed", "effectiveGasPrice"))
    row = {
        "tx_hash": receipt["transactionHash"],
        "transaction_index": idx,
        "sender": payer,
        "executor": executor,
        "gas_used": gas,
        "effective_gas_price_wei": price,
        "network_fee_bnb_wei": gas * price,
        "max_funded_market_legs": len(links) + 1,
        "intermediate_links": links,
        "legs": legs,
        "executor_token_deltas_including_withdrawals": {
            t: v for (a, t), v in balances.items() if a == executor and v
        },
        "sender_erc20_transfer_deltas": {
            t: v for (a, t), v in balances.items() if a == payer and v
        },
        "entity_net_bnb_wei": None,
        "withdrawals_by_log": withdrawals,
    }
    assert not row["executor_token_deltas_including_withdrawals"]
    if idx == 39:
        t_in, t_out = legs[0]["token_in"], legs[-1]["token_out"]
        assert t_in != t_out and moves[179][0:3] == (t_in, payer, t_in)
        assert moves[183][1] == payer and moves[211][2] == t_out and moves[213][2] == payer
        debit = legs[0]["input_raw"] + moves[179][3]
        credit = moves[213][3]
        assert debit // 100 == moves[179][3] and legs[-1]["output_raw"] // 100 == moves[211][3]
        assert row["sender_erc20_transfer_deltas"] == {t_in: -debit, t_out: credit}
        return {
            **row,
            "shape": "control",
            "case_kind": "serial_open_taxed_endpoints",
            "closed_same_asset_market_legs": 0,
            "sender_input_debit_raw": debit,
            "input_token_contract_share_raw": moves[179][3],
            "input_split_logs": [179, 183],
            "sender_output_credit_raw": credit,
            "output_token_contract_share_raw": moves[211][3],
            "output_split_logs": [211, 213],
        }
    last = legs[-1]
    first = legs[0]
    assert last["token_out"] == WBNB and first["token_in"] == (ZERO if idx == 33 else WBNB)
    delta = last["output_raw"] - first["input_raw"]
    ext_index = 138 if idx == 33 else 152
    t_out, src, dst, paid = moves[ext_index]
    assert (t_out, src) == (WBNB, executor) and dst not in (payer, executor)
    assert balances[dst, WBNB] == paid
    remainder = withdrawals[137 if idx == 33 else 151]
    assert delta == paid + remainder
    if idx == 33:
        assert withdrawals[136] == first["input_raw"]
    else:
        assert moves[149][1] == executor and moves[149][2] == first["market"]
    return {
        **row,
        "shape": "unresolved" if idx == 33 else "serial",
        "case_kind": "native_to_wbnb_native_settlement_unseen"
        if idx == 33
        else "four_pool_wbnb_cycle",
        "closed_same_asset_market_legs": 0 if idx == 33 else 4,
        "gross_anchor_spread_wei": delta,
        "external_wbnb_recipient": dst,
        "external_wbnb_transfer_log": ext_index,
        "external_wbnb_received_wei": paid,
        "residual_unwrapped_by_executor_wei": remainder,
        "native_payment_to_first_pool_or_vault_wei": None,
        "native_settlement_required": idx == 33,
    }


def main():
    if not __debug__:
        raise SystemExit("run without Python optimization")
    screen, receipts = read_inputs()
    cases = [reconcile(i, receipts[i]) for i in LEGS]
    a, b, _ = cases
    assert a["executor"] == b["executor"] and a["sender"] != b["sender"]
    assert a["external_wbnb_recipient"] == b["external_wbnb_recipient"]
    assert a["legs"][2]["pool_key"] == b["legs"][2]["pool_key"]
    assert a["legs"][2]["token_in"] == b["legs"][2]["token_out"]
    print(
        json.dumps(
            {
                "chain_id": 56,
                **{
                    k: screen[k]
                    for k in (
                        "block_number",
                        "block_hash",
                        "receipts_root",
                        "all_receipts",
                        "raw_response",
                        "raw_response_sha256",
                        "raw_header",
                        "raw_header_sha256",
                    )
                },
                "screened_two_or_more_events": len(screen["samples"]),
                "selected": len(cases),
                "triport": {"reservations": 2, "requests_of_traces": 0, "header_http": 403},
                "shared_wbnb_recipient": a["external_wbnb_recipient"],
                "shared_wbnb_received_total_wei": a["external_wbnb_received_wei"]
                + b["external_wbnb_received_wei"],
                "common_beneficial_ownership": None,
                "cases": cases,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
