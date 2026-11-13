"""Invented round numbers, not event prices or historical Aave risk parameters."""

import json
import sys
from pathlib import Path


def plan_data():
    exits = []
    for name, price, cp, dp in (
        ("convergence", 2300, 1, 2300),
        ("premium_persists", 2600, 1, 2300),
        ("oracle_jump", 4600, 1, 4600),
        ("collateral_depeg", 2875, 0, 2300),
    ):
        exits.append(
            {
                "name": name,
                "oracle_collateral_usd_e8": 10**8 if cp else 80_000_000,
                "oracle_debt_usd_e8": dp * 10**8,
                "buy_price_collateral_raw_per_debt_token": price * 10**6,
                "buy_depth_debt_raw": 100 * 10**18,
                "debt_growth_e18": 1_000_100_000_000_000_000,
                "collateral_withdraw_liquidity_raw": 100_000 * 10**6,
                "repay_paused": False,
                "withdraw_paused": False,
            }
        )
    return {
        "schema": "oasis-lending-loop-v1",
        "evidence_class": "synthetic",
        "market_mode": "plain_single_collateral",
        "chain_id": 1,
        "collateral": {"address": "0x" + "aa" * 20, "symbol": "SYNUSD", "decimals": 6},
        "debt_asset": {"address": "0x" + "bb" * 20, "symbol": "SYNETH", "decimals": 18},
        "initial_debt_asset_raw": 10**18,
        "initial_collateral_raw": 0,
        "exit_cash_reserve_raw": 20_000 * 10**6,
        "oracle_collateral_usd_e8": 10**8,
        "oracle_debt_usd_e8": 2300 * 10**8,
        "sell_price_collateral_raw_per_debt_token": 2600 * 10**6,
        "sell_depth_debt_raw": 100 * 10**18,
        "target_ltv_bps": 7500,
        "protocol_ltv_bps": 7800,
        "liquidation_threshold_bps": 8000,
        "supply_cap_remaining_raw": 100_000 * 10**6,
        "borrow_cap_remaining_raw": 100 * 10**18,
        "borrow_liquidity_raw": 100 * 10**18,
        "supply_enabled": True,
        "borrow_enabled": True,
        "freeze_after_rounds": 32,
        "max_rounds": 4,
        "non_interest_cost_raw": 25 * 10**6,
        "exits": exits,
    }


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(json.dumps(plan_data(), indent=2) + "\n")
