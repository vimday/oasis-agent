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

from ..models.common import EvidenceRef, canonical_json, evidence_ref_from_json
from .exact_risk import ExactRisk, legacy_ceiling
from .intent import SubmittedTransaction
from .journal import LiveJournal

DAY_ZONE = timezone(timedelta(hours=8))
CHECKPOINT_SECONDS = 3600
MAX_OBSERVATION_FAILURES = 3
MAX_SETTLEMENT_FAILURES = 5


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
    nav_evidence: tuple[EvidenceRef, ...] = ()
    journal_required: bool = False
    exact_risk: ExactRisk | None = None
    signing_attempt: dict[str, Any] | None = None
    settled_blocks: list[dict[str, Any]] = field(default_factory=list)
    # Last cheap in-range check between full observations; display only, never risk input.
    last_light_check: dict[str, int] | None = None
    # Reserved full observations since the last complete one; survives a process crash.
    observation_attempts: int = 0
    settlement_attempts: int = 0

    def integer_risk(self) -> ExactRisk:
        if self.exact_risk is None:
            self.exact_risk = ExactRisk()
            if self.initial_nav_usd is not None:
                if self.checkpoint_nav_usd is None:
                    raise ValueError("legacy risk baseline incomplete")
                self.exact_risk = ExactRisk(
                    basis=legacy_ceiling(self.initial_nav_usd),
                    external=legacy_ceiling(self.external_flow_usd),
                    checkpoint=legacy_ceiling(self.checkpoint_nav_usd),
                    checkpoint_at=self.checkpoint_at,
                    day=self.day,
                    daily_locked=legacy_ceiling(self.daily_loss_usd),
                    migrated_legacy=True,
                )
        self.exact_risk.validate()
        return self.exact_risk

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
    if (
        type(state.observation_attempts) is not int
        or not 0 <= state.observation_attempts <= MAX_OBSERVATION_FAILURES
    ):
        raise ValueError("invalid durable observation attempts")
    if (
        type(state.settlement_attempts) is not int
        or not 0 <= state.settlement_attempts <= MAX_SETTLEMENT_FAILURES
        or (state.pending is None and state.settlement_attempts != 0)
    ):
        raise ValueError("invalid durable settlement attempts")
    if not isinstance(state.settled_blocks, list) or len(state.settled_blocks) > 10_000:
        raise ValueError("settlement anchor budget exceeded")
    for anchor in state.settled_blocks:
        if (
            not isinstance(anchor, dict)
            or set(anchor) != {"number", "hash", "transaction_hash"}
            or type(anchor["number"]) is not int
            or anchor["number"] < 0
            or any(
                not isinstance(anchor[k], str)
                or len(anchor[k]) != 66
                or not anchor[k].startswith("0x")
                or any(c not in "0123456789abcdef" for c in anchor[k][2:])
                for k in ("hash", "transaction_hash")
            )
        ):
            raise ValueError("invalid settlement anchor")
    if state.signing_attempt is not None and not isinstance(state.signing_attempt, dict):
        raise ValueError("invalid signing reservation")
    light = state.last_light_check
    if light is not None and (
        not isinstance(light, dict)
        or set(light) != {"block", "block_timestamp", "observed_at", "tick"}
        or any(type(v) is not int for v in light.values())
        or light["block"] < 0
        or not 0 <= light["block_timestamp"] <= light["observed_at"]
        or not -887272 <= light["tick"] <= 887272
    ):
        raise ValueError("invalid light check record")
    if state.exact_risk is not None:
        state.exact_risk.validate()
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
        type(state.journal_required) is not bool
        or type(state.acknowledge_flow) is not bool
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
    def __init__(self, path: Path, *, require_existing: bool = False) -> None:
        self.path = path
        self.require_existing = require_existing
        self.journal: LiveJournal | None = None

    @property
    def journal_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".journal.sqlite")

    def bind(self, config_hash: str, *, require_existing: bool = False) -> None:
        require_existing = require_existing or self.require_existing
        # Validate any pre-existing journal before allowing writes under this policy.
        if require_existing and not self.journal_path.is_file():
            raise ValueError("recovery requires an existing execution journal")
        original = self.load("live")
        self.journal = LiveJournal(
            self.journal_path, config_hash, require_existing=require_existing
        )
        latest = self.journal.load()
        if latest is None:
            if require_existing:
                raise ValueError("recovery requires a nonempty execution journal")
            self.journal.append(asdict(original))
            original.journal_required = True
            self.save(original)

    def load(self, mode: str, *, read_only: bool = False) -> LoopState:
        if self.require_existing and (
            mode != "live" or not self.path.is_file() or not self.journal_path.is_file()
        ):
            raise ValueError("production startup requires original state and journal")
        projection: bytes | None = None
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            pass
        else:
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 2_000_000:
                    raise ValueError("bounded regular state file required")
                projection = handle.read(2_000_001)
            if len(projection) > 2_000_000:
                raise ValueError("state exceeds byte bound")
            # Validate duplicate/non-finite values before canonicalization in the journal.
            self._decode(projection, mode)
        if mode == "live" and self.journal_path.exists():
            if self.journal is None:
                self.journal = LiveJournal(self.journal_path)
            payload = self.journal.load(projection, read_only=read_only)
            if payload is None:
                raise ValueError("live journal is empty")
            return self._decode(payload, mode)
        state = LoopState(mode=mode) if projection is None else self._decode(projection, mode)
        if state.journal_required:
            raise ValueError("required execution journal missing")
        return state

    def retained_payload(self, state: LoopState) -> bytes:
        """Return the verified bytes of this loaded state without refreshing its CAS.

        Decoding an older snapshot adds optional defaults. Approval hashes must
        still name the original journal bytes, not a new serialization of them.
        A separate reader keeps a changed tip from advancing this writer's CAS.
        """
        journal = self.journal
        if (
            state.mode != "live"
            or journal is None
            or journal.config_hash is None
            or journal.revision is None
            or journal.path != self.journal_path
        ):
            raise ValueError("loaded live journal snapshot required")
        revision, binding = journal.revision, journal.config_hash
        reader = LiveJournal(self.journal_path, binding, require_existing=True)
        payload = reader.load(read_only=True)
        if (
            payload is None
            or (reader.revision, reader.config_hash) != (revision, binding)
            or self.journal is not journal
            or (journal.revision, journal.config_hash) != (revision, binding)
        ):
            raise ValueError("retained journal snapshot changed")
        validate_state(state)
        if canonical_json(asdict(self._decode(payload, "live"))) != canonical_json(asdict(state)):
            raise ValueError("state differs from retained journal snapshot")
        return payload

    @staticmethod
    def _decode(payload: bytes, mode: str) -> LoopState:
        raw = json.loads(payload, object_pairs_hook=_unique_state)
        if raw.get("mode") != mode:
            raise ValueError(f"state file belongs to mode {raw.get('mode')!r}, not {mode!r}")
        # Older live states counted execution gas only. Preserve their total,
        # but never relabel it as fully reconstructed native transaction cost.
        raw.setdefault(
            "legacy_gas_cost_incomplete", mode == "live" and raw.get("gas_spent_wei", 0) > 0
        )
        if raw.get("exact_risk") is not None:
            raw["exact_risk"] = ExactRisk(**raw["exact_risk"])
        raw["nav_evidence"] = tuple(evidence_ref_from_json(r) for r in raw.get("nav_evidence", []))
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

    def historical_pending(self, nonce: int) -> Pending | None:
        if self.journal is None:
            raise ValueError("historical lookup requires a bound journal")
        payload = self.journal.pending_snapshot(nonce)
        if payload is None:
            return None
        state = self._decode(payload, "live")
        if state.signing_attempt is not None or state.pending is None:
            raise ValueError("historical signing outcome unresolved")
        return state.pending

    def save(self, state: LoopState) -> None:
        if self.require_existing and (
            state.mode != "live" or not self.path.is_file() or not self.journal_path.is_file()
        ):
            raise ValueError("production state or journal disappeared")
        if state.journal_required and not self.journal_path.exists():
            raise ValueError("required execution journal missing")
        validate_state(state)
        payload = json.dumps(asdict(state), indent=1, sort_keys=True, allow_nan=False)
        if len(payload.encode()) > 2_000_000:
            raise ValueError("state exceeds byte bound")
        if state.mode == "live" and self.journal_path.exists():
            if self.journal is None:
                raise ValueError("journal state must be loaded before saving")
            self.journal.append(asdict(state))
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
