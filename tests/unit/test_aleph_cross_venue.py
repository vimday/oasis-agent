import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from oasis_agent.cross_venue.aleph_bridge import (
    HEADER_BYTES,
    NUM_EXCHANGES,
    NUM_SYMBOLS,
    SHM_MAGIC_V12,
    SLOT_SIZE,
    AlephShmReader,
    SyntheticBboSlot,
    probe_remote_aleph_lakehouse,
    write_synthetic_aleph_shm_v12,
)
from oasis_agent.cross_venue.funding_basis import SessionStatus, evaluate_funding_basis
from oasis_agent.cross_venue.lead_lag import LeadLagDirection, evaluate_cex_dex_lead_lag


def test_aleph_shm_v12_mmap_roundtrip_and_rejections(tmp_path: Path) -> None:
    shm_path = tmp_path / "aleph-matrix"
    now_ns = 1_795_000_000_000_000_000

    slots = [
        # Slot (0, 0) tests that writing slot (0,0) preserves SHM_MAGIC_V12 in _Reserved.
        SyntheticBboSlot(
            exchange_id=0,
            symbol_id=0,
            timestamp_ns=now_ns - 10_000_000,
            bid_price=100.0,
            bid_size=1.0,
            ask_price=101.0,
            ask_size=1.0,
            seqlock=2,
        ),
        SyntheticBboSlot(
            exchange_id=1,  # hyperliquid
            symbol_id=1002,
            timestamp_ns=now_ns - 50_000_000,
            bid_price=2499.5,
            bid_size=12.5,
            ask_price=2500.25,
            ask_size=10.0,
            seqlock=4,
            symbol_version=7,
        ),
        SyntheticBboSlot(
            exchange_id=2,  # lighter
            symbol_id=1002,
            timestamp_ns=now_ns - 20_000_000,
            bid_price=2499.75,
            bid_size=8.0,
            ask_price=2500.5,
            ask_size=9.0,
            seqlock=6,
            symbol_version=7,
        ),
        SyntheticBboSlot(
            exchange_id=6,  # binance_usdm
            symbol_id=1002,
            timestamp_ns=now_ns - 10_000_000,
            bid_price=2504.0,
            bid_size=25.0,
            ask_price=2504.5,
            ask_size=30.0,
            seqlock=10,
            symbol_version=7,
        ),
        # Odd seqlock (writer active / stuck) on slot 7 (arcus)
        SyntheticBboSlot(
            exchange_id=7,
            symbol_id=1002,
            timestamp_ns=now_ns - 5_000_000,
            bid_price=2500.0,
            bid_size=5.0,
            ask_price=2501.0,
            ask_size=5.0,
            seqlock=11,
        ),
        # Stale timestamp on slot 8 (lighter_robinhood)
        SyntheticBboSlot(
            exchange_id=8,
            symbol_id=1002,
            timestamp_ns=now_ns - 5_000_000_000,
            bid_price=2500.0,
            bid_size=5.0,
            ask_price=2501.0,
            ask_size=5.0,
            seqlock=2,
        ),
        # Crossed book on slot 9 (entropy)
        SyntheticBboSlot(
            exchange_id=9,
            symbol_id=1002,
            timestamp_ns=now_ns - 5_000_000,
            bid_price=2505.0,
            bid_size=5.0,
            ask_price=2501.0,
            ask_size=5.0,
            seqlock=2,
        ),
        # NaN bid on slot 10 (tradexyz)
        SyntheticBboSlot(
            exchange_id=10,
            symbol_id=1002,
            timestamp_ns=now_ns - 5_000_000,
            bid_price=float("nan"),
            bid_size=5.0,
            ask_price=2501.0,
            ask_size=5.0,
            seqlock=2,
        ),
    ]
    write_synthetic_aleph_shm_v12(shm_path, slots)
    assert shm_path.stat().st_size == HEADER_BYTES + NUM_SYMBOLS * NUM_EXCHANGES * SLOT_SIZE

    with AlephShmReader.open(shm_path) as reader:
        assert reader.read_symbol_version(1002) == 7

        binance = reader.read_slot(1002, 6, now_ns=now_ns, max_age_ns=1_000_000_000)
        assert binance is not None
        assert binance.venue == "binance_usdm"
        assert binance.bid_usd_micros == 2_504_000_000
        assert binance.ask_usd_micros == 2_504_500_000
        assert binance.bid_size == Decimal("25.0")

        hl = reader.read_slot(1002, 1, now_ns=now_ns, max_age_ns=1_000_000_000)
        assert hl is not None and hl.venue == "hyperliquid"

        lighter = reader.read_slot(1002, 2, now_ns=now_ns, max_age_ns=1_000_000_000)
        assert lighter is not None and lighter.venue == "lighter"

        odd = reader.read_slot_with_status(1002, 7, now_ns=now_ns, max_age_ns=1_000_000_000)
        assert odd.quote is None
        assert odd.status == "odd_seqlock"

        stale = reader.read_slot_with_status(1002, 8, now_ns=now_ns, max_age_ns=1_000_000_000)
        assert stale.quote is None
        assert stale.status == "stale"

        crossed = reader.read_slot_with_status(1002, 9, now_ns=now_ns, max_age_ns=1_000_000_000)
        assert crossed.quote is None
        assert crossed.status == "crossed_book"

        nan_res = reader.read_slot_with_status(1002, 10, now_ns=now_ns, max_age_ns=1_000_000_000)
        assert nan_res.quote is None
        assert nan_res.status == "invalid_float"

        empty_res = reader.read_slot_with_status(1002, 11, now_ns=now_ns, max_age_ns=1_000_000_000)
        assert empty_res.quote is None
        assert empty_res.status == "empty"

        all_quotes = reader.read_all_exchanges(1002, now_ns=now_ns, max_age_ns=1_000_000_000)
        assert set(all_quotes) == {"hyperliquid", "lighter", "binance_usdm"}


def test_aleph_shm_rejects_identity_mismatch(tmp_path: Path) -> None:
    shm_path = tmp_path / "mismatch-matrix"
    now_ns = 1_795_000_000_000_000_000
    write_synthetic_aleph_shm_v12(
        shm_path,
        [
            SyntheticBboSlot(
                exchange_id=1,
                symbol_id=1002,
                timestamp_ns=now_ns - 1_000_000,
                bid_price=2500.0,
                bid_size=5.0,
                ask_price=2501.0,
                ask_size=5.0,
                seqlock=2,
                slot_exchange_id=6,
            )
        ],
    )
    with AlephShmReader.open(shm_path) as reader:
        res = reader.read_slot_with_status(1002, 1, now_ns=now_ns, max_age_ns=1_000_000_000)
        assert res.quote is None
        assert res.status == "identity_mismatch"


def test_aleph_shm_rejects_bad_magic_and_truncated_file(tmp_path: Path) -> None:
    bad_magic = tmp_path / "bad-magic"
    raw = bytearray(HEADER_BYTES + NUM_EXCHANGES * SLOT_SIZE)
    raw[HEADER_BYTES + 48 : HEADER_BYTES + 64] = b"ALEPHTX_SHM_V07\x00"
    assert len(SHM_MAGIC_V12) == 16
    bad_magic.write_bytes(raw)
    with pytest.raises(ValueError, match="magic"):
        AlephShmReader.open(bad_magic, num_symbols=1)

    truncated = tmp_path / "truncated"
    truncated.write_bytes(b"\x00" * 128)
    with pytest.raises(ValueError, match="truncated"):
        AlephShmReader.open(truncated, num_symbols=1)


def test_probe_remote_aleph_lakehouse_readonly(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(
        argv: list[str], *, shell: bool, check: bool, capture_output: bool, text: bool, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured["shell"] = shell
        captured["check"] = check
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout=(
                "/home/op/aleph-tx-lakehouse/binance_usdm\n"
                "/home/op/aleph-tx-lakehouse/binance_usdm/2026-11-17.parquet\n"
                "/home/op/aleph-tx-lakehouse/binance_usdm/2026-11-18.parquet\n"
                "/home/op/aleph-tx-lakehouse/hyperliquid\n"
                "/home/op/aleph-tx-lakehouse/hyperliquid/2026-11-18.parquet\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = probe_remote_aleph_lakehouse("aleph", timeout_s=5)
    assert res.reachable is True
    assert res.subdirectories == {"binance_usdm": 2, "hyperliquid": 1}
    assert captured["shell"] is False
    assert captured["check"] is False
    argv = captured["argv"]
    assert isinstance(argv, list) and argv[:4] == ["ssh", "-o", "BatchMode=yes", "-o"]

    with pytest.raises(ValueError, match="ssh_host"):
        probe_remote_aleph_lakehouse("aleph;rm -rf /")

    def fake_timeout(
        argv: list[str], *, shell: bool, check: bool, capture_output: bool, text: bool, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    monkeypatch.setattr(subprocess, "run", fake_timeout)
    timeout_res = probe_remote_aleph_lakehouse("aleph", timeout_s=5)
    assert timeout_res.reachable is False
    assert timeout_res.error == "TimeoutExpired"


def test_cex_dex_lead_lag_positive_and_negative_attribution(tmp_path: Path) -> None:
    shm_path = tmp_path / "leadlag-matrix"
    now_ns = 1_795_000_000_000_000_000
    write_synthetic_aleph_shm_v12(
        shm_path,
        [
            SyntheticBboSlot(
                exchange_id=6,
                symbol_id=1002,
                timestamp_ns=now_ns - 5_000_000,
                bid_price=2510.0,
                bid_size=10.0,
                ask_price=2510.5,
                ask_size=10.0,
                seqlock=2,
            )
        ],
    )
    with AlephShmReader.open(shm_path) as reader:
        bbo = reader.read_slot(1002, 6, now_ns=now_ns, max_age_ns=1_000_000_000)
        assert bbo is not None

    # Positive opportunity: Buy 2 ETH on DEX @ $2,500 ($5,000), sell on Binance @ $2,510 ($5,020).
    # Gross = +$20. Costs: DEX 5 bps ($2.50) + CEX 2 bps ($1.004) + slip 1 bps ($0.502)
    # + latency 1 bps ($0.502) + gas $0.05 = ~$4.558 -> net ~$15.442 -> approved!
    pos = evaluate_cex_dex_lead_lag(
        instrument="WETH-USDC",
        direction=LeadLagDirection.DEX_BUY_CEX_SELL,
        qty_base=2 * 10**18,
        base_decimals=18,
        dex_quote_usd_micros=2_500_000_000,
        dex_executable_depth_base=5 * 10**18,
        dex_quote_age_ms=50,
        cex_bbo=bbo,
        dex_pool_fee_bps=5,
        cex_taker_fee_bps=2,
        hedge_slippage_bps=1,
        base_l2_gas_usd_micros=20_000,
        base_l1_gas_usd_micros=30_000,
        latency_drift_bps=1,
    )
    assert pos.approved is True
    assert pos.execution_eligible is False
    assert pos.gross_spread_usd_micros == 20_000_000
    assert pos.worst_case_net_usd_micros > 15_000_000
    assert pos.edge_bps >= 30

    # Negative edge when DEX quote is too close to CEX bid ($2,509 -> gross $2 < ~$4.56 cost).
    neg = evaluate_cex_dex_lead_lag(
        instrument="WETH-USDC",
        direction=LeadLagDirection.DEX_BUY_CEX_SELL,
        qty_base=2 * 10**18,
        base_decimals=18,
        dex_quote_usd_micros=2_509_000_000,
        dex_executable_depth_base=5 * 10**18,
        dex_quote_age_ms=50,
        cex_bbo=bbo,
        dex_pool_fee_bps=5,
        cex_taker_fee_bps=2,
        hedge_slippage_bps=1,
        base_l2_gas_usd_micros=20_000,
        base_l1_gas_usd_micros=30_000,
        latency_drift_bps=1,
    )
    assert neg.approved is False
    assert neg.worst_case_net_usd_micros < 0
    assert "nonpositive_worst_case_net_usd" in neg.rejection_reasons

    # Missing/stale CEX BBO fails closed.
    stale_res = evaluate_cex_dex_lead_lag(
        instrument="WETH-USDC",
        direction=LeadLagDirection.DEX_BUY_CEX_SELL,
        qty_base=2 * 10**18,
        base_decimals=18,
        dex_quote_usd_micros=2_500_000_000,
        dex_executable_depth_base=5 * 10**18,
        dex_quote_age_ms=50,
        cex_bbo=None,
        dex_pool_fee_bps=5,
        cex_taker_fee_bps=2,
        hedge_slippage_bps=1,
        base_l2_gas_usd_micros=20_000,
        base_l1_gas_usd_micros=30_000,
        latency_drift_bps=1,
    )
    assert stale_res.approved is False
    assert "cex_bbo_unavailable_or_stale" in stale_res.rejection_reasons

    # Reverse direction (DEX_SELL_CEX_BUY) & insufficient CEX ask depth fail-closed check.
    sell_pos = evaluate_cex_dex_lead_lag(
        instrument="WETH-USDC",
        direction=LeadLagDirection.DEX_SELL_CEX_BUY,
        qty_base=2 * 10**18,
        base_decimals=18,
        dex_quote_usd_micros=2_525_000_000,
        dex_executable_depth_base=5 * 10**18,
        dex_quote_age_ms=50,
        cex_bbo=bbo,
        dex_pool_fee_bps=5,
        cex_taker_fee_bps=2,
        hedge_slippage_bps=1,
        base_l2_gas_usd_micros=20_000,
        base_l1_gas_usd_micros=30_000,
        latency_drift_bps=1,
    )
    assert sell_pos.approved is True
    assert sell_pos.gross_spread_usd_micros == 29_000_000

    shallow = evaluate_cex_dex_lead_lag(
        instrument="WETH-USDC",
        direction=LeadLagDirection.DEX_SELL_CEX_BUY,
        qty_base=20 * 10**18,  # exceeds ask_size=10
        base_decimals=18,
        dex_quote_usd_micros=2_525_000_000,
        dex_executable_depth_base=25 * 10**18,
        dex_quote_age_ms=50,
        cex_bbo=bbo,
        dex_pool_fee_bps=5,
        cex_taker_fee_bps=2,
        hedge_slippage_bps=1,
        base_l2_gas_usd_micros=20_000,
        base_l1_gas_usd_micros=30_000,
        latency_drift_bps=1,
    )
    assert shallow.approved is False
    assert "insufficient_cex_ask_depth" in shallow.rejection_reasons


def test_funding_basis_session_calendar_and_liquidation_guards() -> None:
    ok = evaluate_funding_basis(
        instrument="NVDA-PERP-RWA",
        proceeds_bid_s_usd_micros=10_020_000_000,
        cost_ask_b_usd_micros=10_000_000_000,
        expected_funding_usd_micros=5_000_000,
        fees_usd_micros=4_000_000,
        borrow_usd_micros=1_000_000,
        gas_usd_micros=500_000,
        hedge_slippage_usd_micros=1_000_000,
        gap_reserve_usd_micros=2_000_000,
        tail_charge_usd_micros=1_500_000,
        session_status=SessionStatus.REGULAR,
        liquidation_distance_bps=2500,
        min_liquidation_distance_bps=1500,
    )
    assert ok.approved is True
    assert ok.execution_eligible is False
    assert ok.net_edge_usd_micros == 15_000_000
    assert ok.edge_bps == 14

    for blocked_session in (SessionStatus.HALTED, SessionStatus.CLOSED, SessionStatus.AUCTION):
        rejected = evaluate_funding_basis(
            instrument="NVDA-PERP-RWA",
            proceeds_bid_s_usd_micros=10_020_000_000,
            cost_ask_b_usd_micros=10_000_000_000,
            expected_funding_usd_micros=5_000_000,
            fees_usd_micros=4_000_000,
            borrow_usd_micros=1_000_000,
            gas_usd_micros=500_000,
            hedge_slippage_usd_micros=1_000_000,
            gap_reserve_usd_micros=2_000_000,
            tail_charge_usd_micros=1_500_000,
            session_status=blocked_session,
            liquidation_distance_bps=2500,
            min_liquidation_distance_bps=1500,
        )
        assert rejected.approved is False
        assert f"session_not_regular:{blocked_session.value}" in rejected.rejection_reasons

    low_liq = evaluate_funding_basis(
        instrument="NVDA-PERP-RWA",
        proceeds_bid_s_usd_micros=10_020_000_000,
        cost_ask_b_usd_micros=10_000_000_000,
        expected_funding_usd_micros=5_000_000,
        fees_usd_micros=4_000_000,
        borrow_usd_micros=1_000_000,
        gas_usd_micros=500_000,
        hedge_slippage_usd_micros=1_000_000,
        gap_reserve_usd_micros=2_000_000,
        tail_charge_usd_micros=1_500_000,
        session_status=SessionStatus.REGULAR,
        liquidation_distance_bps=1200,
        min_liquidation_distance_bps=1500,
    )
    assert low_liq.approved is False
    assert "liquidation_distance_below_minimum" in low_liq.rejection_reasons
