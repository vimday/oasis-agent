"""Calldata and range math for one hookless native-ETH/ERC-20 Uniswap v4 pool.

Pinned to v4-periphery 1.0.3 action ids (contracts/node_modules) and the Base
canonical deployments verified on chain on 2026-10-08.
"""

from dataclasses import dataclass

from eth_abi.abi import encode

from ..models.common import normalize_address
from ..uniswap_v4.liquidity_math import amounts_for_liquidity, sqrt_price_at_tick
from .chain import calldata

Q96 = 1 << 96
ZERO = "0x" + "0" * 40
UINT160_MAX = (1 << 160) - 1

# v4-periphery Actions
MINT_POSITION = 0x02
BURN_POSITION = 0x03
SWAP_EXACT_IN_SINGLE = 0x06
SETTLE_ALL = 0x0C
SETTLE_PAIR = 0x0D
TAKE_ALL = 0x0F
TAKE_PAIR = 0x11
SWEEP = 0x14
# UniversalRouter command
V4_SWAP = 0x10

KEY_ABI = "(address,address,uint24,int24,address)"


@dataclass(frozen=True, slots=True)
class PoolKey:
    currency0: str
    currency1: str
    fee: int
    tick_spacing: int
    hooks: str = ZERO

    def __post_init__(self) -> None:
        for name in ("currency0", "currency1", "hooks"):
            object.__setattr__(self, name, normalize_address(getattr(self, name)))
        if type(self.fee) is not int or not 0 <= self.fee <= 1_000_000:
            raise ValueError("static pool fee must be integer pips")
        if type(self.tick_spacing) is not int or not 1 <= self.tick_spacing <= 32767:
            raise ValueError("tick spacing must be a positive protocol integer")
        if self.currency1 == ZERO:
            raise ValueError("pool currencies must differ")
        if self.currency0 != ZERO:
            raise ValueError("this adapter supports native-ETH currency0 pools only")
        if self.hooks != ZERO:
            raise ValueError("hooked pools are out of scope")

    def tuple(self) -> tuple[str, str, int, int, str]:
        return (self.currency0, self.currency1, self.fee, self.tick_spacing, self.hooks)


def _plan(actions: list[int], params: list[bytes]) -> bytes:
    return encode(["bytes", "bytes[]"], [bytes(actions), params])


def mint_call(
    key: PoolKey,
    lower: int,
    upper: int,
    liquidity: int,
    amount0_max: int,
    amount1_max: int,
    owner: str,
    deadline: int,
) -> bytes:
    """PositionManager.modifyLiquidities: mint, settle both, sweep unused ETH back."""
    mint = encode(
        [KEY_ABI, "int24", "int24", "uint256", "uint128", "uint128", "address", "bytes"],
        [key.tuple(), lower, upper, liquidity, amount0_max, amount1_max, owner, b""],
    )
    settle = encode(["address", "address"], [key.currency0, key.currency1])
    sweep = encode(["address", "address"], [key.currency0, owner])
    unlock = _plan([MINT_POSITION, SETTLE_PAIR, SWEEP], [mint, settle, sweep])
    return calldata("modifyLiquidities(bytes,uint256)", ["bytes", "uint256"], [unlock, deadline])


def burn_call(
    key: PoolKey, token_id: int, amount0_min: int, amount1_min: int, owner: str, deadline: int
) -> bytes:
    """Burn the whole position (liquidity + fees) and take both currencies to owner."""
    burn = encode(
        ["uint256", "uint128", "uint128", "bytes"], [token_id, amount0_min, amount1_min, b""]
    )
    take = encode(["address", "address", "address"], [key.currency0, key.currency1, owner])
    unlock = _plan([BURN_POSITION, TAKE_PAIR], [burn, take])
    return calldata("modifyLiquidities(bytes,uint256)", ["bytes", "uint256"], [unlock, deadline])


def swap_call(
    key: PoolKey, zero_for_one: bool, amount_in: int, min_out: int, deadline: int
) -> bytes:
    """UniversalRouter.execute(V4_SWAP) exact-in single hop; output goes to msg.sender."""
    currency_in, currency_out = (
        (key.currency0, key.currency1) if zero_for_one else (key.currency1, key.currency0)
    )
    swap = encode(
        [f"({KEY_ABI},bool,uint128,uint128,bytes)"],
        [(key.tuple(), zero_for_one, amount_in, min_out, b"")],
    )
    settle = encode(["address", "uint256"], [currency_in, amount_in])
    take = encode(["address", "uint256"], [currency_out, min_out])
    plan = _plan([SWAP_EXACT_IN_SINGLE, SETTLE_ALL, TAKE_ALL], [swap, settle, take])
    return calldata(
        "execute(bytes,bytes[],uint256)",
        ["bytes", "bytes[]", "uint256"],
        [bytes([V4_SWAP]), [plan], deadline],
    )


def erc20_approve_call(spender: str, amount: int) -> bytes:
    return calldata("approve(address,uint256)", ["address", "uint256"], [spender, amount])


def permit2_approve_call(token: str, spender: str, amount: int, expiration: int) -> bytes:
    return calldata(
        "approve(address,address,uint160,uint48)",
        ["address", "address", "uint160", "uint48"],
        [token, spender, amount, expiration],
    )


# ---- range math -----------------------------------------------------------------


def price_from_sqrt(sqrt_price_x96: int, decimals0: int, decimals1: int) -> float:
    """Human price of currency0 in currency1 (e.g. USDC per ETH)."""
    return float((sqrt_price_x96 / Q96) ** 2 * 10 ** (decimals0 - decimals1))


def range_ticks(tick: int, half_width_ticks: int, spacing: int) -> tuple[int, int]:
    center = tick - tick % spacing
    half = max(spacing, half_width_ticks - half_width_ticks % spacing)
    return center - half, center + half


def liquidity_for_amounts(sqrt_p: int, lower: int, upper: int, amount0: int, amount1: int) -> int:
    sa, sb = sqrt_price_at_tick(lower), sqrt_price_at_tick(upper)
    if not sa < sqrt_p < sb:
        raise ValueError("price must be strictly inside the range to mint two-sided")
    l0 = amount0 * (sqrt_p * sb // Q96) // (sb - sqrt_p)
    l1 = amount1 * Q96 // (sqrt_p - sa)
    return min(l0, l1)


def position_amounts(sqrt_p: int, lower: int, upper: int, liquidity: int) -> tuple[int, int]:
    return amounts_for_liquidity(
        sqrt_p, sqrt_price_at_tick(lower), sqrt_price_at_tick(upper), liquidity
    )


def eth_value_share(sqrt_p: int, lower: int, upper: int) -> float:
    """Fraction of position value held in currency0 at the current price."""
    probe = 10**20
    a0, a1 = position_amounts(sqrt_p, lower, upper, probe)
    v0 = a0 * (sqrt_p / Q96) ** 2  # currency0 valued in currency1 raw units
    return v0 / (v0 + a1)


__all__ = [
    "Q96",
    "UINT160_MAX",
    "ZERO",
    "PoolKey",
    "burn_call",
    "erc20_approve_call",
    "eth_value_share",
    "liquidity_for_amounts",
    "mint_call",
    "permit2_approve_call",
    "position_amounts",
    "price_from_sqrt",
    "range_ticks",
    "swap_call",
]
