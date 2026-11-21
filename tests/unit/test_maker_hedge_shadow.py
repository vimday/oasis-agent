"""Unit tests for 9-pair Coinbase IPv4/level-1 fallback and passive maker-hedge shadow."""

import json
import socket
import ssl
import urllib.request
from decimal import Decimal
from typing import Any

import pytest

from oasis_agent.cross_venue.coinbase_trade_flow import CoinbaseTradePrint
from oasis_agent.cross_venue.live_cex_dex_shadow import (
    COINBASE_BOOK_LEVEL1_URL_TEMPLATE,
    MAX_RESPONSE_BYTES,
    SUPPORTED_PRODUCTS,
    CoinbaseL2Book,
    CoinbaseL2Level,
    LiveCexDexError,
    _IPv4PreferredHTTPSConnection,
    _IPv4PreferredHTTPSHandler,
    _validate_coinbase_url,
    build_synthetic_coinbase_l2_book,
    build_synthetic_coinbase_transport,
    evaluate_live_cex_dex_shadow,
    fetch_coinbase_l2_book,
    resolve_coinbase_product,
    serialize_coinbase_book_payload,
)
from oasis_agent.cross_venue.maker_hedge_shadow import (
    MakerFillEvent,
    MakerHedgeConfig,
    MakerHedgeError,
    MakerHedgeLifecycleTracker,
    MakerQuoteCandidate,
    MakerStepResult,
    UnhedgedMakerLot,
    build_maker_quote_candidate,
)
from scripts.run_multicall_100tick_campaign import _select_dynamic_maker_quotes


def _make_book(
    product_id: str,
    *,
    bid: str,
    ask: str,
    size: str = "10.0",
    sequence: int = 100,
) -> CoinbaseL2Book:
    return CoinbaseL2Book(
        product_id=product_id,
        bids=(CoinbaseL2Level(price=Decimal(bid), size=Decimal(size), num_orders=2),),
        asks=(CoinbaseL2Level(price=Decimal(ask), size=Decimal(size), num_orders=2),),
        sequence=sequence,
        retrieved_at_utc="2026-11-18T10:00:00+00:00",
        raw_sha256="22" * 32,
    )


def test_all_nine_coinbase_products_and_aliases_resolved() -> None:
    expected = (
        ("ETH-USD", ("ETH-USD", "ETH", "WETH"), "WETH", 18),
        ("BTC-USD", ("BTC-USD", "BTC", "cbBTC"), "cbBTC", 8),
        ("AERO-USD", ("AERO-USD", "AERO"), "AERO", 18),
        ("VIRTUAL-USD", ("VIRTUAL-USD", "virtual"), "VIRTUAL", 18),
        ("VVV-USD", ("VVV-USD", "vvv"), "VVV", 18),
        ("MORPHO-USD", ("MORPHO-USD", "morpho"), "MORPHO", 18),
        ("DEGEN-USD", ("DEGEN-USD", "degen"), "DEGEN", 18),
        ("WELL-USD", ("WELL-USD", "well"), "WELL", 18),
        ("CBETH-USD", ("CBETH-USD", "cbETH"), "cbETH", 18),
    )
    assert tuple(row[0] for row in expected) == SUPPORTED_PRODUCTS
    assert len(SUPPORTED_PRODUCTS) == 9

    transport = build_synthetic_coinbase_transport()
    for product_id, aliases, token_symbol, decimals in expected:
        for alias in aliases:
            spec = resolve_coinbase_product(alias)
            assert spec.product_id == product_id
            assert spec.token_symbol == token_symbol
            assert spec.token_decimals == decimals
        book_l2 = fetch_coinbase_l2_book(product_id, level=2, transport=transport)
        book_l1 = fetch_coinbase_l2_book(product_id, level=1, transport=transport)
        assert book_l2.product_id == product_id
        assert book_l2.book_level == 2
        assert len(book_l2.bids) == 5
        assert book_l1.product_id == product_id
        assert book_l1.book_level == 1
        assert len(book_l1.bids) == 1
        assert len(book_l1.asks) == 1

    degen_book = build_synthetic_coinbase_l2_book("DEGEN-USD", mid_price=Decimal("0.0050"))
    assert degen_book.best_bid > Decimal(0)
    assert degen_book.best_bid < degen_book.best_ask


def test_btc_level2_byte_limit_fallback_to_level1_and_url_validation() -> None:
    urls_called: list[str] = []
    btc_book = _make_book("BTC-USD", bid="90050.00", ask="90060.00", size="1.5")
    btc_l1_bytes = serialize_coinbase_book_payload(btc_book, level=1)

    def _btc_byte_limit_transport(url: str, _timeout_s: float) -> bytes:
        urls_called.append(url)
        if "level=2" in url:
            raise LiveCexDexError("Coinbase response exceeds byte limit")
        return btc_l1_bytes

    # 0.0056 BTC bought for 500 USDC (~$89,285.71/BTC) vs $90,050 bid => positive edge
    res = evaluate_live_cex_dex_shadow(
        product_or_symbol="BTC-USD",
        trade_notional_usdc=500_000_000,
        dex_buy_token_for_usdc_out=560_000,
        trade_notional_token=560_000,
        dex_sell_token_for_usdc_out=495_000_000,
        gas_cost_usd=Decimal("0.05"),
        transport=_btc_byte_limit_transport,
    )
    assert res.status == "ok"
    assert res.book_level == 1
    assert res.execution_eligible is False
    assert res.best_direction == "buy_dex_sell_cex"
    assert res.best_net_bps is not None and res.best_net_bps > 0
    assert res.to_dict()["book_level"] == 1
    assert urls_called == [
        COINBASE_BOOK_LEVEL1_URL_TEMPLATE.format(product="BTC-USD", level=2),
        COINBASE_BOOK_LEVEL1_URL_TEMPLATE.format(product="BTC-USD", level=1),
    ]

    # MAX_RESPONSE_BYTES payload oversize from parser also triggers single level=1 fallback
    oversize_calls: list[str] = []

    def _oversize_bytes_transport(url: str, _timeout_s: float) -> bytes:
        oversize_calls.append(url)
        if "level=2" in url:
            return b"{" + b" " * MAX_RESPONSE_BYTES + b"}"
        return btc_l1_bytes

    res_oversize = evaluate_live_cex_dex_shadow(
        product_or_symbol="BTC-USD",
        trade_notional_usdc=500_000_000,
        dex_buy_token_for_usdc_out=560_000,
        trade_notional_token=560_000,
        dex_sell_token_for_usdc_out=495_000_000,
        gas_cost_usd=Decimal("0.05"),
        transport=_oversize_bytes_transport,
    )
    assert res_oversize.status == "ok"
    assert res_oversize.book_level == 1
    assert len(oversize_calls) == 2

    # HTTP 429 must fail immediately without falling back to level=1
    rate_limit_calls: list[str] = []

    def _429_transport(url: str, _timeout_s: float) -> bytes:
        rate_limit_calls.append(url)
        raise LiveCexDexError("Coinbase HTTP failure: status 429")

    deg_429 = evaluate_live_cex_dex_shadow(
        product_or_symbol="BTC-USD",
        trade_notional_usdc=500_000_000,
        dex_buy_token_for_usdc_out=560_000,
        trade_notional_token=560_000,
        dex_sell_token_for_usdc_out=495_000_000,
        gas_cost_usd=Decimal("0.05"),
        transport=_429_transport,
    )
    assert deg_429.status == "degraded"
    assert len(rate_limit_calls) == 1

    # Strict URL validation for level=1 and level=2 only
    assert (
        _validate_coinbase_url("https://api.exchange.coinbase.com/products/MORPHO-USD/book?level=1")
        == "https://api.exchange.coinbase.com/products/MORPHO-USD/book?level=1"
    )
    for bad_url in (
        "https://api.exchange.coinbase.com/products/ETH-USD/book?level=3",
        "https://api.exchange.coinbase.com/products/ETH-USD/book?level=2&extra=1",
        "https://api.exchange.coinbase.com/products/UNKNOWN-USD/book?level=2",
        "https://api.exchange.coinbase.com/orders",
        "https://api.exchange.coinbase.com/products/ETH-USD/book?level=1#frag",
    ):
        with pytest.raises(LiveCexDexError):
            _validate_coinbase_url(bad_url)


def test_ipv4_preferred_https_connection_sorts_af_inet_and_recovers_from_ipv6_ssleoferror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class _FakeSocket:
        def __init__(self, af: int, socktype: int, proto: int) -> None:
            self.af = af
            self.socktype = socktype
            self.proto = proto
            self.timeout: float | None = None
            self.closed = False

        def settimeout(self, timeout: float | None) -> None:
            self.timeout = timeout

        def connect(self, sockaddr: tuple[Any, ...]) -> None:
            events.append(f"connect:{self.af}:{sockaddr[0]}")

        def setsockopt(self, _level: int, _optname: int, _value: int) -> None:
            return None

        def close(self) -> None:
            self.closed = True
            events.append(f"close:{self.af}")

    class _FakeTLSContext:
        def __init__(self, *, fail_ipv6_eof: bool = False) -> None:
            self.fail_ipv6_eof = fail_ipv6_eof

        def wrap_socket(self, sock: _FakeSocket, *, server_hostname: str) -> _FakeSocket:
            assert server_hostname == "api.exchange.coinbase.com"
            events.append(f"wrap:{sock.af}")
            if self.fail_ipv6_eof and sock.af == socket.AF_INET6:
                raise ssl.SSLEOFError(8, "EOF occurred in violation of protocol")
            return sock

    # Case 1: DNS returns AF_INET6 before AF_INET -> connection sorts AF_INET first
    monkeypatch.setattr(
        "oasis_agent.cross_venue.live_cex_dex_shadow.socket.getaddrinfo",
        lambda *_a, **_k: [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1", 443, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.18.10.20", 443)),
        ],
    )
    monkeypatch.setattr(
        "oasis_agent.cross_venue.live_cex_dex_shadow.socket.socket",
        _FakeSocket,
    )
    conn = _IPv4PreferredHTTPSConnection(
        "api.exchange.coinbase.com",
        443,
        timeout=3.0,
        context=_FakeTLSContext(fail_ipv6_eof=True),  # type: ignore[arg-type]
    )
    conn.connect()
    assert events == [
        f"connect:{socket.AF_INET}:104.18.10.20",
        f"wrap:{socket.AF_INET}",
    ]
    assert isinstance(conn.sock, _FakeSocket)
    assert conn.sock.timeout == 3.0

    # Case 2: Initial AF_UNSPEC returns only AF_INET6 which fails with SSLEOFError ->
    # connection closes failed IPv6 socket and falls back to AF_INET query
    events.clear()

    def _ipv6_then_ipv4_getaddrinfo(
        _host: str,
        _port: int,
        family: int = 0,
        type: int = 0,
    ) -> list[Any]:
        del type
        if family == socket.AF_INET:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.18.10.21", 443))]
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::2", 443, 0, 0))]

    monkeypatch.setattr(
        "oasis_agent.cross_venue.live_cex_dex_shadow.socket.getaddrinfo",
        _ipv6_then_ipv4_getaddrinfo,
    )
    conn2 = _IPv4PreferredHTTPSConnection(
        "api.exchange.coinbase.com",
        443,
        timeout=2.5,
        context=_FakeTLSContext(fail_ipv6_eof=True),  # type: ignore[arg-type]
    )
    conn2.connect()
    assert events == [
        f"connect:{socket.AF_INET6}:2606:4700::2",
        f"wrap:{socket.AF_INET6}",
        f"close:{socket.AF_INET6}",
        f"connect:{socket.AF_INET}:104.18.10.21",
        f"wrap:{socket.AF_INET}",
    ]
    assert isinstance(conn2.sock, _FakeSocket)
    assert conn2.sock.af == socket.AF_INET

    # Case 3: Handler delegates do_open(_IPv4PreferredHTTPSConnection, req, context=...)
    handler = _IPv4PreferredHTTPSHandler(context=_FakeTLSContext())  # type: ignore[arg-type]
    assert isinstance(handler, urllib.request.HTTPSHandler)
    do_open_args: dict[str, Any] = {}

    def _spy_do_open(http_class: Any, req: Any, **kwargs: Any) -> str:
        do_open_args["http_class"] = http_class
        do_open_args["req"] = req
        do_open_args["kwargs"] = kwargs
        return "ok"

    monkeypatch.setattr(handler, "do_open", _spy_do_open)
    req = urllib.request.Request("https://api.exchange.coinbase.com/products/ETH-USD/book?level=2")
    assert handler.https_open(req) == "ok"  # type: ignore[comparison-overlap]
    assert do_open_args["http_class"] is _IPv4PreferredHTTPSConnection
    assert do_open_args["kwargs"]["context"] is handler._context

    # Case 4: Private IP returned on AF_INET fallback is still rejected fail-closed
    def _private_ipv4_fallback(
        _host: str,
        _port: int,
        family: int = 0,
        type: int = 0,
    ) -> list[Any]:
        del type
        if family == socket.AF_INET:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::3", 443, 0, 0))]

    monkeypatch.setattr(
        "oasis_agent.cross_venue.live_cex_dex_shadow.socket.getaddrinfo",
        _private_ipv4_fallback,
    )
    conn3 = _IPv4PreferredHTTPSConnection(
        "api.exchange.coinbase.com",
        443,
        timeout=2.0,
        context=_FakeTLSContext(fail_ipv6_eof=True),  # type: ignore[arg-type]
    )
    with pytest.raises(LiveCexDexError, match="non-public"):
        conn3.connect()


def test_maker_hedge_lifecycle_wide_spread_non_cross_touch_lift_and_strict_cross() -> None:
    cfg = MakerHedgeConfig(
        trade_notional_usdc=200_000_000,
        cex_maker_fee_bps=Decimal(0),
        adverse_selection_bps=Decimal("1.5"),
        min_quote_edge_bps=Decimal("3.0"),
        tick_improvement_bps=Decimal("0.5"),
        require_strict_cross_for_fill=True,
    )
    tracker = MakerHedgeLifecycleTracker(cfg)
    gas_usd = Decimal("0.04")  # 2.0 bps on $200 notional

    # Tick 1: Wide CEX spread (2990 / 3010), DEX buy = $3000, DEX sell = $2997
    # 200 USDC buys 1/15 ETH (at $3000); 1/15 ETH sells on DEX for 199.80 USDC (at $2997)
    qty_token = (10**18) // 15
    book_t1 = _make_book("ETH-USD", bid="2990.00", ask="3010.00", sequence=101)
    step1 = tracker.step(
        "ETH-USD",
        current_book=book_t1,
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_token,
        trade_notional_token=qty_token,
        dex_sell_usdc_out=199_800_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert isinstance(step1, MakerStepResult)
    assert step1.execution_eligible is False
    assert step1.fills == ()
    assert step1.quote.ask_active is True
    assert step1.quote.bid_active is True
    assert step1.quote.quoted_ask_usd is not None
    assert step1.quote.quoted_bid_usd is not None
    assert book_t1.best_bid < step1.quote.quoted_ask_usd < book_t1.best_ask
    assert book_t1.best_bid < step1.quote.quoted_bid_usd < book_t1.best_ask
    assert step1.quote.ask_edge_bps is not None and step1.quote.ask_edge_bps > Decimal(25)
    assert step1.quote.bid_edge_bps is not None and step1.quote.bid_edge_bps > Decimal(15)
    # Breakeven price produces zero edge
    assert step1.quote.ask_dex_breakeven_usd is not None
    assert step1.quote.bid_dex_breakeven_usd is not None
    assert step1.quote.ask_dex_breakeven_usd > Decimal(3000)
    assert step1.quote.bid_dex_breakeven_usd < Decimal(2997)
    # Exact breakeven algebra: quoting at ask_dex_breakeven_usd yields 0 bps edge
    dex_buy_unit = Decimal(200) / (Decimal(qty_token) / Decimal(10**18))
    ask_gas_bps = (gas_usd / Decimal(200)) * Decimal(10_000)
    breakeven_ask_edge = (
        ((step1.quote.ask_dex_breakeven_usd - dex_buy_unit) / dex_buy_unit) * Decimal(10_000)
        - cfg.cex_maker_fee_bps
        - cfg.adverse_selection_bps
        - ask_gas_bps
    )
    assert breakeven_ask_edge == pytest.approx(Decimal(0), abs=Decimal("1e-12"))
    assert tracker.resting_quotes()["ETH-USD"]["ask_price_usd"] == str(step1.quote.quoted_ask_usd)
    assert step1.quote.to_dict()["execution_eligible"] is False

    # Tick 2: Unchanged / narrowed non-crossed book (2991 / 3009) -> 0 fills!
    book_t2 = _make_book("ETH-USD", bid="2991.00", ask="3009.00", sequence=102)
    step2 = tracker.step(
        "ETH-USD",
        current_book=book_t2,
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_token,
        trade_notional_token=qty_token,
        dex_sell_usdc_out=199_800_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert step2.fills == ()
    assert step2.strict_fills == ()
    assert step2.touch_lift_fills == ()
    resting_t2_ask = step2.quote.quoted_ask_usd
    assert resting_t2_ask is not None

    # Tick 3: Strictly crossed next tick! CEX best_bid jumps to 3010.00 >= resting_t2_ask (~3008.85)
    # Meanwhile DEX buy price drifts adversely UP from $3000 -> $3005 (200 USDC buys fewer tokens)
    qty_token_t3 = (200 * 10**18) // 3005
    book_t3 = _make_book("ETH-USD", bid="3010.00", ask="3012.00", sequence=103)
    step3 = tracker.step(
        "ETH-USD",
        current_book=book_t3,
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_token_t3,
        trade_notional_token=qty_token,
        dex_sell_usdc_out=200_200_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert len(step3.strict_fills) == 1
    fill = step3.strict_fills[0]
    assert isinstance(fill, MakerFillEvent)
    assert fill.side == "ask_maker"
    assert fill.fill_classification == "strict_cross"
    assert fill.quoted_price_usd == resting_t2_ask
    assert fill.next_book_price_usd == Decimal("3010.00")
    assert fill.hedge_dex_price_usd == pytest.approx(Decimal(3005), rel=Decimal("1e-4"))
    assert fill.latency_drift_bps > Decimal(16)  # +16.67 bps adverse drift caught!
    assert Decimal(8) < fill.realized_net_bps < Decimal(12)
    assert fill.realized_net_profit_usd > Decimal(0)
    assert fill.execution_eligible is False

    # Tick 4: Diagnostic touch_lift (ask lifts above resting ask without bid crossing)
    resting_t3_ask = step3.quote.quoted_ask_usd
    assert resting_t3_ask is not None
    book_t4 = _make_book("ETH-USD", bid="3009.00", ask="3014.00", sequence=104)
    step4 = tracker.step(
        "ETH-USD",
        current_book=book_t4,
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_token_t3,
        trade_notional_token=qty_token,
        dex_sell_usdc_out=200_200_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert step4.strict_fills == ()
    assert len(step4.touch_lift_fills) == 1
    assert step4.touch_lift_fills[0].fill_classification == "touch_lift"

    summary = tracker.summary()
    assert summary["total_steps"] == 4
    assert summary["active_quote_legs"] >= 6
    assert summary["strict_cross_fills"] == 1
    assert summary["touch_lift_fills"] == 1
    assert summary["strict_positive_fills"] == 1
    assert summary["strict_mean_realized_net_bps"] == pytest.approx(
        float(fill.realized_net_bps), abs=1e-4
    )
    assert summary["strict_total_net_profit_usd"] == pytest.approx(
        float(fill.realized_net_profit_usd), abs=1e-4
    )
    assert summary["best_quoted_edge_bps"] is not None and summary["best_quoted_edge_bps"] > 25.0
    assert summary["trade_flow_fifo_fills"] == 0
    assert summary["fifo_positive_fills"] == 0
    assert summary["fifo_mean_realized_net_bps"] is None
    assert summary["fifo_total_net_profit_usd"] == 0.0
    assert summary["fifo_total_filled_notional_usd"] == 0.0
    assert summary["feed_gap_cancels"] == 0
    assert summary["batched_fifo_hedges"] == 0
    assert summary["natural_netted_fills"] == 0
    assert summary["open_unhedged_lots"] == 0
    assert summary["unhedged_exposure_usd_micros"] == 0
    assert summary["adaptive_volatility_enabled"] is False
    assert summary["execution_eligible"] is False
    assert step1.quote.effective_adverse_bps == Decimal("1.5")
    assert step1.quote.to_dict()["effective_adverse_bps"] == "1.5"
    assert fill.to_dict()["matched_trade_ids"] == []
    assert fill.to_dict()["fill_ratio"] == 1.0
    assert fill.to_dict()["filled_qty_tokens"] is None
    assert fill.to_dict()["hedge_mode"] == "immediate_dex"


def test_maker_hedge_adverse_drift_loss_bid_maker_and_fail_closed_guards() -> None:
    cfg = MakerHedgeConfig(
        trade_notional_usdc=200_000_000,
        cex_maker_fee_bps=Decimal(0),
        adverse_selection_bps=Decimal("1.5"),
        min_quote_edge_bps=Decimal("3.0"),
        tick_improvement_bps=Decimal("0.5"),
    )
    tracker = MakerHedgeLifecycleTracker(cfg)
    # 8-decimal cbBTC book with tight spread: Post-Only ask capped at best_ask, inactive on low edge
    tight_btc = _make_book("BTC-USD", bid="90000.00", ask="90001.00")
    qty_btc = 222_222  # ~0.00222222 BTC (~$200 at $90,000)
    cand = build_maker_quote_candidate(
        "BTC-USD",
        current_book=tight_btc,
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_btc,
        trade_notional_token=qty_btc,
        dex_sell_usdc_out=200_000_000,
        token_decimals=8,
        gas_cost_usd=Decimal("0.04"),
        config=cfg,
    )
    assert cand.quoted_ask_usd == Decimal("90001.00")
    assert cand.quoted_bid_usd == Decimal("90000.00")
    assert cand.ask_active is False
    assert cand.bid_active is False

    # Bid-maker strict cross with severe adverse DEX drop (realized loss!)
    wide_btc = _make_book("BTC-USD", bid="89500.00", ask="90500.00")
    step_open = tracker.step(
        "BTC-USD",
        current_book=wide_btc,
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_btc,
        trade_notional_token=qty_btc,
        dex_sell_usdc_out=200_500_000,  # DEX sell at ~$90,225
        token_decimals=8,
        gas_cost_usd=Decimal("0.04"),
    )
    assert step_open.quote.bid_active is True
    assert step_open.quote.quoted_bid_usd is not None
    assert step_open.quote.quoted_bid_usd < wide_btc.best_ask

    # Next tick drops sharply: CEX ask <= resting bid AND DEX sell crashes to $195 USDC
    crash_btc = _make_book("BTC-USD", bid="88800.00", ask="89400.00")
    step_crash = tracker.step(
        "BTC-USD",
        current_book=crash_btc,
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_btc,
        trade_notional_token=qty_btc,
        dex_sell_usdc_out=195_000_000,
        token_decimals=8,
        gas_cost_usd=Decimal("0.04"),
    )
    bid_fills = [f for f in step_crash.strict_fills if f.side == "bid_maker"]
    assert len(bid_fills) == 1
    assert bid_fills[0].latency_drift_bps > Decimal(250)
    assert bid_fills[0].realized_net_profit_usd < Decimal(0)
    assert bid_fills[0].realized_net_bps < Decimal(0)
    assert tracker.summary()["strict_positive_fills"] == 0
    assert tracker.summary()["strict_total_net_profit_usd"] < 0.0
    assert len(tracker.strict_fills()) == 1
    assert len(tracker.all_fills()) == 1
    assert tracker.strict_fills()[0].to_dict()["execution_eligible"] is False

    # Unavailable DEX hedge on strict cross produces explicit unhedged warning without crash
    unhedged_tracker = MakerHedgeLifecycleTracker(cfg)
    open_u = unhedged_tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2990.00", ask="3010.00"),
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=(10**18) // 15,
        trade_notional_token=(10**18) // 15,
        dex_sell_usdc_out=199_800_000,
        token_decimals=18,
        gas_cost_usd=Decimal("0.04"),
    )
    assert open_u.quote.ask_active is True
    cross_no_dex = unhedged_tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="3012.00", ask="3015.00"),
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=None,
        trade_notional_token=(10**18) // 15,
        dex_sell_usdc_out=None,
        token_decimals=18,
        gas_cost_usd=Decimal("0.04"),
    )
    assert cross_no_dex.strict_fills == ()
    assert len(cross_no_dex.unhedged_fill_reasons) == 1
    assert "dex_buy_quote_unavailable" in cross_no_dex.unhedged_fill_reasons[0]
    assert unhedged_tracker.summary()["unhedged_fill_warnings"] == 1

    # Fail-closed validation checks
    with pytest.raises(MakerHedgeError, match="execution_eligible"):
        MakerHedgeConfig(execution_eligible=True)  # type: ignore[arg-type]
    with pytest.raises(MakerHedgeError, match="not float"):
        MakerHedgeConfig(adverse_selection_bps=1.5)  # type: ignore[arg-type]
    with pytest.raises(MakerHedgeError, match="execution_eligible"):
        MakerQuoteCandidate(
            product_id="ETH-USD",
            token_symbol="WETH",
            qty_tokens_dec=Decimal(1),
            quoted_ask_usd=None,
            quoted_bid_usd=None,
            ask_dex_breakeven_usd=None,
            bid_dex_breakeven_usd=None,
            ask_edge_bps=None,
            bid_edge_bps=None,
            ask_active=False,
            bid_active=False,
            spread_capture_bps=Decimal(1),
            gas_cost_usd=Decimal(0),
            execution_eligible=True,  # type: ignore[arg-type]
        )
    with pytest.raises(MakerHedgeError, match="does not match"):
        tracker.step(
            "AERO-USD",
            current_book=crash_btc,
            trade_notional_usdc=200_000_000,
            dex_buy_token_out=10**18,
            trade_notional_token=10**18,
            dex_sell_usdc_out=200_000_000,
            token_decimals=18,
            gas_cost_usd=Decimal("0.04"),
        )
    assert json.dumps(tracker.summary())


def test_adaptive_volatility_effective_adverse_bps_and_quote_gating() -> None:
    qty_token = 10**17  # 0.1 ETH -> $300 at $3000/ETH
    notional_usdc = 300_000_000
    gas_usd = Decimal(0)

    # Legacy mode keeps effective_adverse_bps fixed even on noisy +/-10 bps moves
    legacy_cfg = MakerHedgeConfig(
        trade_notional_usdc=notional_usdc,
        adverse_selection_bps=Decimal("1.5"),
        min_quote_edge_bps=Decimal("5.0"),
        tick_improvement_bps=Decimal(0),
        adaptive_volatility_enabled=False,
    )
    legacy_tracker = MakerHedgeLifecycleTracker(legacy_cfg)

    # Adaptive mode widens effective_adverse_bps on noisy books and gates thin margins
    adapt_cfg = MakerHedgeConfig(
        trade_notional_usdc=notional_usdc,
        adverse_selection_bps=Decimal("1.5"),
        min_quote_edge_bps=Decimal("5.0"),
        tick_improvement_bps=Decimal(0),
        adaptive_volatility_enabled=True,
        volatility_multiplier=Decimal("1.5"),
        volatility_window_steps=4,
        max_adaptive_adverse_bps=Decimal("30.0"),
    )
    adapt_tracker = MakerHedgeLifecycleTracker(adapt_cfg)

    # Tick 1 (single observation): both default to adverse_selection_bps=1.5
    # CEX 2997 / 3003 vs DEX buy $3000 (ask gross +10 bps, net +8.5 bps >= 5 -> active)
    b1 = _make_book("ETH-USD", bid="2997.00", ask="3003.00", sequence=1)
    s1_leg = legacy_tracker.step(
        "ETH-USD",
        current_book=b1,
        trade_notional_usdc=notional_usdc,
        dex_buy_token_out=qty_token,
        trade_notional_token=qty_token,
        dex_sell_usdc_out=notional_usdc,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    s1_adp = adapt_tracker.step(
        "ETH-USD",
        current_book=b1,
        trade_notional_usdc=notional_usdc,
        dex_buy_token_out=qty_token,
        trade_notional_token=qty_token,
        dex_sell_usdc_out=notional_usdc,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert s1_leg.quote.effective_adverse_bps == Decimal("1.5")
    assert s1_adp.quote.effective_adverse_bps == Decimal("1.5")
    assert s1_adp.quote.ask_active is True
    assert s1_adp.quote.to_dict()["effective_adverse_bps"] == "1.5"

    # Tick 2 (flat book, same mid=3000.00): rolling vol == 0 -> stays at 1.5
    b2_flat = _make_book("ETH-USD", bid="2997.00", ask="3003.00", sequence=2)
    s2_adp = adapt_tracker.step(
        "ETH-USD",
        current_book=b2_flat,
        trade_notional_usdc=notional_usdc,
        dex_buy_token_out=qty_token,
        trade_notional_token=qty_token,
        dex_sell_usdc_out=notional_usdc,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert s2_adp.quote.effective_adverse_bps == Decimal("1.5")
    assert s2_adp.quote.ask_active is True

    # Ticks 3-6: +/- 20 bps oscillations around $3000 while spread stays ~10 bps each side
    # Window of 4 fills with [0, ~20, ~40, ~40] -> rolling mean > 20 -> capped at 30.0 bps!
    osc_books = (
        ("3003.00", "3009.00", 3_006_000_000),  # mid 3006 (+20 bps)
        ("2991.00", "2997.00", 2_994_000_000),  # mid 2994 (-40 bps)
        ("3003.00", "3009.00", 3_006_000_000),  # mid 3006 (+40 bps)
        ("2991.00", "2997.00", 2_994_000_000),  # mid 2994 (-40 bps)
    )
    last_leg: MakerStepResult | None = None
    last_adp: MakerStepResult | None = None
    for seq_idx, (bid_px, ask_px, dex_ref_micros) in enumerate(osc_books, start=3):
        bk = _make_book("ETH-USD", bid=bid_px, ask=ask_px, sequence=seq_idx)
        # DEX unit price sits at mid (10 bps edge to best_ask/best_bid)
        qty_at_mid = (notional_usdc * 10**18) // dex_ref_micros
        last_leg = legacy_tracker.step(
            "ETH-USD",
            current_book=bk,
            trade_notional_usdc=notional_usdc,
            dex_buy_token_out=qty_at_mid,
            trade_notional_token=qty_at_mid,
            dex_sell_usdc_out=notional_usdc,
            token_decimals=18,
            gas_cost_usd=gas_usd,
        )
        last_adp = adapt_tracker.step(
            "ETH-USD",
            current_book=bk,
            trade_notional_usdc=notional_usdc,
            dex_buy_token_out=qty_at_mid,
            trade_notional_token=qty_at_mid,
            dex_sell_usdc_out=notional_usdc,
            token_decimals=18,
            gas_cost_usd=gas_usd,
        )

    assert last_leg is not None and last_adp is not None
    assert last_leg.quote.effective_adverse_bps == Decimal("1.5")
    assert last_leg.quote.ask_active is True
    # Adaptive cap reached: 30.0 bps > 10 bps gross spread => thin quote gated OFF!
    assert last_adp.quote.effective_adverse_bps == Decimal("30.0")
    assert last_adp.quote.to_dict()["effective_adverse_bps"] == "30.0"
    assert last_adp.quote.ask_active is False
    assert last_adp.quote.bid_active is False
    assert last_adp.quote.ask_edge_bps is not None and last_adp.quote.ask_edge_bps < Decimal(0)
    assert last_adp.quote.ask_dex_breakeven_usd is not None
    assert last_leg.quote.ask_dex_breakeven_usd is not None
    assert last_adp.quote.ask_dex_breakeven_usd > last_leg.quote.ask_dex_breakeven_usd

    # Verify fill adverse cost also reflects effective_adverse_bps on a volatile fill
    vol_fill_tracker = MakerHedgeLifecycleTracker(
        MakerHedgeConfig(
            trade_notional_usdc=notional_usdc,
            adverse_selection_bps=Decimal("1.5"),
            min_quote_edge_bps=Decimal("1.0"),
            tick_improvement_bps=Decimal(0),
            adaptive_volatility_enabled=True,
            volatility_multiplier=Decimal("1.5"),
            volatility_window_steps=2,
            max_adaptive_adverse_bps=Decimal("30.0"),
        )
    )
    vol_fill_tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2990.00", ask="3010.00", sequence=10),
        trade_notional_usdc=notional_usdc,
        dex_buy_token_out=qty_token,
        trade_notional_token=qty_token,
        dex_sell_usdc_out=notional_usdc,
        token_decimals=18,
        gas_cost_usd=Decimal(0),
    )
    # Next tick jumps +20 bps (3000 -> 3006) AND crosses ask at 3010
    crossed_vol = vol_fill_tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="3010.00", ask="3012.00", sequence=11),
        trade_notional_usdc=notional_usdc,
        dex_buy_token_out=qty_token,
        trade_notional_token=qty_token,
        dex_sell_usdc_out=notional_usdc,
        token_decimals=18,
        gas_cost_usd=Decimal(0),
    )
    assert len(crossed_vol.strict_fills) == 1
    # Mid moved from 3000 -> 3011 (~36.67 bps) -> capped at 30.0 bps adverse cost!
    # Proceeds 301.00 - DEX 300.00 - 30bps adv (0.90) = $0.10 net profit!
    assert crossed_vol.quote.effective_adverse_bps == Decimal("30.0")
    assert crossed_vol.strict_fills[0].realized_net_profit_usd == pytest.approx(
        Decimal("0.10"), abs=Decimal("1e-9")
    )

    # Subsequent flat window decays effective_adverse_bps back to adverse_selection_bps=1.5
    for seq_idx in range(7, 12):
        decay_step = adapt_tracker.step(
            "ETH-USD",
            current_book=_make_book("ETH-USD", bid="2997.00", ask="3003.00", sequence=seq_idx),
            trade_notional_usdc=notional_usdc,
            dex_buy_token_out=qty_token,
            trade_notional_token=qty_token,
            dex_sell_usdc_out=notional_usdc,
            token_decimals=18,
            gas_cost_usd=gas_usd,
        )
    assert decay_step.quote.effective_adverse_bps == Decimal("1.5")
    assert decay_step.quote.ask_active is True
    assert adapt_tracker.summary()["adaptive_volatility_enabled"] is True
    assert legacy_tracker.summary()["adaptive_volatility_enabled"] is False


def test_batch_hedge_resting_strict_cross_drawdown_and_config_validation() -> None:
    # Config and UnhedgedMakerLot fail-closed validation
    with pytest.raises(MakerHedgeError, match="adaptive_volatility_enabled"):
        MakerHedgeConfig(adaptive_volatility_enabled=1)  # type: ignore[arg-type]
    with pytest.raises(MakerHedgeError, match="max_acceptable_gas_drag_bps"):
        MakerHedgeConfig(max_acceptable_gas_drag_bps=Decimal(0))
    with pytest.raises(MakerHedgeError, match="min_hedge_notional_usdc"):
        MakerHedgeConfig(min_hedge_notional_usdc=-1)
    with pytest.raises(MakerHedgeError, match="max_unhedged_steps"):
        MakerHedgeConfig(max_unhedged_steps=0)
    with pytest.raises(MakerHedgeError, match="volatility_window_steps"):
        MakerHedgeConfig(volatility_window_steps=0)
    with pytest.raises(MakerHedgeError, match="max_adaptive_adverse_bps"):
        MakerHedgeConfig(
            adaptive_volatility_enabled=True,
            adverse_selection_bps=Decimal("10.0"),
            max_adaptive_adverse_bps=Decimal("5.0"),
        )
    with pytest.raises(MakerHedgeError, match="execution_eligible"):
        UnhedgedMakerLot(
            product_id="ETH-USD",
            side="ask_maker",
            notional_usdc=50_000_000,
            notional_token=20_000_000_000_000_000,
            fill_price_usdc=Decimal(2500),
            maker_fee_usd=Decimal(0),
            adverse_cost_usd=Decimal(0),
            fill_step_idx=0,
            fill_mode="strict_cross",
            adverse_bps=Decimal("1.5"),
            execution_eligible=True,  # type: ignore[arg-type]
        )

    # Batching two $50 strict_cross fills with min_hedge_notional_usdc=$100 charges gas ONCE
    cfg = MakerHedgeConfig(
        trade_notional_usdc=50_000_000,
        cex_maker_fee_bps=Decimal(0),
        adverse_selection_bps=Decimal("1.5"),
        min_quote_edge_bps=Decimal("3.0"),
        tick_improvement_bps=Decimal(0),
        min_hedge_notional_usdc=100_000_000,
        max_acceptable_gas_drag_bps=Decimal("5.0"),
        max_unhedged_steps=8,
        max_unhedged_drift_bps=Decimal("100.0"),
    )
    tracker = MakerHedgeLifecycleTracker(cfg)
    qty_0_02 = 2 * 10**16  # 0.02 ETH ($50 at $2500 DEX buy)
    gas_usd = Decimal("0.04")

    # Step 1: place resting Ask at $2510 (DEX buy = $2500)
    s1 = tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2500.00", ask="2510.00", sequence=1),
        trade_notional_usdc=50_000_000,
        dex_buy_token_out=qty_0_02,
        trade_notional_token=qty_0_02,
        dex_sell_usdc_out=49_900_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert s1.quote.quoted_ask_usd == Decimal("2510.00")

    # Step 2: strict cross fills $50.20 ask (0.02 ETH @ 2510); mid=2515 (+5/token adverse!)
    # Open lot ($50.20 < $100 floor) stays unhedged; unrealized loss = 0.02 * (2515 - 2510) = $0.10
    s2 = tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2510.00", ask="2520.00", sequence=2),
        trade_notional_usdc=50_000_000,
        dex_buy_token_out=qty_0_02,
        trade_notional_token=qty_0_02,
        dex_sell_usdc_out=49_900_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert s2.strict_fills == ()
    assert tracker.summary()["open_unhedged_lots"] == 1
    assert tracker.unhedged_exposure_usd_micros() == 50_200_000
    assert tracker.unrealized_drawdown_usd_micros() == 100_000
    assert tracker.summary()["batched_fifo_hedges"] == 0

    # Step 3: second strict cross fills $50.40 ask (0.02 ETH @ 2520) -> total $100.60 >= $100
    # Triggers ONE batched DEX hedge charging gas_usd ONCE ($0.04) across both lots!
    s3 = tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2520.00", ask="2524.00", sequence=3),
        trade_notional_usdc=50_000_000,
        dex_buy_token_out=qty_0_02,
        trade_notional_token=qty_0_02,
        dex_sell_usdc_out=49_900_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert len(s3.strict_fills) == 2
    assert all(f.hedge_mode == "batched_dex" for f in s3.strict_fills)
    assert tracker.summary()["batched_fifo_hedges"] == 1
    assert tracker.summary()["open_unhedged_lots"] == 0
    assert tracker.unhedged_exposure_usd_micros() == 0
    assert tracker.unrealized_drawdown_usd_micros() == 0

    # Total realized profit: CEX (50.20 + 50.40) - DEX (50 + 50) - 2*adv - 1*gas ($0.04)
    adv_per_lot = Decimal("50.20") * (Decimal("1.5") / Decimal(10_000)) + Decimal("50.40") * (
        Decimal("1.5") / Decimal(10_000)
    )
    expected_total_profit = Decimal("100.60") - Decimal("100.00") - adv_per_lot - gas_usd
    actual_total_profit = sum((f.realized_net_profit_usd for f in s3.strict_fills), Decimal(0))
    assert actual_total_profit == pytest.approx(expected_total_profit, abs=Decimal("1e-10"))
    assert json.dumps(tracker.summary())


def test_multi_product_lot_aging_preserve_queue_and_fifo_dedup() -> None:
    # 1. Invalid booleans for preserve_queue_priority / deduplicate_fifo_and_strict fail closed
    for bad_kw in (
        {"preserve_queue_priority": 1},
        {"deduplicate_fifo_and_strict": "yes"},
    ):
        with pytest.raises(MakerHedgeError):
            MakerHedgeConfig(**bad_kw)  # type: ignore[arg-type]

    # 2. Multi-product interleaving must NOT prematurely trigger batch_age_flush on ETH-USD
    age_cfg = MakerHedgeConfig(
        trade_notional_usdc=50_000_000,
        min_quote_edge_bps=Decimal("3.0"),
        tick_improvement_bps=Decimal(0),
        min_hedge_notional_usdc=100_000_000,
        max_unhedged_steps=4,
        max_unhedged_drift_bps=Decimal("100.0"),
    )
    age_tracker = MakerHedgeLifecycleTracker(age_cfg)
    qty_eth = 2 * 10**16  # 0.02 ETH ($50 @ 2500)
    gas_usd = Decimal("0.04")
    qty_aero = 100 * 10**18  # 100 AERO ($50 @ 0.50)

    # ETH step 0: place resting ask at 2510
    age_tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2500.00", ask="2510.00", sequence=1),
        trade_notional_usdc=50_000_000,
        dex_buy_token_out=qty_eth,
        trade_notional_token=qty_eth,
        dex_sell_usdc_out=49_900_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    # ETH step 1: strict cross opens $50.20 lot (< $100 floor) at ETH step_idx=1
    age_tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2510.00", ask="2511.00", sequence=2),
        trade_notional_usdc=50_000_000,
        dex_buy_token_out=qty_eth,
        trade_notional_token=qty_eth,
        dex_sell_usdc_out=49_900_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert age_tracker.summary()["open_unhedged_lots"] == 1

    # Interleave 8 unrelated product steps: ETH lot age must remain 0 -> must NOT flush
    for idx in range(8):
        age_tracker.step(
            "AERO-USD",
            current_book=_make_book(
                "AERO-USD", bid="0.50", ask="0.501", size="1000.0", sequence=10 + idx
            ),
            trade_notional_usdc=50_000_000,
            dex_buy_token_out=qty_aero,
            trade_notional_token=qty_aero,
            dex_sell_usdc_out=49_900_000,
            token_decimals=18,
            gas_cost_usd=gas_usd,
        )
    assert age_tracker.summary()["open_unhedged_lots"] == 1
    assert age_tracker.summary()["batched_fifo_hedges"] == 0

    # Advance ETH-USD steps 2, 3, 4 (same 2511 ask, non-crossing): age 3 < 4 stays open,
    # then ETH-USD step 5 has current_step_idx=5 -> age = 5 - 1 = 4 >= 4 -> flushes!
    for seq in (3, 4, 5):
        s_wait = age_tracker.step(
            "ETH-USD",
            current_book=_make_book("ETH-USD", bid="2508.00", ask="2511.00", sequence=seq),
            trade_notional_usdc=50_000_000,
            dex_buy_token_out=qty_eth,
            trade_notional_token=qty_eth,
            dex_sell_usdc_out=49_900_000,
            token_decimals=18,
            gas_cost_usd=gas_usd,
        )
        assert s_wait.fills == ()
    assert age_tracker.summary()["open_unhedged_lots"] == 1

    s_flush = age_tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2508.00", ask="2511.00", sequence=6),
        trade_notional_usdc=50_000_000,
        dex_buy_token_out=qty_eth,
        trade_notional_token=qty_eth,
        dex_sell_usdc_out=49_900_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert len(s_flush.strict_fills) == 1
    assert "batch_age_flush" in s_flush.strict_fills[0].hedge_note
    assert age_tracker.summary()["open_unhedged_lots"] == 0
    assert age_tracker.summary()["batched_fifo_hedges"] == 1

    # 3. preserve_queue_priority=True carries forward partially drained queue across unchanged price
    # and resets when quoted price changes
    q_cfg = MakerHedgeConfig(
        trade_notional_usdc=200_000_000,
        min_quote_edge_bps=Decimal("3.0"),
        tick_improvement_bps=Decimal(0),
        preserve_queue_priority=True,
        deduplicate_fifo_and_strict=True,
    )
    q_tracker = MakerHedgeLifecycleTracker(q_cfg)
    qty_0_08 = 8 * 10**16
    book_2512 = _make_book("ETH-USD", bid="2505.00", ask="2512.00", size="2.0", sequence=20)
    q_tracker.step(
        "ETH-USD",
        current_book=book_2512,
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_0_08,
        trade_notional_token=qty_0_08,
        dex_sell_usdc_out=199_800_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    # Print of 1.50 drains queue ahead from 2.0 -> 0.50 even as new book size grows to 5.0!
    book_2512_deep = _make_book("ETH-USD", bid="2505.00", ask="2512.00", size="5.0", sequence=21)
    q_s2 = q_tracker.step(
        "ETH-USD",
        current_book=book_2512_deep,
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_0_08,
        trade_notional_token=qty_0_08,
        dex_sell_usdc_out=199_800_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
        recent_trades=(
            CoinbaseTradePrint(
                product_id="ETH-USD",
                trade_id=5001,
                price=Decimal("2512.00"),
                size=Decimal("1.50"),
                maker_side="sell",
                traded_at_utc="2026-11-19T00:00:01Z",
            ),
        ),
    )
    assert q_s2.fifo_fills == ()
    # Next tick: only 0.55 prints at 2512.00 -> drains remaining 0.50 and fills 0.05 of our 0.08!
    q_s3 = q_tracker.step(
        "ETH-USD",
        current_book=book_2512_deep,
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_0_08,
        trade_notional_token=qty_0_08,
        dex_sell_usdc_out=199_800_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
        recent_trades=(
            CoinbaseTradePrint(
                product_id="ETH-USD",
                trade_id=5002,
                price=Decimal("2512.00"),
                size=Decimal("0.55"),
                maker_side="sell",
                traded_at_utc="2026-11-19T00:00:02Z",
            ),
        ),
    )
    assert len(q_s3.fifo_fills) == 1
    assert q_s3.fifo_fills[0].filled_qty_tokens == "0.05"
    assert q_s3.strict_fills == ()

    # When quoted price changes (2512.00 -> 2513.00), queue resets to new BBO size (5.0)
    book_2513 = _make_book("ETH-USD", bid="2505.00", ask="2513.00", size="5.0", sequence=22)
    q_s4 = q_tracker.step(
        "ETH-USD",
        current_book=book_2513,
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_0_08,
        trade_notional_token=qty_0_08,
        dex_sell_usdc_out=199_800_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert q_s4.quote.quoted_ask_usd == Decimal("2513.00")
    q_s5 = q_tracker.step(
        "ETH-USD",
        current_book=book_2513,
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_0_08,
        trade_notional_token=qty_0_08,
        dex_sell_usdc_out=199_800_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
        recent_trades=(
            CoinbaseTradePrint(
                product_id="ETH-USD",
                trade_id=5003,
                price=Decimal("2513.00"),
                size=Decimal("1.00"),
                maker_side="sell",
                traded_at_utc="2026-11-19T00:00:03Z",
            ),
        ),
    )
    assert q_s5.fifo_fills == ()

    # 4. deduplicate_fifo_and_strict=True suppresses double-count strict_cross on ask side
    # when FIFO already filled on the same step, while still allowing unfilled bid_maker
    # strict_cross without a print
    dex_tracker = MakerHedgeLifecycleTracker(q_cfg)
    dex_s1 = dex_tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2505.00", ask="2515.00", size="1.0", sequence=30),
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_0_08,  # $2500 DEX buy -> ask active @ 2515
        trade_notional_token=qty_0_08,
        dex_sell_usdc_out=201_200_000,  # $2515 DEX sell -> bid active @ 2505
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert dex_s1.quote.ask_active is True
    assert dex_s1.quote.bid_active is True
    # Ask swept via FIFO print + BBO cross on same step -> 1 FIFO fill, 0 strict duplicate
    dex_s2 = dex_tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2516.00", ask="2518.00", size="1.0", sequence=31),
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_0_08,
        trade_notional_token=qty_0_08,
        dex_sell_usdc_out=199_800_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
        recent_trades=(
            CoinbaseTradePrint(
                product_id="ETH-USD",
                trade_id=6001,
                price=Decimal("2516.00"),
                size=Decimal("0.08"),
                maker_side="sell",
                traded_at_utc="2026-11-19T00:01:00Z",
            ),
        ),
    )
    assert len(dex_s2.fifo_fills) == 1
    assert dex_s2.fifo_fills[0].side == "ask_maker"
    assert dex_s2.strict_fills == ()
    assert len(dex_s2.fills) == 1

    # Second tick: bid-only cross without a WS print still emits strict_cross on bid_maker
    dex_s3 = dex_tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2505.00", ask="2515.00", size="1.0", sequence=32),
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_0_08,
        trade_notional_token=qty_0_08,
        dex_sell_usdc_out=201_200_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert dex_s3.quote.bid_active is True
    dex_s4 = dex_tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2500.00", ask="2504.00", size="1.0", sequence=33),
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_0_08,
        trade_notional_token=qty_0_08,
        dex_sell_usdc_out=201_200_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert dex_s4.fifo_fills == ()
    assert len(dex_s4.strict_fills) == 1
    assert dex_s4.strict_fills[0].side == "bid_maker"


def test_cex_dust_unwind_avoids_high_gas_drag_on_micro_partial_fill() -> None:
    """Sub-threshold dust lots unwind on CEX when zero-gas CEX close beats Base DEX gas drag."""
    cfg = MakerHedgeConfig(
        trade_notional_usdc=200_000_000,
        min_quote_edge_bps=Decimal("3.0"),
        min_hedge_notional_usdc=25_000_000,
        max_unhedged_steps=2,
        cex_dust_unwind_enabled=True,
        cex_dust_unwind_taker_fee_bps=Decimal("3.5"),
    )
    tracker = MakerHedgeLifecycleTracker(cfg)
    qty_0_08 = 8 * 10**16  # 0.08 ETH at $2500 = $200
    gas_usd = Decimal("0.01")  # 100 bps gas drag on a $1.00 dust fill

    # Tick 0: post ask at ~$2514.87 (DEX buy at $2500 => ~59 bps gross edge)
    s0 = tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2505.00", ask="2515.00", size="1.0", sequence=50),
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_0_08,
        trade_notional_token=qty_0_08,
        dex_sell_usdc_out=199_800_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert s0.quote.ask_active is True

    # Tick 1: tiny $1.00 partial FIFO ask fill (0.0004 ETH) sits in batch buffer
    s1 = tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2514.00", ask="2514.50", size="1.0", sequence=51),
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_0_08,
        trade_notional_token=qty_0_08,
        dex_sell_usdc_out=199_800_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
        recent_trades=(
            CoinbaseTradePrint(
                product_id="ETH-USD",
                trade_id=7001,
                price=Decimal("2515.00"),
                size=Decimal("0.0004"),
                maker_side="sell",
                traded_at_utc="2026-11-19T02:01:00Z",
            ),
        ),
    )
    assert s1.fifo_fills == ()
    assert tracker.summary()["open_unhedged_lots"] == 1

    # Tick 2 -> Tick 3: age reaches max_unhedged_steps=2; CEX unwind at $2514.50 beats $0.01 DEX gas
    tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2514.00", ask="2514.50", size="1.0", sequence=52),
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_0_08,
        trade_notional_token=qty_0_08,
        dex_sell_usdc_out=199_800_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    s3 = tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2514.00", ask="2514.50", size="1.0", sequence=53),
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_0_08,
        trade_notional_token=qty_0_08,
        dex_sell_usdc_out=199_800_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert len(s3.fifo_fills) == 1
    assert s3.fifo_fills[0].hedge_mode == "cex_dust_unwind"
    assert "cex_dust_unwind" in s3.fifo_fills[0].hedge_note
    assert tracker.summary()["open_unhedged_lots"] == 0
    assert tracker.summary()["cex_dust_unwinds"] == 1
    # Paid ~$0.00035 taker fee instead of burning $0.01 DEX gas on a $1.00 dust fill!
    assert s3.fifo_fills[0].realized_net_profit_usd > Decimal("-0.001")


def test_post_fill_same_side_cooldown_and_opposite_side_allowed() -> None:
    """`post_fill_cooldown_steps` suppresses same-side re-quoting while keeping opposite active."""
    cfg = MakerHedgeConfig(
        trade_notional_usdc=200_000_000,
        min_quote_edge_bps=Decimal("3.0"),
        post_fill_cooldown_steps=2,
    )
    tracker = MakerHedgeLifecycleTracker(cfg)
    qty_0_08 = 8 * 10**16
    gas_usd = Decimal("0.01")

    # Step 0: both ask and bid active around $2500 DEX mid
    s0 = tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2485.00", ask="2515.00", size="1.0", sequence=10),
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_0_08,
        trade_notional_token=qty_0_08,
        dex_sell_usdc_out=200_000_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert s0.quote.ask_active is True
    assert s0.quote.bid_active is True

    # Step 1: strict ask fill lifts resting ask -> ask enters cooldown for steps 1, 2 (2 steps)
    s1 = tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2516.00", ask="2525.00", size="1.0", sequence=11),
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_0_08,
        trade_notional_token=qty_0_08,
        dex_sell_usdc_out=200_000_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert len(s1.strict_fills) == 1
    assert s1.strict_fills[0].side == "ask_maker"
    assert s1.quote.ask_active is False

    # Step 2: still in ask cooldown while opposite bid side remains allowed when edge clears floor
    s2 = tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2485.00", ask="2515.00", size="1.0", sequence=12),
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_0_08,
        trade_notional_token=qty_0_08,
        dex_sell_usdc_out=200_000_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert s2.quote.ask_active is False
    assert s2.quote.bid_active is True

    # Step 3: cooldown still active (fill_step=1 -> until 1+2+1=4; steps 1,2,3 suppressed) -> wait!
    # Wait: fill step 1 + cooldown_steps=2 + 1 = 4, so step 3 is suppressed and step 4 resumes!
    s3 = tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2485.00", ask="2515.00", size="1.0", sequence=13),
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_0_08,
        trade_notional_token=qty_0_08,
        dex_sell_usdc_out=200_000_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert s3.quote.ask_active is False
    assert s3.quote.bid_active is True

    # Step 4: cooldown expires -> ask re-activates
    s4 = tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2485.00", ask="2515.00", size="1.0", sequence=14),
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_0_08,
        trade_notional_token=qty_0_08,
        dex_sell_usdc_out=200_000_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert s4.quote.ask_active is True
    assert s4.quote.bid_active is True
    assert tracker.summary()["cooldown_quote_skips"] == 3


def test_same_dir_momentum_suppresses_toxic_side_and_keeps_counter_side() -> None:
    """Breakout momentum suppresses the toxic side without blocking counter-side liquidity."""
    cfg = MakerHedgeConfig(
        trade_notional_usdc=200_000_000,
        min_quote_edge_bps=Decimal("3.0"),
        max_same_dir_momentum_bps=Decimal("8.0"),
    )
    tracker = MakerHedgeLifecycleTracker(cfg)
    qty_0_08 = 8 * 10**16
    gas_usd = Decimal("0.01")

    # Step 0: baseline mid = $2500.00
    s0 = tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2485.00", ask="2515.00", size="1.0", sequence=20),
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_0_08,
        trade_notional_token=qty_0_08,
        dex_sell_usdc_out=200_000_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert s0.quote.ask_active is True

    # Step 1: upward spike (+40 bps mid jump) suppresses ask_maker while keeping bid_maker active
    s1 = tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2495.00", ask="2525.00", size="1.0", sequence=21),
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_0_08,
        trade_notional_token=qty_0_08,
        dex_sell_usdc_out=201_000_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert s1.quote.ask_active is False
    assert s1.quote.bid_active is True

    # Step 2: sharp drop (-80 bps mid jump) suppresses bid_maker while keeping ask_maker active
    s2 = tracker.step(
        "ETH-USD",
        current_book=_make_book("ETH-USD", bid="2475.00", ask="2505.00", size="1.0", sequence=22),
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=qty_0_08,
        trade_notional_token=qty_0_08,
        dex_sell_usdc_out=199_500_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert s2.quote.ask_active is True
    assert s2.quote.bid_active is False
    assert tracker.summary()["momentum_quote_skips"] == 2


def test_dynamic_maker_quote_sizer_rejects_oversized_inactive_notional() -> None:
    """`_select_dynamic_maker_quotes` chooses smaller active size over large inactive impact."""
    book = _make_book("ETH-USD", bid="2490.00", ask="2505.00", size="1.0", sequence=30)
    cfg = MakerHedgeConfig(
        trade_notional_usdc=25_000_000,
        min_quote_edge_bps=Decimal("10.0"),
        adverse_selection_bps=Decimal("1.5"),
    )

    def _quote_fn(notional_usdc: int) -> tuple[int | None, int, int | None]:
        # At $25-$100 DEX ask is ~$2500 -> edge ~18 bps (active >= 10 bps).
        # Above $100 DEX price impact jumps to $2504 -> edge ~2 bps (inactive < 10 bps).
        usd_val = Decimal(notional_usdc) / Decimal(1_000_000)
        dex_px = Decimal("2500.00") if notional_usdc <= 100_000_000 else Decimal("2504.50")
        tok_out = int((usd_val / dex_px) * Decimal(10**18))
        return tok_out, tok_out, None

    chosen_usdc, _ = _select_dynamic_maker_quotes(
        product_id="ETH-USD",
        token_decimals=18,
        fetched_book=book,
        quote_fn=_quote_fn,
        gas_cost_usd=Decimal("0.01"),
        maker_config=cfg,
        min_clip_usdc=25_000_000,
        max_clip_usdc=500_000_000,
        fallback_notional_usdc=25_000_000,
    )
    assert chosen_usdc <= 100_000_000
