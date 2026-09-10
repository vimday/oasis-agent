"""Base venue adapters: discovery identities, quotes and unsigned swap calldata.

No signing or broadcasting. v4 calldata targets the original Base v4 Universal
Router, using the pre-2.1 IV4Router ABI pinned in contracts/node_modules.
"""

from dataclasses import dataclass
from typing import Any, Literal

from eth_abi.abi import decode, encode
from eth_hash.auto import keccak

from ..models.common import normalize_address, normalize_hash32
from ..uniswap_v4.quotes import (
    QuoteDirection,
    QuoteKind,
    QuotePoolKey,
    SinglePoolQuoteRequest,
    decode_single_pool_quote,
    encode_single_pool_quote,
)
from .rpc import JsonRpc, RpcError, _selector

ZERO = "0x" + "0" * 40
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
V4_MANAGER = "0x498581ff718922c3f8e6a244956af099b2652b2b"
V4_QUOTER = "0x0d5e0f971ed27fbff6c2837bf31316121532048d"
V4_ROUTER = "0x6ff5693b99212da76ad316178a184ab56d299b43"
V4_STATE_VIEW = "0xa3c0c9b65bad0b08107aa264b0f3db444b867a71"
AERO_FACTORY = "0x420dd381b31aef6683db6b902084cb0ffece40da"
AERO_ROUTER = "0xcf77a3ba9a5ca399b7c97c74d54e5b1beb874e43"
# Each factory has its own quoter/router; never cross-wire generations.
SLIPSTREAM = {
    "0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a": (
        "0x254cf9e1e6e233aa1ac962cb9b05b2cfeaae15b0",
        "0xbe6d8f0d05cc4be24d5167a3ef062215be6d18a5",
    ),
    "0xade65c38cd4849adba595a4323a8c7ddfe89716a": (
        "0x3d4c22254f86f64b7ec90ab8f7aec1fbfd271c6c",
        "0xcbbb8035cac7d4b3ca7abb74cf7bdf900215ce0d",
    ),
    "0xf8f2eb4940cfe7d13603dddd87f123820fc061ef": (
        "0x514c8b5f54112481e28028f1166bd78501089259",
        "0x698cb2b6dd822994581fea6ea4fc755d1363a92f",
    ),
}
Venue = Literal["aerodrome", "slipstream", "uniswap_v4"]
KEY_ABI = "(address,address,uint24,int24,address)"

# Canonical USDC/WETH hop pools, dual-source pinned on Base. A USDC-denominated
# pool routes through its venue's own hop (ETH -> WETH -> USDC -> target) so the
# position never leaves the venue router. A factory without a listed hop cannot
# support USDC multi-hop and stays discovery-only.
AERO_USDC_WETH = ("0xcdac0d6c6c59727a65f871236188350531885c43", False)  # (pool, stable)
SLIPSTREAM_USDC_WETH = {
    "0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a": (
        "0xdbc6998296caa1652a810dc8d3baf4a8294330f1",
        1,
    ),
    "0xf8f2eb4940cfe7d13603dddd87f123820fc061ef": (
        "0x4e392fbfe4d0557c82d2f97f02ec39daa31516dd",
        1,
    ),
}
# Canonical liquid v4 WETH/USDC pool (fee, tick_spacing, hooks), dual-source
# pinned on Base. A v4 USDC-denominated pool routes WETH -> USDC -> target via
# the same PoolManager using the V4Quoter/V4Router multi-hop PathKey encoding.
V4_USDC_WETH = (3000, 60, ZERO)
# IV4Router.PathKey layout: (intermediateCurrency, fee, tickSpacing, hooks, hookData)
PATH_ABI = "(address,uint24,int24,address,bytes)"


def call_data(signature: str, types: list[str], values: list[Any]) -> str:
    return _selector(signature) + encode(types, values).hex()


def uint(value: int, bits: int = 256) -> None:
    if type(value) is not int or not 0 < value < 1 << bits:
        raise ValueError(f"positive uint{bits} required")


def encode_slipstream_path(tokens: tuple[str, ...], spacings: tuple[int, ...]) -> bytes:
    """Pack [token0, spacing0, token1, spacing1, ..., tokenN] for Slipstream.

    Slipstream reuses the v3 packed-path layout but keys pools by tick spacing in
    the 3-byte slot where v3 stores the fee. `getPool(tokenA, tokenB, int24)` is
    the factory discriminator, so each interleaved int24 is the hop's spacing.
    """
    if not tokens or len(tokens) != len(spacings) + 1:
        raise ValueError("path needs N spacings interleaving N+1 tokens")
    out = bytearray()
    for index, spacing in enumerate(spacings):
        if type(spacing) is not int or not -(1 << 23) <= spacing < 1 << 23:
            raise ValueError("tick spacing must be an int24")
        out += bytes.fromhex(normalize_address(tokens[index])[2:])
        out += spacing.to_bytes(3, "big", signed=True)
    out += bytes.fromhex(normalize_address(tokens[-1])[2:])
    return bytes(out)


@dataclass(frozen=True)
class VenuePool:
    venue: Venue
    factory: str
    pool: str
    currency0: str
    currency1: str
    fee: int = 0
    tick_spacing: int = 0
    stable: bool = False
    hooks: str = ZERO

    def __post_init__(self) -> None:
        for name in ("factory", "currency0", "currency1", "hooks"):
            object.__setattr__(self, name, normalize_address(getattr(self, name)))
        if self.currency0 >= self.currency1 or type(self.stable) is not bool:
            raise ValueError("canonical token order and boolean stable required")
        if self.venue == "uniswap_v4":
            if self.factory != V4_MANAGER:
                raise ValueError("unapproved PoolManager")
            key = self.key()
            if key.tick_spacing > 32767:
                raise ValueError("invalid v4 tick spacing")
            object.__setattr__(self, "pool", normalize_hash32(self.pool, "pool id"))
            if self.pool != "0x" + keccak(encode([KEY_ABI], [self.key_tuple()])).hex():
                raise ValueError("PoolId does not match PoolKey")
        elif self.venue in ("aerodrome", "slipstream"):
            object.__setattr__(self, "pool", normalize_address(self.pool))
            if self.pool == ZERO:
                raise ValueError("pool must be nonzero")
            if self.venue == "aerodrome" and self.factory != AERO_FACTORY:
                raise ValueError("unapproved Aerodrome factory")
            if self.venue == "slipstream":
                if self.factory not in SLIPSTREAM:
                    raise ValueError("unapproved Slipstream factory")
                uint(self.tick_spacing, 23)
        else:
            raise ValueError("unsupported venue")

    def key(self) -> QuotePoolKey:
        return QuotePoolKey(self.currency0, self.currency1, self.fee, self.tick_spacing, self.hooks)

    def key_tuple(self) -> tuple[str, str, int, int, str]:
        return self.currency0, self.currency1, self.fee, self.tick_spacing, self.hooks

    @property
    def router(self) -> str:
        if self.venue == "uniswap_v4":
            return V4_ROUTER
        if self.venue == "aerodrome":
            return AERO_ROUTER
        return SLIPSTREAM[self.factory][1]

    def other_currency(self, target: str) -> str:
        target = normalize_address(target)
        if target == self.currency0:
            return self.currency1
        if target == self.currency1:
            return self.currency0
        raise ValueError("route does not contain target")

    def usdc_hop(self) -> tuple[Any, ...] | None:
        """Canonical USDC/WETH hop for this venue, or None if unsupported.

        Aerodrome returns (pool, stable), Slipstream (pool, tick_spacing), and
        v4 (fee, tick_spacing, hooks). The pool-address slot is informational
        for classic and Slipstream; only stable/spacing/fee drive the route.
        """
        if self.venue == "aerodrome":
            return AERO_USDC_WETH
        if self.venue == "slipstream":
            return SLIPSTREAM_USDC_WETH.get(self.factory)
        if self.venue == "uniswap_v4":
            return V4_USDC_WETH
        return None

    @property
    def hooked(self) -> bool:
        """True when a v4 pool attaches a non-zero hook template.

        Hooks run arbitrary code on swap, so without a pinned, independently
        verified hook adapter a hooked pool is never executable. Discovery still
        records such pools so operators can see them, but they never become buy
        candidates.
        """
        return self.venue == "uniswap_v4" and self.hooks != ZERO

    def direct_route_enabled(self, target: str) -> bool:
        """True when target pairs directly against WETH or (v4 only) native ETH.

        Used to skip unusable pools (e.g. a USDC or meme-token pair) before any
        quote or swap is attempted, rather than failing later inside `currencies`.
        Hooked v4 pools are always excluded.
        """
        target = normalize_address(target)
        if target not in (self.currency0, self.currency1):
            return False
        if self.hooked:
            return False
        other = self.other_currency(target)
        return other == WETH or (other == ZERO and self.venue == "uniswap_v4")

    def tradeable(self, target: str, usdc: str | None = None) -> bool:
        """True when this pool can fill an order for target against native ETH.

        Direct routes cover WETH and (v4 only) native ETH. A USDC-paired route is
        tradeable only when its venue offers a verified USDC/WETH hop. Hooked v4
        pools are never tradeable.
        """
        target = normalize_address(target)
        if target not in (self.currency0, self.currency1):
            return False
        if self.hooked:
            return False
        other = self.other_currency(target)
        if other == WETH or (other == ZERO and self.venue == "uniswap_v4"):
            return True
        return usdc is not None and other == normalize_address(usdc) and self.usdc_hop() is not None

    def currencies(self, target: str, buy: bool) -> tuple[str, str]:
        target = normalize_address(target)
        if target not in (self.currency0, self.currency1):
            raise ValueError("route does not contain target")
        other = self.other_currency(target)
        if other not in (ZERO, WETH):
            raise ValueError("only native ETH/WETH direct routes are supported")
        if other == ZERO and self.venue != "uniswap_v4":
            raise ValueError("native currency requires v4")
        return (other, target) if buy else (target, other)


def usdc_quote(rpc: JsonRpc, pool: VenuePool, target: str, amount: int, *, buy: bool) -> int:
    """Dual-provider two-hop USDC quote through the venue's USDC/WETH hop."""
    uint(amount)
    target = normalize_address(target)
    hop = pool.usdc_hop()
    if hop is None:
        raise ValueError("no USDC/WETH hop configured for this venue")
    if pool.venue == "aerodrome":
        _hop_pool, hop_stable = hop
        if buy:
            routes = [
                (WETH, USDC, hop_stable, AERO_FACTORY),
                (USDC, target, pool.stable, AERO_FACTORY),
            ]
        else:
            routes = [
                (target, USDC, pool.stable, AERO_FACTORY),
                (USDC, WETH, hop_stable, AERO_FACTORY),
            ]
        data = call_data(
            "getAmountsOut(uint256,(address,address,bool,address)[])",
            ["uint256", "(address,address,bool,address)[]"],
            [amount, routes],
        )
        raw = rpc.dual_eth_call(AERO_ROUTER, data).require_agreement("Aerodrome USDC quote")
        (amounts,) = decode(["uint256[]"], bytes.fromhex(raw[2:]))
        if len(amounts) != 3 or amounts[0] != amount:
            raise RpcError("invalid Aerodrome USDC route amounts")
        out = amounts[2]
    elif pool.venue == "slipstream":
        _hop_pool, hop_spacing = hop
        if buy:
            path = encode_slipstream_path((WETH, USDC, target), (hop_spacing, pool.tick_spacing))
        else:
            path = encode_slipstream_path((target, USDC, WETH), (pool.tick_spacing, hop_spacing))
        data = call_data("quoteExactInput(bytes,uint256)", ["bytes", "uint256"], [path, amount])
        raw = rpc.dual_eth_call(SLIPSTREAM[pool.factory][0], data).require_agreement(
            "Slipstream USDC quote"
        )
        out, _, _, _ = decode(
            ["uint256", "uint160[]", "uint32[]", "uint256"], bytes.fromhex(raw[2:])
        )
    else:  # uniswap_v4: quoteExactInput over a two-hop PathKey[] on the V4Quoter
        fee, spacing, hooks = hop
        if buy:
            hop_path = [
                (USDC, fee, spacing, hooks, b""),
                (target, pool.fee, pool.tick_spacing, pool.hooks, b""),
            ]
            currency_in = WETH
        else:
            hop_path = [
                (USDC, pool.fee, pool.tick_spacing, pool.hooks, b""),
                (WETH, fee, spacing, hooks, b""),
            ]
            currency_in = target
        data = call_data(
            f"quoteExactInput((address,{PATH_ABI}[],uint128))",
            [f"(address,{PATH_ABI}[],uint128)"],
            [(currency_in, hop_path, amount)],
        )
        raw = rpc.dual_eth_call(V4_QUOTER, data).require_agreement("v4 USDC quote")
        out, _ = decode(["uint256", "uint256"], bytes.fromhex(raw[2:]))
    if out <= 0:
        raise RpcError("zero executable USDC output")
    return int(out)


def quote(
    rpc: JsonRpc,
    pool: VenuePool,
    target: str,
    amount: int,
    *,
    buy: bool,
    usdc: str | None = None,
) -> int:
    uint(amount)
    target = normalize_address(target)
    if usdc is not None and pool.other_currency(target) == normalize_address(usdc):
        return usdc_quote(rpc, pool, target, amount, buy=buy)
    token_in, token_out = pool.currencies(target, buy)
    if pool.venue == "uniswap_v4":
        data = encode_single_pool_quote(
            pool.key(),
            SinglePoolQuoteRequest(
                QuoteKind.EXACT_INPUT,
                QuoteDirection.ZERO_FOR_ONE
                if token_in == pool.currency0
                else QuoteDirection.ONE_FOR_ZERO,
                amount,
            ),
        )
        raw = rpc.dual_eth_call(V4_QUOTER, data).require_agreement("v4 quote")
        out = decode_single_pool_quote(raw).quoted_amount
    elif pool.venue == "slipstream":
        data = call_data(
            "quoteExactInputSingle((address,address,uint256,int24,uint160))",
            ["(address,address,uint256,int24,uint160)"],
            [(token_in, token_out, amount, pool.tick_spacing, 0)],
        )
        raw = rpc.dual_eth_call(SLIPSTREAM[pool.factory][0], data).require_agreement(
            "slipstream quote"
        )
        if len(bytes.fromhex(raw[2:])) != 128:
            raise RpcError("invalid Slipstream quote size")
        out = decode(["uint256", "uint160", "uint32", "uint256"], bytes.fromhex(raw[2:]))[0]
    else:
        data = call_data(
            "getAmountsOut(uint256,(address,address,bool,address)[])",
            ["uint256", "(address,address,bool,address)[]"],
            [amount, [(token_in, token_out, pool.stable, pool.factory)]],
        )
        raw = rpc.dual_eth_call(AERO_ROUTER, data).require_agreement("Aerodrome quote")
        (amounts,) = decode(["uint256[]"], bytes.fromhex(raw[2:]))
        if len(amounts) != 2 or amounts[0] != amount:
            raise RpcError("invalid Aerodrome route amounts")
        out = amounts[1]
    if out <= 0:
        raise RpcError("zero executable output")
    return int(out)


def usdc_swap_call(
    pool: VenuePool,
    target: str,
    wallet: str,
    amount: int,
    minimum: int,
    deadline: int,
    *,
    buy: bool,
) -> dict[str, Any]:
    """Build unsigned two-hop USDC swaps; never relax minimum output."""
    uint(amount)
    uint(minimum)
    uint(deadline)
    wallet = normalize_address(wallet)
    target = normalize_address(target)
    hop = pool.usdc_hop()
    if hop is None:
        raise ValueError("no USDC/WETH hop configured for this venue")
    if pool.venue == "aerodrome":
        _hop_pool, hop_stable = hop
        if buy:
            routes = [
                (WETH, USDC, hop_stable, AERO_FACTORY),
                (USDC, target, pool.stable, AERO_FACTORY),
            ]
            data = call_data(
                "swapExactETHForTokens(uint256,(address,address,bool,address)[],address,uint256)",
                ["uint256", "(address,address,bool,address)[]", "address", "uint256"],
                [minimum, routes, wallet, deadline],
            )
        else:
            routes = [
                (target, USDC, pool.stable, AERO_FACTORY),
                (USDC, WETH, hop_stable, AERO_FACTORY),
            ]
            data = call_data(
                "swapExactTokensForETH(uint256,uint256,(address,address,bool,address)[],address,uint256)",
                ["uint256", "uint256", "(address,address,bool,address)[]", "address", "uint256"],
                [amount, minimum, routes, wallet, deadline],
            )
        return {"from": wallet, "to": pool.router, "data": data, "value": hex(amount if buy else 0)}
    if pool.venue == "uniswap_v4":
        fee, spacing, hooks = hop
        uint(amount, 128)
        uint(minimum, 128)
        if pool.hooks != ZERO:
            raise ValueError("v4 hook pools require an independently verified hook adapter")
        if buy:
            hop_path = [
                (USDC, fee, spacing, hooks, b""),
                (target, pool.fee, pool.tick_spacing, pool.hooks, b""),
            ]
            currency_in = WETH
        else:
            hop_path = [
                (USDC, pool.fee, pool.tick_spacing, pool.hooks, b""),
                (WETH, fee, spacing, hooks, b""),
            ]
            currency_in = target
        swap_params = encode(
            [f"(address,{PATH_ABI}[],uint128,uint128)"],
            [(currency_in, hop_path, amount, minimum)],
        )
        params = [
            swap_params,
            encode(["address", "uint256", "bool"], [currency_in, 0, not buy]),
            encode(
                ["address", "address", "uint256"],
                [target if buy else WETH, pool.router if not buy else wallet, 0],
            ),
        ]
        inputs = [encode(["bytes", "bytes[]"], [bytes([0x07, 0x0B, 0x0E]), params])]
        commands = [0x10]  # V4_SWAP
        if buy and currency_in == WETH:
            commands.insert(0, 0x0B)  # WRAP_ETH
            inputs.insert(0, encode(["address", "uint256"], [pool.router, amount]))
        if not buy:
            commands.append(0x0C)  # UNWRAP_WETH
            inputs.append(encode(["address", "uint256"], [wallet, minimum]))
        # Sweep leftover input on partial fill rather than trapping it.
        commands.append(0x04)
        inputs.append(encode(["address", "address", "uint256"], [currency_in, wallet, 0]))
        data = call_data(
            "execute(bytes,bytes[],uint256)",
            ["bytes", "bytes[]", "uint256"],
            [bytes(commands), inputs, deadline],
        )
        return {"from": wallet, "to": pool.router, "data": data, "value": hex(amount if buy else 0)}
    # Slipstream: the router is per-factory, so both hops must live in the same
    # factory. The packed path carries each hop's tick spacing.
    _hop_pool, hop_spacing = hop
    if buy:
        path = encode_slipstream_path((WETH, USDC, target), (hop_spacing, pool.tick_spacing))
    else:
        path = encode_slipstream_path((target, USDC, WETH), (pool.tick_spacing, hop_spacing))
    swap = call_data(
        "exactInput((bytes,address,uint256,uint256,uint256))",
        ["(bytes,address,uint256,uint256,uint256)"],
        [(path, wallet if buy else pool.router, deadline, amount, minimum)],
    )
    calls = [bytes.fromhex(swap[2:])]
    if not buy:
        calls.append(
            bytes.fromhex(
                call_data(
                    "unwrapWETH9(uint256,address)", ["uint256", "address"], [minimum, wallet]
                )[2:]
            )
        )
    else:
        calls.append(bytes.fromhex(_selector("refundETH()")[2:]))
    data = call_data("multicall(bytes[])", ["bytes[]"], [calls])
    return {"from": wallet, "to": pool.router, "data": data, "value": hex(amount if buy else 0)}


def swap_call(
    pool: VenuePool,
    target: str,
    wallet: str,
    amount: int,
    minimum: int,
    deadline: int,
    *,
    buy: bool,
    usdc: str | None = None,
) -> dict[str, Any]:
    """Build unsigned, deadline-bound exact-in swaps; never relax minimum output."""
    uint(amount)
    uint(minimum)
    uint(deadline)
    wallet = normalize_address(wallet)
    target = normalize_address(target)
    if usdc is not None and pool.other_currency(target) == normalize_address(usdc):
        return usdc_swap_call(pool, target, wallet, amount, minimum, deadline, buy=buy)
    token_in, token_out = pool.currencies(target, buy)
    if pool.venue == "aerodrome":
        routes = [(token_in, token_out, pool.stable, pool.factory)]
        if buy:
            data = call_data(
                "swapExactETHForTokens(uint256,(address,address,bool,address)[],address,uint256)",
                ["uint256", "(address,address,bool,address)[]", "address", "uint256"],
                [minimum, routes, wallet, deadline],
            )
        else:
            data = call_data(
                "swapExactTokensForETH(uint256,uint256,(address,address,bool,address)[],address,uint256)",
                ["uint256", "uint256", "(address,address,bool,address)[]", "address", "uint256"],
                [amount, minimum, routes, wallet, deadline],
            )
    elif pool.venue == "slipstream":
        swap = call_data(
            "exactInputSingle((address,address,int24,address,uint256,uint256,uint256,uint160))",
            ["(address,address,int24,address,uint256,uint256,uint256,uint160)"],
            [
                (
                    token_in,
                    token_out,
                    pool.tick_spacing,
                    wallet if buy else pool.router,
                    deadline,
                    amount,
                    minimum,
                    0,
                )
            ],
        )
        calls = [bytes.fromhex(swap[2:])]
        if not buy:
            calls.append(
                bytes.fromhex(
                    call_data(
                        "unwrapWETH9(uint256,address)", ["uint256", "address"], [minimum, wallet]
                    )[2:]
                )
            )
        else:
            calls.append(bytes.fromhex(_selector("refundETH()")[2:]))
        data = call_data("multicall(bytes[])", ["bytes[]"], [calls])
    else:
        uint(amount, 128)
        uint(minimum, 128)
        if pool.hooks != ZERO:
            raise ValueError("v4 hook pools require an independently verified hook adapter")
        params = [
            encode(
                [f"({KEY_ABI},bool,uint128,uint128,bytes)"],
                [(pool.key_tuple(), token_in == pool.currency0, amount, minimum, b"")],
            )
        ]
        # SETTLE takes exact input debt (OPEN_DELTA=0); buy uses router funds.
        params.append(encode(["address", "uint256", "bool"], [token_in, 0, not buy]))
        # TAKE uses full output credit (OPEN_DELTA=0). Swap already enforces min.
        params.append(
            encode(
                ["address", "address", "uint256"],
                [token_out, pool.router if not buy and token_out == WETH else wallet, 0],
            )
        )
        inputs = [encode(["bytes", "bytes[]"], [bytes([0x06, 0x0B, 0x0E]), params])]
        commands = [0x10]
        if buy and token_in == WETH:
            commands.insert(0, 0x0B)
            inputs.insert(0, encode(["address", "uint256"], [pool.router, amount]))
        if not buy and token_out == WETH:
            commands.append(0x0C)
            inputs.append(encode(["address", "uint256"], [wallet, minimum]))
        # Sweep leftover ETH/WETH on partial fill rather than trapping input.
        commands.append(0x04)
        inputs.append(encode(["address", "address", "uint256"], [token_in, wallet, 0]))
        data = call_data(
            "execute(bytes,bytes[],uint256)",
            ["bytes", "bytes[]", "uint256"],
            [bytes(commands), inputs, deadline],
        )
    return {"from": wallet, "to": pool.router, "data": data, "value": hex(amount if buy else 0)}
