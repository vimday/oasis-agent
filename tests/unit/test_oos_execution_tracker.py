"""Unit tests for the event-time ``t -> t+1`` OOS execution and attribution tracker."""

from dataclasses import dataclass
from decimal import Decimal

import pytest

from oasis_agent.cross_venue.aleph_bridge import ShmBboQuote
from oasis_agent.cross_venue.live_cex_dex_shadow import (
    build_synthetic_coinbase_l2_book,
    evaluate_cex_dex_opportunity,
)
from oasis_agent.cross_venue.maker_hedge_shadow import (
    MakerFillEvent,
    MakerHedgeConfig,
    MakerHedgeLifecycleTracker,
)
from oasis_agent.cross_venue.oos_execution_tracker import (
    OosStepResult,
    OosStrategyExecutionTracker,
    OosTrackerConfig,
    PendingTakerIntent,
    RealizedOosTrade,
)


@dataclass(frozen=True)
class _SampleOpp:
    product_id: str
    direction: str
    net_edge_bps: Decimal
    recommended_notional_usdc: int
    token_amount: int
    token_decimals: int
    execution_feasible: bool = True


def test_oos_tracker_config_and_dataclass_validation() -> None:
    cfg = OosTrackerConfig()
    assert cfg.taker_persistence_ticks == 2
    assert cfg.min_signal_edge_bps == 1
    assert cfg.max_signal_age_ms == 30_000
    assert cfg.require_same_direction is True
    assert cfg.default_gas_cost_usd == Decimal("0.0040")
    assert cfg.coinbase_taker_fee_bps == Decimal(15)
    assert cfg.execution_eligible is False

    for kwargs in (
        {"taker_persistence_ticks": 0},
        {"taker_persistence_ticks": True},
        {"min_signal_edge_bps": 0},
        {"min_signal_edge_bps": -1},
        {"max_signal_age_ms": 0},
        {"default_gas_cost_usd": Decimal("-0.01")},
        {"coinbase_taker_fee_bps": Decimal(-1)},
        {"execution_eligible": True},
    ):
        with pytest.raises((ValueError, TypeError)):
            OosTrackerConfig(**kwargs)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="execution_eligible"):
        PendingTakerIntent(
            step_seq=1,
            observed_at_utc="2026-11-18T12:00:00+00:00",
            observed_at_ms=1_795_003_200_000,
            product_id="ETH-USD",
            direction="BUY_DEX_SELL_CEX",
            notional_usdc_micros=500_000_000,
            notional_token_base=10**17,
            token_decimals=18,
            quoted_net_edge_bps=Decimal(10),
            quoted_net_profit_usd=Decimal("0.5"),
            execution_eligible=True,  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="execution_eligible"):
        RealizedOosTrade(
            trade_id="t1",
            strategy="taker",
            product_id="ETH-USD",
            direction="BUY_DEX_SELL_CEX",
            decision_step_seq=1,
            settlement_step_seq=2,
            observed_at_utc="2026-11-18T12:00:02+00:00",
            notional_usd=Decimal(500),
            quoted_net_edge_bps=Decimal(10),
            realized_net_edge_bps=Decimal(8),
            realized_net_profit_usd=Decimal("0.4"),
            gas_cost_usd=Decimal("0.004"),
            fee_usd=Decimal("0.75"),
            execution_eligible=True,  # type: ignore[arg-type]
        )


def test_oos_walk_forward_zero_lookahead_and_exact_math_both_directions() -> None:
    cfg = OosTrackerConfig(
        taker_persistence_ticks=2,
        min_signal_edge_bps=5,
        default_gas_cost_usd=Decimal("0.05"),
        coinbase_taker_fee_bps=Decimal(10),  # 10 bps
    )
    tracker = OosStrategyExecutionTracker(cfg)

    # Tick 1: ETH-USD qualifies BUY_DEX_SELL_CEX -> streak=1, no intent queued yet.
    # Tick 1 quotes are already favorable, proving zero look-ahead (nothing settles on tick 1).
    opp_eth_buy = _SampleOpp(
        product_id="ETH-USD",
        direction="BUY_DEX_SELL_CEX",
        net_edge_bps=Decimal(25),
        recommended_notional_usdc=500_000_000,  # $500
        token_amount=168_000_000_000_000_000,
        token_decimals=18,
    )
    r1 = tracker.step_tick(
        step_seq=1,
        observed_at_utc="2026-11-18T12:00:00+00:00",
        cex_dex_opportunities=[opp_eth_buy],
        latest_dex_quotes_by_product={
            "ETH-USD": (
                500_000_000,
                168_000_000_000_000_000,
                10**17,
                300_000_000,
                18,
                Decimal("0.05"),
            )
        },
        latest_bbo_by_product={"ETH-USD": (Decimal("3010.00"), Decimal("3011.00"))},
    )
    assert isinstance(r1, OosStepResult)
    assert r1.execution_eligible is False
    assert r1.settled_taker_trades == ()
    assert r1.new_pending_intents == ()
    assert r1.rejected_flickers == 0

    # Tick 2: ETH-USD qualifies a 2nd consecutive tick -> intent queued at tick 2, NOT settled yet!
    r2 = tracker.step_tick(
        step_seq=2,
        observed_at_utc="2026-11-18T12:00:02+00:00",
        cex_dex_opportunities=[opp_eth_buy],
        latest_dex_quotes_by_product={
            "ETH-USD": (
                500_000_000,
                168_000_000_000_000_000,
                10**17,
                300_000_000,
                18,
                Decimal("0.05"),
            )
        },
        latest_bbo_by_product={"ETH-USD": (Decimal("3010.00"), Decimal("3011.00"))},
    )
    assert r2.settled_taker_trades == ()
    assert len(r2.new_pending_intents) == 1
    intent = r2.new_pending_intents[0]
    assert intent.step_seq == 2
    assert intent.direction == "BUY_DEX_SELL_CEX"
    assert intent.notional_usdc_micros == 500_000_000
    assert len(tracker.pending_intents) == 1

    # Tick 3: Settles tick 2's intent using tick 3's FRESH DEX quote (scaled from $1000 quote!)
    # and tick 3's Coinbase BBO. Tick 3 also triggers a SELL_DEX_BUY_CEX streak=1 on BTC-USD.
    # For $1000 DEX buy quote -> 0.340 ETH, scaled to $500 intent -> 0.170 ETH.
    # CEX best_bid = 3000.00 -> gross = 0.170 * 3000 = 510.00 USD.
    # Fee (10 bps of 510) = 0.51 USD; gas = 0.05 USD; notional = 500.00 USD.
    # Net profit = 510 - 0.51 - 500 - 0.05 = 9.44 USD; edge = (9.44 / 500) * 10000 = 188.8 bps.
    btc_sell_opp = {
        "product_id": "BTC-USD",
        "direction": "SELL_DEX_BUY_CEX",
        "net_edge_bps": 20,
        "notional_usdc": 900_000_000,
        "token_amount": 1_000_000,  # 0.01 cbBTC (8 decimals)
        "token_decimals": 8,
    }
    r3 = tracker.step_tick(
        step_seq=3,
        observed_at_utc="2026-11-18T12:00:04+00:00",
        cex_dex_opportunities=[btc_sell_opp],
        latest_dex_quotes_by_product={
            "ETH-USD": (
                1_000_000_000,
                340_000_000_000_000_000,
                10**17,
                299_000_000,
                18,
                Decimal("0.05"),
            ),
            "BTC-USD": (900_000_000, 990_000, 2_000_000, 1_804_000_000, 8, Decimal("0.04")),
        },
        latest_bbo_by_product={
            "ETH-USD": {"best_bid_price": "3000.00", "best_ask_price": "3001.00"},
            "BTC-USD": (Decimal("89950.00"), Decimal("90000.00")),
        },
    )
    assert len(r3.settled_taker_trades) == 1
    t_eth = r3.settled_taker_trades[0]
    assert t_eth.strategy == "taker"
    assert t_eth.product_id == "ETH-USD"
    assert t_eth.direction == "BUY_DEX_SELL_CEX"
    assert t_eth.decision_step_seq == 2
    assert t_eth.settlement_step_seq == 3
    assert t_eth.notional_usd == Decimal(500)
    assert t_eth.fee_usd == Decimal("0.51")
    assert t_eth.gas_cost_usd == Decimal("0.05")
    assert t_eth.realized_net_profit_usd == Decimal("9.44")
    assert t_eth.realized_net_edge_bps == Decimal("188.8")
    assert t_eth.execution_eligible is False

    # Tick 4: BTC-USD qualifies a 2nd time -> queues SELL_DEX_BUY_CEX intent for 0.01 cbBTC.
    r4 = tracker.step_tick(
        step_seq=4,
        observed_at_utc="2026-11-18T12:00:06+00:00",
        cex_dex_opportunities=[btc_sell_opp],
        latest_dex_quotes_by_product={},
        latest_bbo_by_product={},
    )
    assert r4.settled_taker_trades == ()
    assert len(r4.new_pending_intents) == 1
    assert r4.new_pending_intents[0].direction == "SELL_DEX_BUY_CEX"

    # Tick 5: Adverse market move on BTC-USD! DEX sell proceeds drop to 1,780 USDC for 0.02 cbBTC
    # -> scaled to 0.01 cbBTC = 890.00 USD; CEX ask = 90000 -> gross CEX cost = 900.00 USD.
    # Fee (10 bps of 900) = 0.90 USD; gas override = 0.10 USD.
    # Realized profit = 890.00 - (900.00 + 0.90) - 0.10 = -11.00 USD! Negative outcome retained!
    r5 = tracker.step_tick(
        step_seq=5,
        observed_at_utc="2026-11-18T12:00:08+00:00",
        cex_dex_opportunities=[],
        latest_dex_quotes_by_product={
            "BTC-USD": (900_000_000, 990_000, 2_000_000, 1_780_000_000, 8, Decimal("0.04"))
        },
        latest_bbo_by_product={"BTC-USD": (Decimal("89990.00"), Decimal("90000.00"))},
        gas_cost_usd_by_product={"BTC-USD": Decimal("0.10")},
    )
    assert len(r5.settled_taker_trades) == 1
    t_btc = r5.settled_taker_trades[0]
    assert t_btc.strategy == "taker"
    assert t_btc.direction == "SELL_DEX_BUY_CEX"
    assert t_btc.decision_step_seq == 4
    assert t_btc.settlement_step_seq == 5
    assert t_btc.notional_usd == Decimal(900)
    assert t_btc.fee_usd == Decimal("0.90")
    assert t_btc.gas_cost_usd == Decimal("0.10")
    assert t_btc.realized_net_profit_usd == Decimal("-11.00")
    assert t_btc.realized_net_edge_bps == (Decimal("-11.00") / Decimal(900)) * Decimal(10_000)

    summary = tracker.summary()
    assert summary["oos_trades_count"] == 2
    assert summary["oos_taker_trades_count"] == 2
    assert summary["oos_maker_trades_count"] == 0
    assert summary["oos_positive_trades"] == 1
    assert summary["oos_win_rate"] == "0.5000"
    assert summary["oos_total_net_profit_usd"] == "-1.560000"
    assert summary["oos_max_drawdown_usd"] == "11.000000"
    assert summary["execution_eligible"] is False


def test_oos_flicker_rejection_direction_reversal_and_stale_expiration() -> None:
    cfg = OosTrackerConfig(
        taker_persistence_ticks=2,
        min_signal_edge_bps=5,
        max_signal_age_ms=5_000,
        require_same_direction=True,
        default_gas_cost_usd=Decimal("0.01"),
        coinbase_taker_fee_bps=Decimal(0),
    )
    tracker = OosStrategyExecutionTracker(cfg)

    opp_buy = {
        "product_id": "ETH-USD",
        "direction": "BUY_DEX_SELL_CEX",
        "net_edge_bps": 10,
        "trade_notional_usdc": 200_000_000,
        "token_amount": 66_000_000_000_000_000,
        "token_decimals": 18,
    }
    opp_sell = {
        "product_id": "ETH-USD",
        "direction": "SELL_DEX_BUY_CEX",
        "net_edge_bps": 10,
        "trade_notional_usdc": 200_000_000,
        "token_amount": 66_000_000_000_000_000,
        "token_decimals": 18,
    }

    # Tick 1: 1-tick qualifying signal -> streak=1
    r1 = tracker.step_tick(
        step_seq=1,
        observed_at_utc="2026-11-18T12:00:00+00:00",
        cex_dex_opportunities=[opp_buy],
        latest_dex_quotes_by_product={},
        latest_bbo_by_product={},
    )
    assert r1.rejected_flickers == 0

    # Tick 2: reverses direction -> prior 1-tick streak rejected as flicker; new streak=1!
    r2 = tracker.step_tick(
        step_seq=2,
        observed_at_utc="2026-11-18T12:00:01+00:00",
        cex_dex_opportunities=[opp_sell],
        latest_dex_quotes_by_product={},
        latest_bbo_by_product={},
    )
    assert r2.rejected_flickers == 1
    assert r2.new_pending_intents == ()

    # Tick 3: drops out completely -> that 1-tick streak is also rejected as flicker!
    r3 = tracker.step_tick(
        step_seq=3,
        observed_at_utc="2026-11-18T12:00:02+00:00",
        cex_dex_opportunities=[],
        latest_dex_quotes_by_product={},
        latest_bbo_by_product={},
    )
    assert r3.rejected_flickers == 1

    # Tick 4-5: sustained 2-tick BUY signal -> queues PendingTakerIntent at t=5 (age limit 5000ms).
    tracker.step_tick(
        step_seq=4,
        observed_at_utc="2026-11-18T12:00:03+00:00",
        cex_dex_opportunities=[opp_buy],
        latest_dex_quotes_by_product={},
        latest_bbo_by_product={},
    )
    r5 = tracker.step_tick(
        step_seq=5,
        observed_at_utc="2026-11-18T12:00:04+00:00",
        cex_dex_opportunities=[opp_buy],
        latest_dex_quotes_by_product={},
        latest_bbo_by_product={},
    )
    assert len(r5.new_pending_intents) == 1

    # Tick 6: +4000ms (<= 5000ms), but quotes unavailable -> stays pending, not expired.
    r6 = tracker.step_tick(
        step_seq=6,
        observed_at_utc="2026-11-18T12:00:08+00:00",
        cex_dex_opportunities=[],
        latest_dex_quotes_by_product={},
        latest_bbo_by_product={},
    )
    assert r6.expired_pending_intents == 0
    assert len(tracker.pending_intents) == 1

    # Tick 7: +6000ms (> 5000ms max_signal_age_ms) -> expires safely!
    r7 = tracker.step_tick(
        step_seq=7,
        observed_at_utc="2026-11-18T12:00:10+00:00",
        cex_dex_opportunities=[],
        latest_dex_quotes_by_product={},
        latest_bbo_by_product={},
    )
    assert r7.expired_pending_intents == 1
    assert r7.settled_taker_trades == ()
    assert len(tracker.pending_intents) == 0

    summary = tracker.summary()
    assert summary["oos_trades_count"] == 0
    assert summary["rejected_one_tick_flickers"] == 2
    assert summary["expired_pending_intents"] == 1
    assert summary["pending_unsettled_intents"] == 0
    assert summary["oos_lcb_net_edge_bps"] == 0
    assert summary["oos_std_net_edge_bps"] == "0"


def test_oos_rejects_infeasible_or_zero_sized_signals_and_accepts_polymorphic_inputs() -> None:
    cfg = OosTrackerConfig(
        taker_persistence_ticks=1,
        min_signal_edge_bps=5,
        require_same_direction=False,
        default_gas_cost_usd=Decimal("0.004"),
        coinbase_taker_fee_bps=Decimal(5),
    )
    tracker = OosStrategyExecutionTracker(cfg)

    book_t1 = build_synthetic_coinbase_l2_book("ETH-USD", mid_price=Decimal("3000.00"))
    dex_buy_eth = 168_000_000_000_000_000  # 0.168 ETH for $500 (~$2976/ETH -> positive edge)
    cex_eval = evaluate_cex_dex_opportunity(
        product_or_symbol="ETH-USD",
        book=book_t1,
        trade_notional_usdc=500_000_000,
        dex_buy_token_for_usdc_out=dex_buy_eth,
        trade_notional_token=10**17,
        dex_sell_token_for_usdc_out=298_000_000,
        gas_cost_usd=Decimal("0.004"),
        cex_taker_fee_bps=Decimal("3.5"),
        latency_haircut_bps=Decimal("1.5"),
    )
    assert cex_eval.status == "ok"

    # Zero-token_amount and execution_feasible=False dicts must NOT qualify.
    bad_opps = [
        {
            "product_id": "BTC-USD",
            "direction": "BUY_DEX_SELL_CEX",
            "net_edge_bps": 50,
            "notional_usdc": 200_000_000,
            "token_amount": 0,
        },
        {
            "product_id": "AERO-USD",
            "direction": "BUY_DEX_SELL_CEX",
            "net_edge_bps": 50,
            "notional_usdc": 200_000_000,
            "token_amount": 10**18,
            "execution_feasible": False,
        },
    ]

    r1 = tracker.step_tick(
        step_seq=1,
        observed_at_utc="2026-11-18T12:00:00+00:00",
        cex_dex_opportunities=[cex_eval, *bad_opps],
        latest_dex_quotes_by_product={},
        latest_bbo_by_product={},
    )
    assert len(r1.new_pending_intents) == 1
    assert r1.new_pending_intents[0].product_id == "ETH-USD"

    # Settle via ShmBboQuote and dict-shaped DEX quote!
    shm_bbo = ShmBboQuote(
        symbol_id=0,
        exchange_id=0,
        venue="coinbase",
        timestamp_ns=1_000_000,
        age_ns=1_000,
        seqlock=2,
        bid_usd_micros=3_010_000_000,
        ask_usd_micros=3_012_000_000,
        bid_size=Decimal(5),
        ask_size=Decimal(5),
    )
    r2 = tracker.step_tick(
        step_seq=2,
        observed_at_utc="2026-11-18T12:00:02+00:00",
        cex_dex_opportunities=[],
        latest_dex_quotes_by_product={
            "ETH-USD": {
                "trade_notional_usdc": 500_000_000,
                "dex_buy_token_out": 168_000_000_000_000_000,
                "trade_notional_token": 10**17,
                "dex_sell_usdc_out": 298_000_000,
                "token_decimals": 18,
            }
        },
        latest_bbo_by_product={"ETH-USD": shm_bbo},
    )
    assert len(r2.settled_taker_trades) == 1
    trade = r2.settled_taker_trades[0]
    assert trade.realized_net_profit_usd > Decimal(5)

    # Single trade LCB must stay conservative (<= 0) when n < 2!
    summary = tracker.summary()
    assert summary["oos_trades_count"] == 1
    assert summary["oos_lcb_net_edge_bps"] <= 0


def test_oos_maker_fill_dedup_unhedged_filtering_and_lcb_stats() -> None:
    maker_cfg = MakerHedgeConfig(
        trade_notional_usdc=200_000_000,
        cex_maker_fee_bps=Decimal(0),
        adverse_selection_bps=Decimal("1.5"),
        min_quote_edge_bps=Decimal("3.0"),
        tick_improvement_bps=Decimal("0.5"),
    )
    maker_tracker = MakerHedgeLifecycleTracker(maker_cfg)
    qty_token = (200 * 10**18) // 3000

    book_t1 = build_synthetic_coinbase_l2_book(
        "ETH-USD", mid_price=Decimal("3000.00"), spread_bps=Decimal("60.0")
    )
    maker_tracker.step(
        "ETH-USD",
        current_book=book_t1,
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_token,
        trade_notional_token=qty_token,
        dex_sell_usdc_out=199_800_000,
        token_decimals=18,
        gas_cost_usd=Decimal("0.04"),
    )
    book_t2 = build_synthetic_coinbase_l2_book(
        "ETH-USD", mid_price=Decimal("3015.00"), spread_bps=Decimal("10.0")
    )
    step2 = maker_tracker.step(
        "ETH-USD",
        current_book=book_t2,
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_token,
        trade_notional_token=qty_token,
        dex_sell_usdc_out=199_800_000,
        token_decimals=18,
        gas_cost_usd=Decimal("0.04"),
    )
    assert len(step2.strict_fills) == 1

    # Add a diagnostic touch_lift event + unhedged/pending dicts to verify filtering & dedup.
    touch_lift_event = MakerFillEvent(
        product_id="ETH-USD",
        side="ask_maker",
        fill_classification="touch_lift",
        quoted_price_usd=Decimal("3005.00"),
        next_book_price_usd=Decimal("3006.00"),
        hedge_dex_price_usd=Decimal("3000.00"),
        realized_net_bps=Decimal(15),
        realized_net_profit_usd=Decimal("0.30"),
        latency_drift_bps=Decimal(0),
    )
    unhedged_dict = {
        "event_type": "maker_fill",
        "status": "unhedged",
        "product_id": "ETH-USD",
        "side": "ask_maker",
        "notional_usdc": 200_000_000,
        "net_profit_usd": "0.50",
    }
    pending_dict = {
        "event_type": "maker_fill",
        "status": "pending",
        "product_id": "ETH-USD",
        "side": "bid_maker",
        "notional_usdc": 200_000_000,
        "net_profit_usd": "0.50",
    }

    oos = OosStrategyExecutionTracker()

    # Tick 1: pass maker_tracker.all_fills() -> 1 settled maker trade.
    res1 = oos.step_tick(
        step_seq=1,
        observed_at_utc="2026-11-18T12:00:00+00:00",
        cex_dex_opportunities=[],
        latest_dex_quotes_by_product={},
        latest_bbo_by_product={},
        maker_fills=[*maker_tracker.all_fills(), touch_lift_event, unhedged_dict, pending_dict],
    )
    assert len(res1.settled_maker_trades) == 1
    assert res1.settled_maker_trades[0].strategy == "maker"

    # Tick 2: RE-PASSING prior maker_tracker.all_fills() MUST NOT double-count!
    res2 = oos.step_tick(
        step_seq=2,
        observed_at_utc="2026-11-18T12:00:02+00:00",
        cex_dex_opportunities=[],
        latest_dex_quotes_by_product={},
        latest_bbo_by_product={},
        maker_fills=maker_tracker.all_fills(),
    )
    assert res2.settled_maker_trades == ()

    # Ticks 3..6: feed 4 more distinct dict maker fills so total = 5 positive OOS trades.
    for idx in range(3, 7):
        ts = f"2026-11-18T12:00:{idx * 2:02d}+00:00"
        res_k = oos.step_tick(
            step_seq=idx,
            observed_at_utc=ts,
            cex_dex_opportunities=[],
            latest_dex_quotes_by_product={},
            latest_bbo_by_product={},
            maker_fills=[
                *maker_tracker.all_fills(),
                {
                    "event_type": "maker_fill",
                    "product_id": "BTC-USD",
                    "side": "bid_maker",
                    "recorded_at_utc": ts,
                    "notional_usdc": 200_000_000,
                    "net_profit_usd": "0.24",
                    "hedge_tx_cost_usd": "0.0040",
                },
            ],
        )
        assert len(res_k.settled_maker_trades) == 1
        assert res_k.settled_maker_trades[0].realized_net_edge_bps == Decimal(12)

    summary = oos.summary()
    assert summary["oos_trades_count"] == 5
    assert summary["oos_taker_trades_count"] == 0
    assert summary["oos_maker_trades_count"] == 5
    assert summary["oos_positive_trades"] == 5
    assert summary["oos_win_rate"] == "1.0000"
    assert Decimal(summary["oos_total_net_profit_usd"]) > Decimal("1.0")
    assert type(summary["oos_lcb_net_edge_bps"]) is int
    assert summary["oos_lcb_net_edge_bps"] >= 5
    assert summary["oos_max_drawdown_usd"] == "0.000000"
    assert Decimal(summary["oos_tail_loss_p5_bps"]) >= Decimal(12)
    assert summary["execution_eligible"] is False


def test_oos_step_monotonicity_and_negative_single_trade_lcb() -> None:
    tracker = OosStrategyExecutionTracker(OosTrackerConfig(taker_persistence_ticks=1))
    tracker.step_tick(
        step_seq=5,
        observed_at_utc="2026-11-18T12:00:05+00:00",
        cex_dex_opportunities=[],
        latest_dex_quotes_by_product={},
        latest_bbo_by_product={},
        maker_fills=[
            {
                "event_type": "maker_fill",
                "product_id": "ETH-USD",
                "side": "ask_maker",
                "recorded_at_utc": "2026-11-18T12:00:05+00:00",
                "notional_usdc": 200_000_000,
                "net_profit_usd": "-0.40",
                "hedge_tx_cost_usd": "0.01",
            }
        ],
    )
    summary = tracker.summary()
    assert summary["oos_trades_count"] == 1
    assert summary["oos_lcb_net_edge_bps"] == -20
    assert summary["oos_lcb_net_edge_bps_exact"] == "-20.000000"
    assert summary["oos_max_drawdown_usd"] == "0.400000"
    assert summary["oos_tail_loss_p5_bps"] == "-20.000000"

    with pytest.raises(ValueError, match="strictly increasing"):
        tracker.step_tick(
            step_seq=5,
            observed_at_utc="2026-11-18T12:00:06+00:00",
            cex_dex_opportunities=[],
            latest_dex_quotes_by_product={},
            latest_bbo_by_product={},
        )

    with pytest.raises(ValueError, match="backwards"):
        tracker.step_tick(
            step_seq=6,
            observed_at_utc="2026-11-18T12:00:04+00:00",
            cex_dex_opportunities=[],
            latest_dex_quotes_by_product={},
            latest_bbo_by_product={},
        )
