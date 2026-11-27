"""Render the dated multichain casebook from the retained case list and verified additions."""

# Chinese document text uses its standard punctuation.
# ruff: noqa: RUF001
import argparse
import csv
import gzip
import json
from collections import Counter, defaultdict
from pathlib import Path

from build_multichain_sample_catalog import COLUMNS, serialize

DOC = Path(__file__).resolve().parents[1]
CHAINS = {
    "ethereum": ("Ethereum", "https://etherscan.io/tx/"),
    "bsc": ("BSC", "https://bscscan.com/tx/"),
    "base": ("Base", "https://basescan.org/tx/"),
    "robinhood": ("Robinhood", "https://robinhoodchain.blockscout.com/tx/"),
    "elysium": ("Elysium", "https://elysiumexplore.com/tx/"),
    "arbitrum": ("Arbitrum One", "https://arbiscan.io/tx/"),
    "optimism": ("Optimism", "https://optimistic.etherscan.io/tx/"),
    "zksync": ("zkSync Era", "https://explorer.zksync.io/tx/"),
}
KINDS = {
    "serial": "串行",
    "batch": "分组",
    "reinvestment": "逐轮复投",
    "mixed": "混合",
    "unresolved": "待核",
    "control": "对照",
}


def additions():
    window = json.loads((DOC / "BSC_GOLD_WINDOW_SCREENED_20261127.json").read_bytes())
    deep = json.loads((DOC / "BSC_GOLD_SETTLEMENT_20261127.json").read_bytes())["cases"]
    bsc_cases = {r["tx_hash"]: r for r in deep}
    assert bsc_cases.keys() <= {r["tx_hash"] for r in window["samples"]}
    for item in window["samples"]:
        case = bsc_cases.get(item["tx_hash"])
        if case:
            assert int(item["receipt_gas_bnb_wei"]) == int(case["network_fee_bnb_wei"])
        yield (
            True,
            {
                "chain_id": 56,
                "chain": "bsc",
                "tx_hash": item["tx_hash"],
                "block_number": window["block_number"],
                "shape": case["shape"] if case else item["shape"],
                "screen_events_max": item["recognized_swap_shapes"],
                "funded_market_swaps_lb": case["max_funded_market_legs"] if case else None,
                "review": "rooted_receipt_transfer_check" if case else "rooted_receipt_swap_shapes",
                "sources": "BSC_gold_screen,BSC_gold_settlement" if case else "BSC_gold_screen",
                "observed_network_cost_native_raw": item["receipt_gas_bnb_wei"],
                "cost_basis": "receipt_gas",
            },
        )
    paper = json.loads((DOC / "MULTIHOP_SECOND_SHARDS_RECEIPTS_20261127.json").read_bytes())
    proofs = {(r["chain_id"], r["tx_hash"]): r for r in paper["cases"]}
    with gzip.open(DOC / "MULTIHOP_SECOND_SHARDS_HASHES_20261127.tsv.gz", "rt") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            assert row["new_to_prior_catalog"] == "1"
            data = {k: row[k] for k in COLUMNS if k in row}
            data.update(
                published_source_row=row["source_row"],
                sources=row["source"],
                shape="unresolved",
                review="published_label",
            )
            proof = proofs.pop((int(row["chain_id"]), row["tx_hash"]), None)
            if proof:
                groups = int(row["published_groups"])
                assert proof["block_number"] == int(row["block_number"])
                assert proof["transaction_index"] == int(row["transaction_index"])
                assert (
                    proof["funding"]["intermediate_evidence"][
                        "closed_amount_groups_with_all_intermediate_transfers"
                    ]
                    == groups
                )
                data.update(
                    review="rooted_receipt_transfer_check",
                    sources=row["source"] + ",CCS24_receipt",
                    shape="batch" if groups > 1 else "serial",
                    funded_market_swaps_lb=int(row["published_max_exact_closed_group"]),
                    observed_network_cost_native_raw=proof["network_cost_wei"],
                    cost_basis="l2_plus_l1" if row["chain"] == "optimism" else "receipt_gas",
                )
            yield bool(proof), data
    assert not proofs


def reinvestment_receipts():
    family = json.loads((DOC / "OPTIMISM_HUNDRED_CYCLE_FAMILY_20261127.json").read_bytes())
    result = {}
    for c in family["cases"]:
        key = (c["chain_id"], c["tx_hash"])
        rounds = c["verified_two_hop_rounds"]
        assert c["distinct_swap_events"] == c["receipt_serial_reinvestment_compatible_hops"]
        assert c["distinct_swap_events"] == rounds * 2 and c["funded_market_swaps_lb"] == 2
        assert c["cash"]["proved_reinvestment_transfer_links"] == rounds - 1
        assert int(c["network_cost_wei"]) == int(c["l1_fee_wei"]) + int(c["l2_fee_wei"])
        assert key not in result
        result[key] = (c["distinct_swap_events"], c["network_cost_wei"])
    extra = json.loads((DOC / "OPTIMISM_136_REINVESTMENT_20261127.json").read_bytes())
    (c,) = extra["cases"]
    key = (10, c["tx_hash"])
    assert key not in result and c["verified_market_legs"] == c["market_swaps"] == 2 * c["rounds"]
    assert (
        c["rounds"]
        == c["within_group_transfer_edges"]
        == 1 + c["cross_group_equal_reinvestment_edges"]
    )
    result[key] = (c["verified_market_legs"], c["network_fee_wei"])
    return result


def rows(catalog_path):
    with (DOC / "MULTICHAIN_MULTIHOP_CASES_20261127.tsv").open() as f:
        cases = list(csv.DictReader(f, delimiter="\t"))
    catalog = {}
    with gzip.open(DOC / "MULTICHAIN_MULTIHOP_ALL_HASHES_20261127.tsv.gz", "rt") as f:
        reader = csv.DictReader(f, delimiter="\t")
        assert tuple(reader.fieldnames) == COLUMNS
        for r in reader:
            key = (int(r["chain_id"]), r["tx_hash"])
            assert key not in catalog
            catalog[key] = r
    for c in cases:
        assert catalog[(int(c["chain_id"]), c["tx_hash"])] == c
    for detailed, c in additions():
        key = (int(c["chain_id"]), c["tx_hash"])
        assert key not in catalog
        catalog[key] = c
        if detailed:
            cases.append(c)
    screen = json.loads((DOC / "CROSS_GROUP_REINVESTMENT_SCREEN_20261127.json").read_bytes())
    receipts = reinvestment_receipts()
    for item in screen["cases"]:
        key = (item["chain_id"], item["tx_hash"])
        row = catalog[key]
        assert int(row["block_number"]) == item["block_number"]
        assert int(row["published_indices"]) == item["published_events"]
        row["review"] = "published_changing_principal_link"
        row["sources"] += ",CCS24_reinvestment_screen"
        projected = max(run["legs"] for run in item["runs"])
        verified, fee = receipts.pop(key, (None, None))
        if verified is not None:
            assert verified == projected and row["chain"] == "optimism"
            row.update(
                review="rooted_receipt_reinvestment",
                shape="reinvestment",
                funded_market_swaps_lb=2,
                observed_network_cost_native_raw=fee,
                cost_basis="l2_plus_l1",
            )
            row["sources"] += ",CCS24_reinvestment_receipt"
        cases.append(dict(row, projected_roll=projected, verified_roll=verified))
    assert not receipts
    assert len({(c["chain"], c["tx_hash"]) for c in cases}) == len(cases)
    if catalog_path:
        for r in catalog.values():
            r["sources"] = set(r["sources"].split(","))
        catalog_path.write_bytes(serialize(catalog))
    return cases


def display(chain, cases):
    print(
        "| 交易哈希 | 块高 | 结构 | 筛选事件／作者索引 | 已核池腿下界 | "
        "跨组复投腿数（作者整数／收据逐轮） | 已知网络费（原生最小单位） |"
    )
    print("| --- | ---: | --- | ---: | ---: | ---: | --- |")
    for c in sorted(cases, key=lambda x: (int(x["block_number"] or 0), x["tx_hash"])):
        tx = c["tx_hash"]
        fee = c.get("observed_network_cost_native_raw")
        fee_text = f"{fee} · {c['cost_basis']}" if fee not in (None, "") else "—"
        count = "／".join(str(c.get(k) or "—") for k in ("screen_events_max", "published_indices"))
        roll = "／".join(str(c.get(k) or "—") for k in ("projected_roll", "verified_roll"))
        print(
            f"| [{tx}]({CHAINS[chain][1]}{tx}) | {c['block_number'] or '—'} | {KINDS[c['shape']]} "
            f"| {count} | {c.get('funded_market_swaps_lb') or '—'} | "
            f"{roll if c.get('projected_roll') else '—'} | {fee_text} |"
        )


def main():
    if not __debug__:
        raise SystemExit("run without Python optimization")
    grouped = defaultdict(list)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, help="optional combined gzip TSV")
    for c in rows(parser.parse_args().catalog):
        grouped[c["chain"]].append(c)
    print("# 黄金样本逐笔登记与对照\n")
    print(
        "每行对应一笔已入块交易，可按完整哈希定位。"
        "优先展示结构已分类的交易及公开收据专项核查。其余待核和对照可展开。"
        "同一交易可以留下多种资产或向第三方付款。表中的费用是链原生币最小单位。"
        "空格记为“—”，实付零以 0 显示。\n"
    )
    print(
        "[样本册与资金解读](../strategies/MULTIHOP_GOLDEN_CASEBOOK.md) · "
        "[五链原件和费用口径](MULTICHAIN_SAMPLE_CATALOG_20261127.md) · "
        "[第一分片结算](PUBLISHED_MULTIHOP_SETTLEMENT_20261127.md) · "
        "[第二分片及两笔新结算](MULTIHOP_SECOND_SHARDS_20261127.md) · "
        "[新 BSC 收据及费用](BSC_GOLD_WINDOW_SCREENED_20261127.json) · "
        "[跨组本金筛选](CROSS_GROUP_REINVESTMENT_SCREEN_20261127.json) · "
        "[百次复投核查](OPTIMISM_HUNDRED_CYCLE_FAMILY_20261127.md) · "
        "[136 次兑换](OPTIMISM_136_REINVESTMENT_20261127.md)\n"
    )
    print(
        "网络费口径中，receipt_gas 表示收据 gas 乘对应价格，l2_plus_l1 含两层费用，"
        "l2_execution_only 仅执行费，l1_plus_l2_partial 表示执行和 L1 部分。"
        "分组包含用户路由。逐轮复投分别列组内连续池腿下界、跨包装或托管的市场兑换腿数，"
        "实际主体损益需单独核实。"
    )
    for chain, (name, _) in CHAINS.items():
        cases = grouped[chain]
        featured = [
            c
            for c in cases
            if c["shape"] in ("serial", "mixed", "batch")
            or "CCS24_receipt" in c.get("sources", "")
            or "BSC_gold_settlement" in c.get("sources", "")
            or "projected_roll" in c
        ]
        others = [c for c in cases if c not in featured]
        counts = Counter(c["shape"] for c in cases)
        print(f"\n## {name}\n")
        print("、".join(f"{label} {counts[k]}" for k, label in KINDS.items() if counts[k]) + "。\n")
        display(chain, featured)
        if others:
            print(f"\n<details><summary>其余 {len(others)} 笔待核交易与对照</summary>\n")
            display(chain, others)
            print("\n</details>")


if __name__ == "__main__":
    main()
