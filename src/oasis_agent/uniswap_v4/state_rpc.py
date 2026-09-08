from dataclasses import dataclass
from typing import Protocol, cast

from eth_abi.abi import decode, encode
from eth_hash.auto import keccak

from ..models.common import BlockRef, normalize_address, normalize_hash32, normalize_hex
from .fees import validate_lp_fee, validate_protocol_fee
from .liquidity_math import MAX_SQRT_PRICE, MAX_TICK, MIN_SQRT_PRICE, MIN_TICK


class StateCallRpc(Protocol):
    def chain_id(self) -> int: ...

    def call(self, to: str, data: str, block: int | str | BlockRef = "latest") -> str: ...


def _selector(signature: str) -> bytes:
    return keccak(signature.encode())[:4]


def _decode_exact(
    types: list[str], raw: str, expected_bytes: int, method: str
) -> tuple[object, ...]:
    payload = bytes.fromhex(normalize_hex(raw)[2:])
    if len(payload) != expected_bytes:
        raise ValueError(f"{method} returned {len(payload)} bytes; expected {expected_bytes}")
    try:
        return cast(tuple[object, ...], decode(types, payload))
    except Exception as exc:
        raise ValueError(f"{method} returned invalid ABI data") from exc


def _validate_tick(tick: int) -> None:
    if isinstance(tick, bool) or not isinstance(tick, int) or not MIN_TICK <= tick <= MAX_TICK:
        raise ValueError("tick is outside canonical TickMath bounds")


@dataclass(frozen=True, slots=True)
class Slot0State:
    sqrt_price_x96: int
    tick: int
    protocol_fee: int
    lp_fee: int

    def __post_init__(self) -> None:
        if not MIN_SQRT_PRICE <= self.sqrt_price_x96 < MAX_SQRT_PRICE:
            raise ValueError("slot0 sqrt price is outside initialized pool bounds")
        _validate_tick(self.tick)
        validate_protocol_fee(self.protocol_fee)
        validate_lp_fee(self.lp_fee)


@dataclass(frozen=True, slots=True)
class TickState:
    liquidity_gross: int
    liquidity_net: int
    fee_growth_outside0_x128: int
    fee_growth_outside1_x128: int


class RpcV4StateSource:
    def __init__(self, rpc: StateCallRpc, state_view: str) -> None:
        self.rpc = rpc
        self.state_view = normalize_address(state_view)

    def _call(self, signature: str, args: bytes, block: BlockRef) -> str:
        if block.chain_id != self.rpc.chain_id():
            raise ValueError("block and RPC chain IDs disagree")
        return self.rpc.call(
            self.state_view,
            "0x" + (_selector(signature) + args).hex(),
            block,
        )

    @staticmethod
    def _pool_arg(pool_id: str) -> bytes:
        return encode(["bytes32"], [bytes.fromhex(normalize_hash32(pool_id, "pool id")[2:])])

    def get_slot0(self, pool_id: str, block: BlockRef) -> Slot0State:
        raw = self._call("getSlot0(bytes32)", self._pool_arg(pool_id), block)
        sqrt_price, tick, protocol_fee, lp_fee = _decode_exact(
            ["uint160", "int24", "uint24", "uint24"], raw, 128, "getSlot0"
        )
        return Slot0State(
            cast(int, sqrt_price),
            cast(int, tick),
            cast(int, protocol_fee),
            cast(int, lp_fee),
        )

    def get_liquidity(self, pool_id: str, block: BlockRef) -> int:
        raw = self._call("getLiquidity(bytes32)", self._pool_arg(pool_id), block)
        (liquidity,) = _decode_exact(["uint128"], raw, 32, "getLiquidity")
        return cast(int, liquidity)

    def get_fee_growth_globals(self, pool_id: str, block: BlockRef) -> tuple[int, int]:
        raw = self._call("getFeeGrowthGlobals(bytes32)", self._pool_arg(pool_id), block)
        growth0, growth1 = _decode_exact(["uint256", "uint256"], raw, 64, "getFeeGrowthGlobals")
        return cast(int, growth0), cast(int, growth1)

    def get_tick_info(self, pool_id: str, tick: int, block: BlockRef) -> TickState:
        _validate_tick(tick)
        raw = self._call(
            "getTickInfo(bytes32,int24)",
            encode(
                ["bytes32", "int24"],
                [bytes.fromhex(normalize_hash32(pool_id, "pool id")[2:]), tick],
            ),
            block,
        )
        gross, net, growth0, growth1 = _decode_exact(
            ["uint128", "int128", "uint256", "uint256"], raw, 128, "getTickInfo"
        )
        return TickState(cast(int, gross), cast(int, net), cast(int, growth0), cast(int, growth1))


__all__ = ["RpcV4StateSource", "Slot0State", "StateCallRpc", "TickState"]
