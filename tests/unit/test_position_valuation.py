"""Pinned position valuation: conservative floors, pinned identity, stale refusal.

The valuation is the first component that turns a live LP position into the USD
micros the risk ledger needs. These tests hold it to the two properties that
matter for capital safety: it never over-states a position (an inflated NAV
would hide a loss), and it refuses anything it cannot attribute to the exact
pool currency, asset and feed round it was given.
"""

from fractions import Fraction

import pytest

from oasis_agent.farming.account_inventory import FeedAnswer
from oasis_agent.farming.position_valuation import (
    CurrencyPrice,
    PositionValuation,
    value_position,
)
from oasis_agent.farming.positions import PositionSnapshot
from oasis_agent.models.common import BlockRef
from oasis_agent.models.v4 import DYNAMIC_FEE_FLAG, PoolKey
from oasis_agent.uniswap_v4.liquidity_math import MAX_TICK, sqrt_price_at_tick
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key
from oasis_agent.uniswap_v4.state_rpc import Slot0State

CHAIN_ID = 8453
BLOCK_NUMBER = 100
BLOCK_HASH = "0x" + "ab" * 32
TIMESTAMP = 1_800_000_000
WETH = "0x" + "11" * 20
USDC = "0x" + "22" * 20
POSITION_MANAGER = "0x" + "44" * 20
OWNER = "0x" + "55" * 20
HOOKS = "0x" + "33" * 20
TOKEN_ID = 7
POOL_MANAGER = "0x" + "44" * 20
TICK_LOWER = -60
TICK_UPPER = 60
LIQUIDITY = 10**24
FEE0 = 11
FEE1 = 13
FEED_DECIMALS = 8
MAX_AGE = 300
WETH_ANSWER = 200_000_000_000  # 2000.00000000 USD per WETH
USDC_ANSWER = 100_000_000  # 1.00000000 USD per USDC


def pool_key() -> PoolKey:
    return PoolKey(WETH, USDC, DYNAMIC_FEE_FLAG, 60, HOOKS)


def snapshot(
    *,
    block: BlockRef | None = None,
    tick_lower: int = TICK_LOWER,
    tick_upper: int = TICK_UPPER,
    liquidity: int = LIQUIDITY,
) -> PositionSnapshot:
    key = pool_key()
    return PositionSnapshot(
        block if block is not None else BlockRef(CHAIN_ID, BLOCK_NUMBER, BLOCK_HASH, TIMESTAMP),
        POSITION_MANAGER,
        TOKEN_ID,
        OWNER,
        POSITION_MANAGER,
        key,
        pool_id_from_key(key),
        "0x" + "66" * 32,
        tick_lower,
        tick_upper,
        False,
        liquidity,
        0,
        0,
        0,
        0,
        FEE0,
        FEE1,
    )


def slot0(*, tick: int = 0) -> Slot0State:
    return Slot0State(sqrt_price_at_tick(tick), tick, 0, 0)


def feed(asset_id: str, answer: int, *, updated_at: int = TIMESTAMP) -> FeedAnswer:
    return FeedAnswer(asset_id, answer, FEED_DECIMALS, 1, updated_at)


def price(
    address: str,
    asset_id: str,
    answer: int,
    *,
    decimals: int = 18,
    updated_at: int = TIMESTAMP,
    max_age: int = MAX_AGE,
) -> CurrencyPrice:
    return CurrencyPrice(
        address,
        asset_id,
        decimals,
        feed(asset_id, answer, updated_at=updated_at),
        max_age,
    )


def weth_price(**overrides: object) -> CurrencyPrice:
    options: dict[str, object] = {
        "address": WETH,
        "asset_id": "weth",
        "answer": WETH_ANSWER,
        "decimals": 18,
    }
    options.update(overrides)
    return price(**options)  # type: ignore[arg-type]


def usdc_price(**overrides: object) -> CurrencyPrice:
    options: dict[str, object] = {
        "address": USDC,
        "asset_id": "usdc",
        "answer": USDC_ANSWER,
        "decimals": 6,
    }
    options.update(overrides)
    return price(**options)  # type: ignore[arg-type]


def true_leg(units: int, answer: int, token_decimals: int) -> Fraction:
    return Fraction(units * answer * 1_000_000, 10**FEED_DECIMALS * 10**token_decimals)


def test_both_legs_are_floored_so_the_position_is_never_over_stated() -> None:
    valuation = value_position(snapshot(), slot0(), currency0=weth_price(), currency1=usdc_price())
    along = true_leg(valuation.total0, WETH_ANSWER, 18)
    across = true_leg(valuation.total1, USDC_ANSWER, 6)
    assert valuation.usd_micros == int(along) + int(across)
    assert Fraction(valuation.usd_micros) <= along + across
    # The exercise is only meaningful if flooring discarded a remainder.
    assert along.denominator != 1


def test_each_leg_is_valued_from_its_own_token_scale_and_feed() -> None:
    valuation = value_position(snapshot(), slot0(), currency0=weth_price(), currency1=usdc_price())
    # 2000 USD per WETH at 18 decimals: one WETH wei is 2e-9 USD micros.
    assert valuation.total0 // 500_000_000 + valuation.total1 == valuation.usd_micros
    assert (valuation.principal0, valuation.principal1) != (0, 0)
    assert valuation.in_range is True


def test_an_out_of_range_position_holds_only_the_leg_left_above_its_range() -> None:
    valuation = value_position(
        snapshot(), slot0(tick=TICK_UPPER + 60), currency0=weth_price(), currency1=usdc_price()
    )
    assert valuation.in_range is False
    assert valuation.principal0 == 0
    assert valuation.total0 == FEE0  # only the uncollected fee remains on the empty side
    assert valuation.principal1 > 0


def test_a_currency_price_for_another_address_is_refused() -> None:
    with pytest.raises(ValueError, match="currency0 valuation is not the position pool currency"):
        value_position(
            snapshot(),
            slot0(),
            currency0=usdc_price(address=USDC, asset_id="usdc-wrong", decimals=18),
            currency1=usdc_price(),
        )
    with pytest.raises(ValueError, match="currency1 valuation is not the position pool currency"):
        value_position(
            snapshot(), slot0(), currency0=weth_price(), currency1=usdc_price(address=WETH)
        )


def test_the_same_asset_cannot_price_both_sides_of_a_pool() -> None:
    with pytest.raises(ValueError, match="pool currencies must be distinct assets"):
        value_position(
            snapshot(), slot0(), currency0=weth_price(), currency1=weth_price(address=USDC)
        )


def test_a_stale_or_future_feed_round_is_refused() -> None:
    value_position(
        snapshot(),
        slot0(),
        currency0=weth_price(updated_at=TIMESTAMP - MAX_AGE),
        currency1=usdc_price(updated_at=TIMESTAMP),
    )
    with pytest.raises(ValueError, match="stale or future currency valuation"):
        value_position(
            snapshot(),
            slot0(),
            currency0=weth_price(updated_at=TIMESTAMP - MAX_AGE - 1),
            currency1=usdc_price(),
        )
    with pytest.raises(ValueError, match="stale or future currency valuation"):
        value_position(
            snapshot(),
            slot0(),
            currency0=weth_price(),
            currency1=usdc_price(updated_at=TIMESTAMP + 1),
        )


def test_a_slot0_tick_that_does_not_bracket_its_own_price_is_refused() -> None:
    with pytest.raises(ValueError, match="slot0 sqrt price is inconsistent with its tick"):
        value_position(
            snapshot(),
            Slot0State(sqrt_price_at_tick(0), 60, 0, 0),
            currency0=weth_price(),
            currency1=usdc_price(),
        )
    with pytest.raises(ValueError, match="slot0 tick has no representable upper bound"):
        value_position(
            snapshot(),
            Slot0State(sqrt_price_at_tick(MAX_TICK) - 1, MAX_TICK, 0, 0),
            currency0=weth_price(),
            currency1=usdc_price(),
        )


def test_an_untimestamped_block_cannot_carry_a_valuation() -> None:
    with pytest.raises(ValueError, match="position valuation requires a timestamped block"):
        value_position(
            snapshot(block=BlockRef(CHAIN_ID, BLOCK_NUMBER, BLOCK_HASH, None)),
            slot0(),
            currency0=weth_price(),
            currency1=usdc_price(),
        )


def test_a_currency_price_must_match_its_own_feed_round_and_bounds() -> None:
    with pytest.raises(ValueError, match="feed round of that asset"):
        CurrencyPrice(WETH, "weth", 18, feed("usdc", USDC_ANSWER), MAX_AGE)
    with pytest.raises(ValueError, match="token decimals must be bounded"):
        CurrencyPrice(WETH, "weth", 37, feed("weth", WETH_ANSWER), MAX_AGE)
    with pytest.raises(ValueError, match="positive feed age bound"):
        CurrencyPrice(WETH, "weth", 18, feed("weth", WETH_ANSWER), 0)


def test_a_valuation_cannot_disagree_with_its_own_principal_and_fees() -> None:
    valuation = value_position(snapshot(), slot0(), currency0=weth_price(), currency1=usdc_price())
    with pytest.raises(ValueError, match="principal plus uncollected fees"):
        PositionValuation(
            pool_id=valuation.pool_id,
            token_id=valuation.token_id,
            block=valuation.block,
            sqrt_price_x96=valuation.sqrt_price_x96,
            in_range=valuation.in_range,
            principal0=valuation.principal0,
            principal1=valuation.principal1,
            uncollected_fee0=valuation.uncollected_fee0,
            uncollected_fee1=valuation.uncollected_fee1,
            total0=valuation.total0 + 1,
            total1=valuation.total1,
            usd_micros=valuation.usd_micros,
        )
