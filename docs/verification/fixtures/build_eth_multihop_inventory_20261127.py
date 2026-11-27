"""Index existing Ethereum multihop evidence without executing captures or accessing the network."""

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path

DOC = Path("docs/verification")
RAW = Path("reports/research")
DATE = "20261127"
HASH = re.compile(r"0x[0-9a-f]{64}\Z")
STAGES = {
    "queue": 0,
    "individual_request": 1,
    "landed_index": 2,
    "reported_review": 3,
    "receipt_review": 4,
    "rooted_receipt": 5,
    "trace_review": 6,
    "rooted_trace": 7,
}
VALID_SHAPES = {"serial", "batch", "mixed", "unresolved", "control"}


def report(name):
    return DOC / f"{name}_{DATE}.json"


def data(path):
    return json.loads(path.read_bytes())


def num(v):
    if isinstance(v, str) and re.fullmatch(r"0x[0-9a-f]{1,16}", v):
        return int(v, 16)
    return int(v) if type(v) is int or (isinstance(v, str) and v.lstrip("-").isdigit()) else None


def count(value):
    return len(value) if isinstance(value, list) else num(value)


def collect_values(row, names):
    return {dest: row[key] for key, dest in names.items() if row.get(key) is not None}


class Inventory:
    def __init__(self):
        self.samples = {}
        self.cohorts = []
        self.diagnostics = []

    def row(self, tx, source, stage, *, shape=None, note=None, identity=None, measures=None):
        assert HASH.fullmatch(tx), (tx, source)
        assert stage in STAGES and (shape is None or shape in VALID_SHAPES)
        if not source.is_file():
            raise ValueError(f"missing inventory source: {source}")
        rec = self.samples.setdefault(tx, {"tx_hash": tx, "shape": "unresolved", "evidence": []})
        for k, raw in (identity or {}).items():
            v = num(raw) if k in ("block_number", "transaction_index") else raw
            if v is None:
                continue
            if k == "block_hash" and not HASH.fullmatch(v):
                raise ValueError(f"invalid block identity for {tx}")
            if k in rec and rec[k] != v:
                raise ValueError(f"conflicting {k} for {tx}: {rec[k]} != {v} ({source})")
            rec[k] = v
        item = {"source": source.as_posix(), "stage": stage}
        if measures:
            item["observations"] = {k: v for k, v in measures.items() if v is not None}
        if note:
            item["scope"] = note
        if item not in rec["evidence"]:
            rec["evidence"].append(item)
        if shape is not None and shape != "unresolved":
            old = rec["shape"]
            if old not in (shape, "unresolved"):
                if "control" in (old, shape):
                    self.diagnostics.append(
                        {
                            "tx_hash": tx,
                            "scope": f"control and {old if shape == 'control' else shape} source",
                        }
                    )
                    rec["shape"] = shape if old == "control" else old
                else:
                    rec["shape"] = "mixed"
            else:
                rec["shape"] = shape

    def source(self, source, stage, tx, row, **opts):
        identity = {
            "block_number": next(
                (
                    row[k]
                    for k in ("height", "block_number", "block")
                    if num(row.get(k)) is not None
                ),
                None,
            ),
            "block_hash": row.get("block_hash"),
            "transaction_index": next(
                (
                    row[k]
                    for k in ("transaction_index", "tx_index", "index", "position", "target_index")
                    if num(row.get(k)) is not None
                ),
                None,
            ),
        }
        self.row(tx, source, stage, identity=identity, **opts)

    def cohort(self, label, path, **fields):
        if not path.is_file():
            raise ValueError(f"missing cohort source: {path}")
        self.cohorts.append({"id": label, "source": path.as_posix(), **fields})

    def finished(self):
        ordered = sorted(self.samples.values(), key=lambda row: row["tx_hash"])
        for rec in ordered:
            rec["evidence"].sort(
                key=lambda x: (-STAGES[x["stage"]], x["source"], json.dumps(x, sort_keys=True))
            )
            rec["strongest_evidence"] = rec["evidence"][0]["stage"]
            metric_names = (
                "funded_market_swap_depth_lower_bound",
                "same_currency_market_swap_depth_lower_bound",
                "verified_closed_hybrid_actions",
                "verified_closed_market_swaps",
                "raw_equal_amount_proxy_depth",
            )
            for name in metric_names:
                witnesses = [
                    item["observations"][name]
                    for item in rec["evidence"]
                    if item.get("observations", {}).get(name) is not None
                ]
                if witnesses:
                    rec[name] = max(witnesses)
            for field in ("receipts_root", "gas_paid_wei", "direct_fee_recipient_wei"):
                values = {}
                for witness in rec["evidence"]:
                    value = witness.get("observations", {}).get(field)
                    if value is not None:
                        value = value if field == "receipts_root" else num(value)
                        if value is not None:
                            values.setdefault(value, []).append(witness["source"])
                if len(values) > 1:
                    self.diagnostics.append(
                        {"tx_hash": rec["tx_hash"], "field": field, "values": values}
                    )
        return {
            "schema": "oasis-ethereum-multihop-inventory-v1",
            "research_date": "2026-11-27",
            "chain_id": 1,
            "chain": "ethereum",
            "samples": ordered,
            "cohorts": sorted(self.cohorts, key=lambda row: row["id"]),
            "counts": {
                "unique_transactions": len(ordered),
                "by_shape": dict(Counter(r["shape"] for r in ordered)),
                "by_strongest_evidence": dict(Counter(r["strongest_evidence"] for r in ordered)),
            },
            "source_conflicts": sorted(
                self.diagnostics, key=lambda row: (row["tx_hash"], row.get("field", "shape"))
            ),
            "limits": [
                (
                    "One entry per full transaction hash. Ethereum block hashes, PoolIds, "
                    "unsigned simulation hashes and unverified short prefixes are excluded."
                ),
                (
                    "Ledger denominators overlap. Cohorts must not be summed and screen-only rows"
                    " do not count as independently audited."
                ),
                (
                    "Shape describes witnessed funding layout, not venue labels, attacker intent,"
                    " strategy edge or net profit."
                ),
                (
                    "Event counts, raw equal-amount proxies, strict market-swap depth, hybrid "
                    "state transitions and partial split layers use separate fields."
                ),
                (
                    "Native receipts and direct payments are observed scope. Unpriced inventory, "
                    "common control, external payments and implicit costs can remain unresolved."
                ),
            ],
        }


def costs(row):
    return collect_values(
        row,
        {
            "gas_used": "gas_used",
            "gas_paid_wei": "gas_paid_wei",
            "direct_fee_recipient_wei": "direct_fee_recipient_wei",
            "combined_visible_eth_weth_after_gas_wei": "visible_role_eth_weth_delta_after_gas_wei",
            "receipt_root": "receipts_root",
            "other_erc20_visible_deltas": "other_erc20_raw",
        },
    )


def recent_tracked(book):
    q = report("MULTIHOP_TRACE_QUEUE")
    d = data(q)
    for r in d["samples"]:
        assert r["chain_id"] == 1
        loans = r.get("verified_weth_principal_groups", [])
        book.source(
            q,
            "rooted_trace" if r["trace_status"] == "receipt_logs_matched" else "rooted_receipt",
            r["tx"],
            dict(r, block_hash=r["hash"]),
            shape="batch" if len(loans) > 1 else None,
            measures={
                **costs(r),
                "archive_selector_events": r["archive_events"],
                "funded_market_swap_depth_lower_bound": r["funded_depth_lower_bound"],
                "trace_status": r["trace_status"],
                "resolved_swap_legs": r.get("resolved"),
                "verified_weth_principal_groups": len(loans),
            },
        )
    book.cohort(
        "family_trace_queue",
        q,
        kind="individual_trace_targets",
        selected=d["selected"],
        verified=d["verified"],
        summary=d["totals"],
    )

    p = report("MULTIHOP_FAMILY_SCREEN")
    d = data(p)
    for r in d["audit_queue"]["transactions"]:
        book.source(p, "queue", r["tx"], r, measures={"selected_by": r["selected_by"]})
    for r in d["reviewed_identities"]:
        assert r["chain_id"] == 1
        book.source(
            p,
            "queue",
            r["tx"],
            r,
            note="Identity already screened elsewhere; no additional audit counted.",
        )
    for r in d["peer_audits"]:
        book.source(
            p,
            "rooted_trace",
            r["tx"],
            dict(r, block_hash=r["hash"]),
            measures={
                **costs(r),
                "archive_selector_events": r["archive_events"],
                "funded_market_swap_depth_lower_bound": len(r["path"]),
                "principal_evidence": r.get("settlement"),
            },
        )
    book.cohort(
        "three_day_family_screen",
        p,
        kind="overlapping_batch_screen",
        days_utc=d["days"],
        selector_rows=d["matched_rows"],
        candidates_at_least_five=23285,
        at_least_100=25,
        individual_trace_queue=d["audit_queue"]["selected_unique"],
    )

    p = report("ETH_109_MOTIVE")
    d = data(p)
    book.source(
        p,
        "rooted_trace",
        d["target_tx"],
        dict(d, height=d["block_height"]),
        shape="batch",
        measures={
            "counting_convention_events": 109,
            "verified_weth_principal_groups": len(d["funding_batches"]),
            "funded_market_swap_depth_lower_bound": d["strict_funded_subgraph"]["witnessed_depth"],
            "partial_allocation_layer_depth": 5,
            "gas_paid_wei": d["gas_paid_wei"],
            "gas_used": d["gas_used"],
            "direct_fee_recipient_wei": d["direct_fee_wei"],
            "receipts_root": d["receipts_root"],
            "visible_wei_if_common_control": d[
                "visible_after_execution_and_direct_fee_if_one_owner_wei"
            ],
        },
        note=(
            "Public fingerprint and four separate fundings; poster identity and offchain "
            "costs remain unresolved."
        ),
    )
    prev = d["oracle_join"]
    book.source(
        p,
        "receipt_review",
        prev["previous_tx"],
        {
            "block_hash": d["block_hash"],
            "height": d["block_height"],
            "index": prev["previous_tx_index"],
        },
        shape="control",
        note="Actual prior oracle update; not itself an arbitrage sample.",
    )
    for k in ("ETH_109_STATE_PAIR", "ETH_109_POST_SETTLEMENT"):
        p = report(k)
        d = data(p)
        book.source(
            p,
            "reported_review",
            d["tx"],
            d,
            note="Same landed transaction, historical counterfactual states only.",
            measures={"conditional_oracle_delta_wei": d.get("net_change_wei")},
        )

    p = report("ETH_TWENTY_FLOW")
    d = data(p)
    book.source(
        p,
        "rooted_trace",
        d["tx"],
        d,
        shape="serial",
        measures={
            "verified_closed_hybrid_actions": d["closed_hybrid_legs"],
            "verified_closed_market_swaps": d["closed_market_swap_legs"],
            "cashout_inclusive_hybrid_actions": d["cashout_hybrid_legs"],
            "consecutive_market_swap_segment": 31,
            "raw_equal_amount_proxy_depth": len(d["candidate_20"]),
            "receipts_root": d["receipt_root"],
            "landed_fees_and_role_cash": d["fees"],
        },
        note=(
            "54 closed actions include RFQ and LP redemption; another swap cashes out. "
            "Role USDC change is -2 raw."
        ),
    )
    p = report("ETH_ARCHIVE_SERIAL_SEARCH")
    d = data(p)
    for rank_index, r in enumerate(d["decoded_cases"]):
        target = r["archive_selection"]
        book.source(
            p,
            "rooted_trace",
            target["tx"],
            target,
            shape="control" if rank_index == 1 else None,
            measures={
                "archive_selector_events": target["events"],
                "pool_keys": target["pools"],
                "raw_equal_amount_proxy_depth": target["amount_depth"],
                "funded_market_swap_depth_lower_bound": count(
                    r["funding_decoder"].get("retained_path")
                ),
                "gas_paid_wei": r["receipt_gas_wei"],
                "receipts_root": r["receipt_root"],
                "unpriced_role_erc20_raw": r["roles_nonzero_erc20_by_address"],
            },
        )
    book.cohort(
        "two_day_aws_archive_serial_screen",
        p,
        kind="full_day_selector_screen",
        days_utc=d["archive_days_utc"],
        candidates_at_least_five=d["screened_candidates"],
        raw_equal_amount_depth_hist=d["amount_depth_histogram"],
        fully_reviewed=len(d["decoded_cases"]),
    )

    p = report("FUNDED_WALK_SAMPLES")
    for i, r in enumerate(data(p)):
        book.source(
            p,
            "rooted_trace",
            r["tx"],
            dict(r, block_hash=r["hash"]),
            shape="serial" if i < 2 else "control",
            measures={**costs(r), "funded_market_swap_depth_lower_bound": len(r["path"])},
            note=(
                "A witnessed funded path within the landed transaction; entire transaction "
                "may contain other actions."
            ),
        )

    p = report("ETH_BATCH_COHORT")
    d = data(p)
    for r in d["rooted_samples"]:
        t = r.get("trace", {})
        broad = t.get("supported_swaps_by_unlock", [])
        book.source(
            p,
            "rooted_trace" if t.get("status") == "rooted_logs_match" else "rooted_receipt",
            r["transaction_hash"],
            r,
            shape=(
                "batch" if len(broad) > 1 else "control" if "control" in r["case_role"] else None
            ),
            measures={
                **costs(r),
                "selection_role": r["case_role"],
                "receipt_shape_events": r["receipt_funding"].get("shape_events"),
                "funded_market_swap_depth_lower_bound": count(t.get("generic_funded_path")),
                "manager_unlocks": t.get("manager_unlock_count"),
                "verified_weth_principal_groups": len(t.get("confirmed_take_settle_groups", [])),
                "direct_fee_recipient_wei": t.get(
                    "explicit_native_payments_to_block_fee_recipient_wei"
                ),
            },
        )
    for r in (d["exact_target"], *d["recurring_samples"]):
        book.source(
            p,
            "queue",
            r["transaction_hash"],
            r,
            note="Per-identity archive screening, independent audit indexed separately.",
        )
    book.cohort(
        "executor_cohort_three_dates",
        p,
        kind="overlapping_batch_screen",
        coverage=d["coverage"],
        executor_transactions=d["executor_transactions"],
        thresholds=d["thresholds"],
        fully_reviewed=len(d["rooted_samples"]),
    )


def held_out(book):
    p = report("BATCH_HUNDRED_HOLDOUT")
    d = data(p)
    for r in d["eligible_tail"]:
        book.source(
            p,
            "queue",
            r["transaction_hash"],
            r,
            measures={"archive_selector_events": r["events"], "pool_keys": r["pool_keys"]},
        )
    for r in d["cases"]:
        t = r["target"]
        strict = r["generic_depth"]
        extra = len(r.get("separate_two_swap_weth_bridges", []))
        closed = r.get("strict_components", {}).get("closed_component_sizes", {})
        book.source(
            p,
            "rooted_trace",
            t["transaction_hash"],
            t,
            shape="batch",
            measures={
                "archive_selector_events": t["events"],
                "pool_keys": t["pool_keys"],
                "generic_funded_market_swap_depth_lower_bound": strict,
                "funded_market_swap_depth_lower_bound": max(strict, 2 if extra else 0),
                "verified_weth_bridge_groups": extra,
                "closed_group_counts_by_depth": closed,
                "swap_bearing_call_groups": len(r["swap_branches"]),
                "morpho_principals": len(r["morpho_principals"]),
                "gas_paid_wei": r["envelope_fees"]["gas_wei"],
                "receipts_root": r["receipts_root"],
                "direct_fee_recipient_wei": r["observed_payout"][
                    "direct_eth_to_header_fee_receiver_wei"
                ],
                "visible_asset_deltas": r["observed_payout"]["visible_role_deltas"],
            },
        )
    book.cohort(
        "nov23_day_eight_selectors",
        p,
        kind="full_day_selector_screen",
        date_utc=d["selection"]["date_utc"],
        thresholds=d["selection"]["denominators"],
        hundred_event_individual_audits=18,
    )

    p = report("BATCH_HUNDRED_CENSUS")
    for r in data(p)["additional_cases"]:
        t, f, s = r["target"], r["funding"], r["settlement"]
        book.source(
            p,
            "rooted_trace" if r["status"] == "verified" else "rooted_receipt",
            t["transaction_hash"],
            t,
            shape="mixed" if f["same_currency_depth"] == 6 else None,
            measures={
                "archive_selector_events": t["events"],
                "pool_keys": t["pool_keys"],
                "resolved_swap_legs": f["resolved_events"],
                "funded_market_swap_depth_lower_bound": f["strict_depth"],
                "same_currency_market_swap_depth_lower_bound": f["same_currency_depth"],
                "receipts_root": r["receipts_root"],
                "gas_paid_wei": r["fees"]["gas_wei"],
                "fee_validation": r["fee_validation"],
                "direct_fee_recipient_wei": s["direct_eth_to_header_fee_receiver_wei"],
                "visible_asset_deltas": s["visible_role_deltas"],
            },
        )
    p = report("ETH_BATCH_INCREMENTAL_NET")
    for r in data(p)["cases"]:
        book.source(
            p,
            "reported_review",
            r["tx"],
            dict(r, block_hash=r["hash"]),
            measures={
                "batch_called": r["batch"].get("called"),
                "observed_conditional_full_minus_drop": r.get("full_minus_drop"),
            },
            note=(
                "Adds a historical simulation comparison to existing landed sample; does not "
                "add a new settlement."
            ),
        )

    p = report("ETH_UNSEEN_DAY_HOLDOUT")
    d = data(p)
    w = d["limited_window"]
    for i, r in enumerate(w["ranked_top"]):
        book.source(
            p,
            "rooted_trace" if i == 0 else "rooted_receipt",
            r["hash"],
            r,
            shape="mixed" if i == 0 else None,
            measures={
                "archive_selector_events": r["events"],
                "pool_keys": r["pool_keys"],
                "raw_equal_amount_proxy_depth": r["equal_amount_proxy_depth"],
                **(
                    {
                        "verified_closed_market_swaps": 6,
                        "verified_short_hybrid_actions": 4,
                        "generic_funded_market_swap_depth_lower_bound": w["audit"]["funding"][
                            "strict_depth"
                        ],
                        "gas_paid_wei": w["audit"]["fees"]["gas_wei"],
                        "direct_fee_recipient_wei": w["audit"]["settlement"][
                            "direct_eth_to_header_fee_receiver_wei"
                        ],
                        "visible_asset_deltas": w["audit"]["settlement"]["visible_role_deltas"],
                    }
                    if i == 0
                    else {}
                ),
            },
            note=None
            if i == 0
            else "Individually ranked from commitment-checked receipts; not individually traced.",
        )
    book.cohort(
        "nov21_contiguous_twelve_rooted_blocks",
        p,
        kind="rooted_entire_block_window",
        thresholds=w["thresholds"],
        complete_blocks=len(w["complete_blocks"]),
        transactions=w["tx_total"],
        traced=1,
    )
    book.cohort(
        "nov26_stopped_full_day", p, kind="incomplete_archive", details=d["incomplete_utc_day"]
    )


def historic(book):
    p = RAW / "eth-batch-selector-cohort-20261123/verified.json"
    d = data(p)
    failures = {f["hash"]: f for f in d["failures"]}
    for r in d["cohort"]:
        f = failures.get(r["hash"])
        book.source(
            p,
            "rooted_receipt" if f else "landed_index",
            r["hash"],
            f or r,
            shape="control" if f else None,
            measures={
                "gas_used": r["gas_used"],
                "gas_paid_wei": r["gas_wei"],
                "receipt_succeeded": r["success"],
                "receipts_root": f.get("receipt_root") if f else None,
            },
        )
    book.cohort(
        "executor_selector_fixed_two_week",
        p,
        kind="fixed_landed_identity_list",
        index_rows=d["all_index_rows"],
        scope=d["scope"],
        all_landed=len(d["cohort"]),
        failure_controls=len(failures),
    )

    p = RAW / "eth-cohort-followup-20261123/verified.json"
    for r in data(p):
        q = r["cohort_row"]
        book.source(
            p,
            "rooted_trace",
            q["hash"],
            dict(q, block_hash=r["block_hash"]),
            shape="batch",
            measures={
                "funded_market_swap_depth_lower_bound": max(
                    (g["core_exact_swaps"] for g in r["groups"] if "core_exact_swaps" in g),
                    default=None,
                ),
                "groups": len(r["groups"]),
                "gas_paid_wei": q["gas_wei"],
                "direct_fee_recipient_wei": r["direct_fee_wei"],
                "visible_net_wei": r["visible_net_wei"],
                "receipts_root": r["receipts_root"],
            },
        )

    p = RAW / "eth-cohort-closure-20261124/verified.json"
    d = data(p)
    for r in d["new_cases"]:
        q = r["cohort_row"]
        routes = r["amount_proven_routes"]
        book.source(
            p,
            "rooted_trace",
            q["hash"],
            dict(q, block_hash=r["block_hash"]),
            shape="batch" if len(routes) > 1 else "serial" if r["known_swaps"] > 1 else "control",
            measures={
                "funded_exchange_actions_by_group": [len(x["all_exchange_legs"]) for x in routes],
                "gas_paid_wei": q["gas_wei"],
                "direct_fee_recipient_wei": r["direct_to_fee_recipient_wei"],
                "receipt_shape_events": r["known_swaps"],
                "receipts_root": r["receipts_root"],
            },
        )
    for tx, eq in d["verified_success_eth_equiv"].items():
        book.row(
            tx,
            p,
            "reported_review",
            measures={"visible_role_eth_weth_delta_after_gas_wei": eq},
            note=(
                "Aggregated economic closure review of previously included successes; source "
                "does not independently rebind all rows."
            ),
        )

    p = RAW / "eth-cohort-cost-outlier-20261123/verified.json"
    r = data(p)
    q = r["cohort_row"]
    book.source(
        p,
        "rooted_trace",
        q["hash"],
        dict(q, block_hash=r["block_hash"]),
        shape="batch",
        measures={
            "receipt_shape_events": r["committed_dex_swaps"],
            "pool_keys": r["logical_pool_count"],
            "funded_groups": len(r["groups"]),
            "market_swaps_per_group_histogram": r["core_swap_histogram"],
            "gas_paid_wei": r["gas_cost_wei"],
            "direct_fee_recipient_wei": r["eth_direct_payment_wei"],
            "visible_role_eth_weth_delta_after_gas_wei": r["visible_net_wei_eq"],
        },
    )

    p = RAW / "eth-long-serial-20261124/named-10/verified.json"
    r = data(p)
    book.source(
        p,
        "rooted_trace",
        r["transaction_hash"],
        r,
        shape="batch",
        measures={
            "receipt_shape_events": r["modeled_swap_events"],
            "pool_keys": r["distinct_pool_identities"],
            "funded_market_swap_depth_lower_bound": r["longest_funded_depth"],
            "funded_groups": len(r["groups"]),
            "gas_paid_wei": r["gas_cost_wei"],
            "direct_fee_recipient_wei": r["direct_fee_recipient_wei"],
            "receipts_root": r["verified_receipts_root"],
        },
    )
    p = DOC / "ETH_LONG_SERIAL_EVIDENCE_20261124.md"
    book.row(
        "0x3223237e50df220f7519be91cf69c5dbd7f32d132653c29724f82af4dfbb23b9",
        p,
        "rooted_receipt",
        shape="control",
        identity={"block_number": 26459067, "transaction_index": 39},
        measures={
            "receipt_shape_events": 6,
            "pool_keys": 6,
            "funded_market_swap_depth_lower_bound": 4,
        },
        note="Open user routing split/merge control; same-asset closure is not claimed.",
    )
    book.cohort(
        "ten_pool_same_block_rooted_screen",
        p,
        kind="rooted_entire_block_window",
        blocks=1,
        transactions=450,
        matching_transactions=82,
        at_least_five=6,
    )
    book.cohort(
        "older_256_block_source_refusal",
        p,
        kind="incomplete_window",
        first_height=26452736,
        last_height=26452991,
        matched_transactions=None,
        reason="anonymous log limit exceeded",
    )

    p = RAW / "eth-funded-holdout-20261124/verified.json"
    for r in data(p)["cases"]:
        routes = r.get("funded_closed_routes") or []
        book.source(
            p,
            "rooted_trace",
            r["tx"],
            r,
            shape="batch" if len(routes) > 1 else "serial" if routes else "control",
            measures={
                "archive_selector_events": r["swap_events"],
                "raw_equal_amount_proxy_depth": r["raw_candidate_depth"],
                "funded_market_swap_depth_lower_bound": max(
                    (len(x["legs"]) for x in routes), default=None
                ),
                "funded_groups": len(routes),
                "gas_paid_wei": r["gas_cost_wei"],
                "receipts_root": r["receipts_root"],
                "direct_fee_recipient_wei": r["direct_native_fee_recipient_wei"],
            },
        )
    p = RAW / "eth-funded-holdout-20261124/selection.json"
    d = data(p)
    for tag in ("top", "top_by_event"):
        for r in d[tag]:
            book.source(
                p,
                "queue",
                r["tx"],
                r,
                measures={
                    "ranking": tag,
                    "raw_equal_amount_proxy_depth": r["raw_candidate_depth"],
                    "archive_selector_events": r["swap_events"],
                },
            )
    book.cohort(
        "64_block_ethereum_funded_holdout",
        p,
        kind="indexed_window",
        matched_log_rows=d["event_rows"],
        matched_transactions=d["transactions_with_events"],
        event_hist=d["event_count_histogram"],
        traced=3,
    )

    p = RAW / "eth-bancor-decoder-20261124/verified.json"
    d = data(p)
    for r in d["cases"]:
        book.source(
            p,
            "rooted_trace",
            r["tx"],
            dict(r, height=d["height"], block_hash=d["block_hash"]),
            shape="serial" if r["depth"] == 7 else "control",
            measures={
                "funded_market_swap_depth_lower_bound": r["depth"],
                "gas_paid_wei": r["gas_paid_wei"],
                "receipts_root": d["receipt_root"],
                "visible_net_wei": r.get("observed_full_net_wei"),
            },
        )
    for p2 in p.parent.glob("*-result.json"):
        r = data(p2)
        book.source(
            p2,
            "trace_review",
            r["transaction_hash"],
            r,
            measures={
                "funded_market_swap_depth_lower_bound": count(
                    r["trace_funding"]["profile"].get("longest_witnessed_path")
                ),
                "receipt_shape_events": r["receipt_swap_events"],
            },
        )
    book.cohort(
        "80_block_bancor_holdout",
        p,
        kind="indexed_window",
        blocks=80,
        matching_blocks=d["blocks_with_shape_matches"],
        matching_transactions=d["with_matches_txs"],
        matched_log_rows=d["matched_event_rows"],
        thresholds=d["selected_raw_dispersion"],
        traced=2,
    )

    p = RAW / "eth-two-four-hop-20261124/verified.json"
    d = data(p)
    for r in d["route_evidence"]:
        book.source(
            p,
            "rooted_trace",
            r["tx"],
            dict(r, block=d["block"], block_hash=d["block_hash"]),
            shape="serial",
            measures={
                "funded_market_swap_depth_lower_bound": len(r["swaps"]),
                "gas_paid_wei": r["gas_cost_wei"],
                "receipts_root": d["receipt_root"],
                "visible_net_wei": r["visible_net_wei"],
            },
        )

    p = RAW / "serial-long-tail-20261123/ethereum/verified.json"
    r = data(p)
    book.source(
        p,
        "rooted_trace",
        r["hash"],
        dict(r, block=num(r["block"]["number"]), block_hash=r["block"]["hash"]),
        shape="batch",
        measures={
            "swap_shape_counts": r["swap_counts"],
            "funded_groups": len(r["successful_groups"]),
            "funded_market_swap_depth_lower_bound": 3,
            "gas_paid_wei": r["gas_wei"],
            "direct_fee_recipient_wei": r["direct_payment_wei"],
            "receipts_root": r["receipt_root"],
        },
        note=(
            "111 event convention, 117 later-decoded pool swaps plus two conversions. "
            "None is the deepest path."
        ),
    )

    p = RAW / "recent-dex-arbitrage-20261123/ethereum/verified.json"
    r = data(p)
    book.source(
        p,
        "trace_review",
        r["tx"],
        r,
        shape="serial",
        measures={
            "funded_market_swap_depth_lower_bound": len(r["legs"]),
            "gas_paid_wei": r["gas_wei"],
            "direct_fee_recipient_wei": r["native_fee_recipient_payment_wei"],
            "visible_net_wei": r["operational_net_eth_equiv_wei"],
        },
    )

    for path in (RAW / "eth-forward-and-tail-20261127/historical").glob("*-verified.json"):
        r = data(path)
        q = r["principal"]
        f = r["generic_funding"]
        s = r["settlement"]
        w = r["serial_witness"]
        book.source(
            path,
            "rooted_trace",
            q["transaction_hash"],
            q,
            shape="serial" if w else "batch",
            measures={
                "receipt_shape_events": f["recognized_receipt_swaps"],
                "funded_market_swap_depth_lower_bound": w["distinct_serial_pools"]
                if w
                else f["depth"],
                "verified_closed_hybrid_actions": w["serial_actions"] if w else None,
                "verified_weth_principal_groups": q["verified_principal_groups"],
                "receipts_root": q["receipts_root"],
                "gas_paid_wei": s["paid_gas_wei"],
                "direct_fee_recipient_wei": s["direct_eth_to_header_fee_receiver_wei"],
                "visible_asset_deltas": s["visible_role_deltas"],
            },
        )

    p = RAW / "eth-transfer-fingerprint-fees-20261127/verified.json"
    d = data(p)
    r = d["target"]
    book.source(
        p,
        "receipt_review",
        r["tx"],
        r,
        measures={"gas_paid_wei": r["receipt_fee_wei"], "transfer_events": r["event_count"]},
    )
    book.cohort(
        "109_fingerprint_fee_screen",
        p,
        kind="indexed_daily_comparison",
        date_utc=d["date"],
        matched_gas_cohort=d["matched"],
        daily=d["daily"],
    )


RAW_METHODS = {
    "debug_traceTransaction",
    "eth_getTransactionReceipt",
    "eth_getTransactionByHash",
    "trace_replayTransaction",
    "trace_transaction",
}


def raw_and_documents(book):
    because = (
        "Raw targeted request proves the requested identity; audit status is recorded"
        " from independent records."
    )
    folders = [p for p in RAW.glob("eth*2026112[2-7]") if p.is_dir()]
    folders += [
        RAW / x
        for x in (
            "funded-walk-20261127",
            "long-search-frontier-20261127",
            "multihop-family-screen-20261127",
            "recent-dex-arbitrage-20261123/ethereum",
            "serial-long-tail-20261123/ethereum",
            "forward-opportunity-20261123",
            "long-dex-20261123/ethereum-rpc",
            "multichain-forward-20261122/ethereum",
        )
    ]
    for folder in sorted(folders):
        for p in sorted(folder.rglob("*")):
            if p.is_symlink() or not p.is_file() or not ("request" in p.name or p.suffix == ".req"):
                continue
            if p.stat().st_size > 1024 * 1024:
                continue
            try:
                response = data(p)
            except ValueError, UnicodeDecodeError:
                continue
            for row in response if isinstance(response, list) else [response]:
                if not isinstance(row, dict) or row.get("method") not in RAW_METHODS:
                    continue
                params = row.get("params")
                if (
                    isinstance(params, list)
                    and params
                    and isinstance(params[0], str)
                    and HASH.fullmatch(params[0])
                ):
                    book.row(
                        params[0],
                        p,
                        "individual_request",
                        note=because,
                        measures={"method": row["method"]},
                    )

    links = re.compile(
        r"https://(?:etherscan\.io|eth\.blockscout\.com)/tx/(0x[0-9a-f]{64})(?![0-9a-f])"
    )
    for p in sorted(DOC.glob("*2026112[2-7].md")):
        if not re.search(r"^(ETH|MULTIHOP|MULTICHAIN|RECENT_DEX|LONG_SWAP|FUNDED_WALK)", p.name):
            continue
        for tx in sorted(set(links.findall(p.read_text()))):
            book.row(
                tx,
                p,
                "landed_index",
                note="Explicit Ethereum explorer transaction reference in dated verification.",
            )
    p = Path("docs/strategies/MULTIHOP_ARBITRAGE_TODO.md")
    book.row(
        "0x275f9642556a58802eafdd8289aa19a7275e9d40962056095bb9b5c51ac3d246",
        p,
        "queue",
        note=(
            "External paper split/merge control reported with Ethereum hash; no local "
            "full audit indexed."
        ),
    )


def additional_queues(book):
    p = RAW / "forward-opportunity-20261123/eth-repeat-verified.json"
    for r in data(p):
        book.source(
            p,
            "rooted_trace",
            r["hash"],
            r,
            shape="batch",
            measures={
                "receipt_events_by_shape": r["swap_family_counts"],
                "successful_self_call_groups": r["successful_self_call_groups"],
                "positive_weth_delta_groups": r["positive_weth_delta_groups"],
                "gas_paid_wei": r["gas_wei"],
                "direct_fee_recipient_wei": r["direct_fee_recipient_wei"],
                "receipts_root": r["receipts_root"],
                "timestamp_utc": r["utc"],
                "visible_role_eth_weth_delta_after_gas_wei": r["observable_total_eth_equiv_wei"],
                "unpriced_role_erc20_raw": r["other_final_erc20_deltas"],
            },
        )

    p = RAW / "long-dex-20261123/ethereum-rpc/result.json"
    d = data(p)
    for r in d["top"]:
        book.source(
            p,
            "receipt_review",
            r["tx"],
            dict(r, block_hash=r["blockHash"]),
            measures={"receipt_shape_events": r["count"], "pool_keys": r["pools"]},
            note="Full-block receipt event screen with limited individually retained rankings.",
        )
    book.cohort(
        "early_eight_block_receipt_screen",
        p,
        kind="full_block_receipt_selector_screen",
        blocks=len(d["succeeded"]),
        transactions_matching=d["transactions"],
        events=d["logs"],
        event_hist=d["hist"],
    )

    p = RAW / "eth-forward-and-tail-20261127/history-unreviewed.json"
    d = data(p)
    for r in (d["batch"], *d["novel_top"]):
        book.source(
            p,
            "queue",
            r["tx"],
            r,
            measures={
                "archive_selector_events": r["events"],
                "pool_keys": r.get("pools"),
                "raw_equal_amount_proxy_depth": r.get("amount_depth"),
                "manager_transfer_pair_proxy": r.get("transfer_pairs"),
            },
        )
    book.cohort(
        "nov23_equal_amount_and_weth_screen",
        p,
        kind="overlapping_batch_screen",
        coverage=d["coverage"],
        candidates_at_least_five=d["processed_txs_ge5"],
        positive_manager_transfer_pair_proxy=d["batch_txs"],
        raw_equal_amount_depth_hist=d["depth_histogram"],
    )

    p = RAW / "eth-open-archive-20261127/selection.json"
    d = data(p)
    for key in ("by_depth", "by_events"):
        for r in d[key]:
            book.source(
                p,
                "queue",
                r["tx"],
                r,
                measures={
                    "rank_dimension": key,
                    "archive_selector_events": r["events"],
                    "pool_keys": r["pools"],
                    "raw_equal_amount_proxy_depth": r["amount_depth"],
                },
            )

    p = RAW / "funded-walk-20261127/blind/selection.json"
    d = data(p)
    for r in d["txs"]:
        book.source(
            p,
            "queue",
            r["tx"],
            r,
            measures={
                "raw_equal_amount_proxy_depth": r["amount_depth"],
                "archive_selector_events": r["events"],
            },
        )
    book.cohort(
        "funded_walk_blind_subsample",
        p,
        kind="individual_ranked_candidates",
        eligible=d["eligible"],
    )

    p = RAW / "eth-long-serial-20261124/named-10/block.scan.json"
    d = data(p)
    for field in ("top_direct", "top_swap"):
        for r in d[field]:
            book.source(
                p,
                "rooted_receipt",
                r["hash"],
                dict(r, block=d["block"], block_hash=d["block_hash"]),
                measures={
                    "receipt_shape_events": r["swap_rows_supported"],
                    "receipt_only_direct_funding_depth": r["direct_depth"],
                    "pool_keys": r["pools"],
                },
            )

    for name, shape, note in (
        (
            "transaction.json",
            "batch",
            "Historical routed split/merge with a common exit, matched to paper illustration.",
        ),
        (
            "harvest.json",
            "control",
            "Landed harvest control with swap-like logs and incomplete explorer transfers.",
        ),
    ):
        p = RAW / "multichain-discovery-20261122/eth-explorer" / name
        r = data(p)
        book.source(
            p,
            "receipt_review",
            r["hash"],
            r,
            shape=shape,
            measures={"gas_used": r["gas_used"], "gas_paid_wei": r["fee"]["value"]},
            note=note,
        )
        discovery = Path("docs/strategies/MULTICHAIN_MULTIHOP_DISCOVERY.md")
        measures = (
            {
                "receipt_shape_events": 7,
                "funded_market_swap_depth_lower_bound": 3,
                "split_branches": 3,
                "gross_weth_wei": 2307106383985019,
                "visible_after_receipt_gas_wei_eq": 55357831012529,
            }
            if name == "transaction.json"
            else {"receipt_logs": 70, "visible_usdc_raw": 19211992442}
        )
        book.source(discovery, "receipt_review", r["hash"], r, measures=measures)

    p = RAW / "eth-batch-cohort-20261127/rankable.csv"
    with p.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    book.cohort(
        "executor_and_comparator_rankable_export",
        p,
        kind="overlapping_indexed_population",
        rows=len(rows),
        unique_transactions=len({r["transaction_hash"] for r in rows}),
        identity_field="transaction_hash",
    )

    p = RAW / "multichain-forward-20261122/ethereum/result.json"
    d = data(p)
    book.cohort(
        "early_forward_v2_synthetic_routes",
        p,
        kind="read_only_simulation_controls",
        states=len(d["samples"]),
        envelopes=len(d["execution_controls"]),
        landed_new_transactions=0,
    )

    p = RAW / "ethereum-traces-20261114/replay-c.json"
    d = data(p)
    assert d["chain_id"] == 1
    for r in d["aave"]["rows"]:
        book.source(
            p,
            "trace_review",
            r["transaction"],
            dict(r, block_hash=r["hash"]),
            shape="control",
            note="Landed Aave control. Reverted subtrees explain apparent duplicate value.",
            measures={
                "trace_frames": r["frame_count"],
                "index_overstatement_wei": r["index_overstatement_wei"],
                "corrected_block_residual_wei": r["corrected_block_residual_wei"],
            },
        )
    book.cohort(
        "nov14_aave_reverted_branch_controls",
        p,
        kind="landed_accounting_controls",
        individually_checked=5,
        remaining_receipt_gap=d["aave"]["receipt_gap"],
    )
    book.cohort(
        "nov14_metric_fixed_parent_simulations",
        p,
        kind="read_only_simulation_controls",
        parent=d["metric"]["parent"],
        reported_arms=len(d["metric"]["arms"]),
        landed_new_transactions=0,
    )
    book.cohort(
        "nov14_metric_api_quotes",
        DOC / "METRIC_RESEARCH_20261114.md",
        kind="provider_quote_only",
        planned_routes=6,
        landed_new_transactions=0,
    )


def main():
    if not __debug__:
        raise SystemExit("run the Ethereum inventory verifier without Python optimization")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    book = Inventory()
    recent_tracked(book)
    held_out(book)
    historic(book)
    additional_queues(book)
    raw_and_documents(book)
    output = json.dumps(book.finished(), sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(output)
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
