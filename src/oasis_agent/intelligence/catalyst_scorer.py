"""Deterministic classification of Equation News items into bounded catalyst signals.

EQN catalysts are intelligence/discovery inputs only (``execution_eligible = False``).
Security incidents, exploits, halts, and delistings always take highest priority and
emit ``PROTOCOL_INCIDENT_FREEZE`` (``risk_freeze = True``).
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from .eqn_catalog import EqnNewsItem, EqnStructuredData

FAST_SHOCK_MAX_WINDOW_S: Final[int] = 60
FAST_SHOCK_MIN_PCT_BPS: Final[int] = 300
OI_PCT_CROWDING_BPS: Final[int] = 500
OI_MC_CROWDING_BPS: Final[int] = 2_000
EXTREME_NEGATIVE_FUNDING_BPS: Final[int] = -5  # -0.05% per funding interval

TIER1_LISTING_CHANNELS: Final[frozenset[str]] = frozenset({"binance", "korea", "alpha", "hl"})
ALPHA_ECOSYSTEM_CHANNELS: Final[frozenset[str]] = frozenset(
    {"alpha", "cz", "earn", "tier2", "tier3", "binance", "korea", "hl"}
)
TRADFI_CHANNELS: Final[frozenset[str]] = frozenset({"tradfi", "tfmoves"})

_FREEZE_EN_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:delist|"
    r"\b(?:hack|hacked|hacker|hacks|exploit|exploited|exploits|exploiting|"
    r"reentrancy|drained|drain|breach|breached|compromise|compromised|"
    r"halt|halted|halting|suspend|suspended|suspends|suspension|"
    r"freeze|frozen|bankrupt|bankruptcy|insolvent|insolvency|"
    r"stolen|rugpull|rug\s+pull|vulnerability|security\s+incident|"
    r"flash\s*loan\s+attack|cyber\s*attack|protocol\s+attack|under\s+attack|"
    r"cease\s+trading|trading\s+halted|investment\s+warning|emergency\s+pause)\b)",
    re.IGNORECASE,
)
_FREEZE_ZH_KEYWORDS: Final[tuple[str, ...]] = (
    "下架",
    "黑客",
    "被盗",
    "漏洞",
    "攻击",
    "暂停交易",
    "暂停充提",
    "停牌",
    "冻结",
    "跑路",
    "破产",
    "投资警示",
)

_LISTING_EN_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:list|lists|listed|listing|launch|launches|launched|launchpool|"
    r"megadrop|spot\s+trading|perpetual|perps?|futures|krw\s+market|"
    r"usdt\s+market|adds|new\s+pair|new\s+contract|trading\s+starts?)\b",
    re.IGNORECASE,
)
_LISTING_ZH_KEYWORDS: Final[tuple[str, ...]] = (
    "上线",
    "上新",
    "现货",
    "永续合约",
    "新币",
    "开放交易",
)

_TRADFI_BEARISH_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:crash|plunge|selloff|sell-off|drop|drops|fall|falls|recession|"
    r"tariff|tariffs|hike|emergency\s+cut|halt|circuit\s+breaker|downgrade)\b",
    re.IGNORECASE,
)
_TRADFI_BULLISH_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:rally|surge|soar|beat|beats|approval|approved|cut\s+rates|stimulus)\b",
    re.IGNORECASE,
)


class CatalystKind(StrEnum):
    TIER1_LISTING = "tier1_listing"
    OI_FUNDING_CROWDING = "oi_funding_crowding"
    FAST_PRICE_SHOCK = "fast_price_shock"
    PROTOCOL_INCIDENT_FREEZE = "protocol_incident_freeze"
    TRADFI_MACRO_SHOCK = "tradfi_macro_shock"
    ALPHA_ECOSYSTEM_ALERT = "alpha_ecosystem_alert"
    GENERAL_INFO = "general_info"


class CatalystDirection(StrEnum):
    BULLISH_CATALYST = "bullish_catalyst"
    BEARISH_ALERT = "bearish_alert"
    NEUTRAL_OBSERVE = "neutral_observe"
    RISK_FREEZE = "risk_freeze"


@dataclass(frozen=True, slots=True)
class CatalystSignal:
    """Immutable classified catalyst signal; never authorizes trade execution."""

    item_id: str
    kind: CatalystKind
    direction: CatalystDirection
    risk_freeze: bool
    symbols: tuple[str, ...]
    channels: tuple[str, ...]
    published_ts_ms: int
    received_ts_ms: int
    raw_sha256: str
    confidence_bps: int
    reason: str
    execution_eligible: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.item_id, str) or not self.item_id.strip():
            raise ValueError("item_id must be non-empty")
        if not isinstance(self.kind, CatalystKind):
            raise TypeError("kind must be CatalystKind")
        if not isinstance(self.direction, CatalystDirection):
            raise TypeError("direction must be CatalystDirection")
        if type(self.risk_freeze) is not bool:
            raise TypeError("risk_freeze must be bool")
        if self.execution_eligible:
            raise ValueError("EQN catalyst signals are never execution_eligible")
        if self.risk_freeze and self.direction is not CatalystDirection.RISK_FREEZE:
            raise ValueError("risk_freeze=True requires RISK_FREEZE direction")
        if not self.risk_freeze and self.direction is CatalystDirection.RISK_FREEZE:
            raise ValueError("RISK_FREEZE direction requires risk_freeze=True")
        if type(self.confidence_bps) is not int or not 0 <= self.confidence_bps <= 10_000:
            raise ValueError("confidence_bps must be an integer in [0, 10000]")


def _has_freeze_signal(text: str, item: EqnNewsItem) -> bool:
    if _FREEZE_EN_RE.search(text) is not None:
        return True
    if any(keyword in text for keyword in _FREEZE_ZH_KEYWORDS):
        return True
    slug = item.source_slug.lower()
    return "delist" in slug or "incident" in slug or "halt" in slug


def _has_listing_signal(text: str, item: EqnNewsItem) -> bool:
    if _LISTING_EN_RE.search(text) is not None:
        return True
    if any(keyword in text for keyword in _LISTING_ZH_KEYWORDS):
        return True
    slug = item.source_slug.lower()
    return any(tok in slug for tok in ("listing", "launch", "alpha"))


def _classify_oi_direction(data: EqnStructuredData) -> CatalystDirection:
    oi_pct = data.oi_pct_bps or 0
    price_pct = data.price_pct_bps if data.price_pct_bps is not None else (data.pct_bps or 0)
    funding = data.funding_rate_bps if data.funding_rate_bps is not None else 0
    direction = data.direction.lower()

    if oi_pct >= OI_PCT_CROWDING_BPS:
        if price_pct > 0 or direction == "up" or funding <= EXTREME_NEGATIVE_FUNDING_BPS:
            return CatalystDirection.BULLISH_CATALYST
        if price_pct < 0 or direction == "down":
            return CatalystDirection.BEARISH_ALERT
    if oi_pct <= -OI_PCT_CROWDING_BPS and (price_pct < 0 or direction == "down"):
        return CatalystDirection.BEARISH_ALERT
    if funding <= EXTREME_NEGATIVE_FUNDING_BPS:
        if price_pct <= -FAST_SHOCK_MIN_PCT_BPS:
            return CatalystDirection.BEARISH_ALERT
        return CatalystDirection.BULLISH_CATALYST
    return CatalystDirection.NEUTRAL_OBSERVE


def _is_oi_crowding(item: EqnNewsItem) -> bool:
    data = item.data
    if data is None:
        return False
    if item.kind != "oi" and "oi" not in item.channels and "oi" not in item.source_slug.lower():
        return False
    oi_pct = abs(data.oi_pct_bps) if data.oi_pct_bps is not None else 0
    oi_mc = data.oi_mc_bps if data.oi_mc_bps is not None else 0
    funding = data.funding_rate_bps if data.funding_rate_bps is not None else 0
    return (
        oi_pct >= OI_PCT_CROWDING_BPS
        or oi_mc >= OI_MC_CROWDING_BPS
        or funding <= EXTREME_NEGATIVE_FUNDING_BPS
    )


def _is_fast_price_shock(item: EqnNewsItem) -> bool:
    data = item.data
    if data is None or data.window_s is None or data.pct_bps is None:
        return False
    if item.kind != "price" and "pricechange" not in item.channels and "major" not in item.channels:
        return False
    return data.window_s <= FAST_SHOCK_MAX_WINDOW_S and abs(data.pct_bps) >= FAST_SHOCK_MIN_PCT_BPS


def classify_catalyst(item: EqnNewsItem) -> CatalystSignal:
    """Classify one validated ``EqnNewsItem`` deterministically with fail-closed freeze priority."""
    text = f"{item.title_en}\n{item.title_zh}".strip()
    channels_set = set(item.channels)

    def make_signal(
        kind: CatalystKind,
        direction: CatalystDirection,
        *,
        risk_freeze: bool = False,
        confidence_bps: int,
        reason: str,
    ) -> CatalystSignal:
        return CatalystSignal(
            item_id=item.item_id,
            kind=kind,
            direction=direction,
            risk_freeze=risk_freeze,
            symbols=item.coins,
            channels=item.channels,
            published_ts_ms=item.published_ts_ms,
            received_ts_ms=item.received_ts_ms,
            raw_sha256=item.raw_sha256,
            confidence_bps=confidence_bps,
            reason=reason,
            execution_eligible=False,
        )

    # 1. Security/hack/exploit/halt/delist ALWAYS wins over listing or price keywords.
    if _has_freeze_signal(text, item):
        return make_signal(
            CatalystKind.PROTOCOL_INCIDENT_FREEZE,
            CatalystDirection.RISK_FREEZE,
            risk_freeze=True,
            confidence_bps=9_500,
            reason="security_incident_or_delist_freeze",
        )

    # 2. TradFi / US stock macro shock for RWA & equity-perp session guards.
    if channels_set & TRADFI_CHANNELS or item.kind in TRADFI_CHANNELS:
        direction = CatalystDirection.NEUTRAL_OBSERVE
        if item.data is not None and item.data.pct_bps is not None:
            if item.data.pct_bps < 0:
                direction = CatalystDirection.BEARISH_ALERT
            elif item.data.pct_bps > 0:
                direction = CatalystDirection.BULLISH_CATALYST
        elif _TRADFI_BEARISH_RE.search(text) is not None:
            direction = CatalystDirection.BEARISH_ALERT
        elif _TRADFI_BULLISH_RE.search(text) is not None:
            direction = CatalystDirection.BULLISH_CATALYST
        return make_signal(
            CatalystKind.TRADFI_MACRO_SHOCK,
            direction,
            confidence_bps=8_000,
            reason="tradfi_macro_guard",
        )

    # 3. Tier-1 exchange / Korea / Binance Alpha / Hyperliquid listing & launch catalysts.
    if channels_set & TIER1_LISTING_CHANNELS and _has_listing_signal(text, item):
        return make_signal(
            CatalystKind.TIER1_LISTING,
            CatalystDirection.BULLISH_CATALYST,
            confidence_bps=8_500,
            reason="tier1_venue_listing_or_launch",
        )

    # 4. Fast price shock (<= 60s window and >= 3% move).
    if _is_fast_price_shock(item):
        assert item.data is not None and item.data.pct_bps is not None
        direction = (
            CatalystDirection.BULLISH_CATALYST
            if item.data.pct_bps > 0
            else CatalystDirection.BEARISH_ALERT
        )
        return make_signal(
            CatalystKind.FAST_PRICE_SHOCK,
            direction,
            confidence_bps=7_500,
            reason="fast_price_shock_le_60s",
        )

    # 5. OI squeezing / crowding / extreme negative funding.
    if _is_oi_crowding(item):
        assert item.data is not None
        return make_signal(
            CatalystKind.OI_FUNDING_CROWDING,
            _classify_oi_direction(item.data),
            confidence_bps=7_500,
            reason="oi_or_funding_crowding",
        )

    # 6. Alpha / ecosystem / secondary venue alerts.
    if channels_set & ALPHA_ECOSYSTEM_CHANNELS:
        return make_signal(
            CatalystKind.ALPHA_ECOSYSTEM_ALERT,
            CatalystDirection.NEUTRAL_OBSERVE,
            confidence_bps=6_000,
            reason="ecosystem_or_secondary_alert",
        )

    return make_signal(
        CatalystKind.GENERAL_INFO,
        CatalystDirection.NEUTRAL_OBSERVE,
        confidence_bps=5_000,
        reason="general_news",
    )
