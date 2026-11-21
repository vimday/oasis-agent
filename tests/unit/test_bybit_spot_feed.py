"""Unit tests for keyless Bybit Spot V5 orderbook & WebSocket trade-flow client."""

import json
import re
from decimal import Decimal

import pytest

from oasis_agent.cross_venue.bybit_spot_feed import (
    BYBIT_SYMBOL_BY_PRODUCT,
    BybitSpotFeedError,
    BybitSpotWebSocketFeed,
    UsdtUsdPeg,
    fetch_bybit_l2_book,
    fetch_bybit_recent_trades,
    fetch_usdt_usd_peg,
    parse_bybit_orderbook_payload,
    parse_bybit_recent_trades_payload,
    parse_usdt_usd_peg_payload,
    resolve_bybit_symbol,
)
from oasis_agent.cross_venue.coinbase_trade_flow import (
    _WS_OP_CLOSE,
    _WS_OP_TEXT,
    build_websocket_frame,
    compute_websocket_accept,
)


def _sample_peg() -> UsdtUsdPeg:
    raw = json.dumps({"bids": [["0.9990", "100000", 1]], "asks": [["0.9992", "100000", 1]]}).encode(
        "utf-8"
    )
    return parse_usdt_usd_peg_payload(raw, retrieved_at_utc="2026-11-19T02:00:00Z")


def test_resolve_bybit_symbol_and_reject_unsupported() -> None:
    assert resolve_bybit_symbol("VVV-USD") == ("VVV-USD", "VVVUSDT")
    assert resolve_bybit_symbol("vvv") == ("VVV-USD", "VVVUSDT")
    assert resolve_bybit_symbol("WETH") == ("ETH-USD", "ETHUSDT")
    assert len(BYBIT_SYMBOL_BY_PRODUCT) == 7
    with pytest.raises(BybitSpotFeedError, match="not supported on Bybit Spot"):
        resolve_bybit_symbol("WELL-USD")


def test_usdt_usd_peg_validation_and_depeg_guard() -> None:
    peg = _sample_peg()
    assert peg.bid == Decimal("0.9990")
    assert peg.ask == Decimal("0.9992")
    assert peg.mid == Decimal("0.9991")
    assert peg.execution_eligible is False

    depegged = json.dumps(
        {"bids": [["0.9000", "1000", 1]], "asks": [["0.9010", "1000", 1]]}
    ).encode("utf-8")
    with pytest.raises(BybitSpotFeedError, match="outside safety band"):
        parse_usdt_usd_peg_payload(depegged)

    crossed = json.dumps({"bids": [["1.0005", "1000", 1]], "asks": [["1.0000", "1000", 1]]}).encode(
        "utf-8"
    )
    with pytest.raises(BybitSpotFeedError, match="crossed"):
        parse_usdt_usd_peg_payload(crossed)


def test_fetch_usdt_usd_peg_via_mock_transport() -> None:
    payload = json.dumps(
        {"bids": [["0.9988", "50000", 2]], "asks": [["0.9989", "40000", 1]]}
    ).encode("utf-8")
    seen_urls: list[str] = []

    def _tr(url: str, _timeout: float) -> bytes:
        seen_urls.append(url)
        return payload

    peg = fetch_usdt_usd_peg(timeout_s=3.0, transport=_tr)
    assert seen_urls == ["https://api.exchange.coinbase.com/products/USDT-USD/book?level=1"]
    assert peg.bid == Decimal("0.9988")
    assert peg.ask == Decimal("0.9989")


def test_parse_and_fetch_bybit_l2_book_normalizes_usdt_to_usd() -> None:
    peg = _sample_peg()
    payload = json.dumps(
        {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "s": "VVVUSDT",
                "b": [["20.00", "10"], ["19.90", "25"]],
                "a": [["20.10", "12"], ["20.20", "30"]],
                "ts": 1795000000000,
                "u": 42,
            },
        }
    ).encode("utf-8")
    book = fetch_bybit_l2_book(
        "VVV-USD",
        usdt_peg=peg,
        limit=25,
        transport=lambda _u, _t: payload,
    )
    assert book.product_id == "VVV-USD"
    assert book.sequence == 42
    assert book.bids[0].price == Decimal("20.00") * peg.mid
    assert book.asks[0].price == Decimal("20.10") * peg.mid
    assert book.best_bid < book.best_ask

    # Verify trade-print normalization aligns bit-for-bit with top-of-book L2 prices
    tob_trades = json.dumps(
        {
            "retCode": 0,
            "result": {
                "category": "spot",
                "list": [
                    {
                        "execId": "1001",
                        "symbol": "VVVUSDT",
                        "price": "20.10",
                        "size": "1.0",
                        "side": "Buy",
                        "time": "1795000001000",
                    },
                    {
                        "execId": "1000",
                        "symbol": "VVVUSDT",
                        "price": "20.00",
                        "size": "1.0",
                        "side": "Sell",
                        "time": "1795000000000",
                    },
                ],
            },
        }
    ).encode("utf-8")
    parsed_tob = parse_bybit_recent_trades_payload("VVV-USD", tob_trades, usdt_peg=peg)
    assert parsed_tob[0].price == book.best_bid
    assert parsed_tob[1].price == book.best_ask

    bad_ret = json.dumps({"retCode": 10001, "result": {}}).encode("utf-8")
    with pytest.raises(BybitSpotFeedError, match="retCode"):
        parse_bybit_orderbook_payload("VVV-USD", bad_ret, usdt_peg=peg)


def test_parse_bybit_recent_trades_inverts_taker_side_to_maker_side() -> None:
    peg = _sample_peg()
    payload = json.dumps(
        {
            "retCode": 0,
            "result": {
                "category": "spot",
                "list": [
                    {
                        "execId": "20002",
                        "symbol": "VVVUSDT",
                        "price": "20.10",
                        "size": "5.5",
                        "side": "Buy",  # taker bought -> lifted resting ask -> maker_side='sell'
                        "time": "1795000002000",
                    },
                    {
                        "execId": "20001",
                        "symbol": "VVVUSDT",
                        "price": "20.00",
                        "size": "3.0",
                        "side": "Sell",  # taker sold -> hit resting bid -> maker_side='buy'
                        "time": "1795000001000",
                    },
                ],
            },
        }
    ).encode("utf-8")
    prints = fetch_bybit_recent_trades(
        "VVV-USD",
        usdt_peg=peg,
        since_trade_id=20000,
        transport=lambda _u, _t: payload,
    )
    assert [p.trade_id for p in prints] == [20001, 20002]
    assert prints[0].maker_side == "buy"
    assert prints[1].maker_side == "sell"
    assert prints[0].price == Decimal("20.00") * peg.mid
    assert prints[1].price == Decimal("20.10") * peg.mid

    filtered = parse_bybit_recent_trades_payload(
        "VVV-USD", payload, usdt_peg=peg, since_trade_id=20001
    )
    assert [p.trade_id for p in filtered] == [20002]


class _FakeWsSocket:
    def __init__(self, server_frames: list[bytes]) -> None:
        self._server_frames = list(server_frames)
        self._incoming = bytearray()
        self.sent = bytearray()
        self.closed = False

    def settimeout(self, _timeout: float) -> None:
        return None

    def setblocking(self, _flag: bool) -> None:
        return None

    def fileno(self) -> int:
        return -1

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)
        if b"Sec-WebSocket-Key:" in data:
            match = re.search(rb"Sec-WebSocket-Key:\s*([^\r\n]+)", data)
            assert match is not None
            accept = compute_websocket_accept(match.group(1).decode("ascii"))
            resp = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
            self._incoming.extend(resp)
            for frame in self._server_frames:
                self._incoming.extend(frame)
            self._server_frames.clear()

    def recv(self, size: int) -> bytes:
        if not self._incoming:
            raise BlockingIOError
        chunk = bytes(self._incoming[:size])
        del self._incoming[:size]
        return chunk

    def close(self) -> None:
        self.closed = True


def test_bybit_ws_feed_handshake_trade_drain_and_close() -> None:
    peg = _sample_peg()
    sub_ack = json.dumps({"success": True, "op": "subscribe"}).encode("utf-8")
    trade_msg = json.dumps(
        {
            "topic": "publicTrade.VVVUSDT",
            "type": "snapshot",
            "data": [
                {
                    "i": "990001",
                    "T": 1795000005000,
                    "p": "21.50",
                    "v": "4.0",
                    "S": "Buy",
                    "s": "VVVUSDT",
                },
                {
                    "i": "990002",
                    "T": 1795000006000,
                    "p": "21.40",
                    "v": "2.5",
                    "S": "Sell",
                    "s": "VVVUSDT",
                },
            ],
        }
    ).encode("utf-8")
    frames = [
        build_websocket_frame(_WS_OP_TEXT, sub_ack, masked=False),
        build_websocket_frame(_WS_OP_TEXT, trade_msg, masked=False),
    ]
    fake_sock = _FakeWsSocket(frames)
    feed = BybitSpotWebSocketFeed(
        ["VVV-USD"],
        usdt_peg=peg,
        socket_factory=lambda _h, _p, _t: fake_sock,
    )
    feed.connect()
    assert feed.is_connected is True
    prints, gap = feed.drain_events()
    assert gap is False
    assert len(prints) == 2
    assert prints[0].product_id == "VVV-USD"
    assert prints[0].trade_id == 990001
    assert prints[0].maker_side == "sell"
    assert prints[0].price == Decimal("21.50") * peg.mid
    assert prints[1].trade_id == 990002
    assert prints[1].maker_side == "buy"

    # Server close triggers gap_detected=True and closes cleanly
    fake_sock._incoming.extend(build_websocket_frame(_WS_OP_CLOSE, b"", masked=False))
    prints2, gap2 = feed.drain_events()
    assert prints2 == ()
    assert gap2 is True
    assert feed.is_connected is False
    assert fake_sock.closed is True
