"""Durable loop state and the USD loss latch, persisted with atomic replace.

Loss accounting is mark-to-market NAV of the dedicated wallet (native ETH, USDC
and the LP position) in USD at the Chainlink ETH/USD price, gas included:
- daily loss is gross over checkpoints: NAV is checkpointed hourly, after every
  confirmed action and at each UTC+8 day change. A decrease between checkpoints is
  locked into the day's loss and a later gain never subtracts it; the live
  drawdown from the last checkpoint is added on top. Checkpointing (not every 30 s
  tick) keeps pure ETH price noise from compounding, while every action, where
  losses are realized, locks in. The decrease across a day boundary is charged to
  the new day, so a missing midnight observation cannot hide it;
- cumulative loss is net capital loss against the funded basis (first NAV plus
  operator-acknowledged external flows). It is never reset by a restart.
Wallet balances may only change through the loop's own confirmed actions; any
other change latches `halted` until an operator acknowledges it as an external
flow. A breach latches `halted`; only an operator can clear it (`--clear-halt`).
"""

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DAY_ZONE = timezone(timedelta(hours=8))
CHECKPOINT_SECONDS = 3600


def day_of(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).astimezone(DAY_ZONE).date().isoformat()


@dataclass(slots=True)
class Position:
    token_id: int
    lower: int
    upper: int
    liquidity: int
    opened_at: int


@dataclass(slots=True)
class Pending:
    kind: str
    tx_hash: str
    nonce: int
    signed_at: int
    intent: dict[str, Any] = field(default_factory=dict)
    to: str = ""


@dataclass(slots=True)
class LoopState:
    mode: str
    position: Position | None = None
    pending: Pending | None = None
    halted: str | None = None
    initial_nav_usd: float | None = None
    external_flow_usd: float = 0.0
    day: str | None = None
    daily_loss_usd: float = 0.0
    checkpoint_nav_usd: float | None = None
    checkpoint_at: int = 0
    last_nav_usd: float | None = None
    # Raw wallet units at the last observation; None forces a fresh baseline.
    last_units: list[int] | None = None
    acknowledge_flow: bool = False
    actions_today: int = 0
    last_action_at: int = 0
    consecutive_reverts: int = 0
    approvals_until: int = 0
    gas_spent_wei: int = 0
    pins: dict[str, str] = field(default_factory=dict)
    # Shadow mode only: paper balances in raw units (wei / USDC micros).
    paper_eth: int = 0
    paper_usdc: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)

    def record(self, event: str, **data: Any) -> None:
        self.history.append({"event": event, **data})
        del self.history[:-200]

    def roll_day(self, timestamp: int, nav: float, flow_usd: float = 0.0) -> None:
        """Account one observation. `flow_usd` is an acknowledged external deposit (+)
        or withdrawal (-) since the previous observation; it is not a gain or loss."""
        today = day_of(timestamp)
        new_day = self.day != today
        if new_day:
            self.day, self.daily_loss_usd, self.actions_today = today, 0.0, 0
        if self.initial_nav_usd is None:
            self.initial_nav_usd = nav
        self.external_flow_usd += flow_usd
        if self.checkpoint_nav_usd is None:
            self.checkpoint_nav_usd, self.checkpoint_at = nav, timestamp
        self.checkpoint_nav_usd += flow_usd
        if new_day or timestamp - self.checkpoint_at >= CHECKPOINT_SECONDS:
            self.checkpoint(timestamp, nav)
        self.last_nav_usd = nav

    def checkpoint(self, timestamp: int, nav: float) -> None:
        if self.checkpoint_nav_usd is not None:
            self.daily_loss_usd += max(0.0, self.checkpoint_nav_usd - nav)
        self.checkpoint_nav_usd, self.checkpoint_at = nav, timestamp

    def losses(self, nav: float) -> tuple[float, float]:
        assert self.initial_nav_usd is not None and self.checkpoint_nav_usd is not None
        basis = self.initial_nav_usd + self.external_flow_usd
        drawdown = max(0.0, self.checkpoint_nav_usd - nav)
        return self.daily_loss_usd + drawdown, max(0.0, basis - nav)


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self, mode: str) -> LoopState:
        if not self.path.exists():
            return LoopState(mode=mode)
        raw = json.loads(self.path.read_text())
        if raw.get("mode") != mode:
            raise ValueError(f"state file belongs to mode {raw.get('mode')!r}, not {mode!r}")
        position = raw.pop("position")
        pending = raw.pop("pending")
        return LoopState(
            position=Position(**position) if position else None,
            pending=Pending(**pending) if pending else None,
            **raw,
        )

    def save(self, state: LoopState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as handle:
            json.dump(asdict(state), handle, indent=1, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(self.path)
