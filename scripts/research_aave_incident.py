"""Replay pinned Aave reserve transitions, oracle marks and selected WETH9 cashflows."""

import argparse
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from eth_hash.auto import keccak

from oasis_agent.arbitrage import aave_incident as incident
from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.arbitrage.cycle_protocols import abi, calldata, data_bytes
from oasis_agent.evidence.public_receipts import ObservedReceipt, decode_receipt, quantity
from oasis_agent.evidence.public_rpc_plan import PublicReadPlan, PublicReceiptPlan
from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.models.common import ZERO_ADDRESS, BlockRef, sha256_hex

from . import capture_public_rpc
from .research_metric import publish

RECEIPT_NAMES = ("v3-core-freeze", "weth-leg-1", "weth-leg-2")
READ_NAMES = (
    "core-before",
    "core-after",
    "last-borrow-parent",
    "last-borrow-parent-sources",
    "weth-buy-parent",
    "weth-buy-parent-sources",
    "weth-buy-end",
    "unwrap-parent",
    "unwrap-end",
)


class Capture:
    def __init__(self, directory: Path, chain: int, *, receipt: bool = False) -> None:
        plan, _ = capture_public_rpc.load_plan(directory / "plan.json")
        if not isinstance(plan, PublicReceiptPlan if receipt else PublicReadPlan):
            raise ValueError("incident_capture_schema")
        report = capture_public_rpc.replay(directory)
        if report["status"] != "complete":
            raise ValueError("incident_capture_incomplete")
        anchor = report["block"]
        self.block = BlockRef(**anchor["ref"])
        if self.block.chain_id != chain or self.block.timestamp is None:
            raise ValueError("incident_chain_or_timestamp")
        self.parent: str = anchor["parent_hash"]
        self.positions = dict(anchor.get("receipt_positions", ()))
        self.records = report["exports"]["records"]
        self.evidence = {
            "source": plan.source,
            "block": anchor["ref"],
            "parent_hash": self.parent,
            "capture_sha256": report["capture_sha256"],
            "plan_sha256": report["plan_sha256"],
        }

    def receipt(self, tx: str) -> ObservedReceipt:
        if set(self.positions) != {tx}:
            raise ValueError("incident_exact_receipt_required")
        raw = self.result("eth_getTransactionReceipt", [tx])
        return decode_receipt(raw, self.block, tx, self.positions[tx])

    def result(self, method: str, params: list[object]) -> object:
        values = [
            item["response"]["result"]
            for item in self.records
            if item["request"]["method"] == method and item["request"]["params"] == params
        ]
        if len(values) != 1:
            raise ValueError("incident_pinned_read_missing_or_duplicate")
        return values[0]

    def state(self, method: str, arg: object) -> object:
        return self.result(
            method,
            [
                arg,
                {"blockHash": self.block.block_hash, "requireCanonical": True},
            ],
        )

    def runtime(self, target: str) -> str:
        runtime = data_bytes(self.state("eth_getCode", target))
        if not runtime:
            raise ValueError("incident_missing_runtime")
        return "0x" + keccak(runtime).hex()

    def word(self, target: str, signature: str, *args: str) -> object:
        return self.state(
            "eth_call",
            {
                "to": target,
                "data": calldata(signature, ("address",) * len(args), args),
            },
        )

    def scalar(self, target: str, signature: str, kind: str, *args: str) -> Any:
        return abi((kind,), data_bytes(self.word(target, signature, *args)))[0]


def adjoining(parent: Capture, receipt: Capture, end: Capture | None = None) -> None:
    if (
        parent.block.number + 1 != receipt.block.number
        or parent.block.block_hash != receipt.parent
        or (end is not None and (end.block != receipt.block or end.parent != receipt.parent))
    ):
        raise ValueError("incident_parent_or_end_block_mismatch")


def configs(plan: incident.Plan, view: Capture) -> dict[str, incident.Reserve]:
    for signature, expected in (
        ("getPool()", plan.pool),
        ("getPoolConfigurator()", plan.configurator),
    ):
        if view.scalar(plan.provider, signature, "address") != expected:
            raise ValueError("incident_registry_binding")
    return {
        token: incident.reserve(view.word(plan.pool, "getConfiguration(address)", token), places)
        for token, places in ((plan.stable, plan.stable_decimals), (plan.weth, 18))
    }


def marks(plan: incident.Plan, view: Capture, sources: Capture) -> dict[str, dict[str, Any]]:
    if view.block != sources.block or view.parent != sources.parent:
        raise ValueError("incident_feed_block_mismatch")
    if view.runtime(plan.oracle) != plan.oracle_runtime_hash:
        raise ValueError("incident_oracle_runtime_mismatch")
    for contract, method, typ, known in (
        (plan.pool, "ADDRESSES_PROVIDER()", "address", plan.provider),
        (plan.provider, "getPriceOracle()", "address", plan.oracle),
        (plan.oracle, "ADDRESSES_PROVIDER()", "address", plan.provider),
        (plan.oracle, "BASE_CURRENCY()", "address", ZERO_ADDRESS),
        (plan.oracle, "BASE_CURRENCY_UNIT()", "uint256", 10**8),
    ):
        if view.scalar(contract, method, typ) != known:
            raise ValueError("incident_oracle_registry_or_usd_mismatch")
    rows = {}
    for token in (plan.stable, plan.weth):
        source = view.scalar(plan.oracle, "getSourceOfAsset(address)", "address", token)
        answer = view.scalar(plan.oracle, "getAssetPrice(address)", "uint256", token)
        if (
            source == ZERO_ADDRESS
            or answer <= 0
            or sources.scalar(source, "decimals()", "uint8") != 8
            or sources.scalar(source, "latestAnswer()", "int256") != answer
        ):
            raise ValueError("incident_source_answer_or_decimals")
        rows[token] = {
            "source": source,
            "observed_market_price_usd_e8": answer,
            "runtime_hash": sources.runtime(source),
            "round": incident.weth_round(
                sources.word(source, "latestRoundData()"),
                observed=view.block,
                market_price=answer,
                age_limit=plan.max_weth_feed_age_seconds,
            )
            if token == plan.weth
            else None,
        }
    return rows


def balances_at(plan: incident.Plan, view: Capture) -> dict[str, int]:
    if view.runtime(plan.weth) != plan.weth9_runtime_hash:
        raise ValueError("incident_wrapper_runtime_mismatch")
    return {
        "weth_raw": view.scalar(plan.weth, "balanceOf(address)", "uint256", plan.account),
        "eth_wei": quantity(view.state("eth_getBalance", plan.account)),
    }


def run(plan_path: Path, root: Path, borrow_directory: Path) -> dict[str, object]:
    raw = read_bounded(plan_path, 32 * 1024)
    strict_json_object(raw)
    plan = incident.Plan.model_validate_json(raw)
    views = {name: Capture(root / name, plan.chain_id, receipt=True) for name in RECEIPT_NAMES}
    views.update({name: Capture(root / name, plan.chain_id) for name in READ_NAMES})
    views["selected-borrow"] = Capture(borrow_directory, plan.chain_id, receipt=True)
    freeze, borrow, buy, exit_leg = (
        views[n]
        for n in (
            "v3-core-freeze",
            "selected-borrow",
            "weth-leg-1",
            "weth-leg-2",
        )
    )
    controls = (
        ("core-before", freeze, "core-after"),
        ("last-borrow-parent", borrow, None),
        ("weth-buy-parent", buy, "weth-buy-end"),
        ("unwrap-parent", exit_leg, "unwrap-end"),
    )
    for before, tx, after in controls:
        adjoining(views[before], tx, views[after] if after else None)
    if not borrow.block.number < freeze.block.number < buy.block.number < exit_leg.block.number:
        raise ValueError("incident_unexpected_order")
    mark_anchors = (("last-borrow-parent", "borrow"), ("weth-buy-parent", "purchase"))
    observed_marks = {
        role: marks(plan, views[name], views[name + "-sources"]) for name, role in mark_anchors
    }
    buy_marks = observed_marks["purchase"]
    flow = incident.purchase(plan, buy.receipt(plan.purchase_tx))
    exit_flow = incident.unwrap(plan, exit_leg.receipt(plan.unwrap_tx), flow["weth_received_raw"])
    balance_anchors = ("weth-buy-parent", "weth-buy-end", "unwrap-parent", "unwrap-end")
    balances = {name: balances_at(plan, views[name]) for name in balance_anchors}
    buy_pre, buy_end, exit_pre, exit_end = (balances[name] for name in balance_anchors)
    principal, borrow_log = incident.borrow_amount(plan, borrow.receipt(plan.borrow_tx))
    borrow_reserves = configs(plan, views["last-borrow-parent"])
    purchase_reserves = configs(plan, views["weth-buy-parent"])
    deltas = {
        "purchase_weth_block_minus_receipt_raw": (
            buy_end["weth_raw"] - buy_pre["weth_raw"] - flow["weth_received_raw"]
        ),
        "unwrap_weth_block_minus_receipt_raw": (
            exit_end["weth_raw"] - exit_pre["weth_raw"] + exit_flow["weth_withdrawn_raw"]
        ),
        "unwrap_eth_block_minus_net_withdrawal_raw": (
            exit_end["eth_wei"]
            - exit_pre["eth_wei"]
            - exit_flow["weth_withdrawn_raw"]
            + exit_flow["gas_paid_native_raw"]
        ),
    }
    freeze_rows = incident.freeze(
        plan,
        freeze.receipt(plan.freeze_tx),
        configs(plan, views["core-before"]),
        configs(plan, views["core-after"]),
    )
    return {
        "schema": "oasis-aave-incident-report-v1",
        "status": "selected_observations_match" if not any(deltas.values()) else "balance_residual",
        "chain_id": plan.chain_id,
        "account": plan.account,
        "plan_sha256": sha256_hex(raw),
        "decoder_sha256": sha256_hex(Path(incident.__file__).read_bytes()),
        "evidence": {name: c.evidence for name, c in views.items()},
        "freeze": {"transaction_hash": plan.freeze_tx, "reserves": freeze_rows},
        "oracle": {
            "currency": "USD",
            "decimals": 8,
            "basis": "parent_block_end",
            "market": plan.oracle,
            "market_runtime_hash": plan.oracle_runtime_hash,
            "marks": observed_marks,
            "exact_transaction_prestate_verified": False,
        },
        "borrow": {
            "transaction_hash": plan.borrow_tx,
            "principal_weth_raw": principal,
            "log_index": borrow_log,
            "reserves_parent_block_end": {
                asset: asdict(row) for asset, row in borrow_reserves.items()
            },
        },
        "purchase": {
            "transaction_hash": plan.purchase_tx,
            **flow,
            "stable": plan.stable,
            "stable_decimals": plan.stable_decimals,
            "weth": plan.weth,
            "weth_decimals": 18,
            "reserves_parent_block_end": {
                asset: asdict(row) for asset, row in purchase_reserves.items()
            },
            **incident.fill_comparison(
                paid=flow["stable_paid_raw"],
                received=flow["weth_received_raw"],
                stable_usd_e8=buy_marks[plan.stable]["observed_market_price_usd_e8"],
                weth_usd_e8=buy_marks[plan.weth]["observed_market_price_usd_e8"],
                stable_decimals=plan.stable_decimals,
            ),
        },
        "unwrap": {"transaction_hash": plan.unwrap_tx, **exit_flow},
        "balances_block_end_raw": balances,
        "block_end_balance_residuals": deltas,
        "settlement_trace_verified": False,
        "independent_providers_verified": False,
        "net_profit": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--campaign", required=True, type=Path)
    parser.add_argument("--borrow-receipt", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = run(args.plan, args.campaign, args.borrow_receipt)
        publish(args.output, report)
        return 3 if report["status"] == "balance_residual" else 0
    except (ValueError, TypeError, OSError, KeyError):
        print("Aave incident replay refused", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
