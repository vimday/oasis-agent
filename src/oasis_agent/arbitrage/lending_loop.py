"""Synthetic lending/spot stress accounting, never an Aave execution simulator.

Fixed fee-inclusive price bounds apply only up to declared aggregate depth. Debt
growth is a scenario input, not an implementation of the variable borrow index.
"""

from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator

from ..models.common import ZERO_ADDRESS
from .cycle_plan import Asset, Frozen, Label, Raw, Size

E18 = 10**18
BPS = 10_000
Bps = Annotated[int, Field(gt=0, lt=BPS)]


def ceil_div(n: int, d: int) -> int:
    return (n + d - 1) // d


def uint(n: int) -> int:
    if not 0 <= n < 1 << 256:
        raise ValueError("scenario_amount_overflow")
    return n


class ExitScenario(Frozen):
    name: Label
    oracle_collateral_usd_e8: Size
    oracle_debt_usd_e8: Size
    buy_price_collateral_raw_per_debt_token: Size
    buy_depth_debt_raw: Raw
    debt_growth_e18: Annotated[int, Field(ge=E18, le=100 * E18)]
    collateral_withdraw_liquidity_raw: Raw
    repay_paused: bool
    withdraw_paused: bool


class LendingPlan(Frozen):
    schema_version: Literal["oasis-lending-loop-v1"] = Field(alias="schema")
    evidence_class: Literal["synthetic"]
    market_mode: Literal["plain_single_collateral"]
    chain_id: Annotated[int, Field(gt=0, lt=1 << 64)]
    collateral: Asset
    debt_asset: Asset
    initial_debt_asset_raw: Raw
    initial_collateral_raw: Raw
    exit_cash_reserve_raw: Raw
    oracle_collateral_usd_e8: Size
    oracle_debt_usd_e8: Size
    sell_price_collateral_raw_per_debt_token: Size
    sell_depth_debt_raw: Raw
    target_ltv_bps: Bps
    protocol_ltv_bps: Bps
    liquidation_threshold_bps: Bps
    supply_cap_remaining_raw: Raw
    borrow_cap_remaining_raw: Raw
    borrow_liquidity_raw: Raw
    supply_enabled: bool
    borrow_enabled: bool
    freeze_after_rounds: Annotated[int, Field(ge=0, le=32)]
    max_rounds: Annotated[int, Field(ge=1, le=32)]
    # Includes gas, builder, failed-attempt allowance and opportunity cost in
    # collateral units; interest is separate via debt_growth_e18, never charged twice.
    non_interest_cost_raw: Raw | None
    exits: Annotated[tuple[ExitScenario, ...], Field(min_length=1, max_length=16)]

    @model_validator(mode="after")
    def scope(self) -> Self:
        assets = (self.collateral, self.debt_asset)
        if any(a.address in (None, ZERO_ADDRESS) or a.decimals > 36 for a in assets):
            raise ValueError("explicit_erc20_identity_required")
        if self.collateral.address == self.debt_asset.address:
            raise ValueError("distinct_assets_required")
        if not self.target_ltv_bps <= self.protocol_ltv_bps < self.liquidation_threshold_bps:
            raise ValueError("ltv_must_leave_liquidation_margin")
        if not self.initial_debt_asset_raw and not self.initial_collateral_raw:
            raise ValueError("initial_capital_required")
        if len({e.name for e in self.exits}) != len(self.exits):
            raise ValueError("duplicate_scenario")
        return self


def health(plan: LendingPlan, collateral: int, debt: int, cp: int, dp: int) -> int | None:
    if not debt:
        return None  # No debt: not an unknown price, and never an invented infinity.
    collateral_usd = collateral * cp // int(10**plan.collateral.decimals)
    debt_usd = ceil_div(debt * dp, int(10**plan.debt_asset.decimals))
    return collateral_usd * plan.liquidation_threshold_bps * E18 // (BPS * debt_usd)


def simulate(plan: LendingPlan) -> dict[str, Any]:
    unit = 10**plan.debt_asset.decimals
    price = plan.sell_price_collateral_raw_per_debt_token
    if plan.initial_debt_asset_raw > plan.sell_depth_debt_raw:
        raise ValueError("initial_sale_depth_insufficient")
    if not plan.supply_enabled:
        raise ValueError("initial_supply_unavailable")
    initial_sale = uint(plan.initial_debt_asset_raw * price // unit)
    cash = uint(plan.initial_collateral_raw + initial_sale)
    collateral = min(cash, plan.supply_cap_remaining_raw)
    cash -= collateral
    debt = 0
    sold = plan.initial_debt_asset_raw
    rounds = []
    stop = "round_limit"
    for index in range(plan.max_rounds):
        if not plan.borrow_enabled or index >= plan.freeze_after_rounds:
            stop = "borrow_disabled_or_frozen"
            break
        # Floor collateral value then target LTV, and floor available debt units.
        value = collateral * plan.oracle_collateral_usd_e8 // 10**plan.collateral.decimals
        capacity = value * plan.target_ltv_bps // BPS * unit // plan.oracle_debt_usd_e8
        amount = min(
            max(0, capacity - debt),
            plan.borrow_cap_remaining_raw - debt,
            plan.borrow_liquidity_raw - debt,
            plan.sell_depth_debt_raw - sold,
        )
        if amount <= 0:
            stop = "ltv_cap_liquidity_or_depth"
            break
        proceeds = uint(amount * price // unit)
        if proceeds == 0:
            stop = "sale_dust"
            break
        debt = uint(debt + amount)
        sold = uint(sold + amount)
        cash = uint(cash + proceeds)
        supplied = min(cash, plan.supply_cap_remaining_raw - collateral)
        cash -= supplied
        collateral = uint(collateral + supplied)
        rounds.append(
            {
                "round": index + 1,
                "borrowed_raw": amount,
                "sale_proceeds_raw": proceeds,
                "supplied_raw": supplied,
                "collateral_raw": collateral,
                "debt_raw": debt,
                "free_cash_raw": cash,
                "health_factor_e18": health(
                    plan, collateral, debt, plan.oracle_collateral_usd_e8, plan.oracle_debt_usd_e8
                ),
            }
        )
    results = []
    for case in plan.exits:
        grown = uint(ceil_div(debt * case.debt_growth_e18, E18))
        buyback = uint(ceil_div(grown * case.buy_price_collateral_raw_per_debt_token, unit))
        hf = health(plan, collateral, grown, case.oracle_collateral_usd_e8, case.oracle_debt_usd_e8)
        # Both baselines keep the same external exit reserve and initial capital.
        hold = uint(
            plan.initial_collateral_raw
            + plan.exit_cash_reserve_raw
            + ceil_div(
                plan.initial_debt_asset_raw * case.buy_price_collateral_raw_per_debt_token, unit
            )
        )
        sale_only = uint(plan.initial_collateral_raw + initial_sale + plan.exit_cash_reserve_raw)
        assets = uint(collateral + cash + plan.exit_cash_reserve_raw)
        gross_equity = assets - buyback
        reasons = []
        if hf is not None and hf < E18:
            reasons.append("liquidatable_stress_state")
        if grown > case.buy_depth_debt_raw:
            reasons.append("buyback_depth_insufficient")
        if grown and case.repay_paused:
            reasons.append("repay_paused")
        if collateral and case.withdraw_paused:
            reasons.append("withdraw_paused")
        if collateral > case.collateral_withdraw_liquidity_raw:
            reasons.append("withdraw_liquidity_insufficient")
        cost = plan.non_interest_cost_raw
        if cost is None:
            reasons.append("non_interest_cost_unknown")
        # Buy/repay must precede withdrawal: collateral cannot fund its own release.
        available = uint(cash + plan.exit_cash_reserve_raw)
        shortfall = max(0, buyback + (cost or 0) - available)
        if shortfall:
            reasons.append("exit_cash_shortfall")
        closed = not reasons
        terminal = gross_equity - cost if closed and cost is not None else None
        results.append(
            {
                "name": case.name,
                "debt_with_interest_raw": grown,
                "interest_debt_raw": grown - debt,
                "buyback_cost_raw": buyback,
                "health_factor_e18": hf,
                "exit_cash_shortfall_raw": shortfall,
                "exit_cash_shortfall_is_lower_bound": cost is None,
                "gross_marked_equity_raw": gross_equity,
                "non_interest_cost_raw": cost,
                "hold_baseline_raw": hold,
                "sale_only_baseline_before_cost_raw": sale_only,
                "conditional_terminal_equity_raw": terminal,
                "conditional_excess_vs_hold_raw": None if terminal is None else terminal - hold,
                "conditional_excess_vs_sale_only_raw": None
                if terminal is None
                else terminal - sale_only,
                "modeled_exit_complete": closed,
                "reasons": reasons,
            }
        )
    return {
        "schema": "oasis-lending-loop-report-v1",
        "evidence_class": "synthetic",
        "chain_id": plan.chain_id,
        "collateral_asset": plan.collateral.model_dump(),
        "debt_asset": plan.debt_asset.model_dump(),
        "rounds": rounds,
        "stop_reason": stop,
        "initial_sale_proceeds_raw": initial_sale,
        "collateral_raw": collateral,
        "free_cash_raw": cash,
        "exit_cash_reserve_raw": plan.exit_cash_reserve_raw,
        "debt_principal_raw": debt,
        "total_sold_debt_asset_raw": sold,
        "exits": results,
        "execution_eligible": False,
        "realized_pnl_raw": None,
        "block_identity": None,
        "limitations": [
            "synthetic_fixed_price_bounds",
            "not_protocol_simulation",
            "no_liquidation_path_or_intraperiod_prices",
            "no_supply_interest",
            "external_exit_cash_only",
            "no_gas_payment_or_atomicity_proof",
        ],
    }
