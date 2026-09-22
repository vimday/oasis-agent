"""Explicit LP position identity carried by retained strategy inputs."""

from dataclasses import dataclass

from .common import normalize_address, normalize_hash32


@dataclass(frozen=True, slots=True)
class LpPositionScope:
    chain_id: int
    position_manager: str
    token_id: int
    pool_id: str
    owner: str

    def __post_init__(self) -> None:
        for name in ("chain_id", "token_id"):
            value = getattr(self, name)
            if type(value) is not int or not 0 < value < 2**256:
                raise ValueError("positive uint256 chain and NFT identity required")
        for name in ("position_manager", "owner"):
            value = normalize_address(getattr(self, name))
            if int(value, 16) == 0:
                raise ValueError("position manager and owner must be nonzero")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "pool_id", normalize_hash32(self.pool_id))
