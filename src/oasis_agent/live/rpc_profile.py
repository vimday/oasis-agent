"""Explicit infrastructure pacing; never resets or expands a retained RPC quota."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self

from .config import _unique

if TYPE_CHECKING:
    from ..rpc.budget import BudgetPolicy


@dataclass(frozen=True, slots=True)
class LiveRpcProfile:
    max_requests: int = 4096
    min_interval_ms: int = 3000

    def __post_init__(self) -> None:
        if type(self.max_requests) is not int or not 1 <= self.max_requests <= 4096:
            raise ValueError("live RPC quota cannot exceed 4096 per provider")
        if type(self.min_interval_ms) is not int or not 100 <= self.min_interval_ms <= 60_000:
            raise ValueError("explicit live RPC interval must be 100..60000 ms")

    @classmethod
    def load(cls, path: Path | None) -> Self:
        if path is None:
            return cls()
        return cls.from_json(path.read_bytes())

    @classmethod
    def from_json(cls, data: bytes) -> Self:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=_unique)
        if not isinstance(raw, dict) or set(raw) != {"max_requests", "min_interval_ms"}:
            raise ValueError("RPC profile requires exactly quota and interval")
        return cls(**raw)

    def policies(self, sources: tuple[str, str]) -> tuple[BudgetPolicy, ...]:
        from ..rpc.budget import BudgetPolicy  # noqa: PLC0415 - static readers need no ledger

        return tuple(BudgetPolicy(s, self.max_requests, self.min_interval_ms) for s in sources)
