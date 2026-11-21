"""Replay one retained OKX Classic Swap quote into conservative paper amounts."""

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ..evidence.rpc_validation import strict_json_object
from ..live.config import BASE_USDC
from ..models.common import normalize_address, sha256_hex
from .models import Address, Digest, Positive, StrictModel, UInt

USD_MICROS = 1_000_000


class UsdcValue(StrictModel):
    low_usd_micros: Positive
    high_usd_micros: Positive
    observed_at: UInt
    evidence_sha256: Digest

    @model_validator(mode="after")
    def ordered(self) -> UsdcValue:
        if self.low_usd_micros > self.high_usd_micros:
            raise ValueError("USDC valuation bounds reversed")
        return self


class QuoteCapture(StrictModel):
    retrieved_at: UInt
    raw_json: Annotated[str, Field(min_length=1, max_length=250_000)]
    extra_cost_usd_micros: UInt


class SpotMarket(StrictModel):
    token: Address
    units: Positive
    usdc: UsdcValue
    buy: QuoteCapture | None = None
    sell: QuoteCapture

    @model_validator(mode="after")
    def tradable_identity(self) -> SpotMarket:
        if int(self.token, 16) in (0, int(BASE_USDC, 16), int("e" * 40, 16)):
            raise ValueError("a distinct Base ERC-20 token is required")
        return self


@dataclass(frozen=True, slots=True)
class PaperQuote:
    usd_micros: int
    token_decimals: int
    retrieved_at: int
    raw_sha256: str


def _number(value: object, *, signed: bool = False) -> Fraction:
    pattern = r"-?[0-9]{1,78}(?:\.[0-9]{1,36})?" if signed else r"[0-9]{1,78}(?:\.[0-9]{1,36})?"
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError("invalid OKX decimal")
    return Fraction(value)


def _amount(value: object) -> int:
    if not isinstance(value, str) or re.fullmatch(r"(?:0|[1-9][0-9]{0,77})", value) is None:
        raise ValueError("invalid OKX base units")
    amount = int(value)
    if amount >= 2**256:
        raise ValueError("OKX amount outside uint256")
    return amount


def _token(value: object, address: str) -> int:
    if (
        not isinstance(value, dict)
        or not isinstance(token := value.get("tokenContractAddress"), str)
        or normalize_address(token) != address
    ):
        raise ValueError("OKX token address differs")
    decimals = _amount(value.get("decimal"))
    if (
        decimals > 77
        or (address == BASE_USDC and decimals != 6)
        or value.get("isHoneyPot") is not False
        or _number(value.get("taxRate")) != 0
    ):
        raise ValueError("OKX token quote metadata unresolved or unsafe")
    return decimals


def replay_quote(
    market: SpotMarket,
    side: Literal["buy", "sell"],
    *,
    now: int,
    max_age: int,
    max_impact_bps: int,
    slippage_bps: int,
) -> PaperQuote:
    leg = market.buy if side == "buy" else market.sell
    if leg is None or not (
        0 <= now - leg.retrieved_at <= max_age and 0 <= now - market.usdc.observed_at <= max_age
    ):
        raise ValueError("fresh quote and USDC bounds required")
    raw = leg.raw_json.encode()
    envelope = strict_json_object(raw)
    rows = envelope.get("data")
    if (
        envelope.get("code") != "0"
        or not isinstance(rows, list)
        or len(rows) != 1
        or not isinstance(rows[0], dict)
    ):
        raise ValueError("one successful OKX quote is required")
    row = rows[0]
    buy = side == "buy"
    if (
        row.get("chainIndex") != "8453"
        or row.get("swapMode") != ("exactOut" if buy else "exactIn")
        or not isinstance(row.get("dexRouterList"), list)
        or not row["dexRouterList"]
        or abs(_number(row.get("priceImpactPercent"), signed=True)) * 100 > max_impact_bps
    ):
        raise ValueError("OKX route, chain, swap mode or impact refused")
    from_decimals = _token(row.get("fromToken"), BASE_USDC if buy else market.token)
    to_decimals = _token(row.get("toToken"), market.token if buy else BASE_USDC)
    debit, credit = (_amount(row.get(key)) for key in ("fromTokenAmount", "toTokenAmount"))
    if not debit or not credit or (credit if buy else debit) != market.units:
        raise ValueError("OKX quote does not cover the exact token size")
    fee = _number(row.get("tradeFee")) * USD_MICROS + leg.extra_cost_usd_micros
    if buy:
        usd = (
            Fraction(
                debit * (10_000 + slippage_bps) * market.usdc.high_usd_micros, 10_000 * USD_MICROS
            )
            + fee
        )
        amount = -(-usd.numerator // usd.denominator)
    else:
        usd = (
            Fraction(
                credit * (10_000 - slippage_bps) * market.usdc.low_usd_micros, 10_000 * USD_MICROS
            )
            - fee
        )
        amount = usd.numerator // usd.denominator
    if not 0 <= amount < 2**256:
        raise ValueError("OKX quote has negative net proceeds or overflows")
    return PaperQuote(
        amount, to_decimals if buy else from_decimals, leg.retrieved_at, sha256_hex(raw)
    )
