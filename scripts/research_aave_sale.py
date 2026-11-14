"""Replay a selected Aave borrow, native sale, collateral refill and subsequent borrow."""

import argparse
import sys
from pathlib import Path
from typing import Any

from oasis_agent.arbitrage import aave_cashflows, aave_history, aave_incident, aave_sale
from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.evidence.public_receipts import transaction_for_receipt
from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.models.common import sha256_hex

from .research_aave_incident import Capture, adjoining, balances_at, marks
from .research_metric import publish


def read_model(path: Path, model: type[Any]) -> tuple[Any, str]:
    raw = read_bounded(path, 32 * 1024)
    strict_json_object(raw)
    return model.model_validate_json(raw), sha256_hex(raw)


def replay(
    incident_path: Path, sale_path: Path, bindings_path: Path, root: Path, links: Path
) -> dict[str, Any]:
    plan, plan_hash = read_model(incident_path, aave_incident.Plan)
    sale_plan, sale_hash = read_model(sale_path, aave_sale.Plan)
    bindings, binding_hash = read_model(bindings_path, aave_cashflows.Plan)
    tx_paths = (
        ("borrow", links / "borrow-tx", sale_plan.first_borrow_tx),
        ("funding_a", links / "funding-a-tx", sale_plan.first_funding_tx),
        ("failed_sale", links / "failed-sale-tx", sale_plan.failed_sale_tx),
        ("funding_b", links / "funding-b-tx", sale_plan.second_funding_tx),
        ("sale", root / "sale-tx", sale_plan.sale_tx),
        ("resupply", links / "resupply-tx", sale_plan.resupply_tx),
        ("next_borrow", links / "next-borrow-tx", sale_plan.subsequent_borrow_tx),
    )
    tx_captures = {name: Capture(path, plan.chain_id, receipt=True) for name, path, _ in tx_paths}
    receipts = {}
    transactions = {}
    for name, _, key in tx_paths:
        cap = tx_captures[name]
        receipt = cap.receipt(key)
        receipts[name] = receipt
        transactions[name] = transaction_for_receipt(
            cap.result("eth_getTransactionByHash", [key]), receipt
        )
    ordering = [(r.block.number, r.transaction_index) for r in receipts.values()]
    if ordering != sorted(set(ordering)):
        raise ValueError("sale_cohort_event_order")
    failed, tx = receipts["failed_sale"], transactions["failed_sale"]
    if (
        failed.status != 0
        or tx.sender != plan.account
        or tx.recipient != sale_plan.sale_entry_point
        or tx.value_raw == 0
        or tx.value_raw != transactions["sale"].value_raw
    ):
        raise ValueError("sale_failed_attempt_roles_or_quantity")
    for name in ("funding_a", "funding_b"):
        funding, envelope = receipts[name], transactions[name]
        if (
            funding.status != 1
            or envelope.recipient != plan.account
            or envelope.sender == plan.account
            or envelope.value_raw <= 0
        ):
            raise ValueError("sale_funding_roles_or_quantity")

    lift = aave_sale.sale_proceeds(
        sale_plan,
        account=plan.account,
        stable=plan.stable,
        weth=plan.weth,
        tx=transactions["sale"],
        receipt=receipts["sale"],
    )
    aave_names = ("borrow", "resupply", "next_borrow")
    market = aave_history.AaveHistoryPlan(
        schema="oasis-aave-receipt-analysis-v1",
        chain_id=plan.chain_id,
        account=plan.account,
        pools=(plan.pool,),
    )
    aave_receipts = tuple(receipts[name] for name in aave_names)
    matches = aave_cashflows.reconcile(market, bindings, aave_receipts)["matches"]
    if (
        [m["event"] for m in matches] != ["borrow", "supply", "borrow"]
        or [m["transaction_hash"] for m in matches] != [r.transaction_hash for r in aave_receipts]
        or any(m["status"] != "matched" for m in matches)
    ):
        raise ValueError("sale_aave_matched_operation_set")
    amounts = [m["expected_underlying_transfer"] for m in matches]
    expected = ((plan.weth, 18), (plan.stable, plan.stable_decimals), (plan.weth, 18))
    if any((a["token"], a["decimals"]) != pair for a, pair in zip(amounts, expected, strict=True)):
        raise ValueError("sale_aave_asset_or_decimals")
    borrowed, supplied, next_borrowed = (m["amount_raw"] for m in amounts)
    if min(borrowed, supplied, next_borrowed) <= 0:
        raise ValueError("sale_aave_operation_empty")
    if amounts[1]["from"] != plan.account or any(
        transactions[name].sender != plan.account or transactions[name].value_raw
        for name in aave_names
    ):
        raise ValueError("sale_aave_transaction_roles")
    gateways = {}
    for name, amt, flow in (
        ("borrow", borrowed, amounts[0]),
        ("next_borrow", next_borrowed, amounts[2]),
    ):
        if flow["to"] != sale_plan.borrow_gateway:
            raise ValueError("sale_aave_borrow_recipient")
        gateways[name] = aave_sale.gateway_withdrawal(
            receipts[name],
            account=plan.account,
            weth=plan.weth,
            gateway=sale_plan.borrow_gateway,
            principal=amt,
        )

    anchors = {}
    for name, dirpath in (
        ("borrow", links),
        ("sale", root),
        ("resupply", links),
    ):
        parent_name = "borrow-open" if name == "borrow" else f"{name}-parent"
        before = Capture(dirpath / parent_name, plan.chain_id)
        after = Capture(dirpath / f"{name}-end", plan.chain_id)
        adjoining(before, tx_captures[name], after)
        anchors[name] = (before, after)
    sources = Capture(root / "sale-sources", plan.chain_id)
    prices = marks(plan, anchors["sale"][0], sources)
    balances = {
        name: [
            {
                **balances_at(plan, cap),
                "stable_raw": cap.scalar(
                    plan.stable, "balanceOf(address)", "uint256", plan.account
                ),
            }
            for cap in pair
        ]
        for name, pair in anchors.items()
    }
    direct = {
        k: aave_sale.native_delta(plan.account, transactions[k], r) for k, r in receipts.items()
    }
    # Each block-end comparison stays visible even if the hypothesis fails.
    expected_blocks = {
        "borrow": {"eth_wei": direct["borrow"] + borrowed, "stable_raw": 0, "weth_raw": 0},
        "sale": {"eth_wei": direct["sale"], "stable_raw": lift["usdc_received_raw"], "weth_raw": 0},
        "resupply": {"eth_wei": direct["resupply"], "stable_raw": -supplied, "weth_raw": 0},
    }
    residuals = {}
    for name, (before, after) in balances.items():
        residuals[name] = {
            key: after[key] - before[key] - expected_value
            for key, expected_value in expected_blocks[name].items()
        }
    residuals["between_borrow_and_sale"] = {
        key: balances["sale"][0][key] - balances["borrow"][1][key]
        for key in ("eth_wei", "stable_raw", "weth_raw")
    }
    residuals["between_borrow_and_sale"]["eth_wei"] -= sum(
        direct[name] for name in ("funding_a", "failed_sale", "funding_b")
    )
    residuals["between_sale_and_resupply"] = {
        key: balances["resupply"][0][key] - balances["sale"][1][key]
        for key in ("eth_wei", "stable_raw", "weth_raw")
    }
    residuals["principal_and_collateral_comparison"] = {
        "eth_wei": lift["eth_sent_wei"] - borrowed,
        "stable_raw": lift["usdc_received_raw"] - supplied,
    }
    match = not any(n for residual in residuals.values() for n in residual.values())
    return {
        "schema": "oasis-aave-sale-report-v1",
        "status": "selected_cashflows_match" if match else "cashflow_residual",
        "chain_id": plan.chain_id,
        "account": plan.account,
        "plan_sha256": {"incident": plan_hash, "sale": sale_hash, "bindings": binding_hash},
        "decoder_sha256": sha256_hex(Path(aave_sale.__file__).read_bytes()),
        "transactions": {
            name: {
                **tx_captures[name].evidence,
                "transaction_hash": r.transaction_hash,
                "transaction_index": r.transaction_index,
                "status": r.status,
                "sender": r.sender,
                "recipient": r.recipient,
                "value_wei": transactions[name].value_raw,
                "selector": transactions[name].selector,
                "sender_gas_wei": r.gas_used * r.effective_gas_price,
                "account_native_delta_from_value_gas_wei": direct[name],
            }
            for name, r in receipts.items()
        },
        "balances": {
            name: {"block_end": values, "captures": [c.evidence for c in anchors[name]]}
            for name, values in balances.items()
        },
        "aave_flows": matches,
        "gateway_withdrawal_log_indices": gateways,
        "sale": {
            **lift,
            "stable": plan.stable,
            "stable_decimals": plan.stable_decimals,
            **aave_incident.fill_comparison(
                paid=lift["usdc_received_raw"],
                received=lift["eth_sent_wei"],
                stable_usd_e8=prices[plan.stable]["observed_market_price_usd_e8"],
                weth_usd_e8=prices[plan.weth]["observed_market_price_usd_e8"],
                stable_decimals=plan.stable_decimals,
            ),
        },
        "oracle": {
            "basis": "sale_parent_block_end",
            "prices": prices,
            "market_evidence": anchors["sale"][0].evidence,
            "source_evidence": sources.evidence,
        },
        "selected_account_gas_wei": sum(
            r.gas_used * r.effective_gas_price
            for r in receipts.values()
            if r.sender == plan.account
        ),
        "residuals_raw": residuals,
        "exact_transaction_prestate_verified": False,
        "wallet_transfer_coverage_verified": False,
        "internal_trace_verified": False,
        "independent_providers_verified": False,
        "net_profit": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "incident-plan",
        "sale-plan",
        "cashflows",
        "sale-campaign",
        "link-campaign",
        "output",
    ):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = replay(
            args.incident_plan,
            args.sale_plan,
            args.cashflows,
            args.sale_campaign,
            args.link_campaign,
        )
        publish(args.output, report)
        return 3 if report["status"] == "cashflow_residual" else 0
    except (ValueError, TypeError, OSError, KeyError):
        print("Aave sale replay refused", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
