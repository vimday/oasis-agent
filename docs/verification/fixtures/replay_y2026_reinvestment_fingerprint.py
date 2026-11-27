"""Replay retained Ethereum funding fingerprints with independent Optimism positive controls."""

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from replay_eth_hundred_census import receipts_proof
from replay_eth_hundred_holdout import digest, envelope_cost, source_bytes
from replay_multihop_second_shard_receipts import legacy_op_header_hash, op_single_receipt_root
from replay_optimism_136_reinvestment import witnesses

from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes, topic
from oasis_agent.evidence.public_receipts import decode_receipt, quantity
from oasis_agent.evidence.research_traces import nested_calls
from oasis_agent.evidence.swap_flow import TRANSFER
from oasis_agent.evidence.swap_flow_trace import (
    TraceSwapFunding,
    _receipt_positions,
    replay_trace_funding,
)
from oasis_agent.evidence.weth_principal import WETH as ETH_WETH
from oasis_agent.models.common import ZERO_ADDRESS

RESEARCH = Path("reports/research")
DOCS = Path("docs/verification")
FROZEN = RESEARCH / "y2026-reinvestment-fingerprint-20261127/scope.json"
FROZEN_SHA256 = "978f42b393520a7b7c489122bf71031ef8cfa221287dd4ed7aa98d3ebe7e3a33"
CENSUS = RESEARCH / "eth-batch-hundred-census-20261127"
HOLDOUT = RESEARCH / "eth-batch-hundred-holdout-20261127"
OP_WETH = "0x4200000000000000000000000000000000000006"
WITHDRAW = topic("Withdrawal(address,uint256)")
DEPOSIT = topic("Deposit(address,uint256)")


@dataclass(frozen=True)
class Movement:
    index: int
    kind: str
    amount: int
    counterparty: str | None = None


@dataclass(frozen=True)
class ClosedRound:
    swaps: tuple[int, ...]
    debit: int
    credit: int


def load(path):
    return json.loads(path.read_bytes())


def ledger(logs, weth):
    """List observed movements per token/holder. ERC-721 shaped logs do not qualify."""
    result = defaultdict(list)
    for n, token, topics, data in logs:
        if not topics or len(data) != 66:
            continue
        if topics[0] == TRANSFER and len(topics) == 3:
            sender, recipient = (abi(("address",), data_bytes(t))[0] for t in topics[1:])
            (amount,) = abi(("uint256",), data_bytes(data))
            if sender == recipient:
                result[token, sender].append(Movement(n, "self", amount))
            else:
                if sender != ZERO_ADDRESS:
                    result[token, sender].append(Movement(n, "debit", amount, recipient))
                if recipient != ZERO_ADDRESS:
                    result[token, recipient].append(Movement(n, "credit", amount, sender))
        elif token == weth and topics[0] in {WITHDRAW, DEPOSIT} and len(topics) == 2:
            (actor,) = abi(("address",), data_bytes(topics[1]))
            (amount,) = abi(("uint256",), data_bytes(data))
            kind = "withdraw" if topics[0] == WITHDRAW else "deposit"
            result[token, actor].append(Movement(n, kind, amount))
    return result


def closed_rounds(funding: TraceSwapFunding):
    proofs = {p.leg.event.log_index: p for p in funding.resolved_swaps}
    following = {link.source: link.target for link in funding.links}
    assert len(following) == len(set(following.values())) == len(funding.links)
    rounds = []
    without_endpoints = 0
    for swap, first in sorted(proofs.items()):
        path = [swap]
        while path[-1] in following:
            next_swap = following[path[-1]]
            assert next_swap not in path
            path.append(next_swap)
            last = proofs[next_swap]
            if last.leg.token_out != first.leg.token_in:
                continue
            if first.input_transfer_log is None or last.output_transfer_log is None:
                without_endpoints += 1
            else:
                rounds.append(
                    ClosedRound(tuple(path), first.input_transfer_log, last.output_transfer_log)
                )
            break
    return rounds, without_endpoints


def matched_paths(source, target, preceding, following):
    return [
        (first, second)
        for first in preceding[source]
        for second in following[target]
        if first.debit < first.credit < second.debit < second.credit
        and set(first.swaps).isdisjoint(second.swaps)
    ]


def inspect(movements, weth, rounds, market_in, market_out, internal_wraps=()):
    starting, ending = defaultdict(list), defaultdict(list)
    for route in rounds:
        starting[route.debit].append(route)
        ending[route.credit].append(route)
    internal = {tuple(group) for group in internal_wraps}
    result = Counter()
    details = {"wrapped_cross_round": [], "direct_cross_round": [], "same_principal_repeat": []}
    adjacent_example = None
    candidate_rounds = set()
    for (currency, holder), entries in sorted(movements.items()):
        for a in entries:
            if a.kind == "debit" and a.amount > 0:
                candidate_rounds.update(
                    (currency, holder, route)
                    for route in starting[a.index]
                    if any(
                        b.index == route.credit and b.kind == "credit" and b.amount > 0
                        for b in entries
                    )
                )
        if currency == weth:
            for i, d in enumerate(entries[3:]):
                a, b, c = entries[i : i + 3]
                if (a.kind, b.kind, c.kind, d.kind) != ("credit", "withdraw", "deposit", "debit"):
                    continue
                if not 0 < a.amount == b.amount == c.amount == d.amount:
                    continue
                indices = (a.index, b.index, c.index, d.index)
                result["wrapped_ledger_matches"] += 1
                if indices in internal:
                    result["wrapped_known_within_round"] += 1
                if indices not in internal and (
                    a.index not in market_out or d.index not in market_in
                ):
                    continue
                result["wrapped_market_matches"] += 1
                paths = matched_paths(a.index, d.index, ending, starting)
                if len(paths) == 1 and indices not in internal:
                    first, second = paths[0]
                    details["wrapped_cross_round"].append(
                        {
                            "holder": holder,
                            "amount_raw": a.amount,
                            "logs": indices,
                            "prior_round": first.swaps,
                            "next_round": second.swaps,
                        }
                    )
                elif len(paths) > 1:
                    result["wrapped_ambiguous_closed_matches"] += 1
        for a, b in pairwise(entries):
            if (a.kind, b.kind) != ("credit", "debit") or not 0 < a.amount == b.amount:
                continue
            result["direct_ledger_matches"] += 1
            if a.index not in market_out or b.index not in market_in:
                continue
            result["direct_market_matches"] += 1
            paths = matched_paths(a.index, b.index, ending, starting)
            if len(paths) == 1:
                first, second = paths[0]
                details["direct_cross_round"].append(
                    {
                        "currency": currency,
                        "holder": holder,
                        "amount_raw": a.amount,
                        "logs": (a.index, b.index),
                        "prior_round": first.swaps,
                        "next_round": second.swaps,
                    }
                )
            elif len(paths) > 1:
                result["direct_ambiguous_closed_matches"] += 1
        # Compare immediate complete, unambiguous holding intervals for same-size principals.
        by_index = {row.index: row for row in entries}
        holder_rounds = sorted(
            (r for token, actor, r in candidate_rounds if (token, actor) == (currency, holder)),
            key=lambda r: (r.debit, r.credit, r.swaps),
        )
        for prior, nxt in pairwise(holder_rounds):
            if prior.credit >= nxt.debit or not set(prior.swaps).isdisjoint(nxt.swaps):
                continue
            if len(starting[prior.debit]) != 1 or len(starting[nxt.debit]) != 1:
                continue
            if len(ending[prior.credit]) != 1 or len(ending[nxt.credit]) != 1:
                continue
            seed, payout, next_seed = (
                by_index[i].amount for i in (prior.debit, prior.credit, nxt.debit)
            )
            result["observable_adjacent_closed_path_pairs"] += 1
            if adjacent_example is None and payout != next_seed:
                adjacent_example = {
                    "currency": currency,
                    "holder": holder,
                    "prior_principal_raw": seed,
                    "prior_payout_raw": payout,
                    "next_principal_raw": next_seed,
                    "prior_round": prior.swaps,
                    "next_round": nxt.swaps,
                    "logs": (prior.debit, prior.credit, nxt.debit),
                }
            if payout == next_seed:
                result["adjacent_closed_path_equal_sizes"] += 1
            if seed == next_seed != payout:
                details["same_principal_repeat"].append(
                    {
                        "currency": currency,
                        "holder": holder,
                        "principal_raw": seed,
                        "prior_payout_raw": payout,
                        "prior_round": prior.swaps,
                        "next_round": nxt.swaps,
                        "logs": (prior.debit, prior.credit, nxt.debit),
                    }
                )
    result["explicit_holder_closed_paths"] = len(candidate_rounds)
    for name in ("observable_adjacent_closed_path_pairs", "adjacent_closed_path_equal_sizes"):
        result.setdefault(name, 0)
    result.update({kind: len(items) for kind, items in details.items()})
    return result, details, adjacent_example


def observed_logs(logs):
    return [(quantity(x["logIndex"]), x["address"], x["topics"], x["data"]) for x in logs]


def archived_case(target, sixteen, normalized, selectors):
    tx_hash, n, h, idx = (
        target[key]
        for key in ("transaction_hash", "block_number", "block_hash", "transaction_index")
    )
    closed_source = HOLDOUT if isinstance(normalized["sources"], list) else CENSUS
    reused = sixteen["reused_witnesses"].get(str(n), {}) if closed_source == CENSUS else {}
    sources, results = {}, {}
    for position, kind in enumerate(("header", "receipts", "trace")):
        stem = f"{tx_hash[2:14] if kind == 'trace' else n}-{kind}"
        old = reused.get(kind)
        root = Path(old["directory"]) if old else closed_source
        raw, request, meta = source_bytes(root, stem)
        recorded = normalized["sources"][position if closed_source == HOLDOUT else kind]
        assert meta == recorded
        operation = json.loads(request)
        if kind == "trace":
            assert operation["method"] == "debug_traceTransaction"
            assert operation["params"][0] == tx_hash
            assert operation["params"][1]["tracer"] == "callTracer"
            assert operation["params"][1]["tracerConfig"] == {"withLog": True}
        elif not old:
            assert (operation["method"], operation["params"][0]) == (
                ("eth_getBlockByNumber", hex(n))
                if kind == "header"
                else ("eth_getBlockReceipts", h)
            )
        if old:
            assert meta["response_sha256"] == old["response_sha256"]
        sources[kind] = {
            "path": str(root / (stem + ".response.json")),
            "sha256": meta["response_sha256"],
        }
        results[kind] = json.loads(raw)["result"]
    header, rows, trace = (results[kind] for kind in ("header", "receipts", "trace"))
    block, root = receipts_proof(header, rows, n, h)
    assert root == normalized["receipts_root"]
    receipt = decode_receipt(rows[idx], block.ref, tx_hash, idx)
    counted = [row for row in receipt.logs if row.topics and row.topics[0] in selectors]
    assert len(counted) == target["events"]
    frames = nested_calls(trace)
    _receipt_positions(trace, frames, receipt)
    tx = header["transactions"][idx]
    signed = isinstance(tx, dict)
    if signed:
        envelope_cost(tx, receipt, quantity(header["baseFeePerGas"]))
        assert (
            frames[()].value == quantity(tx["value"])
            and "0x" + frames[()].data.hex() == tx["input"]
        )
    assert receipt.status == 1
    assert normalized["target"] == target
    funding = replay_trace_funding(receipt, trace)
    ci = {p.input_transfer_log for p in funding.resolved_swaps if p.input_transfer_log is not None}
    co = {
        p.output_transfer_log for p in funding.resolved_swaps if p.output_transfer_log is not None
    }
    exact_rounds, no_endpoints = closed_rounds(funding)
    recorded_pairs = normalized.get("separate_two_swap_weth_bridges", [])
    internal = [row["weth_bridge_logs"] for row in recorded_pairs]
    raw_logs = observed_logs(rows[idx]["logs"])
    counts, details, adjacent_example = inspect(
        ledger(raw_logs, ETH_WETH), ETH_WETH, exact_rounds, ci, co, internal
    )
    assert counts["wrapped_known_within_round"] == len(internal)
    return {
        **target,
        "receipt_root": root,
        "full_signed_envelope_checked": signed,
        "rooted_receipt_logs": len(receipt.logs),
        "trace_matched_logs": len(receipt.logs),
        "resolved_swaps": len(funding.resolved_swaps),
        "resolved_market_debit_logs": len(ci),
        "resolved_market_credit_logs": len(co),
        "strict_funding_links": len(funding.links),
        "same_asset_closed_paths_with_boundary_log_indices": len(exact_rounds),
        "same_asset_closed_paths_missing_boundary_log_index": no_endpoints,
        "counts": dict(counts),
        "one_adjacent_different_size_example": adjacent_example,
        "recognized_within_round_wraps": [
            {"logs": row["weth_bridge_logs"], "swaps": row["swap_events"]} for row in recorded_pairs
        ],
        "details": details,
        "sources": sources,
    }


def family_wire(key, kind):
    base = RESEARCH / "op-hundred-family-20261127/optimism_drpc" / f"{key}-{kind}"
    path = base.with_suffix(".response.json")
    raw = path.read_bytes()
    meta = load(base.with_suffix(".meta.json"))
    assert digest(raw) == meta["response_sha256"]
    return json.loads(raw)["result"], digest(raw)


def control(row, legacy_136):
    if legacy_136:
        header, receipt, _, proof = witnesses(RESEARCH / "optimism-136-reinvestment-20261127")
        response_digest = proof["wires"]["receipt"]["response_sha256"]
        recorded = row["round_evidence"]
        actor = row["executor"]
        expected = row["cross_group_equal_reinvestment_edges"]
        rounds = [
            ClosedRound(
                tuple(r["swap_logs"]), r["weth_to_first_pool_log"], r["weth_from_second_pool_log"]
            )
            for r in recorded
        ]
    else:
        stem = row["tx_hash"][2:8]
        header, _ = family_wire(stem, "header")
        receipt, response_digest = family_wire(stem, "receipt")
        recorded = row["cash"]["rounds"]
        actor = receipt["to"]
        expected = row["cash"]["proved_reinvestment_transfer_links"]
        rounds = [
            ClosedRound(tuple(r["swap_logs"]), r["debit_transfer_log"], r["return_transfer_log"])
            for r in recorded
        ]
    raw_receipts = op_single_receipt_root(
        header, receipt, quantity(header["transactions"][0]["gasPrice"])
    )
    assert raw_receipts == header["receiptsRoot"]
    assert legacy_op_header_hash(header) == row["block_hash"]
    assert (
        receipt["blockHash"] == row["block_hash"] and receipt["transactionHash"] == row["tx_hash"]
    )
    movements = ledger(observed_logs(receipt["logs"]), OP_WETH)
    counts, details, _ = inspect(
        movements, OP_WETH, rounds, {r.debit for r in rounds}, {r.credit for r in rounds}
    )
    assert counts["wrapped_cross_round"] == expected == len(rounds) - 1
    assert {r["holder"] for r in details["wrapped_cross_round"]} == {actor}
    return {
        "tx_hash": row["tx_hash"],
        "chain_id": 10,
        "block_hash": row["block_hash"],
        "receipt_root": raw_receipts,
        "receipt_response_sha256": response_digest,
        "rooted_receipt_logs": len(receipt["logs"]),
        "complete_rounds": len(rounds),
        "market_swap_events": 2 * len(rounds),
        "counts": dict(counts),
        "wrapped_edges": details["wrapped_cross_round"],
    }


def main():
    assert digest(FROZEN.read_bytes()) == FROZEN_SHA256
    scope = load(FROZEN)
    for source, sha256 in scope["frozen_source_sha256"].items():
        assert digest(Path(source).read_bytes()) == sha256
    original = load(DOCS / "BATCH_HUNDRED_HOLDOUT_20261127.json")
    assert original["eligible_tail"] == sorted(
        scope["frozen_transactions"], key=original["eligible_tail"].index
    )
    previous = load(DOCS / "BATCH_HUNDRED_CENSUS_20261127.json")
    premises = {
        r["target"]["transaction_hash"]: r
        for r in (*original["cases"], *previous["additional_cases"])
    }
    select_sql = (HOLDOUT / "select.sql").read_bytes()
    assert digest(select_sql) == original["selection"]["selection_sql_sha256"]
    selectors = {
        value
        for value in select_sql.decode().split("'")
        if value.startswith("0x") and len(value) == 66
    }
    assert len(selectors) == 8
    sixteen = load(CENSUS / "scope.json")
    cases = [
        archived_case(target, sixteen, premises[target["transaction_hash"]], selectors)
        for target in scope["frozen_transactions"]
    ]
    assert len(cases) == 18 and sum(c["events"] for c in cases) == 2175
    signed = [c for c in cases if c["full_signed_envelope_checked"]]
    assert len(signed) == 17
    normalized = load(DOCS / "OPTIMISM_136_REINVESTMENT_20261127.json")["cases"]
    family = load(DOCS / "OPTIMISM_HUNDRED_CYCLE_FAMILY_20261127.json")["cases"]
    controls = [control(r, False) for r in family] + [control(normalized[0], True)]
    numbers = Counter()
    for case in cases:
        numbers.update(case["counts"])
    report = {
        "schema": "y2026-reinvestment-fingerprint/v1",
        "scope_sha256": FROZEN_SHA256,
        "new_network_requests": 0,
        "ethereum_cohort": {
            "date_utc": "2026-11-23",
            "archive_denominators": original["selection"]["denominators"],
            "receipt_root_and_trace_transactions": len(cases),
            "signed_envelope_subset_transactions": len(signed),
            "signed_envelope_subset_event_shapes": sum(c["events"] for c in signed),
            "event_shapes": sum(c["events"] for c in cases),
            "receipt_logs": sum(c["rooted_receipt_logs"] for c in cases),
            "resolved_swaps": sum(c["resolved_swaps"] for c in cases),
            "shape_events_unresolved": sum(c["events"] - c["resolved_swaps"] for c in cases),
            "closed_paths_with_boundary_log_indices": sum(
                c["same_asset_closed_paths_with_boundary_log_indices"] for c in cases
            ),
            "closed_paths_missing_boundary_log_index": sum(
                c["same_asset_closed_paths_missing_boundary_log_index"] for c in cases
            ),
            "strict_inter_swap_funding_links": sum(c["strict_funding_links"] for c in cases),
            "counts": dict(numbers),
            "transaction_hits_by_count": {
                key: sum(c["counts"].get(key, 0) > 0 for c in cases) for key in numbers
            },
            "cases": cases,
        },
        "optimism_2022_positive_controls_excluded_from_ethereum": controls,
    }
    print(json.dumps(report, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
