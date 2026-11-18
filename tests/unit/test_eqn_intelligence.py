"""Unit tests for Equation News (`eqn.news`) intelligence, scoring, and discovery bridge."""

import json
from pathlib import Path

import httpx
import pytest

from oasis_agent.intelligence import (
    EQN_CHANNELS,
    CatalystDirection,
    CatalystKind,
    EqnBatchCapture,
    EqnClient,
    EqnDiscoveryProvider,
    EqnFetchError,
    EqnNewsItem,
    EqnStructuredData,
    EqnWsPushFrame,
    VerifiedSymbolEntry,
    VerifiedSymbolRegistry,
    classify_catalyst,
    parse_news_item,
    parse_news_payload,
    parse_ws_message,
    pct_to_bps,
    ratio_to_bps,
    usd_to_micros,
)
from oasis_agent.launch.discovery_slot import DiscoveryProvider, nominated_markets
from oasis_agent.launch.onchainos_source import SourceFailure
from oasis_agent.models.common import sha256_hex
from scripts.capture_eqn_catalysts import main as capture_main
from scripts.capture_eqn_catalysts import run_capture

BASE_CHAIN_ID = 8453
TOKEN_MMT = "0x" + "11" * 20
TOKEN_ALT = "0x" + "22" * 20
CODE_HASH_A = sha256_hex(b"mmt-runtime-bytecode")
CODE_HASH_B = sha256_hex(b"alt-runtime-bytecode")
NOW_MS = 1_794_981_100_000


def _real_oi_payload() -> dict[str, object]:
    return {
        "items": [
            {
                "id": "15677",
                "no": 15677,
                "path": "/monitor/15677",
                "published_ts": 1_794_981_084_709,
                "channels": ["oi"],
                "kind": "oi",
                "source": {"slug": "price_oi_move"},
                "i18n": {
                    "en": "MMTUSDT price +30.01% and OI +21.03% in 1h",
                    "zh": "MMTUSDT 1小时价格 +30.01%, 持仓 +21.03%",
                },
                "coins": [{"symbol": "MMT"}],
                "data": {
                    "signal": "price_oi_move",
                    "symbol": "MMTUSDT",
                    "base": "MMT",
                    "exchange": "binance",
                    "market": "perp",
                    "window": 3600,
                    "price_pct": 30.013,
                    "oi_pct": 21.028,
                    "direction": "up",
                    "oi_usd": 17041250.7,
                    "market_cap": 43718748.0,
                    "oi_mc_ratio": 0.4216,
                    "vol_24h_usd": 271831285.4,
                    "funding_rate_pct": -0.002871,
                    "funding_interval_h": 4,
                    "funding_next_ts": 1_794_988_800_000,
                },
            }
        ],
        "next_cursor": "cursor-15677",
    }


def _real_price_payload() -> dict[str, object]:
    return {
        "items": [
            {
                "id": "15680",
                "no": 15680,
                "path": "/monitor/15680",
                "published_ts": 1_794_981_090_000,
                "channels": ["pricechange"],
                "kind": "price",
                "source": {"slug": "fast_price_move"},
                "i18n": {
                    "en": "ALT dropped -6.47% in 10s",
                    "zh": "ALT 10秒下跌 -6.47%",
                },
                "coins": [{"symbol": "ALT"}],
                "data": {
                    "signal": "price_move",
                    "symbol": "ALTUSDT",
                    "base": "ALT",
                    "window": 10,
                    "pct": -6.47,
                    "price": 0.01445,
                    "base_price": 0.01545,
                    "liquidity_rank": 829,
                },
            }
        ],
        "next_cursor": None,
    }


def test_eqn_channel_catalog_covers_all_14_public_channels_and_is_frozen() -> None:
    expected = {
        "main": ("EquationNews", True),
        "pricechange": ("Price Moves", False),
        "major": ("Big Moves", False),
        "tier2": ("Tier-2 Listings", False),
        "tradfi": ("Equation TradFi", False),
        "oi": ("OI & Price Moves", False),
        "cz": ("CZ & Yi He Watch", False),
        "earn": ("Earn Alerts", False),
        "tier3": ("Tier-3 Listings", False),
        "binance": ("Binance Monitor", False),
        "korea": ("Korean Exchanges", False),
        "alpha": ("Binance Alpha", False),
        "hl": ("Hyperliquid", False),
        "tfmoves": ("US Stock Big Moves", False),
    }
    assert set(EQN_CHANNELS) == set(expected)
    assert len(EQN_CHANNELS) == 14
    for key, (name_en, is_main) in expected.items():
        spec = EQN_CHANNELS[key]
        assert spec.key == key
        assert spec.name_en == name_en
        assert spec.is_main is is_main
        assert spec.telegram.strip()
        assert spec.name_zh.strip()
    with pytest.raises(TypeError):
        EQN_CHANNELS["new"] = EQN_CHANNELS["main"]  # type: ignore[index]


def test_exact_decimal_conversions_and_zero_float_domain_models() -> None:
    assert pct_to_bps("0.29") == 29
    assert pct_to_bps("30.013") == 3001
    assert pct_to_bps("21.028") == 2103
    assert pct_to_bps("-6.47") == -647
    assert ratio_to_bps("0.4216") == 4216
    assert usd_to_micros("17041250.7") == 17_041_250_700_000

    with pytest.raises(TypeError):
        EqnStructuredData(pct_bps=12.5)  # type: ignore[arg-type]

    oi_bytes = json.dumps(_real_oi_payload()).encode("utf-8")
    oi_batch = parse_news_payload(oi_bytes, channel="oi", received_ts_ms=NOW_MS)
    assert isinstance(oi_batch, EqnBatchCapture)
    assert oi_batch.raw_sha256 == sha256_hex(oi_bytes)
    assert oi_batch.next_cursor == "cursor-15677"
    item = oi_batch.items[0]
    assert item.clock_skew_ms == 1_794_981_084_709 - NOW_MS
    assert item.data is not None
    assert type(item.data.price_pct_bps) is int
    assert item.data.price_pct_bps == 3001
    assert item.data.pct_bps == 3001
    assert item.data.oi_pct_bps == 2103
    assert item.data.oi_usd_micros == 17_041_250_700_000
    assert item.data.market_cap_usd_micros == 43_718_748_000_000
    assert item.data.oi_mc_bps == 4216
    assert item.data.vol_24h_usd_micros == 271_831_285_400_000
    assert item.data.funding_rate_bps == 0
    assert item.data.funding_rate_e8 == -2871
    assert item.data.funding_interval_h == 4
    assert item.data.funding_next_ts_ms == 1_794_988_800_000

    price_bytes = json.dumps(_real_price_payload()).encode("utf-8")
    price_batch = parse_news_payload(price_bytes, channel="pricechange", received_ts_ms=NOW_MS)
    pitem = price_batch.items[0]
    assert pitem.data is not None
    assert pitem.data.window_s == 10
    assert pitem.data.pct_bps == -647
    assert pitem.data.price_usd_micros == 14_450
    assert pitem.data.base_price_usd_micros == 15_450
    assert pitem.data.liquidity_rank == 829


def test_domain_models_reject_empty_ids_non_positive_ts_and_future_skew() -> None:
    digest = sha256_hex(b"item")
    with pytest.raises(ValueError):
        EqnNewsItem(
            item_id="",
            published_ts_ms=NOW_MS,
            received_ts_ms=NOW_MS,
            raw_sha256=digest,
        )
    with pytest.raises(ValueError):
        EqnNewsItem(
            item_id="1",
            published_ts_ms=0,
            received_ts_ms=NOW_MS,
            raw_sha256=digest,
        )
    with pytest.raises(ValueError):
        EqnNewsItem(
            item_id="1",
            published_ts_ms=NOW_MS + 6_000,
            received_ts_ms=NOW_MS,
            raw_sha256=digest,
            max_future_skew_ms=5_000,
        )
    with pytest.raises(ValueError):
        EqnWsPushFrame(
            frame_id="",
            source_name="EquationNews",
            news_title="Alert",
            coins_included=("BTC",),
            url="https://eqn.news/monitor/1",
            published_ts_ms=NOW_MS,
            received_ts_ms=NOW_MS,
            raw_sha256=digest,
        )


def test_eqn_client_fetch_news_mcp_ws_and_fail_closed_controls() -> None:
    with pytest.raises(EqnFetchError):
        EqnClient(rest_url="http://eqn.news/api/v1/news")
    with pytest.raises(EqnFetchError):
        EqnClient(rest_url="https://evil.example/api/v1/news")
    with pytest.raises(EqnFetchError):
        EqnClient(mcp_url="https://evil.example/public/mcp")

    seen_requests: list[tuple[str, str]] = []
    oi_raw = json.dumps(_real_oi_payload()).encode("utf-8")
    mcp_body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "result": {"content": [{"type": "text", "text": "MMT news"}], "isError": False},
        }
    ).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append((request.method, str(request.url)))
        if request.url.path == "/api/v1/news":
            return httpx.Response(200, content=oi_raw)
        if request.url.path == "/public/mcp":
            body = json.loads(request.content)
            assert body["jsonrpc"] == "2.0"
            assert body["method"] == "tools/call"
            assert body["params"]["name"] == "get_news"
            assert body["id"] == 7
            return httpx.Response(200, content=mcp_body)
        return httpx.Response(404)

    with EqnClient(max_requests=2, transport=httpx.MockTransport(handler)) as client:
        batch = client.fetch_news(channel="oi", limit=2, cursor="c1", received_ts_ms=NOW_MS)
        assert len(batch.items) == 1
        assert batch.raw_sha256 == sha256_hex(oi_raw)
        assert client.requests_remaining == 1

        mcp_res = client.call_mcp("get_news", {"id": "15677"}, request_id=7)
        assert mcp_res.raw_sha256 == sha256_hex(mcp_body)
        assert client.requests_remaining == 0

        with pytest.raises(EqnFetchError):
            client.fetch_news(channel="all", limit=10, received_ts_ms=NOW_MS)

    assert len(seen_requests) == 2
    assert "ch=oi" in seen_requests[0][1]
    assert "limit=2" in seen_requests[0][1]
    assert "cursor=c1" in seen_requests[0][1]

    def fail_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/news":
            return httpx.Response(503, content=b"unavailable")
        return httpx.Response(200, content=b"{bad json")

    with EqnClient(max_requests=2, transport=httpx.MockTransport(fail_handler)) as bad_client:
        with pytest.raises(EqnFetchError):
            bad_client.fetch_news(received_ts_ms=NOW_MS)
        assert bad_client.requests_used == 1
        with pytest.raises(EqnFetchError):
            bad_client.call_mcp("list_news", {"limit": 5}, request_id=1)
        assert bad_client.requests_used == 2
        with pytest.raises(EqnFetchError):
            bad_client.call_mcp("disallowed_tool", {})

    assert parse_ws_message("pong", NOW_MS) == "pong"
    assert parse_ws_message(b"pong", NOW_MS) == "pong"
    ws_frame = parse_ws_message(
        json.dumps(
            {
                "source_name": "EquationNews",
                "news_title": "Binance Will List MMT",
                "coins_included": ["mmt"],
                "url": "https://eqn.news/monitor/15699",
                "timestamp": NOW_MS - 100,
            }
        ),
        NOW_MS,
    )
    assert isinstance(ws_frame, EqnWsPushFrame)
    assert ws_frame.coins_included == ("MMT",)
    assert ws_frame.clock_skew_ms == -100

    with pytest.raises(ValueError):
        parse_ws_message(
            json.dumps(
                {
                    "source_name": "EquationNews",
                    "news_title": "Future Frame",
                    "coins_included": ["MMT"],
                    "url": "https://eqn.news/monitor/2",
                    "timestamp": NOW_MS + 10_000,
                }
            ),
            NOW_MS,
        )


def test_catalyst_classifier_prioritizes_security_freeze_and_covers_all_kinds() -> None:
    digest = sha256_hex(b"catalyst")

    def make_item(
        item_id: str,
        *,
        channels: list[str],
        kind: str = "news",
        title_en: str = "",
        title_zh: str = "",
        coins: list[str] | None = None,
        data: dict[str, object] | None = None,
    ) -> EqnNewsItem:
        raw: dict[str, object] = {
            "id": item_id,
            "published_ts": NOW_MS - 1_000,
            "channels": channels,
            "kind": kind,
            "i18n": {"en": title_en, "zh": title_zh},
            "coins": [{"symbol": s} for s in (coins or [])],
        }
        if data is not None:
            raw["data"] = data
        return parse_news_item(raw, received_ts_ms=NOW_MS, raw_sha256=digest)

    # Freeze overrides Tier-1 listing keywords in binance channel.
    delist_item = make_item(
        "f1",
        channels=["binance"],
        title_en="Binance will list XYZ and DELIST MMT due to bridge exploit",
        coins=["MMT"],
    )
    sig_freeze = classify_catalyst(delist_item)
    assert sig_freeze.kind is CatalystKind.PROTOCOL_INCIDENT_FREEZE
    assert sig_freeze.direction is CatalystDirection.RISK_FREEZE
    assert sig_freeze.risk_freeze is True
    assert sig_freeze.execution_eligible is False

    hack_zh = make_item(
        "f2",
        channels=["main"],
        title_zh="某协议遭黑客攻击并暂停交易",
        coins=["ALT"],
    )
    assert classify_catalyst(hack_zh).risk_freeze is True

    listing = make_item(
        "l1",
        channels=["korea"],
        title_en="Upbit adds KRW market for MMT",
        coins=["MMT"],
    )
    sig_listing = classify_catalyst(listing)
    assert sig_listing.kind is CatalystKind.TIER1_LISTING
    assert sig_listing.direction is CatalystDirection.BULLISH_CATALYST
    assert sig_listing.risk_freeze is False

    oi_item = parse_news_payload(
        json.dumps(_real_oi_payload()).encode("utf-8"),
        channel="oi",
        received_ts_ms=NOW_MS,
    ).items[0]
    sig_oi = classify_catalyst(oi_item)
    assert sig_oi.kind is CatalystKind.OI_FUNDING_CROWDING
    assert sig_oi.direction is CatalystDirection.BULLISH_CATALYST

    price_item = parse_news_payload(
        json.dumps(_real_price_payload()).encode("utf-8"),
        channel="pricechange",
        received_ts_ms=NOW_MS,
    ).items[0]
    sig_price = classify_catalyst(price_item)
    assert sig_price.kind is CatalystKind.FAST_PRICE_SHOCK
    assert sig_price.direction is CatalystDirection.BEARISH_ALERT

    tradfi_item = make_item(
        "t1",
        channels=["tfmoves"],
        title_en="NVDA plunge triggers Nasdaq circuit breaker",
        coins=["NVDA"],
    )
    sig_tradfi = classify_catalyst(tradfi_item)
    assert sig_tradfi.kind is CatalystKind.TRADFI_MACRO_SHOCK
    assert sig_tradfi.direction is CatalystDirection.BEARISH_ALERT

    alpha_item = make_item(
        "a1",
        channels=["alpha"],
        title_en="Binance Alpha points update",
        coins=["MMT"],
    )
    assert classify_catalyst(alpha_item).kind is CatalystKind.ALPHA_ECOSYSTEM_ALERT

    general_item = make_item("g1", channels=["main"], title_en="Weekly recap")
    assert classify_catalyst(general_item).kind is CatalystKind.GENERAL_INFO

    # Extreme negative funding triggers OI_FUNDING_CROWDING even with small OI move.
    funding_item = make_item(
        "oi_fund",
        channels=["oi"],
        kind="oi",
        title_en="Funding extreme negative",
        coins=["MMT"],
        data={"oi_pct": 1.0, "funding_rate_pct": -0.08, "oi_mc_ratio": 0.05},
    )
    assert classify_catalyst(funding_item).kind is CatalystKind.OI_FUNDING_CROWDING

    # Price move > 60s window does not trigger FAST_PRICE_SHOCK.
    slow_price = make_item(
        "p_slow",
        channels=["pricechange"],
        kind="price",
        title_en="Slow move",
        coins=["ALT"],
        data={"window": 120, "pct": 8.0},
    )
    assert classify_catalyst(slow_price).kind is CatalystKind.GENERAL_INFO


def test_discovery_bridge_enforces_registry_pinning_freshness_and_freeze() -> None:
    registry = VerifiedSymbolRegistry(
        [
            VerifiedSymbolEntry(
                chain_id=BASE_CHAIN_ID,
                symbol="MMT",
                token_address=TOKEN_MMT,
                code_sha256=CODE_HASH_A,
            ),
            VerifiedSymbolEntry(
                chain_id=BASE_CHAIN_ID,
                symbol="ALT",
                token_address=TOKEN_ALT,
                code_sha256=CODE_HASH_B,
            ),
        ]
    )
    digest = sha256_hex(b"bridge")
    items = (
        # Valid active listing for MMT + unregistered UNPINNED token.
        parse_news_item(
            {
                "id": "1",
                "published_ts": NOW_MS - 10_000,
                "channels": ["binance"],
                "i18n": {"en": "Binance Alpha launches MMT and UNPINNED"},
                "coins": [{"symbol": "MMT"}, {"symbol": "UNPINNED"}],
            },
            received_ts_ms=NOW_MS,
            raw_sha256=digest,
        ),
        # Another bullish item for ALT, shadowed by freeze item below!
        parse_news_item(
            {
                "id": "2",
                "published_ts": NOW_MS - 5_000,
                "channels": ["hl"],
                "i18n": {"en": "Hyperliquid launches ALT perp"},
                "coins": [{"symbol": "ALT"}],
            },
            received_ts_ms=NOW_MS,
            raw_sha256=digest,
        ),
        # Active security freeze for ALT -> must block ALT nomination completely!
        parse_news_item(
            {
                "id": "3",
                "published_ts": NOW_MS - 2_000,
                "channels": ["main"],
                "i18n": {"en": "ALT protocol exploited and halted"},
                "coins": [{"symbol": "ALT"}],
            },
            received_ts_ms=NOW_MS,
            raw_sha256=digest,
        ),
        # Stale item (> 60s max_age_ms) must be ignored.
        parse_news_item(
            {
                "id": "4",
                "published_ts": NOW_MS - 120_000,
                "channels": ["binance"],
                "i18n": {"en": "Old news"},
                "coins": [{"symbol": "OLDVERIFIED"}],
            },
            received_ts_ms=NOW_MS,
            raw_sha256=digest,
        ),
    )

    provider = EqnDiscoveryProvider(
        registry,
        items=items,
        max_age_ms=60_000,
        clock_ms=lambda: NOW_MS,
    )
    typed_slot: DiscoveryProvider = provider
    assert typed_slot.name == "eqn_news"
    addresses = typed_slot.nominate(BASE_CHAIN_ID)
    assert typed_slot.calls == 1
    assert len(addresses) == 1
    assert addresses[0].lower() == TOKEN_MMT
    assert provider.unmapped_symbols == ("UNPINNED",)
    assert provider.frozen_symbols == ("ALT",)
    assert provider.stale_item_ids == ("4",)

    # Nominated markets enter SpotAgent with all market metrics unknown.
    markets = nominated_markets(addresses, observed_at=NOW_MS // 1000)
    assert len(markets) == 1
    assert markets[0].liquidity_usd_micros is None
    assert markets[0].market_cap_usd_micros is None

    # Wrong chain returns empty nominations (records pinned symbols as unmapped on that chain).
    assert provider.nominate(1) == []

    # Client fetch failure raises SourceFailure per DiscoveryProvider contract.
    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with EqnClient(max_requests=1, transport=httpx.MockTransport(broken)) as client:
        live_provider = EqnDiscoveryProvider(registry, client=client, clock_ms=lambda: NOW_MS)
        with pytest.raises(SourceFailure):
            live_provider.nominate(BASE_CHAIN_ID)


def test_capture_eqn_catalysts_cli_replay_and_immutability(tmp_path: Path) -> None:
    input_path = tmp_path / "fixture.json"
    input_raw = json.dumps(_real_oi_payload()).encode("utf-8")
    input_path.write_bytes(input_raw)

    out_dir = tmp_path / "run1"
    exit_code = capture_main(
        [
            "--mode",
            "replay",
            "--channel",
            "oi",
            "--limit",
            "20",
            "--input",
            str(input_path),
            "--out",
            str(out_dir),
            "--received-ts-ms",
            str(NOW_MS),
        ]
    )
    assert exit_code == 0

    capture = json.loads((out_dir / "capture.json").read_text())
    catalysts = json.loads((out_dir / "catalysts.json").read_text())
    manifest = json.loads((out_dir / "manifest.json").read_text())
    assert capture["execution_eligible"] is False
    assert catalysts["execution_eligible"] is False
    assert manifest["execution_eligible"] is False
    assert manifest["raw_input_sha256"] == sha256_hex(input_raw)
    assert manifest["capture_sha256"] == sha256_hex((out_dir / "capture.json").read_bytes())
    assert manifest["catalysts_sha256"] == sha256_hex((out_dir / "catalysts.json").read_bytes())
    assert manifest["item_count"] == 1

    # Refuses to overwrite existing capture directory outputs.
    assert (
        capture_main(
            [
                "--mode",
                "replay",
                "--input",
                str(input_path),
                "--out",
                str(out_dir),
                "--received-ts-ms",
                str(NOW_MS),
            ]
        )
        == 2
    )

    # Public-live mode via injected client.
    def ok_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=input_raw)

    out_live = tmp_path / "run_live"
    with EqnClient(max_requests=1, transport=httpx.MockTransport(ok_handler)) as client:
        live_manifest = run_capture(
            mode="public-live",
            channel="oi",
            limit=5,
            out_dir=out_live,
            received_ts_ms=NOW_MS,
            client=client,
        )
    assert live_manifest["mode"] == "public-live"
    assert live_manifest["execution_eligible"] is False
