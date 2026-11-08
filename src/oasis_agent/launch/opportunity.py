"""Deterministic opportunity decisions over provider claims: skip, flip, LP handoff or exit.

Pure functions only. Inputs are OKX onchainos provider claims (not chain attestations);
missing or malformed fields fail closed to `skip`/`exit`, never to a permissive default.
Money is integer USD micros, ratios are integer basis points.
"""

import re
from dataclasses import dataclass, replace
from fractions import Fraction
from typing import Annotated, Any, Literal

from pydantic import Field

from ..models.common import normalize_address
from .models import StrictModel

USD = 1_000_000
Bps = Annotated[int, Field(ge=0, le=100_000)]
Action = Literal["skip", "watch", "flip", "lp_candidate", "hold", "exit"]
_DECIMAL = re.compile(r"-?[0-9]{1,40}(?:\.[0-9]{1,60})?")
# security token-scan boolean flags that are hard blockers when true.
RISK_FLAGS = (
    "isHoneypot",
    "isAirdropScam",
    "isCounterfeit",
    "isCounterfeitStockToken",
    "isFakeLiquidity",
    "isFundLinkage",
    "isHasAssetEditAuth",
    "isHasBlockingHis",
    "isHasFrozenAuth",
    "isLiquidityRemoval",
    "isMintable",
    "isNotOpenSource",
    "isOverIssued",
    "isWash",
    "isWash2",
    "isDumping",
    "isRubbishAirdrop",
)


def decimal(value: object) -> Fraction | None:
    """Exact provider decimal string, or None when absent/malformed. Never a float."""
    if not isinstance(value, str) or _DECIMAL.fullmatch(value) is None:
        return None
    return Fraction(value)


def micros(value: object) -> int | None:
    number = decimal(value)
    return None if number is None or number < 0 else int(number * USD)


def pct_bps(value: object) -> int | None:
    """Provider percent string ("6.13") to signed basis points, truncated toward zero."""
    number = decimal(value)
    return None if number is None else int(number * 100)


def positive_price(value: object) -> Fraction | None:
    number = decimal(value)
    return number if number is not None and number > 0 else None


def count(value: object) -> int | None:
    number = decimal(value)
    if number is None or number < 0 or number.denominator != 1:
        return None
    return int(number)


@dataclass(frozen=True, slots=True)
class Market:
    """One token's merged claims at one tick; None means the provider did not say."""

    token: str
    symbol: str
    observed_at: int
    liquidity_usd_micros: int | None
    market_cap_usd_micros: int | None
    holders: int | None
    top10_bps: int | None
    unique_traders: int | None
    buys: int | None
    sells: int | None
    first_trade_at: int | None
    change_5m_bps: int | None = None
    change_1h_bps: int | None = None
    change_24h_bps: int | None = None
    volume_24h_usd_micros: int | None = None
    smart_wallets: int = 0
    smart_sold_bps: int | None = None
    risk_flags: tuple[str, ...] | None = None
    tax_bps: int | None = None
    dev_rug_count: int | None = None
    price: Fraction | None = None
    high_24h: Fraction | None = None
    gmgn_listed: bool = False
    provider_disagreement: tuple[str, ...] = ()


def market_from_hot(row: dict[str, Any], observed_at: int) -> Market | None:
    try:
        token = normalize_address(str(row.get("tokenContractAddress", "")))
    except ValueError:
        return None
    first = count(row.get("firstTradeTime"))
    return Market(
        token=token,
        symbol=str(row.get("tokenSymbol", ""))[:32],
        observed_at=observed_at,
        liquidity_usd_micros=micros(row.get("liquidity")),
        market_cap_usd_micros=micros(row.get("marketCap")),
        holders=count(row.get("holders")),
        top10_bps=pct_bps(row.get("top10HoldPercent")),
        unique_traders=count(row.get("uniqueTraders")),
        buys=count(row.get("txsBuy")),
        sells=count(row.get("txsSell")),
        first_trade_at=None if first is None else first // 1000,
        price=positive_price(row.get("price")),
    )


def market_from_signal(row: object, observed_at: int) -> Market | None:
    """A token named by a smart-money/KOL signal; unstated fields stay None (fail closed)."""
    if not isinstance(row, dict) or not isinstance(token := row.get("token"), dict):
        return None
    if row.get("walletType") not in ("1", "2"):
        return None
    try:
        address = normalize_address(str(token.get("tokenAddress", "")))
    except ValueError:
        return None
    return Market(
        token=address,
        symbol=str(token.get("symbol", ""))[:32],
        observed_at=observed_at,
        liquidity_usd_micros=None,
        market_cap_usd_micros=micros(token.get("marketCapUsd")),
        holders=count(token.get("holders")),
        top10_bps=pct_bps(token.get("top10HolderPercent")),
        unique_traders=None,
        buys=None,
        sells=None,
        first_trade_at=None,
        price=positive_price(row.get("price")),
    )


def merge_price(market: Market, row: dict[str, Any]) -> Market:
    return replace(
        market,
        change_5m_bps=pct_bps(row.get("priceChange5M")),
        change_1h_bps=pct_bps(row.get("priceChange1H")),
        change_24h_bps=pct_bps(row.get("priceChange24H")),
        volume_24h_usd_micros=micros(row.get("volume24H")),
        liquidity_usd_micros=micros(row.get("liquidity")) or market.liquidity_usd_micros,
        price=positive_price(row.get("price")) or market.price,
        high_24h=positive_price(row.get("maxPrice")),
    )


def security_flags(row: dict[str, Any]) -> tuple[tuple[str, ...] | None, int | None]:
    """Raised risk flags and max(buy, sell) tax; (None, None) if any flag is not a boolean."""
    if any(not isinstance(row.get(flag), bool) for flag in RISK_FLAGS):
        return None, None
    buy, sell = pct_bps(row.get("buyTaxes")), pct_bps(row.get("sellTaxes"))
    flags = tuple(flag for flag in RISK_FLAGS if row[flag] is True)
    return flags, None if buy is None or sell is None else max(buy, sell)


def merge_security(market: Market, row: dict[str, Any]) -> Market:
    flags, tax = security_flags(row)
    return replace(market, risk_flags=flags, tax_bps=tax)


def merge_signals(market: Market, rows: list[Any], now: int, window: int) -> Market:
    """Distinct smart-money/KOL wallets that bought this token inside the window."""
    wallets: set[str] = set()
    sold: list[int] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("token"), dict):
            continue
        try:
            token = normalize_address(str(row["token"].get("tokenAddress", "")))
        except ValueError:
            continue
        at = count(row.get("timestamp"))
        if token != market.token or at is None or not 0 <= now - at // 1000 <= window:
            continue
        if row.get("walletType") not in ("1", "2"):
            continue
        wallets.update(w.lower() for w in str(row.get("triggerWalletAddress", "")).split(",") if w)
        if (ratio := pct_bps(row.get("soldRatioPercent"))) is not None:
            sold.append(ratio)
    return replace(market, smart_wallets=len(wallets), smart_sold_bps=max(sold) if sold else None)


class DecisionPolicy(StrictModel):
    """Declared research thresholds; they are hypotheses to falsify in shadow, not a proven edge."""

    version: Literal["oasis.opportunity-policy.v1"] = "oasis.opportunity-policy.v1"
    min_liquidity_usd_micros: Annotated[int, Field(gt=0)] = 50_000 * USD
    min_holders: Annotated[int, Field(gt=0)] = 300
    max_top10_bps: Annotated[int, Field(gt=0, le=10_000)] = 5_000
    max_tax_bps: Annotated[int, Field(ge=0, le=1_000)] = 0
    min_age_seconds: Annotated[int, Field(ge=0)] = 1_800
    max_dev_rugs: Annotated[int, Field(ge=0)] = 0
    min_trend_1h_bps: Annotated[int, Field(gt=0)] = 300
    max_chase_5m_bps: Annotated[int, Field(gt=0)] = 300
    max_dump_5m_bps: Annotated[int, Field(gt=0)] = 500
    # Pullback from the 24h high: enter on a dip inside an uptrend, not at the top and
    # not after the trend has broken.
    min_pullback_bps: Annotated[int, Field(ge=0, le=10_000)] = 300
    max_pullback_bps: Annotated[int, Field(gt=0, le=10_000)] = 3_000
    max_smart_sold_bps: Annotated[int, Field(ge=0, le=10_000)] = 5_000
    min_score_bps: Annotated[int, Field(ge=0, le=10_000)] = 5_500
    max_round_trip_cost_bps: Annotated[int, Field(gt=0, le=2_000)] = 300
    max_impact_bps: Annotated[int, Field(gt=0, le=2_000)] = 150
    gas_per_leg_usd_micros: Annotated[int, Field(ge=0)] = 50_000
    # Charged on every paper leg for quote-to-inclusion drift; a shadow-only haircut.
    execution_slippage_bps: Annotated[int, Field(ge=0, le=500)] = 50
    position_bps_of_wallet: Annotated[int, Field(gt=0, le=10_000)] = 2_000
    max_liquidity_share_bps: Annotated[int, Field(gt=0, le=1_000)] = 50
    max_positions: Annotated[int, Field(gt=0, le=10)] = 3
    stop_loss_bps: Annotated[int, Field(gt=0, le=5_000)] = 800
    take_profit_bps: Annotated[int, Field(gt=0, le=50_000)] = 2_500
    trailing_bps: Annotated[int, Field(gt=0, le=5_000)] = 1_000
    max_hold_seconds: Annotated[int, Field(gt=0)] = 4 * 3600
    lp_min_liquidity_usd_micros: Annotated[int, Field(gt=0)] = 200_000 * USD
    lp_min_turnover_bps: Annotated[int, Field(gt=0)] = 20_000
    lp_max_abs_change_24h_bps: Annotated[int, Field(gt=0)] = 1_500
    signal_window_seconds: Annotated[int, Field(gt=0, le=86_400)] = 3_600


@dataclass(frozen=True, slots=True)
class Assessment:
    token: str
    action: Action
    score_bps: int
    reasons: tuple[str, ...]


def _clamp(numerator: int, denominator: int, weight: int) -> int:
    if denominator <= 0:
        return 0
    return max(0, min(weight, numerator * weight // denominator))


def _gate(out: list[str], name: str, value: int | None, ok: bool) -> None:
    """Unknown and failed are distinct: `<name>_unknown` can still be resolved by enrichment."""
    if value is None:
        out.append(name + "_unknown")
    elif not ok:
        out.append(name + "_failed")


def hard_blockers(m: Market, p: DecisionPolicy) -> list[str]:
    """Safety and liquidity gates; any unknown input is itself a blocker."""
    out: list[str] = []
    liquidity, holders, top10, tax = m.liquidity_usd_micros, m.holders, m.top10_bps, m.tax_bps
    age = None if m.first_trade_at is None else m.observed_at - m.first_trade_at
    _gate(out, "liquidity", liquidity, (liquidity or 0) >= p.min_liquidity_usd_micros)
    _gate(out, "holders", holders, (holders or 0) >= p.min_holders)
    _gate(out, "top10", top10, top10 is not None and top10 <= p.max_top10_bps)
    _gate(out, "age", age, age is not None and age >= p.min_age_seconds)
    if m.risk_flags is None:
        out.append("security_unknown")
    else:
        out.extend("risk:" + flag for flag in m.risk_flags)
    _gate(out, "tax", tax, tax is not None and tax <= p.max_tax_bps)
    if m.dev_rug_count is not None and m.dev_rug_count > p.max_dev_rugs:
        out.append("dev_rug_history")
    # Two providers materially disagreeing means at least one is wrong; neither is trusted.
    out.extend("provider_disagree:" + field for field in m.provider_disagreement)
    return out


def decided(reasons: tuple[str, ...]) -> bool:
    """True when some blocker is already a known failure, so enrichment cannot rescue it."""
    return any(not r.endswith("_unknown") for r in reasons)


def pullback_bps(m: Market) -> int | None:
    """Distance below the 24h high, floored; None if either price is unknown or incoherent."""
    if m.price is None or m.high_24h is None or m.price > m.high_24h:
        return None
    return int((m.high_24h - m.price) * 10_000 / m.high_24h)


def assess(m: Market, p: DecisionPolicy) -> Assessment:
    """Classify one token: flip (momentum pullback entry), lp_candidate (steady turnover), skip."""
    blockers = hard_blockers(m, p)
    if blockers:
        return Assessment(m.token, "skip", 0, tuple(blockers))
    reasons: list[str] = []
    c5, c1h, c24 = m.change_5m_bps, m.change_1h_bps, m.change_24h_bps
    liquidity = m.liquidity_usd_micros or 0
    if (
        m.volume_24h_usd_micros is not None
        and c24 is not None
        and liquidity >= p.lp_min_liquidity_usd_micros
        and m.volume_24h_usd_micros * 10_000 >= p.lp_min_turnover_bps * liquidity
        and abs(c24) <= p.lp_max_abs_change_24h_bps
    ):
        lp = ("lp_turnover_high", "lp_volatility_bounded", "requires_separate_lp_evaluation")
        return Assessment(m.token, "lp_candidate", 0, lp)
    if c5 is None or c1h is None:
        return Assessment(m.token, "watch", 0, ("momentum_unknown",))
    if c1h < p.min_trend_1h_bps:
        reasons.append("no_uptrend")
    if c5 > p.max_chase_5m_bps:
        reasons.append("chasing_spike")
    if c5 < -p.max_dump_5m_bps:
        reasons.append("dumping_now")
    if m.smart_sold_bps is not None and m.smart_sold_bps > p.max_smart_sold_bps:
        reasons.append("smart_money_exiting")
    pullback = pullback_bps(m)
    if pullback is None:
        reasons.append("pullback_unknown")
    elif pullback < p.min_pullback_bps:
        reasons.append("at_high_no_pullback")
    elif pullback > p.max_pullback_bps:
        reasons.append("trend_broken")
    # Unknown buy/sell split (signal-only discovery) scores zero for buy pressure.
    buys, sells = m.buys or 0, m.sells or 0
    # Weights: trend 25%, buy pressure 20%, smart-money confluence 25%, depth 15%,
    # holder dispersion 15%. Saturation points are declared hypotheses for shadow review:
    # +20% 1h, 70% buy share (net 40% of flow), 4 smart wallets, 5x minimum depth.
    score = (
        _clamp(c1h, 2_000, 2_500)
        + _clamp((buys - sells) * 10, (buys + sells) * 4, 2_000)
        + _clamp(m.smart_wallets, 4, 2_500)
        + _clamp(liquidity, 5 * p.min_liquidity_usd_micros, 1_500)
        + _clamp(p.max_top10_bps - (m.top10_bps or 0), p.max_top10_bps, 1_500)
    )
    if score < p.min_score_bps:
        reasons.append("score_below_threshold")
    if reasons:
        return Assessment(m.token, "watch", score, tuple(reasons))
    return Assessment(
        m.token, "flip", score, ("uptrend_pullback_entry", f"pullback_bps:{pullback}")
    )


MARKET_SNAPSHOT = "oasis.spot-market.v1"
_FRACTION = re.compile(r"^-?(0|[1-9][0-9]*)/[1-9][0-9]*$")
_INT_FIELDS = (
    "observed_at",
    "liquidity_usd_micros",
    "market_cap_usd_micros",
    "holders",
    "top10_bps",
    "unique_traders",
    "buys",
    "sells",
    "first_trade_at",
    "change_5m_bps",
    "change_1h_bps",
    "change_24h_bps",
    "volume_24h_usd_micros",
    "smart_wallets",
    "smart_sold_bps",
    "tax_bps",
    "dev_rug_count",
)
_REQUIRED_INTS = frozenset({"observed_at", "smart_wallets"})


def market_to_json(m: Market) -> dict[str, Any]:
    """Lossless snapshot of the exact Market an assessment saw. None stays null; prices
    are exact `numerator/denominator` strings; tuples become lists."""

    def fraction(value: Fraction | None) -> str | None:
        return None if value is None else f"{value.numerator}/{value.denominator}"

    return {
        "schema": MARKET_SNAPSHOT,
        "token": m.token,
        "symbol": m.symbol,
        **{name: getattr(m, name) for name in _INT_FIELDS},
        "risk_flags": None if m.risk_flags is None else list(m.risk_flags),
        "price": fraction(m.price),
        "high_24h": fraction(m.high_24h),
        "gmgn_listed": m.gmgn_listed,
        "provider_disagreement": list(m.provider_disagreement),
    }


def market_from_json(raw: object) -> Market:
    """The exact Market back; anything malformed or of another schema raises ValueError."""
    if not isinstance(raw, dict) or raw.get("schema") != MARKET_SNAPSHOT:
        raise ValueError("unsupported market snapshot")
    expected = {"schema", "token", "symbol", "risk_flags", "price", "high_24h", "gmgn_listed"}
    if set(raw) != expected | set(_INT_FIELDS) | {"provider_disagreement"}:
        raise ValueError("market snapshot fields differ")
    ints: dict[str, int | None] = {}
    for name in _INT_FIELDS:
        value = raw[name]
        if value is None and name not in _REQUIRED_INTS:
            ints[name] = None
        elif type(value) is int:
            ints[name] = value
        else:
            raise ValueError(f"market snapshot {name} is not an integer")

    def fraction(value: object) -> Fraction | None:
        if value is None:
            return None
        if not isinstance(value, str) or not _FRACTION.fullmatch(value):
            raise ValueError("market snapshot price is not an exact fraction")
        return Fraction(value)

    def strings(value: object) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError("market snapshot list is malformed")
        return tuple(value)

    if normalize_address(str(raw["token"])) != raw["token"] or not isinstance(raw["symbol"], str):
        raise ValueError("market snapshot identity is malformed")
    if type(raw["gmgn_listed"]) is not bool:
        raise ValueError("market snapshot gmgn_listed is not a boolean")
    smart = ints.pop("smart_wallets")
    observed = ints.pop("observed_at")
    assert smart is not None and observed is not None  # nosec B101 - required above
    return Market(
        token=raw["token"],
        symbol=raw["symbol"],
        observed_at=observed,
        smart_wallets=smart,
        risk_flags=None if raw["risk_flags"] is None else strings(raw["risk_flags"]),
        price=fraction(raw["price"]),
        high_24h=fraction(raw["high_24h"]),
        gmgn_listed=raw["gmgn_listed"],
        provider_disagreement=strings(raw["provider_disagreement"]),
        **ints,
    )
