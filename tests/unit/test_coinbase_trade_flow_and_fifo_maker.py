"""Unit tests for Coinbase trade-flow client (REST + RFC-6455 WS) and FIFO maker hedge."""

import base64
import hashlib
import json
import socket
import ssl
import struct
import threading
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Any

import pytest

from oasis_agent.cross_venue.coinbase_trade_flow import (
    COINBASE_TRADES_URL_TEMPLATE,
    MAX_TRADES_RESPONSE_BYTES,
    MAX_WS_FRAME_BYTES,
    RFC6455_GUID,
    CoinbaseTradeFlowError,
    CoinbaseTradePrint,
    CoinbaseWebSocketFeed,
    _connect_tls_ipv4_preferred,
    _default_trades_https_get,
    _validate_coinbase_trades_url,
    build_websocket_frame,
    compute_websocket_accept,
    fetch_coinbase_recent_trades,
    parse_coinbase_trades_payload,
)
from oasis_agent.cross_venue.live_cex_dex_shadow import (
    CoinbaseL2Book,
    CoinbaseL2Level,
    _IPv4PreferredHTTPSHandler,
)
from oasis_agent.cross_venue.maker_hedge_shadow import (
    MakerHedgeConfig,
    MakerHedgeError,
    MakerHedgeLifecycleTracker,
)


def _make_book(
    product_id: str = "ETH-USD",
    *,
    bid: str,
    ask: str,
    bid_size: str = "5.0",
    ask_size: str = "5.0",
    sequence: int = 100,
) -> CoinbaseL2Book:
    return CoinbaseL2Book(
        product_id=product_id,
        bids=(CoinbaseL2Level(price=Decimal(bid), size=Decimal(bid_size), num_orders=2),),
        asks=(CoinbaseL2Level(price=Decimal(ask), size=Decimal(ask_size), num_orders=2),),
        sequence=sequence,
        retrieved_at_utc="2026-11-18T11:00:00+00:00",
        raw_sha256="33" * 32,
    )


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("unexpected EOF reading test frame")
        buf.extend(chunk)
    return bytes(buf)


def _read_client_ws_frame(sock: socket.socket) -> tuple[bool, int, bytes]:
    b0, b1 = _recv_exact(sock, 2)
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", _recv_exact(sock, 2))
    elif length == 127:
        (length,) = struct.unpack("!Q", _recv_exact(sock, 8))
    mask_key = _recv_exact(sock, 4) if masked else b""
    raw = _recv_exact(sock, length)
    payload = bytes(b ^ mask_key[i & 3] for i, b in enumerate(raw)) if masked else raw
    return fin, opcode, payload


def _read_http_upgrade_headers(sock: socket.socket) -> tuple[str, dict[str, str]]:
    raw = bytearray()
    while b"\r\n\r\n" not in raw:
        chunk = sock.recv(4096)
        if not chunk:
            raise EOFError("unexpected EOF reading HTTP upgrade request")
        raw.extend(chunk)
    head_text = raw.split(b"\r\n\r\n", 1)[0].decode("ascii")
    lines = head_text.split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, _, val = line.partition(":")
        headers[name.strip().lower()] = val.strip()
    return lines[0], headers


def test_coinbase_trade_print_validation_and_rest_payload_parsing() -> None:
    tp = CoinbaseTradePrint(
        product_id="eth",
        trade_id=101,
        price=Decimal("3005.50"),
        size=Decimal("0.25"),
        maker_side="sell",
        traded_at_utc="2026-11-18T11:10:00.123456Z",
    )
    assert tp.product_id == "ETH-USD"
    assert tp.notional_usd == Decimal("751.375")
    d = tp.to_dict()
    assert d["product_id"] == "ETH-USD"
    assert d["trade_id"] == 101
    assert d["price"] == "3005.50"
    assert d["size"] == "0.25"
    assert d["maker_side"] == "sell"
    assert d["notional_usd"] == 751.375
    assert json.dumps(d)

    for bad_kwargs in (
        {
            "product_id": "UNKNOWN-USD",
            "trade_id": 1,
            "price": Decimal(1),
            "size": Decimal(1),
            "maker_side": "buy",
            "traded_at_utc": "2026-11-18T00:00:00Z",
        },
        {
            "product_id": "ETH-USD",
            "trade_id": 0,
            "price": Decimal(1),
            "size": Decimal(1),
            "maker_side": "buy",
            "traded_at_utc": "2026-11-18T00:00:00Z",
        },
        {
            "product_id": "ETH-USD",
            "trade_id": True,
            "price": Decimal(1),
            "size": Decimal(1),
            "maker_side": "buy",
            "traded_at_utc": "2026-11-18T00:00:00Z",
        },
        {
            "product_id": "ETH-USD",
            "trade_id": 1,
            "price": 3000.0,
            "size": Decimal(1),
            "maker_side": "buy",
            "traded_at_utc": "2026-11-18T00:00:00Z",
        },
        {
            "product_id": "ETH-USD",
            "trade_id": 1,
            "price": Decimal("NaN"),
            "size": Decimal(1),
            "maker_side": "buy",
            "traded_at_utc": "2026-11-18T00:00:00Z",
        },
        {
            "product_id": "ETH-USD",
            "trade_id": 1,
            "price": Decimal(1),
            "size": Decimal(0),
            "maker_side": "buy",
            "traded_at_utc": "2026-11-18T00:00:00Z",
        },
        {
            "product_id": "ETH-USD",
            "trade_id": 1,
            "price": Decimal(1),
            "size": Decimal(1),
            "maker_side": "taker",
            "traded_at_utc": "2026-11-18T00:00:00Z",
        },
        {
            "product_id": "ETH-USD",
            "trade_id": 1,
            "price": Decimal(1),
            "size": Decimal(1),
            "maker_side": "buy",
            "traded_at_utc": "  ",
        },
    ):
        with pytest.raises(CoinbaseTradeFlowError):
            CoinbaseTradePrint(**bad_kwargs)  # type: ignore[arg-type]

    # REST payload parsing: descending API order, duplicate trade_id, watermark filtering
    payload = json.dumps(
        [
            {
                "trade_id": 503,
                "price": "3002.10",
                "size": "0.40",
                "side": "sell",
                "time": "2026-11-18T11:00:03Z",
            },
            {
                "trade_id": 501,
                "price": "3000.00",
                "size": "0.10",
                "side": "buy",
                "time": "2026-11-18T11:00:01Z",
            },
            {
                "trade_id": 502,
                "price": "3001.50",
                "size": "0.20",
                "side": "sell",
                "time": "2026-11-18T11:00:02Z",
            },
            {
                "trade_id": 502,
                "price": "3001.50",
                "size": "0.20",
                "side": "sell",
                "time": "2026-11-18T11:00:02Z",
            },
        ]
    ).encode("utf-8")
    parsed_all = parse_coinbase_trades_payload("ETH-USD", payload)
    assert [p.trade_id for p in parsed_all] == [501, 502, 503]
    parsed_since = parse_coinbase_trades_payload("ETH-USD", payload, since_trade_id=501)
    assert [p.trade_id for p in parsed_since] == [502, 503]
    assert parse_coinbase_trades_payload("ETH-USD", b"[]") == ()

    # Malformed REST payloads fail closed
    for bad_payload in (
        b"{}",
        b"not-json",
        b'[{"trade_id":1,"price":"NaN","size":"1","side":"buy","time":"2026-11-18T00:00:00Z"}]',
        b'[{"trade_id":1,"price":NaN,"size":"1","side":"buy","time":"2026-11-18T00:00:00Z"}]',
        b'[{"trade_id":1,"trade_id":2,"price":"1","size":"1","side":"buy","time":"2026-11-18T00:00:00Z"}]',
        b'[{"trade_id":1,"price":"1","size":"1","side":"buy","time":"2026-11-18T00:00:00Z"},{"trade_id":1,"price":"2","size":"1","side":"buy","time":"2026-11-18T00:00:00Z"}]',
        b"[" + b" " * MAX_TRADES_RESPONSE_BYTES + b"]",
    ):
        with pytest.raises(CoinbaseTradeFlowError):
            parse_coinbase_trades_payload("ETH-USD", bad_payload)


def test_fetch_coinbase_recent_trades_url_proxy_bypass_ssrf_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_urls: list[str] = []

    def _mock_transport(url: str, timeout_s: float) -> bytes:
        seen_urls.append(url)
        assert timeout_s == 4.5
        return json.dumps(
            [
                {
                    "trade_id": 12,
                    "price": "90000.00",
                    "size": "0.05",
                    "side": "sell",
                    "time": "2026-11-18T11:00:02Z",
                },
                {
                    "trade_id": 11,
                    "price": "89999.00",
                    "size": "0.01",
                    "side": "buy",
                    "time": "2026-11-18T11:00:01Z",
                },
            ]
        ).encode("utf-8")

    prints = fetch_coinbase_recent_trades(
        "cbBTC",
        limit=25,
        since_trade_id=11,
        timeout_s=4.5,
        transport=_mock_transport,
    )
    assert seen_urls == [COINBASE_TRADES_URL_TEMPLATE.format(product="BTC-USD", limit=25)]
    assert [p.trade_id for p in prints] == [12]
    assert prints[0].product_id == "BTC-USD"

    for bad_url in (
        "http://api.exchange.coinbase.com/products/ETH-USD/trades?limit=50",
        "https://evil.example/products/ETH-USD/trades?limit=50",
        "https://userinfo@" + "api.exchange.coinbase.com/products/ETH-USD/trades?limit=50",
        "https://api.exchange.coinbase.com:8443/products/ETH-USD/trades?limit=50",
        "https://api.exchange.coinbase.com/products/ETH-USD/book?level=2",
        "https://api.exchange.coinbase.com/products/ETH-USD/trades?limit=0",
        "https://api.exchange.coinbase.com/products/ETH-USD/trades?limit=1001",
        "https://api.exchange.coinbase.com/products/ETH-USD/trades?limit=50#frag",
    ):
        with pytest.raises(CoinbaseTradeFlowError):
            _validate_coinbase_trades_url(bad_url)

    # Verify _default_trades_https_get injects ProxyHandler({}), _IPv4PreferredHTTPSHandler,
    # and _NoRedirectHandler even when https_proxy env points at 127.0.0.1.
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:9999")
    monkeypatch.setattr(
        "oasis_agent.cross_venue.coinbase_trade_flow.socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.55", 443))],
    )

    captured_handlers: list[Any] = []

    class _FakeResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self, n: int) -> bytes:
            return self._body[:n]

        def close(self) -> None:
            return None

        def __enter__(self) -> "_FakeResponse":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    class _FakeOpener:
        def __init__(self, handlers: tuple[Any, ...]) -> None:
            captured_handlers.extend(handlers)

        def open(self, req: urllib.request.Request, timeout: float) -> _FakeResponse:
            assert req.full_url == (
                "https://api.exchange.coinbase.com/products/ETH-USD/trades?limit=10"
            )
            assert timeout == 3.0
            return _FakeResponse(b"[]")

    def _fake_build_opener(*handlers: Any) -> _FakeOpener:
        return _FakeOpener(handlers)

    monkeypatch.setattr(urllib.request, "build_opener", _fake_build_opener)
    raw = _default_trades_https_get(
        "https://api.exchange.coinbase.com/products/ETH-USD/trades?limit=10",
        3.0,
    )
    assert raw == b"[]"
    proxy_handlers = [h for h in captured_handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {}
    assert any(isinstance(h, _IPv4PreferredHTTPSHandler) for h in captured_handlers)
    redirect_handler = next(
        h for h in captured_handlers if isinstance(h, urllib.request.HTTPRedirectHandler)
    )
    with pytest.raises(CoinbaseTradeFlowError, match="redirects are forbidden"):
        redirect_handler.redirect_request(None, _FakeResponse(b""), 302, "Found", {}, "https://x")

    # SSRF non-public IP rejection on REST path
    monkeypatch.setattr(
        "oasis_agent.cross_venue.coinbase_trade_flow.socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(CoinbaseTradeFlowError, match="non-public"):
        _default_trades_https_get(
            "https://api.exchange.coinbase.com/products/ETH-USD/trades?limit=10",
            3.0,
        )


def test_websocket_rfc6455_handshake_fragments_ping_pong_close_and_dedup() -> None:
    key_bytes = b"the sample nonce"  # canonical RFC-6455 Section 1.3 16-byte nonce
    sec_key = base64.b64encode(key_bytes).decode("ascii")
    expected_accept = compute_websocket_accept(sec_key)
    assert expected_accept == base64.b64encode(
        hashlib.sha1((sec_key + RFC6455_GUID).encode("ascii"), usedforsecurity=False).digest()
    ).decode("ascii")

    client_sock, server_sock = socket.socketpair()
    server_errors: list[BaseException] = []
    client_received_subscribe: list[dict[str, Any]] = []
    client_pong_payloads: list[bytes] = []
    client_close_received: list[bool] = []

    def _server_worker() -> None:
        try:
            server_sock.settimeout(5.0)
            req_line, req_headers = _read_http_upgrade_headers(server_sock)
            assert req_line == "GET / HTTP/1.1"
            assert req_headers["upgrade"].lower() == "websocket"
            assert "upgrade" in req_headers["connection"].lower()
            assert req_headers["sec-websocket-key"] == sec_key
            assert req_headers["sec-websocket-version"] == "13"

            # Send 101 Switching Protocols + trailing initial last_match frame in the same write!
            last_match = json.dumps(
                {
                    "type": "last_match",
                    "product_id": "ETH-USD",
                    "trade_id": 100,
                    "price": "3000.00",
                    "size": "0.50",
                    "side": "sell",
                    "time": "2026-11-18T11:15:00Z",
                }
            ).encode("utf-8")
            handshake_resp = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {expected_accept}\r\n"
                "\r\n"
            ).encode("ascii") + build_websocket_frame(0x1, last_match, masked=False)
            server_sock.sendall(handshake_resp)

            # Verify client sends masked subscribe frame
            fin, opcode, sub_bytes = _read_client_ws_frame(server_sock)
            assert fin is True
            assert opcode == 0x1
            client_received_subscribe.append(json.loads(sub_bytes.decode("utf-8")))

            # Send duplicate match (trade_id=100), fragmented new match (trade_id=101)
            # interrupted mid-message by a masked server Ping (0x9), and heartbeat.
            dup_msg = json.dumps(
                {
                    "type": "match",
                    "product_id": "ETH-USD",
                    "trade_id": 100,
                    "price": "3000.00",
                    "size": "0.50",
                    "side": "sell",
                    "time": "2026-11-18T11:15:00Z",
                }
            ).encode("utf-8")
            frag_msg = json.dumps(
                {
                    "type": "match",
                    "product_id": "ETH-USD",
                    "trade_id": 101,
                    "price": "3001.25",
                    "size": "0.75",
                    "side": "buy",
                    "time": "2026-11-18T11:15:01Z",
                }
            ).encode("utf-8")
            mid = len(frag_msg) // 2
            hb_msg = json.dumps({"type": "heartbeat", "sequence": 1}).encode("utf-8")

            server_sock.sendall(
                build_websocket_frame(0x1, dup_msg, masked=False)
                + build_websocket_frame(0x1, frag_msg[:mid], masked=False, fin=False)
                + build_websocket_frame(0x9, b"ping-1", masked=True, mask_key=b"\x01\x02\x03\x04")
                + build_websocket_frame(0x0, frag_msg[mid:], masked=False, fin=True)
                + build_websocket_frame(0x1, hb_msg, masked=False)
            )

            # Read client masked Pong reply to Ping
            p_fin, p_op, p_pay = _read_client_ws_frame(server_sock)
            assert p_fin is True
            assert p_op == 0xA
            client_pong_payloads.append(p_pay)

            # Send match (trade_id=102) followed by server Close frame (0x8)
            match_102 = json.dumps(
                {
                    "type": "match",
                    "product_id": "BTC-USD",
                    "trade_id": 202,
                    "price": "90010.00",
                    "size": "0.10",
                    "side": "sell",
                    "time": "2026-11-18T11:15:02Z",
                }
            ).encode("utf-8")
            server_sock.sendall(
                build_websocket_frame(0x1, match_102, masked=False)
                + build_websocket_frame(0x8, struct.pack("!H", 1000), masked=False)
            )

            c_fin, c_op, _c_pay = _read_client_ws_frame(server_sock)
            assert c_fin is True
            assert c_op == 0x8
            client_close_received.append(True)
        except BaseException as exc:  # noqa: BLE001
            server_errors.append(exc)
        finally:
            server_sock.close()

    thread = threading.Thread(target=_server_worker, daemon=True)
    thread.start()

    with CoinbaseWebSocketFeed(
        ["eth", "BTC-USD"],
        socket_connector=lambda _h, _p, _t: client_sock,
        key_bytes=key_bytes,
    ) as feed:
        assert feed.is_connected is True
        assert feed.product_ids == ("ETH-USD", "BTC-USD")

        # Drain until trade_id 101 arrives and pong is sent
        collected: list[CoinbaseTradePrint] = []
        for _ in range(40):
            batch, gap = feed.drain_events()
            assert gap is False
            collected.extend(batch)
            if len(collected) >= 2:
                break
            threading.Event().wait(0.01)

        assert [p.trade_id for p in collected] == [100, 101]
        assert collected[0].maker_side == "sell"
        assert collected[1].maker_side == "buy"
        assert feed.last_trade_id_by_product == {"ETH-USD": 101}

        # Drain final match (202) + Close frame (0x8)
        final_prints: list[CoinbaseTradePrint] = []
        saw_gap = False
        for _ in range(40):
            batch, gap = feed.drain_events()
            final_prints.extend(batch)
            if gap:
                saw_gap = True
                break
            threading.Event().wait(0.01)

        assert saw_gap is True
        assert [p.trade_id for p in final_prints] == [202]
        assert final_prints[0].product_id == "BTC-USD"
        assert feed.is_connected is False

    thread.join(timeout=3.0)
    assert not server_errors
    assert client_received_subscribe == [
        {"type": "subscribe", "product_ids": ["ETH-USD", "BTC-USD"], "channels": ["matches"]}
    ]
    assert client_pong_payloads == [b"ping-1"]
    assert client_close_received == [True]


def test_websocket_rejects_bad_accept_oversize_frame_and_handles_ssl_pending_and_gaps() -> None:
    # 1. Invalid Sec-WebSocket-Accept fails closed and closes socket
    c1, s1 = socket.socketpair()
    s1.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: wrong-accept-digest\r\n"
            "\r\n"
        ).encode("ascii")
    )
    s1.shutdown(socket.SHUT_WR)
    try:
        with pytest.raises(CoinbaseTradeFlowError, match="Sec-WebSocket-Accept"):
            CoinbaseWebSocketFeed(
                ["ETH-USD"],
                socket_connector=lambda _h, _p, _t: c1,
                key_bytes=b"0123456789abcdef",
            )
    finally:
        s1.close()

    # 2. Oversized frame (> 1_048_576 bytes) fails closed immediately from frame header
    key_b = b"0123456789abcdef"
    accept = compute_websocket_accept(base64.b64encode(key_b).decode("ascii"))
    c2, s2 = socket.socketpair()
    oversize_hdr = struct.pack("!BBQ", 0x81, 127, MAX_WS_FRAME_BYTES + 1)
    s2.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        ).encode("ascii")
        + oversize_hdr
    )
    s2.shutdown(socket.SHUT_WR)
    try:
        feed2 = CoinbaseWebSocketFeed(
            ["ETH-USD"],
            socket_connector=lambda _h, _p, _t: c2,
            key_bytes=key_b,
        )
        with pytest.raises(CoinbaseTradeFlowError, match="exceeds limit"):
            feed2.drain_events()
        assert feed2.is_connected is False
    finally:
        s2.close()

    # 3. SSLSocket.pending() > 0 works even when select.select reports socket not readable,
    # plus "error" message sets gap_detected=True and unexpected socket EOF sets gap_detected=True.
    c3, s3 = socket.socketpair()
    s3.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        ).encode("ascii")
    )

    class _MockPendingSSLSocket:
        def __init__(self, inner: socket.socket) -> None:
            self._inner = inner
            self._decrypted = bytearray()
            self.closed = False

        def fileno(self) -> int:
            return self._inner.fileno()

        def settimeout(self, timeout: float) -> None:
            self._inner.settimeout(timeout)

        def sendall(self, data: bytes) -> None:
            self._inner.sendall(data)

        def pending(self) -> int:
            return len(self._decrypted)

        def recv(self, n: int) -> bytes:
            if self._decrypted:
                out = bytes(self._decrypted[:n])
                del self._decrypted[:n]
                return out
            return self._inner.recv(n)

        def close(self) -> None:
            self.closed = True
            self._inner.close()

    err_frame = build_websocket_frame(
        0x1,
        json.dumps({"type": "error", "message": "rate limit"}).encode("utf-8"),
        masked=False,
    )
    match_frame = build_websocket_frame(
        0x1,
        json.dumps(
            {
                "type": "match",
                "product_id": "ETH-USD",
                "trade_id": 77,
                "price": "3010.00",
                "size": "0.10",
                "side": "sell",
                "time": "2026-11-18T11:20:00Z",
            }
        ).encode("utf-8"),
        masked=False,
    )
    mock_ssl = _MockPendingSSLSocket(c3)
    try:
        feed3 = CoinbaseWebSocketFeed(
            ["ETH-USD"],
            socket_connector=lambda _h, _p, _t: mock_ssl,
            key_bytes=key_b,
        )
        # Put decrypted bytes only in OpenSSL buffer while OS socketpair has 0 bytes readable
        mock_ssl._decrypted.extend(match_frame + err_frame)
        prints3, gap3 = feed3.drain_events()
        assert [p.trade_id for p in prints3] == [77]
        assert gap3 is True
        assert mock_ssl.closed is False

        # Now peer closes unexpectedly -> EOF sets gap_detected=True and closes feed socket
        s3.close()
        prints_eof, gap_eof = feed3.drain_events()
        assert prints_eof == ()
        assert gap_eof is True
        assert mock_ssl.closed is True
        assert feed3.is_connected is False
    finally:
        s3.close()
        if not mock_ssl.closed:
            mock_ssl.close()


def test_websocket_tls_dns_ssrf_tun_and_ipv4_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class _FakeSock:
        def __init__(self, af: int, socktype: int, proto: int) -> None:
            self.af = af
            self.timeout: float | None = None

        def settimeout(self, t: float) -> None:
            self.timeout = t

        def connect(self, sockaddr: tuple[Any, ...]) -> None:
            events.append(f"connect:{self.af}:{sockaddr[0]}")

        def setsockopt(self, _lvl: int, _opt: int, _val: int) -> None:
            return None

        def close(self) -> None:
            events.append(f"close:{self.af}")

    class _FakeCtx:
        def wrap_socket(self, sock: _FakeSock, *, server_hostname: str) -> _FakeSock:
            assert server_hostname == "ws-feed.exchange.coinbase.com"
            events.append(f"wrap:{sock.af}")
            if sock.af == socket.AF_INET6:
                raise ssl.SSLEOFError(8, "ipv6 eof")
            return sock

    def _v6_then_v4(_h: str, _p: int, family: int = 0, type: int = 0) -> list[Any]:
        del type
        if family == socket.AF_INET:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.66", 443))]
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::10", 443, 0, 0))]

    monkeypatch.setattr(
        "oasis_agent.cross_venue.coinbase_trade_flow.socket.getaddrinfo", _v6_then_v4
    )
    monkeypatch.setattr("oasis_agent.cross_venue.coinbase_trade_flow.socket.socket", _FakeSock)
    monkeypatch.setattr(
        "oasis_agent.cross_venue.coinbase_trade_flow.ssl.create_default_context",
        lambda **_k: _FakeCtx(),
    )

    wrapped = _connect_tls_ipv4_preferred("ws-feed.exchange.coinbase.com", 443, 3.0)
    assert isinstance(wrapped, _FakeSock)
    assert wrapped.af == socket.AF_INET
    assert events == [
        f"connect:{socket.AF_INET6}:2606:4700::10",
        f"wrap:{socket.AF_INET6}",
        f"close:{socket.AF_INET6}",
        f"connect:{socket.AF_INET}:198.18.0.66",
        f"wrap:{socket.AF_INET}",
    ]

    monkeypatch.setattr(
        "oasis_agent.cross_venue.coinbase_trade_flow.socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))],
    )
    with pytest.raises(CoinbaseTradeFlowError, match="non-public"):
        _connect_tls_ipv4_preferred("ws-feed.exchange.coinbase.com", 443, 3.0)


def test_fifo_maker_hedge_price_improvement_bbo_queue_drain_partial_gas_and_bid() -> None:
    cfg = MakerHedgeConfig(
        trade_notional_usdc=200_000_000,
        cex_maker_fee_bps=Decimal(0),
        adverse_selection_bps=Decimal("1.5"),
        min_quote_edge_bps=Decimal("3.0"),
        tick_improvement_bps=Decimal("5.0"),
    )
    gas_usd = Decimal("0.20")
    qty_1_eth = 10**18  # 1.0 ETH
    # Use small-spread book where 5 bps improvement places quote strictly inside spread
    # vs tight book where quote joins BBO.
    tracker = MakerHedgeLifecycleTracker(cfg)

    # Case 1: Price-improved Ask ($200 -> 0.08 ETH = $2500/ETH vs CEX 2505 / 2515).
    # 5 bps improvement places ask strictly inside spread (2513.7425 < 2515).
    dex_buy_0_08 = 8 * 10**16  # 0.08 ETH ($200 / 0.08 = $2500/ETH)
    book_wide = _make_book(
        "ETH-USD", bid="2505.00", ask="2515.00", bid_size="4.0", ask_size="4.0", sequence=1
    )
    step1 = tracker.step(
        "ETH-USD",
        current_book=book_wide,
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=dex_buy_0_08,
        trade_notional_token=dex_buy_0_08,
        dex_sell_usdc_out=199_800_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert step1.quote.ask_active is True
    quoted_ask_1 = step1.quote.quoted_ask_usd
    assert quoted_ask_1 is not None
    assert quoted_ask_1 < book_wide.best_ask  # price improvement -> queue_ahead_ask == 0

    # Partial FIFO fill: 0.02 ETH (25% of 0.08 ETH) lifted at quoted_ask_1 (maker_side="sell"),
    # PLUS a print at quoted_ask_1 with maker_side="buy" (must be IGNORED for resting ask!).
    # Note book_next does NOT cross (2506 / 2515), so strict_fills == (), touch_lift_fills == ().
    book_t2 = _make_book(
        "ETH-USD", bid="2506.00", ask="2515.00", bid_size="4.0", ask_size="4.0", sequence=2
    )
    prints_t2 = (
        CoinbaseTradePrint(
            product_id="ETH-USD",
            trade_id=1001,
            price=quoted_ask_1,
            size=Decimal("0.05"),
            maker_side="buy",  # bid hit, NOT ask lift -> ignored!
            traded_at_utc="2026-11-18T11:25:00Z",
        ),
        CoinbaseTradePrint(
            product_id="ETH-USD",
            trade_id=1002,
            price=quoted_ask_1,
            size=Decimal("0.02"),
            maker_side="sell",  # ask lifted -> fills 0.02 / 0.08 = 0.25!
            traded_at_utc="2026-11-18T11:25:01Z",
        ),
    )
    step2 = tracker.step(
        "ETH-USD",
        current_book=book_t2,
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=dex_buy_0_08,
        trade_notional_token=dex_buy_0_08,
        dex_sell_usdc_out=199_800_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
        recent_trades=prints_t2,
    )
    assert step2.strict_fills == ()
    assert step2.touch_lift_fills == ()
    assert len(step2.fifo_fills) == 1
    f1 = step2.fifo_fills[0]
    assert f1.fill_classification == "trade_flow_fifo"
    assert f1.side == "ask_maker"
    assert f1.filled_qty_tokens == "0.02"
    assert f1.fill_ratio == pytest.approx(0.25, abs=1e-9)
    assert f1.matched_trade_ids == (1002,)
    assert f1.execution_eligible is False

    # Verify partial fill deducts FULL gas_usd ($0.20), NOT 25% ($0.05)!
    expected_cex_proceeds = quoted_ask_1 * Decimal("0.02")
    expected_dex_cost = Decimal("0.02") * Decimal(2500)  # $50.00
    expected_adv = expected_dex_cost * (Decimal("1.5") / Decimal(10_000))
    expected_profit = expected_cex_proceeds - expected_dex_cost - expected_adv - gas_usd
    assert f1.realized_net_profit_usd == pytest.approx(expected_profit, abs=Decimal("1e-10"))

    # Case 2: Joining BBO (tick_improvement_bps = 0 => queue_ahead = asks[0].size = 2.0)
    cfg_join = MakerHedgeConfig(
        trade_notional_usdc=200_000_000,
        cex_maker_fee_bps=Decimal(0),
        adverse_selection_bps=Decimal("1.5"),
        min_quote_edge_bps=Decimal("3.0"),
        tick_improvement_bps=Decimal(0),
    )
    tracker_join = MakerHedgeLifecycleTracker(cfg_join)
    book_join = _make_book(
        "ETH-USD", bid="2508.00", ask="2512.00", bid_size="2.0", ask_size="2.0", sequence=10
    )
    step_j1 = tracker_join.step(
        "ETH-USD",
        current_book=book_join,
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=dex_buy_0_08,
        trade_notional_token=dex_buy_0_08,
        dex_sell_usdc_out=201_200_000,  # $2515/ETH on DEX sell -> bid active too!
        token_decimals=18,
        gas_cost_usd=gas_usd,
    )
    assert step_j1.quote.quoted_ask_usd == Decimal("2512.00")
    assert step_j1.quote.quoted_bid_usd == Decimal("2508.00")
    assert step_j1.quote.ask_active is True
    assert step_j1.quote.bid_active is True

    # Insufficient queue drain (only 1.50 out of 2.00 ahead at 2512.00) -> 0 fills!
    step_j2 = tracker_join.step(
        "ETH-USD",
        current_book=book_join,
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=dex_buy_0_08,
        trade_notional_token=dex_buy_0_08,
        dex_sell_usdc_out=201_200_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
        recent_trades=(
            CoinbaseTradePrint(
                product_id="ETH-USD",
                trade_id=2001,
                price=Decimal("2512.00"),
                size=Decimal("1.50"),
                maker_side="sell",
                traded_at_utc="2026-11-18T11:26:00Z",
            ),
        ),
    )
    assert step_j2.fifo_fills == ()

    # Now 1.80 drains part of 2.00, then 0.25 drains last 0.20 + fills 0.05 of our 0.08,
    # then a sweep at 2512.50 fills remaining 0.03! Also duplicate trade_id=2001 ignored!
    step_j3 = tracker_join.step(
        "ETH-USD",
        current_book=book_join,
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=dex_buy_0_08,
        trade_notional_token=dex_buy_0_08,
        dex_sell_usdc_out=201_200_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
        recent_trades=(
            CoinbaseTradePrint(
                product_id="ETH-USD",
                trade_id=2001,
                price=Decimal("2512.00"),
                size=Decimal("1.50"),
                maker_side="sell",
                traded_at_utc="2026-11-18T11:26:00Z",
            ),
            CoinbaseTradePrint(
                product_id="ETH-USD",
                trade_id=2002,
                price=Decimal("2512.00"),
                size=Decimal("1.80"),
                maker_side="sell",
                traded_at_utc="2026-11-18T11:26:01Z",
            ),
            CoinbaseTradePrint(
                product_id="ETH-USD",
                trade_id=2003,
                price=Decimal("2512.00"),
                size=Decimal("0.25"),
                maker_side="sell",
                traded_at_utc="2026-11-18T11:26:02Z",
            ),
            CoinbaseTradePrint(
                product_id="ETH-USD",
                trade_id=2004,
                price=Decimal("2512.50"),
                size=Decimal("0.10"),
                maker_side="sell",
                traded_at_utc="2026-11-18T11:26:03Z",
            ),
        ),
    )
    assert len(step_j3.fifo_fills) == 1
    fj = step_j3.fifo_fills[0]
    assert fj.filled_qty_tokens == "0.08"
    assert fj.fill_ratio == pytest.approx(1.0, abs=1e-9)
    assert fj.matched_trade_ids == (2003, 2004)
    # Weighted print price = (0.05*2512.00 + 0.03*2512.50) / 0.08 = 2512.1875
    assert fj.next_book_price_usd == Decimal("2512.1875")

    # Case 3: Bid-maker FIFO fill on price < quoted_bid sweep + missing t+1 DEX hedge warning
    step_j4 = tracker_join.step(
        "ETH-USD",
        current_book=book_join,
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=dex_buy_0_08,
        trade_notional_token=dex_buy_0_08,
        dex_sell_usdc_out=201_200_000,
        token_decimals=18,
        gas_cost_usd=gas_usd,
        recent_trades=(
            CoinbaseTradePrint(
                product_id="ETH-USD",
                trade_id=2005,
                price=Decimal("2507.50"),  # strictly below bid 2508.00 -> clears queue & fills!
                size=Decimal("0.04"),
                maker_side="buy",
                traded_at_utc="2026-11-18T11:26:04Z",
            ),
        ),
    )
    assert len(step_j4.fifo_fills) == 1
    fbid = step_j4.fifo_fills[0]
    assert fbid.side == "bid_maker"
    assert fbid.filled_qty_tokens == "0.04"
    assert fbid.fill_ratio == pytest.approx(0.5, abs=1e-9)
    assert fbid.matched_trade_ids == (2005,)
    assert fbid.realized_net_profit_usd > Decimal(0)

    # Missing t+1 DEX hedge quote records explicit FIFO unhedged warning
    step_j5 = tracker_join.step(
        "ETH-USD",
        current_book=book_join,
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=None,
        trade_notional_token=qty_1_eth // 10,
        dex_sell_usdc_out=None,
        token_decimals=18,
        gas_cost_usd=gas_usd,
        recent_trades=(
            CoinbaseTradePrint(
                product_id="ETH-USD",
                trade_id=2006,
                price=Decimal("2513.00"),
                size=Decimal("0.05"),
                maker_side="sell",
                traded_at_utc="2026-11-18T11:26:05Z",
            ),
        ),
    )
    assert step_j5.fifo_fills == ()
    assert any(
        "fifo_ask_fill_missing_t1_dex_buy_hedge" in reason
        for reason in step_j5.unhedged_fill_reasons
    )

    summary = tracker_join.summary()
    assert summary["trade_flow_fifo_fills"] == 2
    assert summary["fifo_positive_fills"] == 2
    assert summary["fifo_mean_realized_net_bps"] is not None
    assert summary["fifo_total_net_profit_usd"] > 0.0
    assert summary["fifo_total_filled_notional_usd"] == pytest.approx(
        float(Decimal("0.08") * Decimal("2512.00") + Decimal("0.04") * Decimal("2508.00")),
        abs=1e-4,
    )
    assert summary["feed_gap_cancels"] == 0
    assert summary["execution_eligible"] is False
    assert json.dumps(summary)


def test_feed_gap_cancels_prior_resting_quote_and_rejects_bad_trades_input() -> None:
    cfg = MakerHedgeConfig(
        trade_notional_usdc=200_000_000,
        cex_maker_fee_bps=Decimal(0),
        adverse_selection_bps=Decimal("1.5"),
        min_quote_edge_bps=Decimal("3.0"),
        tick_improvement_bps=Decimal("1.0"),
    )
    tracker = MakerHedgeLifecycleTracker(cfg)
    dex_buy = 8 * 10**16
    book1 = _make_book("ETH-USD", bid="2505.00", ask="2515.00")
    step1 = tracker.step(
        "ETH-USD",
        current_book=book1,
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=dex_buy,
        trade_notional_token=dex_buy,
        dex_sell_usdc_out=199_800_000,
        token_decimals=18,
        gas_cost_usd=Decimal("0.04"),
    )
    assert step1.quote.ask_active is True
    assert "ETH-USD" in tracker.resting_quotes()

    # Crossed book + matching prints MUST NOT emit fills when feed_gap_detected=True
    crossed_book = _make_book("ETH-USD", bid="2520.00", ask="2522.00")
    step2 = tracker.step(
        "ETH-USD",
        current_book=crossed_book,
        trade_notional_usdc=200_000_000,
        dex_buy_token_out=dex_buy,
        trade_notional_token=dex_buy,
        dex_sell_usdc_out=199_800_000,
        token_decimals=18,
        gas_cost_usd=Decimal("0.04"),
        recent_trades=(
            CoinbaseTradePrint(
                product_id="ETH-USD",
                trade_id=9001,
                price=Decimal("2520.00"),
                size=Decimal("1.0"),
                maker_side="sell",
                traded_at_utc="2026-11-18T11:30:00Z",
            ),
        ),
        feed_gap_detected=True,
    )
    assert step2.fills == ()
    assert step2.fifo_fills == ()
    assert step2.strict_fills == ()
    assert "ETH-USD" not in tracker.resting_quotes()
    assert any("feed_gap_cancelled_prior_quote" in r for r in step2.unhedged_fill_reasons)
    assert tracker.summary()["feed_gap_cancels"] == 1
    assert tracker.summary()["trade_flow_fifo_fills"] == 0
    assert tracker.summary()["strict_cross_fills"] == 0

    with pytest.raises(MakerHedgeError, match="CoinbaseTradePrint"):
        tracker.step(
            "ETH-USD",
            current_book=book1,
            trade_notional_usdc=200_000_000,
            dex_buy_token_out=dex_buy,
            trade_notional_token=dex_buy,
            dex_sell_usdc_out=199_800_000,
            token_decimals=18,
            gas_cost_usd=Decimal("0.04"),
            recent_trades=["bad"],  # type: ignore[list-item]
        )


def test_end_to_end_ws_drain_to_fifo_maker_and_oversized_fragment_rejection() -> None:
    # Verify non-blocking empty drain + live WS match -> MakerHedgeLifecycleTracker FIFO fill
    key_b = bytes(range(16))
    accept = compute_websocket_accept(base64.b64encode(key_b).decode("ascii"))
    c_sock, s_sock = socket.socketpair()
    s_sock.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        ).encode("ascii")
    )

    cfg = MakerHedgeConfig(
        trade_notional_usdc=200_000_000,
        cex_maker_fee_bps=Decimal(0),
        adverse_selection_bps=Decimal("1.5"),
        min_quote_edge_bps=Decimal("3.0"),
        tick_improvement_bps=Decimal("5.0"),
    )
    tracker = MakerHedgeLifecycleTracker(cfg)
    dex_buy = 8 * 10**16
    book1 = _make_book("ETH-USD", bid="2505.00", ask="2515.00")

    with CoinbaseWebSocketFeed(
        ["ETH-USD"],
        socket_connector=lambda _h, _p, _t: c_sock,
        key_bytes=key_b,
    ) as feed:
        # Empty non-blocking drain returns immediately with no gap
        empty_prints, empty_gap = feed.drain_events()
        assert empty_prints == ()
        assert empty_gap is False

        s1 = tracker.step(
            "ETH-USD",
            current_book=book1,
            trade_notional_usdc=200_000_000,
            dex_buy_token_out=dex_buy,
            trade_notional_token=dex_buy,
            dex_sell_usdc_out=199_800_000,
            token_decimals=18,
            gas_cost_usd=Decimal("0.04"),
            recent_trades=empty_prints,
            feed_gap_detected=empty_gap,
        )
        assert s1.quote.quoted_ask_usd is not None

        # Server emits an aggressive print sweeping above s1.quote.quoted_ask_usd
        s_sock.sendall(
            build_websocket_frame(
                0x1,
                json.dumps(
                    {
                        "type": "match",
                        "product_id": "ETH-USD",
                        "trade_id": 4401,
                        "price": str(s1.quote.quoted_ask_usd + Decimal("0.50")),
                        "size": "0.10",
                        "side": "sell",
                        "time": "2026-11-18T11:35:00Z",
                    }
                ).encode("utf-8"),
                masked=False,
            )
        )
        ws_prints, ws_gap = feed.drain_events()
        assert ws_gap is False
        assert len(ws_prints) == 1

        # Next book also crosses strictly -> both fifo_fills and strict_fills populated separately
        crossed_book = _make_book(
            "ETH-USD",
            bid=str(s1.quote.quoted_ask_usd + Decimal("0.20")),
            ask=str(s1.quote.quoted_ask_usd + Decimal("1.00")),
        )
        s2 = tracker.step(
            "ETH-USD",
            current_book=crossed_book,
            trade_notional_usdc=200_000_000,
            dex_buy_token_out=dex_buy,
            trade_notional_token=dex_buy,
            dex_sell_usdc_out=199_800_000,
            token_decimals=18,
            gas_cost_usd=Decimal("0.04"),
            recent_trades=ws_prints,
            feed_gap_detected=ws_gap,
        )
        assert len(s2.fifo_fills) == 1
        assert len(s2.strict_fills) == 1
        assert len(s2.fills) == 2
        assert tracker.summary()["trade_flow_fifo_fills"] == 1
        assert tracker.summary()["strict_cross_fills"] == 1

    s_sock.close()

    # Fragmented WebSocket message exceeding max_frame_bytes fails closed
    c_frag, s_frag = socket.socketpair()
    s_frag.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        ).encode("ascii")
        + build_websocket_frame(0x1, b"x" * 70, masked=False, fin=False)
        + build_websocket_frame(0x0, b"y" * 70, masked=False, fin=True)
    )
    s_frag.shutdown(socket.SHUT_WR)
    try:
        feed_frag = CoinbaseWebSocketFeed(
            ["ETH-USD"],
            max_frame_bytes=100,
            socket_connector=lambda _h, _p, _t: c_frag,
            key_bytes=key_b,
        )
        with pytest.raises(CoinbaseTradeFlowError, match="fragmented message exceeds"):
            feed_frag.drain_events()
    finally:
        s_frag.close()
