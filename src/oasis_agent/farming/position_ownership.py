from dataclasses import dataclass

from eth_hash.auto import keccak

from ..models.chain import LogRecord
from ..models.common import ZERO_ADDRESS, BlockRef, normalize_address, normalize_hash32
from .positions import PositionSnapshot

TRANSFER_EVENT_SIGNATURE = "Transfer(address,address,uint256)"
TRANSFER_TOPIC = "0x" + keccak(TRANSFER_EVENT_SIGNATURE.encode()).hex()
UINT256_MAX = (1 << 256) - 1


def _topic_address(topic: str) -> str:
    raw = bytes.fromhex(topic[2:])
    if len(raw) != 32 or raw[:12] != b"\x00" * 12:
        raise ValueError("indexed owner address is not ABI padded")
    return normalize_address("0x" + raw[-20:].hex())


@dataclass(frozen=True, slots=True)
class PositionTransferEvent:
    block: BlockRef
    transaction_hash: str
    transaction_index: int
    log_index: int
    position_manager: str
    from_owner: str
    to_owner: str
    token_id: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "transaction_hash", normalize_hash32(self.transaction_hash, "transaction hash")
        )
        object.__setattr__(self, "position_manager", normalize_address(self.position_manager))
        object.__setattr__(self, "from_owner", normalize_address(self.from_owner))
        object.__setattr__(self, "to_owner", normalize_address(self.to_owner))
        for name, value in (
            ("transaction_index", self.transaction_index),
            ("log_index", self.log_index),
            ("token_id", self.token_id),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must be nonnegative")
        if self.token_id > UINT256_MAX:
            raise ValueError("token_id must fit uint256")
        # ERC-721 permits nonzero self-transfers (and clears token approval).
        # Retain them in the audit stream even though ownership does not change.
        if self.from_owner == ZERO_ADDRESS and self.to_owner == ZERO_ADDRESS:
            raise ValueError("position transfer endpoints cannot both be zero")
        if self.block.timestamp is None:
            raise ValueError("position transfer requires a timestamped block")

    @property
    def position(self) -> tuple[int, int, int]:
        return self.block.number, self.transaction_index, self.log_index


@dataclass(frozen=True, slots=True)
class PositionOwnershipReplay:
    opening: PositionSnapshot
    closing: PositionSnapshot
    transfers: tuple[PositionTransferEvent, ...]
    final_owner: str


def decode_position_transfer(
    block: BlockRef,
    position_manager: str,
    log: LogRecord,
) -> PositionTransferEvent:
    manager = normalize_address(position_manager)
    if log.removed:
        raise ValueError("removed position Transfer log cannot enter canonical replay")
    if log.address != manager:
        raise ValueError("Transfer log was not emitted by the pinned PositionManager")
    if len(log.topics) != 4 or log.topics[0] != TRANSFER_TOPIC or log.data != "0x":
        raise ValueError("invalid ERC-721 Transfer event")
    if log.block_hash is None or log.transaction_index is None:
        raise ValueError("position Transfer log lacks block binding")
    if log.block_number != block.number or log.block_hash != block.block_hash:
        raise ValueError("position Transfer log does not match the pinned block")
    token_id = int(normalize_hash32(log.topics[3], "token ID topic"), 16)
    return PositionTransferEvent(
        block,
        log.transaction_hash,
        log.transaction_index,
        log.log_index,
        manager,
        _topic_address(log.topics[1]),
        _topic_address(log.topics[2]),
        token_id,
    )


def replay_position_ownership(
    opening: PositionSnapshot,
    closing: PositionSnapshot,
    transfers: tuple[PositionTransferEvent, ...],
) -> PositionOwnershipReplay:
    if opening.position_manager != closing.position_manager or opening.token_id != closing.token_id:
        raise ValueError("position identity changed across ownership replay")
    if opening.block.chain_id != closing.block.chain_id:
        raise ValueError("position ownership replay cannot cross chains")
    if opening.block.number > closing.block.number:
        raise ValueError("position ownership replay window is reversed")
    positions = tuple(event.position for event in transfers)
    if positions != tuple(sorted(positions)) or len(set(positions)) != len(positions):
        raise ValueError("position transfers must be in strict event order")

    owner = opening.nft_owner
    for event in transfers:
        if event.position_manager != opening.position_manager or event.token_id != opening.token_id:
            raise ValueError("ownership event belongs to a different position")
        if event.block.chain_id != opening.block.chain_id:
            raise ValueError("ownership event belongs to a different chain")
        if not opening.block.number < event.block.number <= closing.block.number:
            raise ValueError("ownership event is outside the snapshot interval")
        if event.from_owner != owner:
            raise ValueError("position ownership chain is discontinuous")
        if event.to_owner == ZERO_ADDRESS:
            raise ValueError("live closing snapshot cannot follow a position burn")
        owner = event.to_owner
    if owner != closing.nft_owner:
        raise ValueError("replayed owner does not match closing snapshot")
    return PositionOwnershipReplay(opening, closing, transfers, owner)


__all__ = [
    "TRANSFER_EVENT_SIGNATURE",
    "TRANSFER_TOPIC",
    "PositionOwnershipReplay",
    "PositionTransferEvent",
    "decode_position_transfer",
    "replay_position_ownership",
]
