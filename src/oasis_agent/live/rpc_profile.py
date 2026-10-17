"""Explicit infrastructure pacing; never resets or expands a retained RPC quota."""

import json
from dataclasses import dataclass
from pathlib import Path

from ..rpc.budget import BudgetPolicy
from .config import _unique


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
    def load(cls, path: Path | None) -> "LiveRpcProfile":
        if path is None:
            return cls()
        raw = json.loads(path.read_text(), object_pairs_hook=_unique)
        if not isinstance(raw, dict) or set(raw) != {"max_requests", "min_interval_ms"}:
            raise ValueError("RPC profile requires exactly quota and interval")
        return cls(**raw)

    def policies(self, sources: tuple[str, str]) -> tuple[BudgetPolicy, ...]:
        return tuple(BudgetPolicy(s, self.max_requests, self.min_interval_ms) for s in sources)
