"""Value conservation, liquidation stress and sequential unwind funding."""

import json

import pytest

from oasis_agent.arbitrage.lending_loop import E18, LendingPlan, simulate
from scripts import research_lending_loop as cli
from tests.fixtures.lending_loop import plan_data


def run(data=None):
    return simulate(LendingPlan.model_validate_json(json.dumps(data or plan_data())))


def test_borrowed_sales_are_matched_by_debt_and_baselines():
    p = plan_data()
    r = run(p)
    assert r["total_sold_debt_asset_raw"] == p["initial_debt_asset_raw"] + r["debt_principal_raw"]
    assert r["collateral_raw"] + r["free_cash_raw"] == (
        r["initial_sale_proceeds_raw"] + sum(x["sale_proceeds_raw"] for x in r["rounds"])
    )
    c, flat, jump, _ = r["exits"]
    assert c["conditional_excess_vs_hold_raw"] > 0
    assert c["conditional_terminal_equity_raw"] == (
        r["collateral_raw"]
        + r["free_cash_raw"]
        + p["exit_cash_reserve_raw"]
        - c["buyback_cost_raw"]
        - p["non_interest_cost_raw"]
    )
    assert flat["conditional_excess_vs_hold_raw"] < 0
    assert flat["conditional_excess_vs_sale_only_raw"] < 0
    assert "liquidatable_stress_state" in jump["reasons"]
    assert jump["conditional_terminal_equity_raw"] is None
    assert r["realized_pnl_raw"] is None and not r["execution_eligible"]


def test_locked_collateral_cannot_finance_its_own_release():
    p = plan_data()
    p["exit_cash_reserve_raw"] = 0
    c = run(p)["exits"][0]
    assert c["gross_marked_equity_raw"] > 0
    assert c["reasons"] == ["exit_cash_shortfall"]
    assert c["exit_cash_shortfall_raw"] == c["buyback_cost_raw"] + p["non_interest_cost_raw"]
    assert c["conditional_excess_vs_hold_raw"] is None


@pytest.mark.parametrize("field", ["borrow_cap_remaining_raw", "borrow_liquidity_raw"])
def test_capacity_is_shared_across_rounds(field):
    p = plan_data()
    p[field] = 10**18
    r = run(p)
    assert r["debt_principal_raw"] == 10**18
    assert r["stop_reason"] == "ltv_cap_liquidity_or_depth"


def test_depth_counts_initial_inventory_and_all_borrowed_sales():
    p = plan_data()
    p["sell_depth_debt_raw"] = 2 * 10**18
    assert run(p)["debt_principal_raw"] == 10**18
    p["sell_depth_debt_raw"] = 10**18 - 1
    with pytest.raises(ValueError, match="initial_sale_depth"):
        run(p)


def test_supply_cap_leaves_unencumbered_cash_and_stops_ltv_growth():
    p = plan_data()
    p["supply_cap_remaining_raw"] = 2600 * 10**6
    r = run(p)
    assert len(r["rounds"]) == 1
    assert r["collateral_raw"] == p["supply_cap_remaining_raw"]
    assert r["free_cash_raw"] == r["rounds"][0]["sale_proceeds_raw"]


@pytest.mark.parametrize("n", [0, 1, 2])
def test_freeze_stops_new_rounds_but_not_repayment(n):
    p = plan_data()
    p["freeze_after_rounds"] = n
    r = run(p)
    assert len(r["rounds"]) == n
    assert r["stop_reason"] == "borrow_disabled_or_frozen"
    assert r["exits"][0]["modeled_exit_complete"]


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("buy_depth_debt_raw", 0, "buyback_depth_insufficient"),
        ("repay_paused", True, "repay_paused"),
        ("withdraw_paused", True, "withdraw_paused"),
        ("collateral_withdraw_liquidity_raw", 0, "withdraw_liquidity_insufficient"),
        ("oracle_collateral_usd_e8", 10_000_000, "liquidatable_stress_state"),
    ],
)
def test_exit_failures_remain_visible(field, value, reason):
    p = plan_data()
    p["exits"][0][field] = value
    c = run(p)["exits"][0]
    assert reason in c["reasons"]
    assert c["conditional_terminal_equity_raw"] is None


def test_unknown_cost_never_becomes_zero():
    p = plan_data()
    p["non_interest_cost_raw"] = None
    c = run(p)["exits"][0]
    assert c["non_interest_cost_raw"] is None
    assert c["reasons"] == ["non_interest_cost_unknown"]
    assert c["conditional_terminal_equity_raw"] is None


def test_interest_and_cost_monotonically_reduce_excess():
    p = plan_data()
    before = run(p)["exits"][0]
    p["non_interest_cost_raw"] += 1
    p["exits"][0]["debt_growth_e18"] += 1
    after = run(p)["exits"][0]
    assert after["debt_with_interest_raw"] >= before["debt_with_interest_raw"]
    assert after["buyback_cost_raw"] >= before["buyback_cost_raw"]
    assert after["conditional_excess_vs_hold_raw"] < before["conditional_excess_vs_hold_raw"]


@pytest.mark.parametrize("chain", [1, 8453, 4663, 99901])
def test_identical_math_is_chain_parametric_without_deployment_claim(chain):
    p = plan_data()
    p["chain_id"] = chain
    r = run(p)
    assert r["chain_id"] == chain
    assert r["exits"] == run()["exits"]
    assert r["block_identity"] is None


@pytest.mark.parametrize("value", [True, 1.1, "100", -1, 2**256])
def test_invalid_amounts_fail_closed(value):
    p = plan_data()
    p["initial_debt_asset_raw"] = value
    with pytest.raises(ValueError):
        run(p)


@pytest.mark.parametrize(
    "update",
    [
        {"protocol_ltv_bps": 8000},
        {"target_ltv_bps": 7900},
        {"evidence_class": "historical"},
        {"max_rounds": 33},
        {"supply_enabled": False},
        {"initial_debt_asset_raw": 0},
    ],
)
def test_invalid_scope_or_unavailable_supply(update):
    p = plan_data()
    p.update(update)
    with pytest.raises(ValueError):
        run(p)


def test_decimal_change_preserves_economics_on_exact_values():
    p = plan_data()
    p.update(
        oracle_debt_usd_e8=2000 * 10**8,
        sell_price_collateral_raw_per_debt_token=2000 * 10**6,
        target_ltv_bps=5000,
    )
    for e in p["exits"]:
        e["debt_growth_e18"] = E18
    first = run(p)
    for field in (
        "initial_debt_asset_raw",
        "sell_depth_debt_raw",
        "borrow_cap_remaining_raw",
        "borrow_liquidity_raw",
    ):
        p[field] //= 10**10
    p["debt_asset"]["decimals"] = 8
    for e in p["exits"]:
        e["buy_depth_debt_raw"] //= 10**10
    second = run(p)
    assert first["collateral_raw"] == second["collateral_raw"]
    assert first["debt_principal_raw"] == second["debt_principal_raw"] * 10**10
    for a, b in zip(first["exits"], second["exits"], strict=True):
        assert a["conditional_excess_vs_hold_raw"] == b["conditional_excess_vs_hold_raw"]


def test_entrypoint_is_deterministic_and_refuses_overwrite(tmp_path, monkeypatch):
    path, output = tmp_path / "plan.json", tmp_path / "report.json"
    path.write_text(json.dumps(plan_data()))
    monkeypatch.setattr("sys.argv", ["research", "--plan", str(path), "--output", str(output)])
    assert cli.main() == 0
    raw = output.read_bytes()
    assert cli.main() == 2
    assert output.read_bytes() == raw


def test_ratio_above_one_still_respects_caps_and_round_limit():
    p = plan_data()
    p.update(sell_price_collateral_raw_per_debt_token=4000 * 10**6, max_rounds=32)
    r = run(p)
    assert r["debt_principal_raw"] <= p["borrow_cap_remaining_raw"]
    assert r["total_sold_debt_asset_raw"] <= p["sell_depth_debt_raw"]
    assert r["collateral_raw"] <= p["supply_cap_remaining_raw"]
    assert len(r["rounds"]) <= 32
