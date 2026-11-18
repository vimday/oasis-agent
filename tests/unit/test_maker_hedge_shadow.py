"""Unit tests for 9-pair Coinbase IPv4/level-1 fallback and passive maker-hedge shadow."""

import json
import socket
import ssl
import urllib.request
from decimal import Decimal
from typing import Any

import pytest

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
    build_maker_quote_candidate,
)


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
    assert summary["execution_eligible"] is False


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
