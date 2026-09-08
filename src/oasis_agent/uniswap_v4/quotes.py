"""Strict ABI helpers for pinned Uniswap v4 single-pool quotes.

The helpers are intentionally free of RPC and signing concerns.  They encode the
canonical ``IV4Quoter`` single-pool calls and reject non-canonical return data so
the acquisition layer can compare the same economic fact across providers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from eth_abi.abi import decode, encode
from eth_hash.auto import keccak

from ..models.common import normalize_address, normalize_hex
from ..models.v4 import DYNAMIC_FEE_FLAG

UINT128_MAX = (1 << 128) - 1
UINT24_MAX = (1 << 24) - 1
INT24_MIN = -(1 << 23)
INT24_MAX = (1 << 23) - 1


class QuoteKind(str, Enum):
    EXACT_INPUT = "exact_input"
    EXACT_OUTPUT = "exact_output"


class QuoteDirection(str, Enum):
    ZERO_FOR_ONE = "zero_for_one"
    ONE_FOR_ZERO = "one_for_zero"

    @property
    def zero_for_one(self) -> bool:
        return self is QuoteDirection.ZERO_FOR_ONE


@dataclass(frozen=True, slots=True)
class QuotePoolKey:
    currency0: str
    currency1: str
    fee: int
    tick_spacing: int
    hooks: str

    def __post_init__(self) -> None:
        currency0 = normalize_address(self.currency0)
        currency1 = normalize_address(self.currency1)
        hooks = normalize_address(self.hooks)
        if int(currency0, 16) >= int(currency1, 16):
            raise ValueError("currency0 must sort before currency1")
        if type(self.fee) is not int or not 0 <= self.fee <= UINT24_MAX:
            raise ValueError("fee must be a uint24")
        if self.fee != DYNAMIC_FEE_FLAG and self.fee >= 1_000_000:
            raise ValueError("static pool fee must be below 1,000,000 pips")
        if type(self.tick_spacing) is not int or not INT24_MIN <= self.tick_spacing <= INT24_MAX:
            raise ValueError("tick_spacing must be an int24")
        if self.tick_spacing <= 0:
            raise ValueError("tick_spacing must be positive")
        object.__setattr__(self, "currency0", currency0)
        object.__setattr__(self, "currency1", currency1)
        object.__setattr__(self, "hooks", hooks)


@dataclass(frozen=True, slots=True)
class SinglePoolQuoteRequest:
    kind: QuoteKind
    direction: QuoteDirection
    amount: int
    hook_data: str = "0x"

    def __post_init__(self) -> None:
        if not isinstance(self.kind, QuoteKind):
            raise TypeError("kind must be QuoteKind")
        if not isinstance(self.direction, QuoteDirection):
            raise TypeError("direction must be QuoteDirection")
        if type(self.amount) is not int or not 0 < self.amount <= UINT128_MAX:
            raise ValueError("quote amount must be a positive uint128")
        object.__setattr__(self, "hook_data", normalize_hex(self.hook_data))


@dataclass(frozen=True, slots=True)
class SinglePoolQuote:
    quoted_amount: int
    gas_estimate: int

    def __post_init__(self) -> None:
        for name in ("quoted_amount", "gas_estimate"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value < 1 << 256:
                raise ValueError(f"{name} must be a uint256")


def encode_single_pool_quote(pool: QuotePoolKey, request: SinglePoolQuoteRequest) -> str:
    function = (
        "quoteExactInputSingle"
        if request.kind is QuoteKind.EXACT_INPUT
        else "quoteExactOutputSingle"
    )
    signature = f"{function}(((address,address,uint24,int24,address),bool,uint128,bytes))"
    selector = keccak(signature.encode())[:4]
    arguments = encode(
        ["((address,address,uint24,int24,address),bool,uint128,bytes)"],
        [
            (
                (pool.currency0, pool.currency1, pool.fee, pool.tick_spacing, pool.hooks),
                request.direction.zero_for_one,
                request.amount,
                bytes.fromhex(request.hook_data[2:]),
            )
        ],
    )
    return "0x" + (selector + arguments).hex()


def decode_single_pool_quote(value: str) -> SinglePoolQuote:
    normalized = normalize_hex(value)
    raw = bytes.fromhex(normalized[2:])
    if len(raw) != 64:
        raise ValueError("quote result must contain exactly two ABI words")
    quoted_amount, gas_estimate = decode(["uint256", "uint256"], raw, strict=True)
    return SinglePoolQuote(int(quoted_amount), int(gas_estimate))


__all__ = [
    "QuoteDirection",
    "QuoteKind",
    "QuotePoolKey",
    "SinglePoolQuote",
    "SinglePoolQuoteRequest",
    "decode_single_pool_quote",
    "encode_single_pool_quote",
]
