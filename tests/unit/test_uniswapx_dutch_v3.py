import pytest

from oasis_agent.errors import ExecutionBlockedError
from oasis_agent.intents.dutch_v3 import (
    BASE_CHAIN_ID,
    DEPRECATED_PRIORITY_REACTOR_BASE,
    DutchV3Curve,
    DutchV3Order,
    DutchV3Output,
    resolve_dutch_v3_amounts,
)
from oasis_agent.intents.filler_shadow import evaluate_dutch_v3_fill
from oasis_agent.models.common import ZERO_ADDRESS, sha256_hex

REACTOR_ADDR = "0x" + "33" * 20
SWAPPER_ADDR = "0x" + "44" * 20
EXCLUSIVE_ADDR = "0x" + "55" * 20
FILLER_ADDR = "0x" + "66" * 20
WETH_ADDR = "0x" + "77" * 20
USDC_ADDR = "0x" + "88" * 20


def _sample_order(
    *,
    exclusive_filler: str = ZERO_ADDRESS,
    exclusivity_override_bps: int = 0,
    exclusivity_end_block: int = 1_000,
    reactor_address: str = REACTOR_ADDR,
) -> DutchV3Order:
    # 1.0 WETH input (1e18), decaying USDC output from 2,520 -> 2,480 USDC (6 decimals)
    # Piecewise nonlinear decay over 30 blocks:
    #   +10 blocks: -24 USDC (2.4 USDC/block) -> 2,496 USDC
    #   +30 blocks: -40 USDC (0.8 USDC/block over blocks 10..30) -> 2,480 USDC
    return DutchV3Order(
        order_hash="0x" + sha256_hex(b"dutch-v3-order-1"),
        chain_id=BASE_CHAIN_ID,
        reactor_address=reactor_address,
        swapper=SWAPPER_ADDR,
        nonce=1,
        decay_start_block=1_000,
        decay_end_block=1_030,
        deadline_block=1_040,
        exclusivity_end_block=exclusivity_end_block,
        exclusive_filler=exclusive_filler,
        exclusivity_override_bps=exclusivity_override_bps,
        input_token=WETH_ADDR,
        input_start_amount=10**18,
        input_end_amount=10**18,
        outputs=(
            DutchV3Output(
                token=USDC_ADDR,
                start_amount=2_520_000_000,
                end_amount=2_480_000_000,
                recipient=SWAPPER_ADDR,
                curve=DutchV3Curve(
                    relative_blocks=(10, 30),
                    relative_amounts=(24_000_000, 40_000_000),
                ),
            ),
        ),
    )


def test_nonlinear_piecewise_curve_interpolation_and_rising_input() -> None:
    order = _sample_order()

    at_start = resolve_dutch_v3_amounts(order, 1_000, FILLER_ADDR)
    assert at_start.input_amount == 10**18
    assert at_start.output_amounts == (2_520_000_000,)

    # Halfway through first segment (+5 blocks): 2,520 - 12 = 2,508 USDC
    seg1_mid = resolve_dutch_v3_amounts(order, 1_005, FILLER_ADDR)
    assert seg1_mid.output_amounts == (2_508_000_000,)

    # Exact breakpoint (+10 blocks): 2,520 - 24 = 2,496 USDC
    seg1_end = resolve_dutch_v3_amounts(order, 1_010, FILLER_ADDR)
    assert seg1_end.output_amounts == (2_496_000_000,)

    # Halfway through second segment (+20 blocks): 2,520 - (24 + 8) = 2,488 USDC
    seg2_mid = resolve_dutch_v3_amounts(order, 1_020, FILLER_ADDR)
    assert seg2_mid.output_amounts == (2_488_000_000,)

    # End block (+30 blocks) and post-decay (+35 <= deadline): 2,480 USDC
    at_end = resolve_dutch_v3_amounts(order, 1_030, FILLER_ADDR)
    assert at_end.output_amounts == (2_480_000_000,)
    after_end = resolve_dutch_v3_amounts(order, 1_035, FILLER_ADDR)
    assert after_end.output_amounts == (2_480_000_000,)

    # Rising input curve test
    rising_order = DutchV3Order(
        order_hash="0x" + sha256_hex(b"dutch-v3-rising-input"),
        chain_id=BASE_CHAIN_ID,
        reactor_address=REACTOR_ADDR,
        swapper=SWAPPER_ADDR,
        nonce=2,
        decay_start_block=1_000,
        decay_end_block=1_020,
        deadline_block=1_030,
        exclusivity_end_block=1_000,
        exclusive_filler=ZERO_ADDRESS,
        exclusivity_override_bps=0,
        input_token=WETH_ADDR,
        input_start_amount=1_000_000,
        input_end_amount=1_050_000,
        input_curve=DutchV3Curve(relative_blocks=(10, 20), relative_amounts=(-30_000, -50_000)),
        outputs=(
            DutchV3Output(
                token=USDC_ADDR,
                start_amount=2_500_000_000,
                end_amount=2_500_000_000,
                recipient=SWAPPER_ADDR,
                curve=DutchV3Curve(relative_blocks=(), relative_amounts=()),
            ),
        ),
    )
    assert resolve_dutch_v3_amounts(rising_order, 1_005, FILLER_ADDR).input_amount == 1_015_000
    assert resolve_dutch_v3_amounts(rising_order, 1_015, FILLER_ADDR).input_amount == 1_040_000

    with pytest.raises(ExecutionBlockedError, match="expired"):
        resolve_dutch_v3_amounts(order, 1_041, FILLER_ADDR)


def test_exclusivity_override_and_strict_exclusive_fail_closed() -> None:
    # Exclusive filler active until block 1010 with 50 bps override.
    override_order = _sample_order(
        exclusive_filler=EXCLUSIVE_ADDR,
        exclusivity_override_bps=50,
        exclusivity_end_block=1_010,
    )

    # Non-exclusive filler at block 1005 must improve output (2,508 USDC * 1.005 = 2,520.54 USDC).
    non_excl = resolve_dutch_v3_amounts(override_order, 1_005, FILLER_ADDR)
    assert non_excl.exclusivity_override_applied is True
    assert non_excl.exclusivity_override_bps_used == 50
    assert non_excl.output_amounts == (2_520_540_000,)

    # Exclusive filler itself pays no override inside window.
    excl = resolve_dutch_v3_amounts(override_order, 1_005, EXCLUSIVE_ADDR)
    assert excl.exclusivity_override_applied is False
    assert excl.output_amounts == (2_508_000_000,)

    # At exclusivity_end_block (1010), open auction: no override for anyone.
    post_excl = resolve_dutch_v3_amounts(override_order, 1_010, FILLER_ADDR)
    assert post_excl.exclusivity_override_applied is False
    assert post_excl.output_amounts == (2_496_000_000,)

    # Strict exclusivity (exclusivity_override_bps == 0) fails closed for non-exclusive filler,
    # and opens cleanly at exclusivity_end_block.
    strict_order = _sample_order(
        exclusive_filler=EXCLUSIVE_ADDR,
        exclusivity_override_bps=0,
        exclusivity_end_block=1_010,
    )
    with pytest.raises(ExecutionBlockedError, match="exclusive_window_strict_no_override"):
        resolve_dutch_v3_amounts(strict_order, 1_005, FILLER_ADDR)
    assert resolve_dutch_v3_amounts(strict_order, 1_010, FILLER_ADDR).output_amounts == (
        2_496_000_000,
    )

    blocked_shadow = evaluate_dutch_v3_fill(
        order=strict_order,
        fill_block=1_005,
        filler_address=FILLER_ADDR,
        input_token_decimals=18,
        input_hedge_bid_usd_micros=2_500_000_000,
        output_token_decimals=(6,),
        output_hedge_ask_usd_micros=(1_000_000,),
        hedge_fee_bps=2,
        hedge_slippage_bps=1,
        reactor_callback_gas_usd_micros=40_000,
        quote_latency_ms=100,
    )
    assert blocked_shadow.approved is False
    assert blocked_shadow.execution_eligible is False
    assert "exclusive_window_strict_no_override" in blocked_shadow.rejection_reasons


def test_deprecated_priority_reactor_rejected() -> None:
    with pytest.raises(ValueError, match="PriorityOrderReactor"):
        _sample_order(reactor_address=DEPRECATED_PRIORITY_REACTOR_BASE)

    with pytest.raises(ValueError, match="PriorityOrderReactor"):
        _sample_order(reactor_address=DEPRECATED_PRIORITY_REACTOR_BASE.upper().replace("0X", "0x"))


def test_shadow_filler_decisions_and_500ms_sla_guard() -> None:
    order = _sample_order()

    # At block 1030, filler gets 1.0 WETH (hedge bid $2,500) and pays 2,480 USDC (ask $1.00).
    # Gross = +$20. Costs: hedge fee 2 bps ($0.50) + slip 1 bps ($0.25) + gas ($0.04) = $0.79
    # Net = +$19.21 -> approved in shadow mode!
    good = evaluate_dutch_v3_fill(
        order=order,
        fill_block=1_030,
        filler_address=FILLER_ADDR,
        input_token_decimals=18,
        input_hedge_bid_usd_micros=2_500_000_000,
        output_token_decimals=(6,),
        output_hedge_ask_usd_micros=(1_000_000,),
        hedge_fee_bps=2,
        hedge_slippage_bps=1,
        reactor_callback_gas_usd_micros=40_000,
        quote_latency_ms=180,
        amm_baseline_fee_bps=5,
        sla_budget_ms=500,
    )
    assert good.approved is True
    assert good.execution_eligible is False
    assert good.saved_amm_fee_bps == 5
    assert good.saved_amm_fee_usd_micros == 1_250_000
    assert good.worst_case_net_usd_micros == 19_210_000

    # Latency 501 ms exceeds 500 ms hard SLA -> rejected!
    slow = evaluate_dutch_v3_fill(
        order=order,
        fill_block=1_030,
        filler_address=FILLER_ADDR,
        input_token_decimals=18,
        input_hedge_bid_usd_micros=2_500_000_000,
        output_token_decimals=(6,),
        output_hedge_ask_usd_micros=(1_000_000,),
        hedge_fee_bps=2,
        hedge_slippage_bps=1,
        reactor_callback_gas_usd_micros=40_000,
        quote_latency_ms=501,
        amm_baseline_fee_bps=5,
        sla_budget_ms=500,
    )
    assert slow.approved is False
    assert slow.execution_eligible is False
    assert "sla_budget_exceeded" in slow.rejection_reasons

    # At block 1000, filler must pay 2,520 USDC for $2,500 WETH -> negative net edge!
    unprofitable = evaluate_dutch_v3_fill(
        order=order,
        fill_block=1_000,
        filler_address=FILLER_ADDR,
        input_token_decimals=18,
        input_hedge_bid_usd_micros=2_500_000_000,
        output_token_decimals=(6,),
        output_hedge_ask_usd_micros=(1_000_000,),
        hedge_fee_bps=2,
        hedge_slippage_bps=1,
        reactor_callback_gas_usd_micros=40_000,
        quote_latency_ms=120,
    )
    assert unprofitable.approved is False
    assert unprofitable.worst_case_net_usd_micros < 0
    assert "nonpositive_worst_case_net_usd" in unprofitable.rejection_reasons
