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
import math
import os
import stat
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..models.common import EvidenceRef, evidence_ref_from_json
from .intent import SubmittedTransaction

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
    transaction: SubmittedTransaction | None = None
    plan_hash: str = ""
    wallet_evidence: tuple[EvidenceRef, ...] = ()


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
    # An explicit observation time, never a file-mtime or process-health claim.
    last_observation: dict[str, Any] | None = None
    # Raw wallet units at the last observation; None forces a fresh baseline.
    last_units: list[int] | None = None
    acknowledge_flow: bool = False
    actions_today: int = 0
    last_action_at: int = 0
    consecutive_reverts: int = 0
    approvals_until: int = 0
    gas_spent_wei: int = 0
    legacy_gas_cost_incomplete: bool = False
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


def _unique_state(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate live state key")
        result[key] = value
    return result


def validate_state(state: LoopState) -> None:
    """Reject corrupt risk inputs instead of allowing NaN to bypass loss comparisons."""
    if state.mode not in {"live", "shadow"}:
        raise ValueError("invalid loop mode")
    for name in ("initial_nav_usd", "checkpoint_nav_usd", "last_nav_usd", "daily_loss_usd"):
        value = getattr(state, name)
        if value is None and name != "daily_loss_usd":
            continue
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid NAV/loss checkpoint")
    if type(state.external_flow_usd) not in (int, float) or not math.isfinite(
        state.external_flow_usd
    ):
        raise ValueError("invalid external flow checkpoint")
    for name in (
        "checkpoint_at",
        "actions_today",
        "last_action_at",
        "consecutive_reverts",
        "approvals_until",
        "gas_spent_wei",
        "paper_eth",
        "paper_usdc",
    ):
        value = getattr(state, name)
        if type(value) is not int or not 0 <= value < 2**256:
            raise ValueError("invalid state counter")
    if (
        type(state.acknowledge_flow) is not bool
        or type(state.legacy_gas_cost_incomplete) is not bool
    ):
        raise ValueError("invalid state flag")
    if state.last_units is not None and (
        not isinstance(state.last_units, list)
        or len(state.last_units) != 3
        or any(type(v) is not int or not 0 <= v < 2**256 for v in state.last_units)
    ):
        raise ValueError("invalid wallet checkpoint")
    if not isinstance(state.history, list) or len(state.history) > 200:
        raise ValueError("invalid state history")


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self, mode: str) -> LoopState:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return LoopState(mode=mode)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 2_000_000:
                raise ValueError("bounded regular state file required")
            payload = handle.read(2_000_001)
        if len(payload) > 2_000_000:
            raise ValueError("state exceeds byte bound")
        raw = json.loads(payload, object_pairs_hook=_unique_state)
        if raw.get("mode") != mode:
            raise ValueError(f"state file belongs to mode {raw.get('mode')!r}, not {mode!r}")
        # Older live states counted execution gas only. Preserve their total,
        # but never relabel it as fully reconstructed native transaction cost.
        raw.setdefault(
            "legacy_gas_cost_incomplete", mode == "live" and raw.get("gas_spent_wei", 0) > 0
        )
        position = raw.pop("position")
        pending = raw.pop("pending")
        if pending and pending.get("transaction") is not None:
            pending["transaction"] = SubmittedTransaction(**pending["transaction"])
        if pending:
            pending["wallet_evidence"] = tuple(
                evidence_ref_from_json(r) for r in pending.get("wallet_evidence", [])
            )
        state = LoopState(
            position=Position(**position) if position else None,
            pending=Pending(**pending) if pending else None,
            **raw,
        )
        validate_state(state)
        return state

    def save(self, state: LoopState) -> None:
        validate_state(state)
        payload = json.dumps(asdict(state), indent=1, sort_keys=True, allow_nan=False)
        if len(payload.encode()) > 2_000_000:
            raise ValueError("state exceeds byte bound")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A predictable .tmp filename could follow a stale or malicious symlink.
        with tempfile.NamedTemporaryFile(
            mode="w", dir=self.path.parent, prefix=self.path.name + ".", delete=False
        ) as handle:
            tmp = Path(handle.name)
            try:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        try:
            tmp.replace(self.path)
        finally:
            tmp.unlink(missing_ok=True)
        directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
