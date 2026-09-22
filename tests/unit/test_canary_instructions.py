"""User limits remain exact, explicit and incapable of enabling live execution."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from oasis_agent.models.common import canonical_json
from oasis_agent.risk.canary_instructions import (
    capital_limit_breaches,
    parse_canary_instructions,
    usd_micros,
)


def pending():
    data = json.loads(
        (Path(__file__).parents[2] / "configs/canary.robinhood.pending.json").read_bytes()
    )
    data["maximum_total_capital_usd"] = None
    data["status"] = "pending_total_capital_and_production_validation"
    return canonical_json(data)


def test_authorized_capital_is_500_usd_without_execution_authority():
    payload = (Path(__file__).parents[2] / "configs/canary.robinhood.pending.json").read_bytes()
    limits = parse_canary_instructions(payload)
    assert limits.maximum_total_capital_usd_micros == 500_000_000
    assert limits.blockers == ()
    assert not json.loads(limits.payload)["execution_eligible"]


def test_recorded_owner_limits_are_exact_and_missing_capital_blocks():
    limits = parse_canary_instructions(pending())
    assert limits.maximum_daily_loss_usd_micros == 200_000_000
    assert limits.maximum_cumulative_loss_usd_micros == 500_000_000
    assert limits.wallet_dedicated_to_project
    assert limits.blockers == ("total_capital_missing",)
    assert not json.loads(limits.payload)["execution_eligible"]
    assert capital_limit_breaches(
        limits,
        committed_capital_usd_micros=0,
        daily_loss_usd_micros=0,
        cumulative_loss_usd_micros=0,
    ) == ("total_capital_missing",)


@pytest.mark.parametrize("amount", [True, 200, 200.0, "NaN", "1e2", "-1", "1.0000001", "01", " 2"])
def test_usd_never_coerces_or_rounds(amount):
    with pytest.raises(ValueError):
        usd_micros(amount)


@pytest.mark.parametrize("whole", [0, 1, 10**6, 10**12 - 1])
def test_decimal_string_conversion_preserves_every_micro(whole):
    # Deterministic property sweep across representable integer/fraction boundaries.
    for fractional in (*range(0, 1_000_000, 1001), 1, 999998, 999999):
        assert usd_micros(f"{whole}.{fractional:06d}") == whole * 1_000_000 + fractional


def test_loss_boundaries_do_not_allow_one_more_trade_at_the_limit():
    limits = replace(parse_canary_instructions(pending()), maximum_total_capital_usd_micros=10**9)
    assert (
        capital_limit_breaches(
            limits,
            committed_capital_usd_micros=10**9,
            daily_loss_usd_micros=200_000_000 - 1,
            cumulative_loss_usd_micros=500_000_000 - 1,
        )
        == ()
    )
    assert capital_limit_breaches(
        limits,
        committed_capital_usd_micros=10**9 + 1,
        daily_loss_usd_micros=200_000_000,
        cumulative_loss_usd_micros=500_000_000,
    ) == ("total_capital_exceeded", "daily_loss_limit_reached", "cumulative_loss_limit_reached")


@pytest.mark.parametrize(
    "mutation", ["enable", "float", "unknown", "duplicate", "currency", "status"]
)
def test_instruction_file_cannot_silently_enable_or_relax_limits(mutation):
    data = json.loads(pending())
    if mutation == "enable":
        data["execution_enabled"] = True
    elif mutation == "float":
        data["maximum_daily_loss_usd"] = 200.0
    elif mutation == "unknown":
        data["bypass"] = True
    elif mutation == "currency":
        data["valuation_currency"] = "USDG"
    elif mutation == "status":
        data["status"] = "pending_production_validation"
    else:
        with pytest.raises(ValueError):
            parse_canary_instructions(b'{"schema": 1, "schema": 2}')
        return
    with pytest.raises(ValueError):
        parse_canary_instructions(canonical_json(data))
