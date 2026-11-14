"""Reconcile the frozen Aave wallet receipts and native corrections from wire evidence."""

import argparse
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from sqlite3 import Error as DatabaseError

from oasis_agent.arbitrage.aave_wallet import reconcile_wallet
from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.arbitrage.cycle_plan import address, hash32, utc
from oasis_agent.evidence.public_receipts import decode_receipt, quantity, receipt_header
from oasis_agent.evidence.research_traces import first_failure, flat_calls
from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.evidence.state_trie import prague_header
from oasis_agent.models.common import BlockRef, canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from oasis_agent.rpc.protocol import response_envelope

from . import capture_public_rpc as rpc
from . import research_aave_incident, research_aave_receipts, research_aave_sale
from . import research_aave_wallet as wallet
from .research_ethereum_traces import BLOCKSCOUT, aave_report, retained

SOURCE = "alchemy_public_ethereum"
RESIDUAL_STARTS = (26301326, 26301390, 26301454)
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"


@dataclass(frozen=True)
class Batch:
    entries: tuple
    source: str = SOURCE
    chain_id: int = 1

    def requests(self):
        return tuple((entry[0], entry[1]) for entry in self.entries)


def block_receipts(campaign, expected, indexed_hashes):
    scope, scope_raw = rpc.load(campaign / "scope.json", rpc.PublicScope)
    count = sum(3 + len(txs) for _, txs in expected)
    ledger = campaign / "original-budget.sqlite3"
    if (
        [(s.source, s.max_requests) for s in scope.sources] != [(SOURCE, count)]
        or wallet.load(campaign / "identity.json")
        != {
            "ledger_identity": rpc.identity(ledger),
            "scope_sha256": sha256_hex(scope_raw),
            "root": str(campaign.resolve()),
        }
        or any((campaign / name).exists() for name in ("STOP", "ACTIVE.json"))
    ):
        raise ValueError("original receipt campaign required")
    with RpcBudget(ledger, rpc.policies(scope), read_only=True) as budget:
        if budget.counts() != {SOURCE: count}:
            raise ValueError("receipt campaign reservation mismatch")
    for b, txs in expected:
        head = ("eth_getBlockByNumber", [hex(b), False], b, txs, None)
        entries = (
            ("eth_chainId", [], 0, (), None),
            head,
            *(("eth_getTransactionReceipt", [tx], b, txs, None) for tx in txs),
            (*head[:4], "opening"),
        )
        plan = Batch(entries)
        raw = canonical_json(
            {
                "schema": "aave-historical-receipt-batch-local-v1",
                "source": SOURCE,
                "entries": entries,
            }
        )

        length = len(entries)

        def validate(index, response, first, number=b, hashes=txs, length=length):
            result = response["result"]
            if index == 0:
                if result != "0x1":
                    raise ValueError("receipt chain mismatch")
            elif index in (1, length - 1):
                block = receipt_header(result, 1, number, hashes)
                expected_hash = indexed_hashes.get(number, block.ref.block_hash)
                prague_header(result, number, expected_hash)
                if index != 1 and block != first:
                    raise ValueError("closing receipt block changed")
                return block
            else:
                tx = hashes[index - 2]
                decode_receipt(result, first.ref, tx, dict(first.receipt_positions)[tx])
            return first

        report = rpc.replay_loaded(campaign / f"block-{b}", plan, raw, validate=validate)
        if report["status"] != "complete":
            raise ValueError("receipt batch incomplete")
        block = BlockRef(**report["block"]["ref"])
        positions = dict(report["block"]["receipt_positions"])
        for record in report["exports"]["records"][2:-1]:
            tx = record["request"]["params"][0]
            yield decode_receipt(record["response"]["result"], block, tx, positions[tx])


def previous_receipts(research):
    directories = (
        *sorted((research / "aave-index-20261114/alchemy-campaign").glob("tx-*")),
        *sorted((research / "aave-ledger-20261114/wallet-rpc/campaign").glob("block-*")),
        *(
            research / "aave-regimes-20261114/campaign" / name
            for name in ("weth-leg-1", "weth-leg-2")
        ),
        *sorted(
            (research / "aave-five-traces-20261114/alternate-identity/campaign").glob("receipt-*")
        ),
    )
    for directory in directories:
        report = rpc.replay(directory)
        if report["status"] != "complete":
            continue
        anchor = BlockRef(**report["block"]["ref"])
        positions = dict(report["block"]["receipt_positions"])
        for exchange in report["exports"]["records"]:
            request = exchange["request"]
            if request["method"] == "eth_getTransactionReceipt":
                tx = request["params"][0]
                yield decode_receipt(exchange["response"]["result"], anchor, tx, positions[tx])


def balances(root, first, account, indexed_hashes):
    plan_raw = read_bounded(root / "design.json", 4096)
    plan = strict_json_object(plan_raw)
    last = first + 63
    expiry = utc(plan["expires"])
    if plan != {
        "schema": "aave-native-residual-scan-local-v1",
        "account": account,
        "first": first,
        "last": last,
        "max_requests": 132,
        "source": SOURCE,
        "minimum_interval_ms": 250,
        "expires": plan["expires"],
    } or any((root / n).exists() for n in ("STOP", "ACTIVE")):
        raise ValueError("wrong balance design")
    ledger = root / "original-budget.sqlite3"
    if wallet.load(root / "identity.json") != {
        "sha256": sha256_hex(plan_raw),
        "ledger": rpc.identity(ledger),
    }:
        raise ValueError("balance campaign identity mismatch")
    with RpcBudget(ledger, (BudgetPolicy(SOURCE, 132, 250),), read_only=True) as budget:
        if budget.counts() != {SOURCE: 132}:
            raise ValueError("balance budget mismatch")
    i = prior_begin = prior_end = 0

    def read(method, params):
        nonlocal i, prior_begin, prior_end
        i += 1
        req = read_bounded(root / f"{i}.request", 4096)
        body = read_bounded(root / f"{i}.response", 2**20)
        meta = wallet.load(root / f"{i}.meta")
        begin, end = meta["started_ms"], meta["ended_ms"]
        if (
            req != canonical_json({"jsonrpc": "2.0", "method": method, "params": params, "id": i})
            or meta["status"] != 200
            or meta["error"] is not None
            or (sha256_hex(req), sha256_hex(body))
            != (meta["request_sha256"], meta["response_sha256"])
            or not prior_end <= begin <= end < int(expiry.timestamp() * 1000)
            or (prior_begin and begin - prior_begin < 250)
        ):
            raise ValueError("balance wire preservation")
        prior_begin, prior_end = begin, end
        return response_envelope(body, i)["result"]

    if read("eth_chainId", []) != "0x1":
        raise ValueError("balance chain mismatch")
    prior = None
    values = []
    for block in range(first - 1, last + 1):
        result = read("eth_getBlockByNumber", [hex(block), False])
        snap, _ = prague_header(result, block, indexed_hashes.get(block, hash32(result["hash"])))
        if prior and snap.parent_hash != prior:
            raise ValueError("balance block lineage mismatch")
        prior = snap.ref.block_hash
        value = quantity(
            read("eth_getBalance", [account, {"blockHash": prior, "requireCanonical": True}])
        )
        values.append((block, prior, value))
    prague_header(read("eth_getBlockByNumber", [hex(last), False]), last, prior)
    return values


def trace_correction(body, receipt, account, indexed):
    frames = flat_calls(strict_json_object(b'{"frames":' + body + b"}")["frames"])
    root = frames[()]
    if (root.sender, root.recipient, root.error is None) != (
        receipt.sender,
        receipt.recipient,
        bool(receipt.status),
    ) or account in (root.sender, root.recipient):
        raise ValueError("trace root does not match retained receipt")
    movements = []
    for path, frame in sorted(frames.items()):
        if (
            not path
            or frame.kind != "CALL"
            or not frame.value
            or account not in (frame.sender, frame.recipient)
        ):
            continue
        native = ((frame.recipient == account) - (frame.sender == account)) * frame.value
        movements.append(
            {
                "path": path,
                "first_failed_path": first_failure(frames, path),
                "native_delta_raw": native,
            }
        )
    observed = sum(m["native_delta_raw"] for m in movements if m["first_failed_path"] is None)
    return {"frames": len(frames), "correction": observed - indexed, "movements": movements}


def residuals(root, movements, indexed_hashes, prior, receipts, account):
    by_block = defaultdict(list)
    internal = Counter()
    for move in movements:
        if move.kind != "erc20":
            by_block[move.block].append(move)
        if move.kind == "internal" and move.success:
            internal[move.transaction] += move.delta(account)
    earlier = {row["block"]: row["index_overstatement_wei"] for row in prior["rows"]}
    observed = []
    for start in RESIDUAL_STARTS:
        values = balances(root / f"aave-residual-{start}", start, account, indexed_hashes)
        for left, (number, hash_, after) in pairwise(values):
            transactions = by_block[number]
            net = sum(
                (m.delta(account) if m.success else 0)
                - (m.gas_paid_raw if m.kind == "external" and m.sender == account else 0)
                for m in transactions
            )
            difference = after - left[2] - net + earlier.get(number, 0)
            if difference:
                observed.append(
                    (number, hash_, difference, sorted({m.transaction for m in transactions}))
                )
    if sum(row[2] for row in observed) != prior["residual_after_trace_correction_wei"]:
        raise ValueError("remaining block residual allocation mismatch")
    specs = [
        ("GET", f"{BLOCKSCOUT}/api/v2/transactions/{tx}/raw-trace", None)
        for _, _, _, txs in observed
        for tx in txs
    ]
    replies = iter(retained(root / "aave-residual-trace", "blockscout", specs))
    output = []
    for number, hash_, expected, transactions in observed:
        rows = []
        for tx in transactions:
            body, meta = next(replies)
            receipt = receipts[tx]
            if receipt.block.block_hash != hash_ or receipt.block.number != number:
                raise ValueError("receipt must match corrected block")
            rows.append(
                {
                    "tx": tx,
                    "trace_sha256": meta["response_sha256"],
                    **trace_correction(body, receipt, account, internal[tx]),
                }
            )
        if sum(r["correction"] for r in rows) != expected:
            raise ValueError("remaining trace does not reconcile block")
        output.append({"block": number, "hash": hash_, "residual_before": expected, "traces": rows})
    if next(replies, None) is not None:
        raise ValueError("unexpected retained trace")
    return output


def valuation(research, prior, opening, closing, receipts):
    regimes = research / "aave-regimes-20261114"
    sale_root = research / "aave-sale-20261114"
    indexed = research / "aave-index-20261114"
    incident_plan = regimes / "incident-plan.json"
    bindings = indexed / "cashflow-boundaries-plan.json"
    original_receipts = indexed / "alchemy-campaign"
    sale = research_aave_sale.replay(
        incident_plan,
        sale_root / "reviewed-sale-plan.json",
        bindings,
        sale_root / "campaign",
        sale_root / "links-campaign",
    )
    incident = research_aave_incident.run(
        incident_plan, regimes / "campaign", original_receipts / "tx-37"
    )
    scaled = research_aave_receipts.replay(
        research / "aave-history-20261114/analysis-plan.json",
        sorted(original_receipts.glob("tx-*")),
        bindings,
        tuple(indexed / "boundaries/campaign" / name for name in ("start", "end")),
        scaled=True,
    )
    if any(
        r["account"] != prior["account"] or r["chain_id"] != 1 for r in (sale, incident, scaled)
    ):
        raise ValueError("different account or chain in valuation")
    funding = []
    for label in ("funding_a", "funding_b"):
        tx = sale["transactions"][label]
        receipt = receipts[tx["transaction_hash"]]
        if (
            tx["recipient"] != prior["account"]
            or receipt.status != 1
            or (receipt.sender, receipt.recipient) != (tx["sender"], tx["recipient"])
            or receipt.block != BlockRef(**tx["block"])
        ):
            raise ValueError("funding transaction identity mismatch")
        funding.append(
            {"tx": receipt.transaction_hash, "wei": tx["value_wei"], "sender": receipt.sender}
        )
    reserve_balances = []
    for name, height, underlying in (
        ("opening", prior["first_block"] - 1, opening),
        ("closing", prior["last_block"], closing),
    ):
        point = scaled["position_boundaries"][name]
        if point["block"]["number"] != height:
            raise ValueError("position boundary changed")
        by_role = {(row["role"], row["reserve"]): row["balance_raw"] for row in point["reserves"]}
        if (
            by_role["underlying", USDC] != underlying[USDC]
            or by_role["underlying", WETH] != underlying[WETH]
        ):
            raise ValueError("asset boundary mismatch")
        reserve_balances.append(
            (
                by_role["a_token", USDC],
                by_role["a_token", WETH] - by_role["variable_debt", WETH],
            )
        )
    marks = incident["oracle"]["marks"]["purchase"]
    stable = marks[USDC]["observed_market_price_usd_e8"]
    ether = marks[WETH]["observed_market_price_usd_e8"]
    if not all(type(n) is int and n > 0 for n in (stable, ether)):
        raise ValueError("invalid retained marks")
    start, end = reserve_balances
    usd_stable_raw = (closing[USDC] - opening[USDC] + end[0] - start[0]) * stable * 10**12
    usd_ether_raw = (
        closing[None] - opening[None] + closing[WETH] - opening[WETH] + end[1] - start[1]
    ) * ether
    marked_e26 = usd_stable_raw + usd_ether_raw
    funding_e26 = sum(f["wei"] for f in funding) * ether
    return {
        "classification": "single_account_constant_mark_cashflow_diagnostic",
        "mark_source": "retained_Aave_purchase_parent_observation",
        "mark_currency": "USD",
        "mark_decimals": 8,
        "value_decimals": 26,
        "usdc_mark": stable,
        "eth_weth_mark": ether,
        "identified_external_funding": funding,
        "net_wallet_eth_change_wei": closing[None] - opening[None],
        "net_aave_collateral_usdc_change_raw": end[0] - start[0],
        "covered_net_asset_change_usd_raw": marked_e26,
        "after_identified_funding_usd_raw": marked_e26 - funding_e26,
        "remaining_external_flow_attribution_complete": False,
        "net_pnl": None,
    }


def run(research):
    root = research / "metric-aave-followup-20261114"
    baseline = wallet.load(
        research / "aave-regimes-20261114/wallet-plus-pair.json", 4 * 1024 * 1024
    )
    if baseline["chain_id"] != 1:
        raise ValueError("Ethereum research only")
    account = address(baseline["account"])
    movements, index_ref = wallet.index(
        research / "aave-ledger-20261114/wallet-index",
        account,
        baseline["first_block"],
        baseline["last_block"],
        {row["address"]: row["decimals"] for row in baseline["assets"] if row["address"]},
    )
    prior = aave_report(research, research / "ethereum-traces-20261114")
    target = wallet.load(root / "aave-targets.json")
    missing = set(baseline["comparison"]["transactions_without_receipts"]) - {
        r["transaction"] for r in prior["rows"]
    }
    blocks = defaultdict(set)
    indexed_hashes = {}
    for move in movements:
        if move.transaction in missing:
            blocks[move.block].add(move.transaction)
        if (
            move.block_hash
            and indexed_hashes.setdefault(move.block, move.block_hash) != move.block_hash
        ):
            raise ValueError("index block hashes conflict")
    expected = [(b, tuple(sorted(txs))) for b, txs in sorted(blocks.items())]
    if (
        target
        != {
            "account": account,
            "base_index": index_ref,
            "blocks": [[b, list(txs)] for b, txs in expected],
        }
        or index_ref != baseline["index"]
        or len(missing) != prior["receipt_gap"]
    ):
        raise ValueError("frozen candidate set mismatch")
    observations = list(block_receipts(root / "aave-receipts/campaign", expected, indexed_hashes))
    if {r.transaction_hash for r in observations} != missing:
        raise ValueError("missing frozen receipt")
    receipts = {}
    for entry in (*observations, *previous_receipts(research)):
        if receipts.setdefault(entry.transaction_hash, entry) != entry:
            raise ValueError("conflicting receipts")
    start = {
        None if x["asset"] == "native" else x["asset"]: x["opening_raw"]
        for x in baseline["comparison"]["assets"]
    }
    end = {
        None if x["asset"] == "native" else x["asset"]: x["closing_raw"]
        for x in baseline["comparison"]["assets"]
    }
    native_root = research / "aave-ledger-20261114/wallet-rpc/campaign"
    positions_root = research / "aave-index-20261114/boundaries/campaign"
    native_captures = []
    for name, asset_balances in (("start", start), ("end", end)):
        position = rpc.replay(positions_root / name)
        if position["status"] != "complete":
            raise ValueError("position boundary incomplete")
        actual, capture = wallet.native(
            native_root / f"native-{name}", BlockRef(**position["block"]["ref"]), account
        )
        if actual != asset_balances[None]:
            raise ValueError("native balance boundary changed")
        native_captures.append(capture)
    if native_captures != baseline["native_capture_hashes"]:
        raise ValueError("native balance baseline does not match retained captures")
    report = reconcile_wallet(account, start, end, movements, tuple(receipts.values()))
    if report["transactions_without_receipts"] or report["disagreements"]:
        raise ValueError("frozen receipts did not reconcile")
    corrections = residuals(root, movements, indexed_hashes, prior, receipts, account)
    (native,) = [row for row in report["assets"] if row["asset"] == "native"]
    if native["balance_minus_index_raw"] != prior["original_native_residual_wei"]:
        raise ValueError("native baseline changed")
    final = (
        native["balance_minus_index_raw"]
        + prior["index_overstatement_wei"]
        - sum(row["residual_before"] for row in corrections)
    )
    if final:
        raise ValueError("native residual not zero")
    owned = [receipt for receipt in receipts.values() if receipt.sender == account]
    return {
        "schema": "oasis-aave-wallet-closure-v1",
        "chain_id": 1,
        "account": account,
        "receipt_reconciliation": report,
        "corrected_native_residual_wei": final,
        "additional_receipt_status_counts": dict(
            sorted(Counter(str(r.status) for r in observations).items())
        ),
        "residual_corrections": corrections,
        "wallet_failed_gas_wei": sum(
            r.gas_used * r.effective_gas_price for r in owned if not r.status
        ),
        "valuation": valuation(research, baseline, start, end, receipts),
        "execution_eligible": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        rpc.publish(args.output, run(args.research))
    except (ValueError, TypeError, KeyError, OSError, DatabaseError) as exc:
        print(f"Aave closure replay refused ({type(exc).__name__})", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
