"""Offline unit tests for ProxyHandler({}) bypass, dynamic CEX-DEX, lending cap, and rehearsal."""

import json
import socket
import urllib.request
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from oasis_agent.cross_venue.live_cex_dex_shadow import (
    CexDexTickEvaluation,
    CoinbaseBookCapacity,
    CoinbaseL2Book,
    CoinbaseL2Level,
    _default_https_get,
    build_synthetic_coinbase_l2_book,
    compute_coinbase_book_capacity_usdc,
    evaluate_dynamic_cex_dex_opportunity,
)
from oasis_agent.fastpath.lending_reader import (
    LendingReaderError,
    filter_and_cap_liquidation_candidates,
)
from oasis_agent.fastpath.lending_reader import (
    _default_https_transport as _lending_default_https_transport,
)
from oasis_agent.fastpath.liquidation import LiquidationCandidate
from oasis_agent.fastpath.multicall_universe import CBBTC
from oasis_agent.fastpath.multicall_universe import (
    _default_https_transport as _multicall_default_https_transport,
)
from oasis_agent.live.dual_rpc_probe import (
    _default_https_transport as _dual_default_https_transport,
)
from oasis_agent.models.common import canonical_json, normalize_address, sha256_hex
from oasis_agent.sniping.venues import USDC, WETH
from scripts import verify_profit_authority_rehearsal as rehearsal_cli


def _synth_addr(idx: int) -> str:
    return f"0x{idx:040x}"


def _synth_hash(tag: bytes) -> str:
    return "0x" + sha256_hex(tag)


class _FakeTLSSocket:
    def __init__(
        self,
        *,
        response_body: bytes,
        records: dict[str, Any],
    ) -> None:
        self._body = response_body
        self._records = records
        self._sent = bytearray()
        self._closed = False

    def settimeout(self, timeout: float | None) -> None:
        self._records.setdefault("timeouts", []).append(timeout)

    def bind(self, address: tuple[str, int]) -> None:
        self._records["bound"] = address

    def connect(self, sockaddr: tuple[Any, ...]) -> None:
        self._records.setdefault("connects", []).append(sockaddr)

    def setsockopt(self, level: int, optname: int, value: int) -> None:
        self._records.setdefault("sockopts", []).append((level, optname, value))

    def sendall(self, data: bytes) -> None:
        self._sent.extend(data)
        self._records["sent"] = bytes(self._sent)

    def makefile(self, mode: str = "rb", *_args: object, **_kwargs: object) -> BytesIO:
        assert "r" in mode
        http_payload = (
            b"HTTP/1.1 200 OK\r\n"
            + f"Content-Length: {len(self._body)}\r\n".encode("ascii")
            + b"Content-Type: application/json\r\n"
            + b"Connection: close\r\n\r\n"
            + self._body
        )
        return BytesIO(http_payload)

    def close(self) -> None:
        self._closed = True


def _install_fake_direct_tls(
    monkeypatch: pytest.MonkeyPatch,
    module_path: str,
    *,
    response_body: bytes,
) -> dict[str, Any]:
    records: dict[str, Any] = {}
    fake_ip = "93.184.216.34"

    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:7890", "http": "http://127.0.0.1:7890"},
    )
    monkeypatch.setattr(
        f"{module_path}.socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (fake_ip, 443))],
    )

    def _fake_socket(
        family: int = socket.AF_INET,
        socktype: int = socket.SOCK_STREAM,
        proto: int = 0,
    ) -> _FakeTLSSocket:
        records.setdefault("families", []).append(family)
        return _FakeTLSSocket(response_body=response_body, records=records)

    class _FakeSSLContext:
        def wrap_socket(self, sock: _FakeTLSSocket, *, server_hostname: str) -> _FakeTLSSocket:
            records.setdefault("server_hostnames", []).append(server_hostname)
            return sock

    monkeypatch.setattr(f"{module_path}.socket.socket", _fake_socket)
    monkeypatch.setattr(
        f"{module_path}.ssl.create_default_context",
        lambda **_k: _FakeSSLContext(),
    )
    return records


def test_proxy_handler_bypasses_macos_system_proxy_in_all_four_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coinbase_body = b'{"sequence":1,"bids":[["3000.00","1.0",1]],"asks":[["3001.00","1.0",1]]}'
    cb_rec = _install_fake_direct_tls(
        monkeypatch,
        "oasis_agent.cross_venue.live_cex_dex_shadow",
        response_body=coinbase_body,
    )
    got_cb = _default_https_get(
        "https://api.exchange.coinbase.com/products/ETH-USD/book?level=2", 5.0
    )
    assert got_cb == coinbase_body
    assert cb_rec["connects"] == [("93.184.216.34", 443)]
    assert cb_rec["server_hostnames"] == ["api.exchange.coinbase.com"]
    assert cb_rec["sent"].startswith(b"GET /products/ETH-USD/book?level=2 HTTP/1.1")
    assert b"CONNECT " not in cb_rec["sent"]

    rpc_url = "https://primary-base-rpc.test"
    rpc_body = b'{"jsonrpc":"2.0","id":1,"result":"0x2105"}'
    for mod_path, transport_fn in (
        ("oasis_agent.fastpath.lending_reader", _lending_default_https_transport),
        ("oasis_agent.fastpath.multicall_universe", _multicall_default_https_transport),
        ("oasis_agent.live.dual_rpc_probe", _dual_default_https_transport),
    ):
        rec = _install_fake_direct_tls(monkeypatch, mod_path, response_body=rpc_body)
        # Confirm getproxies() really is injecting 127.0.0.1:7890 in this environment.
        assert urllib.request.getproxies()["https"] == "http://127.0.0.1:7890"
        got_rpc = transport_fn(rpc_url, b'{"jsonrpc":"2.0","id":1}', 5.0)
        assert got_rpc == rpc_body
        assert rec["connects"] == [("93.184.216.34", 443)]
        assert rec["server_hostnames"] == ["primary-base-rpc.test"]
        assert rec["sent"].startswith(b"POST / HTTP/1.1")
        assert b"CONNECT " not in rec["sent"]


def test_compute_coinbase_book_capacity_and_backward_compatible_eval() -> None:
    legacy_eval = CexDexTickEvaluation(
        product_id="ETH-USD",
        token_symbol="WETH",
        status="degraded",
        error_reason="offline",
        cex_taker_fee_bps=3.5,
        latency_haircut_bps=1.5,
        gas_cost_usd=0.05,
        best_bid_usd=None,
        best_ask_usd=None,
        cex_mid_usd=None,
        book_sequence=None,
        book_sha256=None,
        buy_dex_sell_cex=None,
        buy_cex_sell_dex=None,
        best_direction=None,
        best_net_bps=None,
        best_raw_spread_bps=None,
        maker_zero_fee_net_bps=None,
    )
    assert legacy_eval.selected_notional_usdc is None
    assert legacy_eval.book_capacity_usdc is None
    assert legacy_eval.to_dict()["selected_notional_usdc"] is None
    assert legacy_eval.to_dict()["book_capacity_usdc"] is None

    book = CoinbaseL2Book(
        product_id="ETH-USD",
        bids=(
            CoinbaseL2Level(price=Decimal("3000.00"), size=Decimal("0.5")),  # $1500 (within 50 bps)
            CoinbaseL2Level(price=Decimal("2990.00"), size=Decimal("0.2")),  # $598  (within 50 bps)
            CoinbaseL2Level(price=Decimal("2900.00"), size=Decimal("10.0")),  # outside 50 bps
        ),
        asks=(
            CoinbaseL2Level(price=Decimal("3001.00"), size=Decimal("0.4")),  # $1200.40 (within)
            CoinbaseL2Level(price=Decimal("3050.00"), size=Decimal("10.0")),  # outside 50 bps
        ),
        sequence=42,
        retrieved_at_utc="2026-11-18T00:00:00+00:00",
        raw_sha256=sha256_hex(b"eth-cap-book"),
        book_level=2,
    )
    cap = compute_coinbase_book_capacity_usdc(book, max_slippage_bps=Decimal(50))
    assert isinstance(cap, CoinbaseBookCapacity)
    assert cap.bid_capacity_usdc == 2_098_000_000
    assert cap.ask_capacity_usdc == 1_200_400_000
    assert cap.balanced_capacity_usdc == 1_200_400_000
    assert cap.max_directional_capacity_usdc == 2_098_000_000

    with pytest.raises(ValueError, match="max_slippage_bps"):
        compute_coinbase_book_capacity_usdc(book, max_slippage_bps=Decimal(10_000))


def test_evaluate_dynamic_cex_dex_btc_l1_auto_sizes_down_to_book_capacity() -> None:
    btc_l1 = CoinbaseL2Book(
        product_id="BTC-USD",
        bids=(CoinbaseL2Level(price=Decimal("90000.00"), size=Decimal("0.0020")),),  # $180.00
        asks=(CoinbaseL2Level(price=Decimal("90010.00"), size=Decimal("0.0020")),),  # $180.02
        sequence=101,
        retrieved_at_utc="2026-11-18T00:00:00+00:00",
        raw_sha256=sha256_hex(b"btc-l1-180"),
        book_level=1,
    )

    seen_notionals: list[int] = []

    def _quote_dex(notional_usdc: int) -> tuple[int | None, int, int | None]:
        seen_notionals.append(notional_usdc)
        # DEX is 40 bps cheaper than CEX best bid ($89,640/BTC): buy_dex_sell_cex wins!
        notional_usd = Decimal(notional_usdc) / Decimal(1_000_000)
        dex_price = Decimal("89640.00")
        btc_out = int((notional_usd / dex_price) * Decimal(10**8))
        sell_usdc_out = int((Decimal(btc_out) / Decimal(10**8)) * Decimal("89600.00") * 1_000_000)
        return btc_out, btc_out, sell_usdc_out

    ev = evaluate_dynamic_cex_dex_opportunity(
        product_or_symbol="BTC-USD",
        book=btc_l1,
        min_notional_usdc=10_000_000,
        max_notional_usdc=1_000_000_000,
        quote_dex_for_usdc_fn=_quote_dex,
        gas_cost_usd=Decimal("0.02"),
    )
    assert seen_notionals
    assert max(seen_notionals) <= 180_020_000
    assert ev.status == "ok"
    assert ev.book_level == 1
    assert ev.best_direction == "buy_dex_sell_cex"
    assert ev.selected_notional_usdc is not None
    assert 10_000_000 <= ev.selected_notional_usdc <= 180_000_000
    assert ev.book_capacity_usdc == 180_020_000
    assert ev.buy_dex_sell_cex is not None
    assert ev.buy_dex_sell_cex.feasible is True
    assert ev.buy_dex_sell_cex.net_profit_usd is not None
    assert ev.buy_dex_sell_cex.net_profit_usd > 0
    assert ev.execution_eligible is False
    payload = ev.to_dict()
    assert payload["selected_notional_usdc"] == ev.selected_notional_usdc
    assert payload["book_capacity_usdc"] == 180_020_000


def test_evaluate_dynamic_cex_dex_deep_eth_scales_to_convex_optimum_and_handles_edge_cases() -> (
    None
):
    deep_eth = build_synthetic_coinbase_l2_book(
        "ETH-USD",
        mid_price=Decimal("3000.00"),
        spread_bps=Decimal("1.0"),
        step_bps=Decimal("1.0"),
        level_size=Decimal("15.0"),
        levels=5,
    )

    # Convex impact vs fixed gas ($0.50): gross spread starts at +45 bps, minus 5 bps CEX/latency
    # costs = +40 bps net before impact, decaying linearly at 10 bps per $1,000 notional.
    # Profit(N) ≈ N * (0.0040 - N / 1_000_000) - 0.50 -> unique interior optimum at N* = $2,000!
    def _convex_dex_quote(notional_usdc: int) -> tuple[int | None, int, int | None]:
        n_usd = Decimal(notional_usdc) / Decimal(1_000_000)
        dex_price = Decimal("2986.50") * (Decimal(1) + (n_usd / Decimal(1_000_000)))
        eth_out_wei = int((n_usd / dex_price) * Decimal(10**18))
        sell_usdc_out = int((Decimal(eth_out_wei) / Decimal(10**18)) * Decimal("2980.00") * 10**6)
        return eth_out_wei, eth_out_wei, sell_usdc_out

    ev = evaluate_dynamic_cex_dex_opportunity(
        product_or_symbol="ETH-USD",
        book=deep_eth,
        min_notional_usdc=10_000_000,
        max_notional_usdc=5_000_000_000,
        quote_dex_for_usdc_fn=_convex_dex_quote,
        gas_cost_usd=Decimal("0.50"),
    )
    assert ev.status == "ok"
    assert ev.best_direction == "buy_dex_sell_cex"
    assert ev.selected_notional_usdc is not None
    # True interior peak ~$2,000 lies strictly between the 35% ($1,750) and 50% ($2,500) knots.
    assert 1_850_000_000 <= ev.selected_notional_usdc <= 2_150_000_000
    assert ev.buy_dex_sell_cex is not None
    assert ev.buy_dex_sell_cex.net_profit_usd is not None
    assert ev.buy_dex_sell_cex.net_profit_usd > 3.30

    # Insufficient capacity (< min_notional_usdc) must short-circuit without calling DEX quoter.
    calls = 0

    def _never_called(_n: int) -> tuple[int | None, int, int | None]:
        nonlocal calls
        calls += 1
        return None, 0, None

    tiny_book = CoinbaseL2Book(
        product_id="ETH-USD",
        bids=(CoinbaseL2Level(price=Decimal("3000.00"), size=Decimal("0.01")),),  # $30
        asks=(CoinbaseL2Level(price=Decimal("3001.00"), size=Decimal("0.01")),),  # $30.01
        sequence=2,
        retrieved_at_utc="2026-11-18T00:00:00+00:00",
        raw_sha256=sha256_hex(b"tiny-eth"),
        book_level=1,
    )
    insuff = evaluate_dynamic_cex_dex_opportunity(
        product_or_symbol="ETH-USD",
        book=tiny_book,
        min_notional_usdc=100_000_000,
        max_notional_usdc=500_000_000,
        quote_dex_for_usdc_fn=_never_called,
        gas_cost_usd=Decimal("0.10"),
    )
    assert calls == 0
    assert insuff.status == "degraded"
    assert insuff.error_reason == "insufficient_capacity_usdc:30010000<100000000"
    assert insuff.selected_notional_usdc is None
    assert insuff.book_capacity_usdc == 30_010_000

    # Unprofitable across all sizes must pick highest net_profit_usd (smallest loss).
    unprof = evaluate_dynamic_cex_dex_opportunity(
        product_or_symbol="ETH-USD",
        book=deep_eth,
        min_notional_usdc=25_000_000,
        max_notional_usdc=500_000_000,
        quote_dex_for_usdc_fn=lambda n: (
            int((Decimal(n) / Decimal(1_000_000) / Decimal("3050.00")) * Decimal(10**18)),
            int((Decimal(n) / Decimal(1_000_000) / Decimal("3050.00")) * Decimal(10**18)),
            int(Decimal(n) * Decimal("0.98")),
        ),
        gas_cost_usd=Decimal("0.50"),
    )
    assert unprof.status == "ok"
    assert unprof.selected_notional_usdc == 25_000_000
    assert unprof.buy_dex_sell_cex is not None
    assert unprof.buy_dex_sell_cex.net_profit_usd is not None
    assert unprof.buy_dex_sell_cex.net_profit_usd < 0

    # All-degraded DEX quotes fall back cleanly to min_notional_usdc.
    all_deg = evaluate_dynamic_cex_dex_opportunity(
        product_or_symbol="ETH-USD",
        book=deep_eth,
        min_notional_usdc=25_000_000,
        max_notional_usdc=500_000_000,
        quote_dex_for_usdc_fn=lambda _n: (None, 0, None),
        gas_cost_usd=Decimal("0.10"),
    )
    assert all_deg.status == "degraded"
    assert all_deg.selected_notional_usdc == 25_000_000
    assert all_deg.book_capacity_usdc is not None


def _make_liq_candidate(
    *,
    debt_token: str = USDC,
    collateral_token: str = WETH,
    repay_debt_raw: int,
    seized_collateral_raw: int = 10**17,
    swap_out_debt_raw: int,
    flash_fee_debt_raw: int = 0,
    gas_cost_in_debt_raw: int = 1_000_000,
    borrower_idx: int = 1,
) -> LiquidationCandidate:
    net_profit = swap_out_debt_raw - repay_debt_raw - flash_fee_debt_raw - gas_cost_in_debt_raw
    return LiquidationCandidate(
        protocol="aave_v3",
        market_id=_synth_addr(0x100),
        borrower=_synth_addr(0x200 + borrower_idx),
        debt_token=normalize_address(debt_token),
        collateral_token=normalize_address(collateral_token),
        repay_debt_raw=repay_debt_raw,
        seized_collateral_raw=seized_collateral_raw,
        swap_pool_id=_synth_addr(0x300),
        swap_out_debt_raw=swap_out_debt_raw,
        flash_fee_debt_raw=flash_fee_debt_raw,
        gas_cost_in_debt_raw=gas_cost_in_debt_raw,
        net_profit_debt_raw=net_profit,
        health_factor_wad=900_000_000_000_000_000,
        block_number=58_900_000,
        block_hash=_synth_hash(b"liq-block"),
        execution_eligible=False,
    )


def test_filter_and_cap_liquidation_candidates_headroom_and_dust_rules() -> None:
    usdc_dust = _make_liq_candidate(
        repay_debt_raw=5_000_000,  # $5 (< $10 default dust)
        swap_out_debt_raw=7_000_000,
        gas_cost_in_debt_raw=500_000,
        borrower_idx=1,
    )
    usdc_c1 = _make_liq_candidate(
        repay_debt_raw=300_000_000,  # $300
        seized_collateral_raw=105_000_000_000_000_000,
        swap_out_debt_raw=312_000_000,
        flash_fee_debt_raw=1_500_000,
        gas_cost_in_debt_raw=1_000_000,
        borrower_idx=2,
    )
    usdc_c2 = _make_liq_candidate(
        repay_debt_raw=300_000_000,  # $300 -> will cap to $150 remaining headroom
        seized_collateral_raw=105_000_000_000_000_000,
        swap_out_debt_raw=312_000_000,
        flash_fee_debt_raw=1_500_000,
        gas_cost_in_debt_raw=1_000_000,
        borrower_idx=3,
    )
    usdc_c3 = _make_liq_candidate(
        repay_debt_raw=100_000_000,  # $100 -> zero remaining headroom
        swap_out_debt_raw=105_000_000,
        gas_cost_in_debt_raw=1_000_000,
        borrower_idx=4,
    )

    # Zero / exhausted headroom drops all candidates.
    assert (
        filter_and_cap_liquidation_candidates(
            [usdc_c1, usdc_c2],
            max_repay_usd_micros=0,
        )
        == ()
    )
    assert (
        filter_and_cap_liquidation_candidates(
            [usdc_c1],
            max_repay_usd_micros=9_000_000,
            min_repay_usd_micros=10_000_000,
        )
        == ()
    )

    # Dust skipped without consuming headroom; c1 kept at $300; c2 capped to $150; c3 dropped.
    capped = filter_and_cap_liquidation_candidates(
        [usdc_dust, usdc_c1, usdc_c2, usdc_c3],
        max_repay_usd_micros=450_000_000,
        min_repay_usd_micros=10_000_000,
    )
    assert len(capped) == 2
    assert capped[0] == usdc_c1
    assert capped[1].repay_debt_raw == 150_000_000
    assert capped[1].seized_collateral_raw == 52_500_000_000_000_000
    assert capped[1].swap_out_debt_raw == 156_000_000
    assert capped[1].flash_fee_debt_raw == 750_000
    assert capped[1].gas_cost_in_debt_raw == 1_000_000
    assert capped[1].net_profit_debt_raw == 156_000_000 - 150_000_000 - 750_000 - 1_000_000
    assert capped[1].execution_eligible is False

    # Fixed gas wiping out profit after capping must discard the candidate.
    high_gas_cand = _make_liq_candidate(
        repay_debt_raw=1_000_000_000,  # $1,000, 2% gross = $20, $5 gas -> $15 net
        swap_out_debt_raw=1_020_000_000,
        gas_cost_in_debt_raw=5_000_000,
    )
    assert (
        filter_and_cap_liquidation_candidates(
            [high_gas_cand],
            max_repay_usd_micros=200_000_000,  # 2% of $200 = $4 < $5 fixed gas
        )
        == ()
    )

    # Non-USDC debt tokens use explicit pricing and fail closed when unpriced.
    weth_cand = _make_liq_candidate(
        debt_token=WETH,
        collateral_token=USDC,
        repay_debt_raw=5 * 10**17,  # 0.5 WETH = $1,500 at $3,000/WETH
        seized_collateral_raw=1_575 * 10**6,
        swap_out_debt_raw=525 * 10**15,
        gas_cost_in_debt_raw=1 * 10**15,
    )
    cbbtc_cand = _make_liq_candidate(
        debt_token=CBBTC,
        collateral_token=USDC,
        repay_debt_raw=10_000_000,  # 0.1 cbBTC = $9,000 at $90,000/BTC
        seized_collateral_raw=9_450 * 10**6,
        swap_out_debt_raw=10_500_000,
        gas_cost_in_debt_raw=1_000,
    )
    # Unpriced WETH / cbBTC must be skipped when no price map is provided.
    assert (
        filter_and_cap_liquidation_candidates([weth_cand], max_repay_usd_micros=500_000_000) == ()
    )

    priced_weth = filter_and_cap_liquidation_candidates(
        [weth_cand, cbbtc_cand],
        max_repay_usd_micros=500_000_000,
        debt_token_prices_usd_micros={WETH: 3_000_000_000},
    )
    assert len(priced_weth) == 1
    assert priced_weth[0].debt_token == normalize_address(WETH)
    assert priced_weth[0].repay_debt_raw == (500_000_000 * 10**18) // 3_000_000_000
    assert priced_weth[0].net_profit_debt_raw > 0

    with pytest.raises(LendingReaderError, match="must be positive"):
        filter_and_cap_liquidation_candidates(
            [usdc_c1],
            max_repay_usd_micros=100_000_000,
            debt_token_prices_usd_micros={WETH: 0},
        )


def test_verify_profit_authority_rehearsal_end_to_end_and_out_guards(tmp_path: Path) -> None:
    report = rehearsal_cli.run_rehearsal()
    assert report["schema_version"] == "oasis.profit-authority-rehearsal.v1"
    assert report["passed"] is True
    assert report["execution_eligible"] is False
    assert report["keyless_ephemeral"] is True
    assert report["ephemeral_dirs_cleaned"] is True
    assert report["network_calls"] == 0

    body_without_sha = {k: v for k, v in report.items() if k != "report_sha256"}
    assert report["report_sha256"] == sha256_hex(canonical_json(body_without_sha))

    charter = report["policies"]["charter_default_500_200"]
    assert charter["max_capital_usd_micros"] == 500_000_000
    assert charter["max_daily_loss_usd_micros"] == 200_000_000
    assert charter["gate8_status_proof_verified"] is True
    assert charter["in_limit_signatures_used"] == 1
    assert charter["over_daily_loss_refused"] is True
    assert charter["over_daily_loss_reason"] == "daily_loss_limit_reached"
    assert charter["first_failure_halted"] is False
    assert charter["two_failure_reconcile_halt_latched"] is True
    assert charter["halt_persisted_across_reload"] is True
    assert charter["restarted_daemon_refused_sign"] is True

    scaled = report["policies"]["scaled_config_5000_750"]
    assert scaled["max_capital_usd_micros"] == 5_000_000_000
    assert scaled["max_daily_loss_usd_micros"] == 750_000_000
    assert scaled["charter_default_blocked_250_loss"] is True
    assert scaled["scaled_1500_cap_250_loss_signed"] is True
    assert scaled["signatures_used"] == 1
    assert scaled["over_800_daily_loss_refused"] is True
    assert scaled["over_800_daily_loss_reason"] == "daily_loss_limit_reached"

    # CLI --out writes deterministic JSON, refuses overwrite, and refuses symlinks.
    out_file = tmp_path / "rehearsal.json"
    assert rehearsal_cli.main(["--out", str(out_file)]) == 0
    written = json.loads(out_file.read_text(encoding="utf-8"))
    assert written == report

    with pytest.raises(SystemExit) as exc_exists:
        rehearsal_cli.main(["--out", str(out_file)])
    assert exc_exists.value.code == 2

    sym_file = tmp_path / "sym_rehearsal.json"
    sym_file.symlink_to(out_file)
    with pytest.raises(SystemExit) as exc_sym:
        rehearsal_cli.main(["--out", str(sym_file)])
    assert exc_sym.value.code == 2
