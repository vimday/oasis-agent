from __future__ import annotations

from dataclasses import dataclass

from ..models.common import BlockRef, normalize_hash32
from .replay import PoolAccountingReplay
from .state_rpc import Slot0State


@dataclass(frozen=True, slots=True)
class PoolCheckpoint:
    block: BlockRef
    pool_id: str
    slot0: Slot0State
    liquidity: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "pool_id", normalize_hash32(self.pool_id, "pool id"))
        if isinstance(self.liquidity, bool) or not isinstance(self.liquidity, int):
            raise TypeError("checkpoint liquidity must be an integer")
        if not 0 <= self.liquidity < 1 << 128:
            raise ValueError("checkpoint liquidity must fit uint128")


@dataclass(frozen=True, slots=True)
class PoolCheckpointReconciliation:
    replay: PoolAccountingReplay
    checkpoint: PoolCheckpoint
    closed: bool
    dynamic_persistent_fee: int | None
    last_effective_swap_fee: int | None


def reconcile_pool_checkpoint(
    replay: PoolAccountingReplay, checkpoint: PoolCheckpoint
) -> PoolCheckpointReconciliation:
    state = replay.final_state
    if checkpoint.block != state.block:
        raise ValueError("checkpoint block does not match replay final block")
    if checkpoint.pool_id != state.pool_id:
        raise ValueError("checkpoint belongs to a different pool")
    if checkpoint.slot0.sqrt_price_x96 != state.sqrt_price_x96:
        raise ValueError("replayed sqrt price does not match StateView")
    if checkpoint.slot0.tick != state.tick:
        raise ValueError("replayed tick does not match StateView")
    if checkpoint.liquidity != state.liquidity:
        raise ValueError("replayed liquidity does not match StateView")
    if not state.dynamic_fee and checkpoint.slot0.lp_fee != state.configured_pool_fee:
        raise ValueError("static configured fee does not match StateView")
    return PoolCheckpointReconciliation(
        replay,
        checkpoint,
        True,
        checkpoint.slot0.lp_fee if state.dynamic_fee else None,
        state.last_fee,
    )


__all__ = ["PoolCheckpoint", "PoolCheckpointReconciliation", "reconcile_pool_checkpoint"]
