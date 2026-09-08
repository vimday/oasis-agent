from dataclasses import dataclass

from ..models.chain import LogRecord
from ..models.common import BlockRef, normalize_address
from ..models.v4 import ModifyLiquidityObservation
from ..uniswap_v4.events import decode_modify_liquidity, decode_modify_position


@dataclass(frozen=True, slots=True)
class CorrelatedPositionModification:
    block: BlockRef
    transaction_hash: str
    core_log_index: int
    position_manager_log_index: int
    position_manager: str
    initiator: str
    observation: ModifyLiquidityObservation


def correlate_position_modification(
    block: BlockRef,
    pool_manager: str,
    position_manager: str,
    core_log: LogRecord,
    position_manager_log: LogRecord,
) -> CorrelatedPositionModification:
    """Correlate a deployment-specific mirror event, not a canonical PosM requirement.

    The locked upstream v4-periphery PositionManager does not emit ModifyPosition.
    Use position_dataset for its core-event/NFT replay; require deployment evidence
    before applying this optional adapter to another implementation.
    """
    manager = normalize_address(position_manager)
    if core_log.address != normalize_address(pool_manager):
        raise ValueError("core modification log was not emitted by the pinned PoolManager")
    if position_manager_log.address != manager:
        raise ValueError("periphery modification log was not emitted by the pinned PositionManager")
    for log in (core_log, position_manager_log):
        if log.block_hash is None or log.transaction_index is None:
            raise ValueError("position modification log lacks block binding")
        if (
            log.block_number != block.number
            or log.block_hash != block.block_hash
            or log.transaction_hash != core_log.transaction_hash
        ):
            raise ValueError("position modification logs do not share the pinned transaction")
    if core_log.transaction_index != position_manager_log.transaction_index:
        raise ValueError("position modification logs have different transaction indexes")
    if core_log.log_index >= position_manager_log.log_index:
        raise ValueError("PositionManager mirror event must follow the core event")

    core = decode_modify_liquidity(core_log)
    mirror = decode_modify_position(position_manager_log)
    if core.sender != manager:
        raise ValueError("core position sender is not the pinned PositionManager")
    if (
        core.pool_id,
        core.tick_lower,
        core.tick_upper,
        core.liquidity_delta,
        core.salt,
    ) != (
        mirror.pool_id,
        mirror.tick_lower,
        mirror.tick_upper,
        mirror.liquidity_delta,
        mirror.salt,
    ):
        raise ValueError("PositionManager mirror event does not match the core event")
    return CorrelatedPositionModification(
        block,
        core_log.transaction_hash,
        core_log.log_index,
        position_manager_log.log_index,
        manager,
        mirror.sender,
        core,
    )


__all__ = ["CorrelatedPositionModification", "correlate_position_modification"]
