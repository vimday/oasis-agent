"""Verify a BSC builder recipient and the complete visible cash in eight Era closures."""

import argparse
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import httpx
from eth_account import Account
from eth_hash.auto import keccak
from replay_y2026_bsc_era_followup import BSC_HASH, ERA_WETH, REPORTS, bsc_prior
from replay_y2026_era_window import BOOT, ETH, rebuild_hash
from replay_y2026_l2_fresh import Lane, digest, read
from replay_y2026_multichain_expanded import signed_bytes
from replay_y2026_op_arb import moves
from replay_y2026_open_tail import receipt_paths

from oasis_agent.arbitrage.cycle_protocols import abi, calldata, data_bytes
from oasis_agent.evidence.public_receipts import quantity as q
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts.capture_public_rpc import durable_new

SUMMARY = Path(__file__).resolve().parents[1] / "Y2026_BSC_ERA_FOLLOWUP_20261128.json"
ERA_WIRES = REPORTS / "y2026-bsc-era-followup-20261128/era_drpc"
ERA_INDEX = REPORTS / "y2026-l2-fresh-follow-20261128/scope.json"
RECEIVER = "0x4848489f0b2bedd788c696e2d79b6b69d7484848"
BUILDER_DOC = "https://docs.48.club/puissant-builder/send-bundle"
STABLE = "0x3355df6d4c9c3035724fd0e3914de96a5a83aaf4"
USDC_CASE = "0xf0e3189dc6dd18dfb6269ae85b5aecf2b1287241d968e0922d98ccdb5159a493"
PCS_FACTORY = "0x1bb72e0cbbea93c08f535fc7856e0338d7f7a8ab"
PCS_QUOTER = "0x3d146fce6c1006857750cbe8af44f76a28041ccc"
PCS_POOLS = (
    "0x291d9f9764c72c9ba6ff47b451a9f7885ebf9977",
    "0xfb467bedf483ef92d54b6615770ebedd9f639a50",
)
SOURCES = {
    "bsc": (56, "https://bsc-rpc.publicnode.com", 3),
    "era_envelopes": (324, "https://public.1rpc.io/zksync2-era", 9),
    "era_debug_official": (324, "https://mainnet.era.zksync.io", 9),
    "era_debug_drpc": (324, "https://zksync.drpc.org", 9),
    "era_quote": (324, "https://zksync.drpc.org", 22),
}
SUPPLEMENT = {
    "era_full_blocks": (324, "https://zksync.drpc.org", 9),
    "era_debug_omnia": (324, "https://endpoints.omniatech.io/v1/zksync-era/mainnet/public", 9),
    "era_debug_quicknode": (324, "https://docs-demo.zksync-mainnet.quiknode.pro/", 9),
}
STATE_SOURCE = {"era_state": (324, "https://zksync.drpc.org", 80)}
ENVELOPE_SCOPE = REPORTS / "y2026-bsc-era-costs-extra-20261128"
INTERVAL_MS = 480


def selected_cases():
    candidates = read(SUMMARY)["era"]
    rows = [
        r
        for r in candidates
        if r["tx"] == USDC_CASE or any(v > 0 for v in r["weth_closure_minus_full_receipt_fee_wei"])
    ]
    assert len(rows) == 8
    return rows


def capture_doc(root, capture):
    label = "builder_source"
    folder = root / label
    policy = (BudgetPolicy(label, 1, 0),)
    if capture:
        folder.mkdir()
    with RpcBudget(folder / "original.sqlite3", policy, read_only=not capture) as budget:
        if capture:
            assert budget.reserve(label, now_ms=time.time_ns() // 10**6) == 0
            durable_new(folder / "request", BUILDER_DOC.encode())
            try:
                with (
                    httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client,
                    client.stream(
                        "GET", BUILDER_DOC, headers={"Accept": "text/markdown"}
                    ) as response,
                ):
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 1024 * 1024:
                            raise ValueError("builder documentation body exceeded cap")
                    durable_new(folder / "response", bytes(raw))
                    status = {"http": response.status_code, "sha256": sha256_hex(bytes(raw))}
            except (httpx.HTTPError, ValueError, OSError) as exc:
                status = {"fault": str(exc) or type(exc).__name__}
            durable_new(
                folder / "STOP.json", canonical_json(status | {"retrieved": time.time_ns()})
            )
        assert budget.counts() == {label: 1}
    status = read(folder / "STOP.json")
    assert (folder / "request").read_bytes() == BUILDER_DOC.encode()
    if "sha256" in status:
        assert digest(folder / "response") == status["sha256"]
    raw = (folder / "response").read_bytes().lower() if "sha256" in status else b""
    hit = status.get("http") == 200 and RECEIVER.encode() in raw and b"builder control eoa" in raw
    return status | {"url": BUILDER_DOC, "recipient_documented": hit}


def freeze(root, supplements, state):
    sources = STATE_SOURCE if state else SUPPLEMENT if supplements else SOURCES
    plan = {
        "schema": "oasis.y2026.bsc-era-exact-costs.v1",
        "frozen_utc": datetime.now(UTC).isoformat(),
        "driver": digest(Path(__file__)),
        "previous_report": digest(SUMMARY),
        "index": digest(ERA_INDEX),
        "cases": [r["tx"] for r in selected_cases()],
        "bsc_major": BSC_HASH,
        "builder_doc": BUILDER_DOC,
        "rpc_spending_usd": 0,
        "sources": {k: list(v) for k, v in sources.items()},
        "budgets": {k: v[2] for k, v in sources.items()},
        "minimum_interval_ms": 900 if supplements else INTERVAL_MS,
        "quote_pools": list(PCS_POOLS),
        "quote_factory": PCS_FACTORY,
        "quoter_v2": PCS_QUOTER,
        "page_requests": 0 if supplements else 1,
    }
    if supplements:
        plan["supplements_scope"] = digest(supplements / "scope.json")
    if state:
        plan["state_of"] = digest(ENVELOPE_SCOPE / "scope.json")
        plan["state_sources"] = {
            f"h{height}": digest(ENVELOPE_SCOPE / f"era_full_blocks/h{height}.response")
            for height in dict.fromkeys(r["height"] for r in selected_cases())
        }
    root.mkdir()
    durable_new(root / "scope.json", canonical_json(plan))
    durable_new(root / "capture.py", Path(__file__).read_bytes())


def quote(ask, stable):
    parent = read(ERA_WIRES / f"h{stable['height']}.response")["result"]["parentHash"]
    fees = stable["receipt_fee_wei"]
    [cycle] = stable["closed_cycles"]
    gross = cycle["gross_raw"]
    for label, hash_ in (("parent", parent), ("settlement", stable["hash"])):
        head = ask(label + "-header", "eth_getBlockByHash", [hash_, False])
        assert head["hash"] == rebuild_hash(head) == hash_
        assert q(head["number"]) == stable["height"] - (label == "parent")
        pin = {"blockHash": hash_, "requireCanonical": True}

        def call(tag, addr, sig, types=(), args=(), *, state_label=label, selector=pin):
            return ask(
                f"{state_label}-{tag}",
                "eth_call",
                [{"to": addr, "data": calldata(sig, types, args)}, selector],
            )

        if label == "parent":
            assert abi(("uint8",), data_bytes(call("stable-decimals", STABLE, "decimals()"))) == (
                6,
            )
        for i, pool in enumerate(PCS_POOLS):
            [tier] = abi(("uint24",), data_bytes(call(f"pool-{i}-fee", pool, "fee()")))
            [expected] = abi(
                ("address",),
                data_bytes(
                    call(
                        f"pool-{i}-factory",
                        PCS_FACTORY,
                        "getPool(address,address,uint24)",
                        ("address", "address", "uint24"),
                        (STABLE, ERA_WETH, tier),
                    )
                ),
            )
            assert expected == pool
            tup = "(address,address,uint256,uint24,uint160)"
            for method, quantity in (
                ("quoteExactOutputSingle", fees),
                ("quoteExactInputSingle", gross),
            ):
                parsed = call(
                    f"q{i}-{method}",
                    PCS_QUOTER,
                    f"{method}({tup})",
                    (tup,),
                    ((STABLE, ERA_WETH, quantity, tier, 0),),
                )
                assert abi(("uint256", "uint160", "uint32", "uint256"), data_bytes(parsed))[0] > 0


def state_fields(cases):
    for height in dict.fromkeys(r["height"] for r in cases):
        selected = [r for r in cases if r["height"] == height]
        keys = sorted(
            {
                (r[role], asset)
                for r in selected
                for role in ("recipient", "payer")
                for asset in (ETH, r["closed_cycles"][0]["asset"])
            }
        )
        raw = read(ENVELOPE_SCOPE / f"era_full_blocks/h{height}.response")["result"]
        assert raw["hash"] == selected[0]["hash"]
        yield height, keys, raw


def balances(ask, cases):
    for height, pairs, block in state_fields(cases):
        settled = ask(f"r{height}", "eth_getBlockReceipts", [block["hash"]])
        assert len(settled) == len(block["transactions"])
        for label, hash_ in (("p", block["parentHash"]), ("c", block["hash"])):
            if label == "p":
                parent = ask(f"p{height}-header", "eth_getBlockByHash", [hash_, False])
                assert (q(parent["number"]), rebuild_hash(parent)) == (height - 1, hash_)
            pin = {"blockHash": hash_, "requireCanonical": True}
            for index, (acc, token) in enumerate(pairs):
                if token == ETH:
                    method, args = "eth_getBalance", [acc, pin]
                else:
                    call = calldata("balanceOf(address)", ("address",), (acc,))
                    method, args = "eth_call", [{"to": token, "data": call}, pin]
                ask(f"{label}{height}-{index}", method, args)


def run(root, scope, capture):
    cases = selected_cases()
    stable = next(r for r in cases if r["tx"] == USDC_CASE)
    bsc_head, _, _ = bsc_prior()

    def one(source, chain, endpoint):
        rows = {}

        def action(wire):
            def ask(label, method, params):
                answer = wire(label, method, params)
                rows[label] = answer
                return answer

            assert q(ask("chain", "eth_chainId", [])) == chain
            if source == "bsc":
                for label, key in (("first", 0), ("penultimate", -2)):
                    ask(label, "eth_getTransactionByHash", [bsc_head["transactions"][key]])
            elif source == "era_envelopes":
                for item in cases:
                    ask(item["tx"][2:], "eth_getTransactionByHash", [item["tx"]])
            elif source == "era_full_blocks":
                for height, hash_ in dict.fromkeys((r["height"], r["hash"]) for r in cases):
                    h = ask(f"h{height}", "eth_getBlockByHash", [hash_, True])
                    assert h["hash"] == hash_ and q(h["number"]) == height
            elif source.startswith("era_debug_"):
                for item in cases:
                    ask(
                        item["tx"][2:],
                        "debug_traceTransaction",
                        [
                            item["tx"],
                            {"tracer": "callTracer"},
                        ],
                    )
            elif source == "era_state":
                balances(ask, cases)
            else:
                quote(ask, stable)

        stop = Lane(root / source, source, endpoint, capture, scope).run(action)
        return rows, stop

    with ThreadPoolExecutor(max_workers=5) as pool:
        docs = pool.submit(capture_doc, root, capture) if scope["page_requests"] else None
        pending = {
            name: pool.submit(one, name, chain, endpoint)
            for name, (chain, endpoint, _) in scope["sources"].items()
            if name != "era_quote"
        }
        result = {k: f.result() for k, f in pending.items()}
        # Keep consecutive diagnostic and quotation work on the shared dRPC gateway separate.
        if "era_quote" in scope["sources"]:
            result["era_quote"] = one("era_quote", *SOURCES["era_quote"][:2])
        return result, docs.result() if docs else None


def load_scope(root, capture):
    scope = read(root / "scope.json")
    assert digest(root / "capture.py") == scope["driver"]
    if capture:
        assert digest(Path(__file__)) == scope["driver"]
    assert digest(SUMMARY) == scope["previous_report"] and digest(ERA_INDEX) == scope["index"]
    assert scope["cases"] == [r["tx"] for r in selected_cases()]
    sources = (
        STATE_SOURCE
        if scope.get("state_of")
        else SUPPLEMENT
        if scope.get("supplements_scope")
        else SOURCES
    )
    assert scope["sources"] == {k: list(v) for k, v in sources.items()}
    if "state_of" in scope:
        assert scope["state_of"] == digest(ENVELOPE_SCOPE / "scope.json")
        assert all(
            digest(ENVELOPE_SCOPE / f"era_full_blocks/{label}.response") == expected
            for label, expected in scope["state_sources"].items()
        )
    return scope


def bsc_builder(lane, docs):
    head, _, _ = bsc_prior()
    bsc = read(SUMMARY)["bsc"]
    [payment] = bsc["direct_native_transfers"]
    assert payment["recipient"] == RECEIVER
    assert bsc["native_beneficiary_state_deltas"][RECEIVER] == payment["wei"]
    evidence = {"payment": payment, "official_claim": docs, "block_coinbase": head["miner"]}
    for label, index in (("first", 0), ("penultimate", -2)):
        tx = lane.get(label)
        if tx is None:
            continue
        height, canonical_index = q(head["number"]), index % len(head["transactions"])
        raw = signed_bytes(tx, 56)
        assert (tx["hash"], tx["blockHash"], q(tx["blockNumber"]), q(tx["transactionIndex"])) == (
            head["transactions"][index],
            head["hash"],
            height,
            canonical_index,
        )
        assert "0x" + keccak(raw).hex() == tx["hash"]
        assert Account.recover_transaction(raw).lower() == tx["from"] == RECEIVER
        evidence[label] = {
            "hash": tx["hash"],
            "index": canonical_index,
            "signer": tx["from"],
            "to": tx["to"],
            "value_wei": q(tx["value"]),
            "gas_price_wei": q(tx["gasPrice"]),
        }
        if label == "first":
            marker = data_bytes(tx["input"]).split(b"\n", 1)[0].decode()
            assert marker == "Block built by Puissant, 48Club"
            assert tx["to"] == RECEIVER and q(tx["value"]) == q(tx["gasPrice"]) == 0
            evidence[label]["marker"] = marker
    return evidence


def envelope_for(case, lanes):
    h = lanes.get("era_full_blocks", ({}, {}))[0].get(f"h{case['height']}")
    original = lanes["era_envelopes"][0].get(case["tx"][2:])
    if h is not None:
        txs = h["transactions"]
        anchor = h | {"transactions": [t["hash"] for t in txs]}
        assert rebuild_hash(anchor) == case["hash"]
        included = txs[case["tx_index"]]
        if original is not None:
            assert all(original[k] == included[k] for k in ("hash", "from", "to", "value", "input"))
        original = included
    if original is None:
        return None
    # Era can omit an empty access list. The reconstruction below must still match the hash.
    if q(original["type"]) == 2 and "accessList" not in original:
        original = original | {"accessList": []}
    signed = signed_bytes(original, 324)
    assert (
        q(original["blockNumber"]),
        original["blockHash"],
        q(original["transactionIndex"]),
        original["hash"],
        original["from"],
        original["to"],
    ) == (
        case["height"],
        case["hash"],
        case["tx_index"],
        case["tx"],
        case["payer"],
        case["recipient"],
    )
    assert "0x" + keccak(signed).hex() == case["tx"]
    assert Account.recover_transaction(signed).lower() == case["payer"]
    return original


def era_trace(trace, case, tx, native_events, gas):
    # Era wraps the signed call in a bootloader tree and models native value via a system contract.
    if isinstance(trace, list):
        [trace] = trace
        trace = trace["result"]
    assert trace["to"] == BOOT and not trace["error"] and q(trace["gasUsed"]) == gas
    app, sys_moves, valued_calls = [], [], []
    pending, commit_count = [((), trace)], 0
    signature = data_bytes(calldata("transferFromTo(address,address,uint256)"))[:4]
    while pending:
        path, row = pending.pop()
        assert len(path) <= 64 and 0 <= commit_count + len(pending) < 4096
        if row["error"]:
            continue
        commit_count += 1
        value, data = q(row["value"]), data_bytes(row["input"])
        source, target = row["from"], row["to"]
        if (
            source == case["payer"]
            and target == case["recipient"]
            and (tx is None or (data, value) == (data_bytes(tx["input"]), q(tx["value"])))
        ):
            app.append(list(path))
        if value and row["type"].lower() == "call":
            valued_calls.append({"path": list(path), "from": source, "to": target, "wei": value})
        if target == ETH and data[:4] == signature:
            payer, recipient, amount = abi(("address", "address", "uint256"), data[4:])
            if amount:
                sys_moves.append((payer, recipient, amount))
        pending.extend(((*path, j), child) for j, child in reversed(list(enumerate(row["calls"]))))
    assert len(app) == 1
    assert Counter(sys_moves) == Counter(tuple(x[1:]) for x in native_events)
    return {
        "committed_frame_count": commit_count,
        "signed_application_call": app[0] if tx is not None else None,
        "observed_application_call": app[0],
        "matched_native_system_moves": len(sys_moves),
        "value_frames": valued_calls,
    }


def era_settlement(case, lanes):
    tx = envelope_for(case, lanes)
    receipt = read(ERA_WIRES / f"r{case['tx'][2:]}.response")["result"]
    assert receipt["transactionHash"] == case["tx"] and receipt["blockHash"] == case["hash"]
    log_checksum = {
        k: receipt[k]
        for k in (
            "transactionHash",
            "blockHash",
            "blockNumber",
            "status",
            "type",
            "from",
            "to",
            "effectiveGasPrice",
            "gasUsed",
            "cumulativeGasUsed",
            "logsBloom",
            "logs",
        )
    }
    assert sha256_hex(canonical_json(log_checksum)) == case["receipt_fingerprint_sha256"]
    price, gas = q(receipt["effectiveGasPrice"]), q(receipt["gasUsed"])
    fee = price * gas
    assert case["reconciled_fee_payers"] == {case["payer"]: fee} and fee == case["receipt_fee_wei"]
    [cycle] = case["closed_cycles"]
    actors = {case["payer"], case["recipient"]}
    assert cycle["holder"] == case["recipient"] and len(actors) == 2
    delta, native_events = Counter(), []
    remove_fee = 0
    for index, (asset, src, dst, value) in moves(receipt).items():
        delta[src, asset] -= value
        delta[dst, asset] += value
        if asset == ETH:
            native_events.append([index, src, dst, value])
            if {src, dst} == {case["payer"], BOOT}:
                remove_fee += value if dst == BOOT else -value
    assert remove_fee == fee
    role_deltas = [
        {"account": addr, "asset": asset, "delta": amount}
        for (addr, asset), amount in sorted(delta.items())
        if addr in actors and amount
    ]
    assert {(r["account"], r["asset"], r["delta"]) for r in role_deltas} == {
        (case["payer"], ETH, -fee),
        (case["recipient"], cycle["asset"], cycle["gross_raw"]),
    }
    traces = {}
    for name, (lane, _) in lanes.items():
        if name.startswith("era_debug_") and case["tx"][2:] in lane:
            trace = lane[case["tx"][2:]]
            traces[name] = era_trace(trace, case, tx, native_events, gas)
    same_asset_net = cycle["gross_raw"] - fee if cycle["asset"] == ERA_WETH else None
    return {
        "tx": case["tx"],
        "hash": case["hash"],
        "height": case["height"],
        "signed_sender_recovered": tx is not None,
        "tx_value_wei": q(tx["value"]) if tx is not None else None,
        "receipt_fee_wei": fee,
        "gas_used": gas,
        "price_wei_per_gas": price,
        "receipt_native_movements": native_events,
        "native_non_fee_movements": [
            row for row in native_events if set(row[1:3]) != {case["payer"], BOOT}
        ],
        "terminal_account_deltas": role_deltas,
        "conditional_same_owner_at_par_wei": same_asset_net,
        "max_extra_gas_at_original_effective_price": (same_asset_net - 1) // price
        if same_asset_net is not None and same_asset_net > 0
        else None,
        "trace_proofs": traces,
    }


def stable_valuation(quotes, case, settlement):
    receipt = read(ERA_WIRES / f"r{USDC_CASE[2:]}.response")["result"]
    [cycle] = case["closed_cycles"]
    [leg] = [
        x
        for x in receipt_paths(receipt, read(ERA_INDEX)["index_topics"])["receipt_legs"]
        if x["event"] == cycle["legs"][0]
    ]
    assert (leg["in_token"], leg["out_token"]) == (STABLE, ERA_WETH)
    fee = settlement["receipt_fee_wei"]
    surplus = cycle["gross_raw"]
    implied = Fraction(leg["in_raw"], leg["out_raw"])
    implied_fee = -(fee * (-implied.numerator) // implied.denominator)
    result = {
        "bridge_token": STABLE,
        "bridged_usdc_raw": surplus,
        "same_tx_implied_usdc_e_raw_per_ether": [implied.numerator * 10**18, implied.denominator],
        "same_tx_implied_cost_rounded_up_usdc_e_raw": implied_fee,
        "same_tx_implied_after_fee_usdc_e_raw": surplus - implied_fee,
        "break_even_usdc_e_raw_per_ether": [surplus * 10**18, fee],
        "independent_quote": [],
    }
    for label in ("parent", "settlement"):
        for i, pool in enumerate(PCS_POOLS):
            proof = [
                quotes.get(f"{label}-q{i}-{method}")
                for method in ("quoteExactInputSingle", "quoteExactOutputSingle")
            ]
            if None in proof:
                continue
            sold, replacement = [
                abi(("uint256", "uint160", "uint32", "uint256"), data_bytes(p)) for p in proof
            ]
            residual = sold[0] - fee
            result["independent_quote"].append(
                {
                    "state": label,
                    "hash": quotes[label + "-header"]["hash"],
                    "pool": pool,
                    "fee_tier": abi(("uint24",), data_bytes(quotes[f"{label}-pool-{i}-fee"]))[0],
                    "gross_liquidation_weth_wei": sold[0],
                    "exact_fee_replacement_cost_usdc_e_raw": replacement[0],
                    "net_after_original_fee_before_liquidation_tx_wei": residual,
                    "net_after_original_fee_before_liquidation_tx_usdc_e_raw": surplus
                    - replacement[0],
                    "max_added_gas_at_original_effective_price": (residual - 1)
                    // settlement["price_wei_per_gas"]
                    if residual > 0
                    else None,
                    "quoter_execution_gas_estimates_not_transaction_fees": [
                        sold[3],
                        replacement[3],
                    ],
                }
            )
    return result


def state_proofs(lane, cases):
    result = []
    for height, pairs, block in state_fields(cases):
        receipt_list = lane.get(f"r{height}")
        if receipt_list is None:
            continue
        assert [r["transactionHash"] for r in receipt_list] == [
            t["hash"] for t in block["transactions"]
        ]
        expected = Counter()
        for i, receipt in enumerate(receipt_list):
            assert (
                q(receipt["blockNumber"]),
                q(receipt["transactionIndex"]),
                receipt["blockHash"],
            ) == (
                height,
                i,
                block["hash"],
            )
            # Era's failed user calls can still retain system fee logs.
            for asset, src, dst, value in moves(receipt).values():
                expected[dst, asset] += value
                expected[src, asset] -= value
        checks = []
        for index, (address, token) in enumerate(pairs):
            low, high = lane.get(f"p{height}-{index}"), lane.get(f"c{height}-{index}")
            amount = None if low is None or high is None else int(high, 16) - int(low, 16)
            checks.append(
                {
                    "address": address,
                    "asset": token,
                    "delta_after_minus_parent": amount,
                    "delta_by_block_receipts": expected[address, token],
                    "exact": None if amount is None else amount == expected[address, token],
                }
            )
        result.append(
            {
                "height": height,
                "block_hash": block["hash"],
                "prior_hash": block["parentHash"],
                "whole_block_receipts": len(receipt_list),
                "reverted_receipts": sum(q(row["status"]) == 0 for row in receipt_list),
                "state_checks": checks,
            }
        )
    return result


def summary(root, lanes, docs, extra=None, state=None):
    cases = selected_cases()
    settlements = [era_settlement(case, lanes) for case in cases]
    stable = next(case for case in cases if case["tx"] == USDC_CASE)
    return {
        "scope_sha256": digest(root / "scope.json"),
        "supplemental_scope_sha256": digest(extra / "scope.json") if extra else None,
        "state_scope_sha256": digest(state / "scope.json") if state else None,
        "rpc_reservations": sum(sum(stop["counts"].values()) for _, stop in lanes.values()),
        "rpc_spending_usd": 0,
        "sources": {name: stop for name, (_, stop) in lanes.items()},
        "bsc": bsc_builder(lanes["bsc"][0], docs),
        "era": settlements,
        "era_block_boundary_reconciliation": state_proofs(lanes["era_state"][0], cases)
        if state
        else [],
        "stable_valuation": stable_valuation(
            lanes["era_quote"][0], stable, next(x for x in settlements if x["tx"] == USDC_CASE)
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("freeze", "capture", "replay"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--also", type=Path)
    parser.add_argument("--state", action="store_true")
    parser.add_argument("--state-evidence", type=Path)
    args = parser.parse_args()
    if args.mode == "freeze":
        freeze(args.root, args.also, args.state)
        return
    capture = args.mode == "capture"
    lanes, docs = run(args.root, load_scope(args.root, capture), capture)
    if args.also is not None:
        assert not capture
        supplemental_scope = load_scope(args.also, False)
        assert supplemental_scope["supplements_scope"] == digest(args.root / "scope.json")
        other, _ = run(args.also, supplemental_scope, False)
        lanes.update(other)
    if args.state_evidence is not None:
        assert not capture and args.also is not None
        state_scope = load_scope(args.state_evidence, False)
        assert state_scope["state_of"] == digest(args.also / "scope.json")
        state, _ = run(args.state_evidence, state_scope, False)
        lanes.update(state)
    if capture:
        result = {name: stop for name, (_, stop) in lanes.items()}
    else:
        result = summary(args.root, lanes, docs, args.also, args.state_evidence)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
