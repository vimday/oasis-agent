"""Fixed-size native ETH round-trip quote evidence on Robinhood Chain."""

import re
from dataclasses import dataclass

from eth_abi.abi import encode
from eth_hash.auto import keccak

from ..arbitrage.cycle_protocols import abi, calldata, data_bytes
from ..errors import FeeEncodingError
from ..fastpath.multicall_universe import (
    MULTICALL3,
    _decode_aggregate3_results,
    _encode_aggregate3_calldata,
)
from ..models.common import ZERO_ADDRESS
from ..uniswap_v4.quotes import QuotePoolKey, decode_single_pool_quote
from ..uniswap_v4.state_rpc import Slot0State
from .nitro_block import NitroHeader, nitro_header
from .robinhood_nitro import CHAIN_ID, FeedMessage
from .rpc_validation import rpc_quantity, strict_integer, strict_list, strict_object, strict_string

MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
STATE_VIEW = "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b"
QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"
POOL_ABI = ("address", "address", "uint24", "int24", "address")
PATH_ABI = "(address,(address,uint24,int24,address,bytes)[],uint128)"
INT128 = 1 << 127


@dataclass(frozen=True, slots=True)
class NativeRoute:
    name: str
    amount: int
    pools: tuple[QuotePoolKey, ...]

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", self.name):
            raise ValueError("invalid route name")
        if type(self.amount) is not int or not 0 < self.amount < INT128:
            raise ValueError("route amount outside positive int128")
        if not 2 <= len(self.pools) <= 200 or len(set(self.pools)) != len(self.pools):
            raise ValueError("route requires 2 to 200 distinct pools")
        for pool in self.pools:
            if pool.hooks != ZERO_ADDRESS or pool.fee >= 1_000_000 or pool.tick_spacing > 32_767:
                raise ValueError("route requires canonical static no-hook PoolKeys")
        self.path()

    def path(self) -> tuple[tuple[str, int, int, str, bytes], ...]:
        currency = ZERO_ADDRESS
        path = []
        for pool in self.pools:
            if currency == pool.currency0:
                currency = pool.currency1
            elif currency == pool.currency1:
                currency = pool.currency0
            else:
                raise ValueError("noncontiguous route")
            path.append((currency, pool.fee, pool.tick_spacing, pool.hooks, b""))
        if currency != ZERO_ADDRESS:
            raise ValueError("route does not close in native ETH")
        return tuple(path)

    def quote_data(self) -> str:
        return calldata(
            f"quoteExactInput({PATH_ABI})", (PATH_ABI,), ((ZERO_ADDRESS, self.path(), self.amount),)
        )


def parse_routes(raw: object) -> tuple[NativeRoute, ...]:
    doc = strict_object(raw, "quote routes")
    if not 1 <= len(doc) <= 8:
        raise ValueError("quote scope requires 1 to 8 routes")
    routes = []
    for name, raw_route in doc.items():
        route = strict_object(raw_route, "quote route")
        if set(route) != {"amount_in", "pools"}:
            raise ValueError("unexpected quote route fields")
        pools = []
        for row in strict_list(route["pools"], "route pools"):
            key = strict_list(row, "PoolKey")
            if len(key) != 5:
                raise ValueError("PoolKey must have five fields")
            pools.append(
                QuotePoolKey(
                    strict_string(key[0], "currency0"),
                    strict_string(key[1], "currency1"),
                    strict_integer(key[2], "fee"),
                    strict_integer(key[3], "tick spacing"),
                    strict_string(key[4], "hooks"),
                )
            )
        routes.append(NativeRoute(name, strict_integer(route["amount_in"], "input"), tuple(pools)))
    if sum(len(route.pools) for route in routes) > 400:
        raise ValueError("total quote scope exceeds 400 legs")
    return tuple(routes)


def assert_feed_header(raw: object, message: FeedMessage) -> tuple[NitroHeader, int]:
    if message.block_hash is None:
        raise ValueError("missing feed block hash")
    header = nitro_header(raw, message.sequence, message.block_hash, chain_id=CHAIN_ID)
    if header.ref.timestamp != message.timestamp or header.transactions[1:] != message.transactions:
        raise ValueError("feed disagrees with Nitro time or ordered transaction hashes")
    return header, rpc_quantity(strict_object(raw, "header")["baseFeePerGas"])


def _pool_ids(routes: tuple[NativeRoute, ...]) -> dict[bytes, QuotePoolKey]:
    return {
        keccak(
            encode(
                POOL_ABI, (pool.currency0, pool.currency1, pool.fee, pool.tick_spacing, pool.hooks)
            )
        ): pool
        for route in routes
        for pool in route.pools
    }


def quote_multicall(routes: tuple[NativeRoute, ...]) -> str:
    calls = [
        (MULTICALL3, False, data_bytes(calldata("getChainId()"))),
        (STATE_VIEW, False, data_bytes(calldata("poolManager()"))),
        (QUOTER, False, data_bytes(calldata("poolManager()"))),
    ]
    calls.extend(
        (STATE_VIEW, False, data_bytes(calldata(getter, ("bytes32",), (pool_id,))))
        for pool_id in _pool_ids(routes)
        for getter in ("getSlot0(bytes32)", "getLiquidity(bytes32)")
    )
    # Reverted economic probes are retained independently of the state sentinels.
    calls.extend((QUOTER, True, data_bytes(route.quote_data())) for route in routes)
    return _encode_aggregate3_calldata(calls)


def read_quotes(raw: object, routes: tuple[NativeRoute, ...], base_fee: int) -> dict[str, object]:
    pools = _pool_ids(routes)
    raw_bytes = data_bytes(raw)
    results = _decode_aggregate3_results(raw_bytes, 3 + 2 * len(pools) + len(routes))
    identity = results[:3]
    if (
        not all(ok for ok, _ in identity)
        or abi(("uint256",), identity[0][1]) != (CHAIN_ID,)
        or any(abi(("address",), result) != (MANAGER,) for _, result in identity[1:])
    ):
        raise ValueError("quote state identity sentinels disagree")
    state = []
    offset = 3
    for pool_id, pool in pools.items():
        (slot_ok, slot), (liquidity_ok, liquidity) = results[offset : offset + 2]
        offset += 2
        if not (slot_ok and liquidity_ok):
            raise ValueError("quote state pool read reverted")
        price, tick, protocol_fee, lp_fee = abi(("uint160", "int24", "uint24", "uint24"), slot)
        (active,) = abi(("uint128",), liquidity)
        try:
            Slot0State(price, tick, protocol_fee, lp_fee)
        except FeeEncodingError as exc:
            raise ValueError("quote state pool fee encoding invalid") from exc
        if lp_fee != pool.fee:
            raise ValueError("pool static fee disagrees with key")
        state.append(
            {
                "pool_id": "0x" + pool_id.hex(),
                "sqrt_price_x96": price,
                "tick": tick,
                "protocol_fee_packed": protocol_fee,
                "lp_fee": lp_fee,
                "liquidity": active,
            }
        )
    quotes = []
    for route, (ok, payload) in zip(routes, results[offset:], strict=True):
        quote: dict[str, object] = {
            "route": route.name,
            "hops": len(route.pools),
            "input_wei": route.amount,
        }
        if ok:
            result = decode_single_pool_quote("0x" + payload.hex())
            if result.quoted_amount >= INT128 or result.gas_estimate <= 0:
                raise ValueError("quoter result out of range")
            gross = result.quoted_amount - route.amount
            quote.update(
                {
                    "status": "quoted",
                    "output_wei": result.quoted_amount,
                    "gross_wei": gross,
                    "quoter_gas_estimate": result.gas_estimate,
                    "optimistic_net_wei_before_other_costs": gross - 21_000 * base_fee,
                }
            )
        else:
            quote.update({"status": "quoter_reverted", "revert_data": "0x" + payload.hex()})
        quotes.append(quote)
    return {"pools": state, "quotes": quotes, "whole_transaction_simulated": False}
