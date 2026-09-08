from typing import Protocol, cast

from eth_abi.abi import decode, encode
from eth_hash.auto import keccak

from ..models.common import BlockRef, normalize_address, normalize_hash32, normalize_hex
from ..models.v4 import PoolKey
from .positions import CorePositionState


class PositionCallRpc(Protocol):
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


class RpcPositionStateSource:
    """Block-pinned reader for canonical v4 PositionManager and StateView contracts."""

    def __init__(self, rpc: PositionCallRpc, position_manager: str, state_view: str) -> None:
        self.rpc = rpc
        self.position_manager = normalize_address(position_manager)
        self.state_view = normalize_address(state_view)

    def _call(self, to: str, signature: str, args: bytes, block: BlockRef) -> str:
        if block.chain_id != self.rpc.chain_id():
            raise ValueError("block and RPC chain IDs disagree")
        data = "0x" + (_selector(signature) + args).hex()
        return self.rpc.call(to, data, block)

    @staticmethod
    def _token_arg(token_id: int) -> bytes:
        return encode(["uint256"], [token_id])

    def owner_of(self, token_id: int, block: BlockRef) -> str:
        raw = self._call(
            self.position_manager,
            "ownerOf(uint256)",
            self._token_arg(token_id),
            block,
        )
        (owner,) = _decode_exact(["address"], raw, 32, "ownerOf")
        return normalize_address(cast(str, owner))

    def pool_and_position_info(self, token_id: int, block: BlockRef) -> tuple[PoolKey, int]:
        raw = self._call(
            self.position_manager,
            "getPoolAndPositionInfo(uint256)",
            self._token_arg(token_id),
            block,
        )
        key_value, packed_info = _decode_exact(
            ["(address,address,uint24,int24,address)", "uint256"],
            raw,
            192,
            "getPoolAndPositionInfo",
        )
        currency0, currency1, fee, tick_spacing, hooks = cast(
            tuple[str, str, int, int, str], key_value
        )
        return (
            PoolKey(currency0, currency1, fee, tick_spacing, hooks),
            cast(int, packed_info),
        )

    def position_liquidity(self, token_id: int, block: BlockRef) -> int:
        raw = self._call(
            self.position_manager,
            "getPositionLiquidity(uint256)",
            self._token_arg(token_id),
            block,
        )
        (liquidity,) = _decode_exact(["uint128"], raw, 32, "getPositionLiquidity")
        return cast(int, liquidity)

    def core_position(
        self, pool_id: str, core_position_id: str, block: BlockRef
    ) -> CorePositionState:
        args = encode(
            ["bytes32", "bytes32"],
            [
                bytes.fromhex(normalize_hash32(pool_id, "pool id")[2:]),
                bytes.fromhex(normalize_hash32(core_position_id, "position id")[2:]),
            ],
        )
        raw = self._call(
            self.state_view,
            "getPositionInfo(bytes32,bytes32)",
            args,
            block,
        )
        liquidity, growth0, growth1 = _decode_exact(
            ["uint128", "uint256", "uint256"], raw, 96, "getPositionInfo"
        )
        return CorePositionState(cast(int, liquidity), cast(int, growth0), cast(int, growth1))

    def fee_growth_inside(
        self, pool_id: str, tick_lower: int, tick_upper: int, block: BlockRef
    ) -> tuple[int, int]:
        args = encode(
            ["bytes32", "int24", "int24"],
            [bytes.fromhex(normalize_hash32(pool_id, "pool id")[2:]), tick_lower, tick_upper],
        )
        raw = self._call(
            self.state_view,
            "getFeeGrowthInside(bytes32,int24,int24)",
            args,
            block,
        )
        growth0, growth1 = _decode_exact(["uint256", "uint256"], raw, 64, "getFeeGrowthInside")
        return cast(int, growth0), cast(int, growth1)


__all__ = ["PositionCallRpc", "RpcPositionStateSource"]
