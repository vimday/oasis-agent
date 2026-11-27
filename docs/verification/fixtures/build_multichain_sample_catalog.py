"""Join landed transaction identities from retained research, width screens and public shards."""

import argparse
import csv
import gzip
import json
import re
from collections import Counter, defaultdict
from hashlib import sha256
from io import BytesIO, TextIOWrapper
from pathlib import Path

from replay_published_multihop_shards import SHARDS, published_rows

HERE = Path(__file__).resolve().parents[1]
CHAINS = {
    1: "ethereum",
    10: "optimism",
    56: "bsc",
    324: "zksync",
    4663: "robinhood",
    8453: "base",
    42161: "arbitrum",
    99901: "elysium",
}
DETAILS = {1: "ETH", 56: "BSC", 4663: "ROBINHOOD", 8453: "BASE", 99901: "ELYSIUM"}
COLUMNS = (
    "chain_id",
    "chain",
    "tx_hash",
    "block_number",
    "review",
    "shape",
    "screen_events_max",
    "funded_market_swaps_lb",
    "closed_hybrid_actions",
    "market_swaps_in_closed_route",
    "published_source_row",
    "published_indices",
    "published_groups",
    "published_max_exact_closed_group",
    "observed_network_cost_native_raw",
    "cost_basis",
    "sources",
)


def exact_value(values):
    values = {int(v) for v in values if v is not None}
    if len(values) > 1:
        raise ValueError("conflicting values for a common field")
    return next(iter(values), None)


def detail_fields(chain, case):
    obs = case.get("observations", [])
    height = exact_value(
        [case.get("block_number"), case.get("block"), *(o.get("height") for o in obs)]
    )
    basis = "receipt_gas"
    if chain == 1:
        obs = [e.get("observations", {}) for e in case["evidence"]]
        level = case["strongest_evidence"]
        cost = exact_value(o.get("gas_paid_wei") for o in obs)
        depths = [case.get("funded_market_swap_depth_lower_bound")]
        depths.extend(o.get("consecutive_market_swap_segment") for o in obs)
        depth = max((int(d) for d in depths if d is not None), default=None)
    elif chain == 56:
        level = case["evidence_level"]
        cost = case.get("receipt_gas_bnb_wei")
        depth = case.get("max_proven_funded_pool_swaps")
    elif chain == 99901:
        level = "event_control" if all(o["cohort"] != "anomaly" for o in obs) else "rooted_receipt"
        winner = next((o["receipt_decoding"] for o in obs if o.get("role") == "winner"), {})
        cost = winner.get("receipt_gas_product_native_raw")
        depth = sum(leg["protocol"] != "wrapped_burn" for leg in winner["legs"]) if winner else None
    else:
        level = "+".join(sorted({o["kind"] for o in obs}))
        keys = ("l2_execution_wei",) if chain == 8453 else ("gas_wei",)
        cost = exact_value(o.get(k) for k in keys for o in obs)
        if chain == 8453:
            published_costs = [
                o.get(k)
                for k in ("partial_fee_l1_l2_wei", "known_execution_and_l1_fee_wei")
                for o in obs
            ]
            published_costs.extend(
                int(o["l1_data_wei"]) + int(o["l2_execution_wei"])
                for o in obs
                if o.get("l1_data_wei") is not None and o.get("l2_execution_wei") is not None
            )
            both = exact_value(published_costs)
            cost, basis = (
                (both, "l1_plus_l2_partial") if both is not None else (cost, "l2_execution_only")
            )
        depth = max(
            (
                int(o[k])
                for k in ("verified_serial_swaps", "funded_swaps")
                for o in obs
                if o.get(k) is not None
            ),
            default=None,
        )
    return {
        "review": level,
        "shape": case["shape"],
        "block_number": height,
        "funded_market_swaps_lb": depth,
        "closed_hybrid_actions": case.get("verified_closed_hybrid_actions"),
        "market_swaps_in_closed_route": case.get("verified_closed_market_swaps"),
        "observed_network_cost_native_raw": cost,
        "cost_basis": basis if cost is not None else None,
    }


def build(research):
    entries = {}

    def get(chain, tx, height=None):
        if chain not in CHAINS or not re.fullmatch("0x[0-9a-f]{64}", tx):
            raise ValueError("invalid chain or transaction hash")
        row = entries.setdefault(
            (chain, tx),
            {
                "chain_id": chain,
                "chain": CHAINS[chain],
                "tx_hash": tx,
                "sources": set(),
                "review": "screen_only",
                "shape": "unresolved",
            },
        )
        row["block_number"] = exact_value([height, row.get("block_number")])
        return row

    for chain, alias in DETAILS.items():
        path = HERE / f"{alias}_MULTIHOP_SAMPLE_INVENTORY_20261127.json"
        obj = json.loads(path.read_bytes())
        assert obj["chain_id"] == chain
        seen = set()
        for case in obj["samples"]:
            tx = case["tx_hash"]
            assert tx not in seen
            seen.add(tx)
            common = detail_fields(chain, case)
            row = get(chain, tx, common["block_number"])
            row.update(common)
            row["sources"].add(alias)
    raw_screen = (research / "screened.tsv").read_bytes()
    manifest = json.loads((HERE / "MULTIHOP_SCREENED_SOURCES_20261127.json").read_bytes())
    if sha256(raw_screen).hexdigest() != manifest["screened_tsv"]["sha256"]:
        raise ValueError("screened input differs from the reviewed evidence manifest")
    for line in csv.DictReader(raw_screen.decode().splitlines(), delimiter="\t"):
        row = get(int(line["chain_id"]), line["tx_hash"], line["block_number"] or None)
        if line["matched_events"]:
            row["screen_events_max"] = max(
                int(line["matched_events"]), row.get("screen_events_max", 0)
            )
        row["sources"].add(line["source"])
    new_bsc = json.loads((HERE / "BSC_NEXT_WINDOW_SCREENED_20261127.json").read_bytes())
    assert new_bsc["chain_id"] == 56
    positions = set()
    seen_logs = set()
    for case in new_bsc["samples"]:
        row = get(56, case["tx_hash"], new_bsc["block_number"])
        indices = case["swap_log_indices"]
        assert len(indices) >= 2 and indices == sorted(set(indices))
        assert case["transaction_index"] not in positions and seen_logs.isdisjoint(indices)
        positions.add(case["transaction_index"])
        seen_logs.update(indices)
        row["sources"].add("BSC_next_rooted_screen")
        row["screen_events_max"] = max(len(indices), row.get("screen_events_max", 0))
        if row["review"] == "screen_only":
            row["review"] = "rooted_receipt_swap_shapes"
        row["observed_network_cost_native_raw"] = exact_value(
            [row.get("observed_network_cost_native_raw"), case["receipt_gas_bnb_wei"]]
        )
        row["cost_basis"] = "receipt_gas"
    proofs = json.loads((HERE / "PUBLISHED_MULTIHOP_RECEIPTS_20261127.json").read_bytes())[
        "audited"
    ]
    for chain in SHARDS:
        for obj, _ in published_rows(research, chain):
            row = get(obj["chain_id"], obj["tx_hash"], obj["block"])
            row["sources"].add("CCS24")
            row.update(
                published_source_row=obj["source_row"],
                published_indices=obj["distinct_indices"],
                published_groups=obj["groups"],
                published_max_exact_closed_group=obj["max_amount_matched_closed_group"],
            )
            if row["review"] == "screen_only":
                row["review"] = "published_label"
        for proof in proofs[chain]:
            row = get(proof["chain_id"], proof["tx_hash"], proof["block"])
            counts = proof["intermediate_evidence"]
            all_closed = (
                counts.get("closed_amount_groups_with_all_intermediate_transfers", 0)
                == proof["groups"]
            )
            if row["review"] == "published_label":
                row["review"] = "receipt_transfer_check"
                custody_groups = proof.get("shared_vault_custody_groups", [])
                covered_groups = {group["group_number"] for group in custody_groups}
                vault_closed = covered_groups == set(range(1, proof["groups"] + 1))
                if all_closed:
                    row["shape"] = "batch" if proof["groups"] > 1 else "serial"
                    row["funded_market_swaps_lb"] = proof["max_amount_matched_closed_group"]
                elif vault_closed:
                    row["review"] = "receipt_shared_vault_transfers"
                    row["shape"] = "batch" if proof["groups"] > 1 else "serial"
                    row["funded_market_swaps_lb"] = max(len(g["edges"]) + 1 for g in custody_groups)
            row["sources"].add("CCS24_receipt")
            exact_value(
                [proof["observed_network_fee_wei"], row.get("observed_network_cost_native_raw")]
            )
            row["observed_network_cost_native_raw"] = proof["observed_network_fee_wei"]
            row["cost_basis"] = "l2_plus_l1" if chain == "optimism" else "receipt_gas"
    return entries


def write_table(entries, stream):
    writer = csv.DictWriter(stream, fieldnames=COLUMNS, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    for key in sorted(entries):
        row = entries[key]
        writer.writerow({**row, "sources": ",".join(sorted(row["sources"]))})


def serialize(entries):
    # Empty gzip filename and fixed mtime make the artifact independent of the output path and date.
    output = BytesIO()
    with (
        gzip.GzipFile(fileobj=output, mode="wb", mtime=0, compresslevel=9) as gz,
        TextIOWrapper(gz, encoding="utf-8", newline="") as stream,
    ):
        write_table(entries, stream)
    return output.getvalue()


def main():
    if not __debug__:
        raise SystemExit("run without Python optimization")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("research_root", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--case-output", type=Path)
    args = p.parse_args()
    entries = build(args.research_root)
    args.output.write_bytes(serialize(entries))
    if args.case_output:
        detailed_sources = set(DETAILS.values()) | {"CCS24_receipt", "BSC_next_rooted_screen"}
        cases = {k: row for k, row in entries.items() if row["sources"] & detailed_sources}
        with args.case_output.open("w", encoding="utf-8", newline="") as stream:
            write_table(cases, stream)
    counts = defaultdict(Counter)
    for (chain, _), r in entries.items():
        counts[CHAINS[chain]]["unique_hashes"] += 1
        for source in ("CCS24", "CCS24_receipt", DETAILS.get(chain)):
            if source and source in r["sources"]:
                counts[CHAINS[chain]][source] += 1
        counts[CHAINS[chain]]["rows_with_network_cost"] += int(
            r.get("observed_network_cost_native_raw") is not None
        )
    print(json.dumps(counts, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
