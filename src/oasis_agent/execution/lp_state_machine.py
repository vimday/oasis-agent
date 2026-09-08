"""LP rebalance execution state machine (blueprint section 6.5).

The state set is the canonical Phase-1 lifecycle:

    OBSERVED -> PROPOSED -> RESERVED -> PINNED_SIMULATED -> POLICY_APPROVED
             -> SIGNED -> SUBMITTED -> SOFT_CONFIRMED -> RECONCILING
             -> L1_POSTED/FINALIZED -> CLOSED

    Any state -> EXPIRED
    Any uncertain state -> HALTED; only verified risk reduction may proceed

Intent identity includes chain, position, old/new ranges, exact amounts,
maximum swap input, minimum output, fee/gas bounds, nonce, state block/hash,
deadline, config hash, and code hashes. A ledger wrapper enforces that retries
cannot create a second economic rebalance: once an intent hash has been
consumed (submitted or later), recreating it requires a fresh nonce.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..errors import ExecutionBlockedError
from ..models.common import canonical_json, normalize_address, normalize_hash32, sha256_hex
from ..uniswap_v4.liquidity_math import MAX_TICK, MIN_TICK


class LpState(str, Enum):
    OBSERVED = "observed"
    PROPOSED = "proposed"
    RESERVED = "reserved"
    PINNED_SIMULATED = "pinned_simulated"
    POLICY_APPROVED = "policy_approved"
    SIGNED = "signed"
    SUBMITTED = "submitted"
    SOFT_CONFIRMED = "soft_confirmed"
    RECONCILING = "reconciling"
    L1_POSTED = "l1_posted"
    FINALIZED = "finalized"
    CLOSED = "closed"
    EXPIRED = "expired"
    HALTED = "halted"


class LpIntentMode(str, Enum):
    RECENTER = "recenter"
    REMOVE = "remove"


VALID_TRANSITIONS: dict[LpState, frozenset[LpState]] = {
    LpState.OBSERVED: frozenset({LpState.PROPOSED, LpState.EXPIRED}),
    LpState.PROPOSED: frozenset({LpState.RESERVED, LpState.EXPIRED}),
    LpState.RESERVED: frozenset({LpState.PINNED_SIMULATED, LpState.EXPIRED, LpState.HALTED}),
    LpState.PINNED_SIMULATED: frozenset({LpState.POLICY_APPROVED, LpState.EXPIRED, LpState.HALTED}),
    LpState.POLICY_APPROVED: frozenset({LpState.SIGNED, LpState.EXPIRED, LpState.HALTED}),
    LpState.SIGNED: frozenset({LpState.SUBMITTED, LpState.EXPIRED, LpState.HALTED}),
    LpState.SUBMITTED: frozenset({LpState.SOFT_CONFIRMED, LpState.HALTED, LpState.EXPIRED}),
    LpState.SOFT_CONFIRMED: frozenset({LpState.RECONCILING, LpState.HALTED}),
    LpState.RECONCILING: frozenset({LpState.L1_POSTED, LpState.FINALIZED, LpState.HALTED}),
    LpState.L1_POSTED: frozenset({LpState.FINALIZED, LpState.HALTED}),
    LpState.FINALIZED: frozenset({LpState.CLOSED, LpState.HALTED}),
    # CLOSED and EXPIRED are terminal; from HALTED only verified risk reduction
    # (a safe unwind to CLOSED) may proceed, never a new exposure.
    LpState.CLOSED: frozenset(),
    LpState.EXPIRED: frozenset(),
    LpState.HALTED: frozenset({LpState.CLOSED}),
}


@dataclass(frozen=True, slots=True)
class TickRange:
    tick_lower: int
    tick_upper: int
    tick_spacing: int

    def __post_init__(self) -> None:
        for name, value in (
            ("tick_lower", self.tick_lower),
            ("tick_upper", self.tick_upper),
            ("tick_spacing", self.tick_spacing),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
        if self.tick_spacing <= 0:
            raise ValueError("tick_spacing must be positive")
        if self.tick_spacing > 32767:
            raise ValueError("tick_spacing exceeds canonical v4 maximum")
        if self.tick_lower < MIN_TICK or self.tick_upper > MAX_TICK:
            raise ValueError("range exceeds canonical v4 tick bounds")
        if self.tick_lower >= self.tick_upper:
            raise ValueError("tick_lower must be below tick_upper")
        if self.tick_lower % self.tick_spacing != 0 or self.tick_upper % self.tick_spacing != 0:
            raise ValueError("range ticks must align to tick spacing")


@dataclass(frozen=True, slots=True)
class LpIntent:
    """Fully pinned economic intent for one LP rebalance."""

    intent_id: str
    chain_id: int
    pool_id: str
    position_token_id: int
    mode: LpIntentMode
    target: str
    calldata_hash: str
    old_range: TickRange
    new_range: TickRange
    liquidity: int
    amount0_min: int
    amount1_min: int
    amount0_max: int
    amount1_max: int
    max_swap_input: int
    max_fee_pips: int
    max_gas_units: int
    nonce: int
    state_block: int
    state_hash: str
    deadline_timestamp: int
    config_hash: str
    code_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.intent_id:
            raise ValueError("intent_id is required")
        if not self.pool_id:
            raise ValueError("pool_id is required")
        if not self.state_hash or not self.config_hash:
            raise ValueError("state_hash and config_hash are required")
        if not self.code_hashes or any(not h for h in self.code_hashes):
            raise ValueError("code_hashes must be a nonempty tuple of nonempty hashes")
        if not isinstance(self.mode, LpIntentMode):
            raise TypeError("mode must be an LpIntentMode")
        object.__setattr__(self, "target", normalize_address(self.target))
        if int(self.target, 16) == 0:
            raise ValueError("intent target must be nonzero")
        for name in ("pool_id", "calldata_hash", "state_hash", "config_hash"):
            object.__setattr__(self, name, normalize_hash32(getattr(self, name), name))
        object.__setattr__(
            self, "code_hashes", tuple(normalize_hash32(value) for value in self.code_hashes)
        )
        for name, value in (
            ("chain_id", self.chain_id),
            ("position_token_id", self.position_token_id),
            ("liquidity", self.liquidity),
            ("amount0_min", self.amount0_min),
            ("amount1_min", self.amount1_min),
            ("amount0_max", self.amount0_max),
            ("amount1_max", self.amount1_max),
            ("max_swap_input", self.max_swap_input),
            ("max_fee_pips", self.max_fee_pips),
            ("max_gas_units", self.max_gas_units),
            ("nonce", self.nonce),
            ("state_block", self.state_block),
            ("deadline_timestamp", self.deadline_timestamp),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value >= 1 << 256:
                raise ValueError(f"{name} exceeds uint256")
        if self.chain_id <= 0 or self.position_token_id <= 0:
            raise ValueError("chain_id and position_token_id must be positive")
        if (
            self.liquidity < 0
            or self.amount0_min < 0
            or self.amount1_min < 0
            or self.amount0_max < 0
            or self.amount1_max < 0
            or self.max_swap_input < 0
            or self.max_fee_pips < 0
            or self.max_gas_units < 0
            or self.nonce < 0
        ):
            raise ValueError("amount, bound, and nonce fields must be nonnegative")
        if self.nonce >= 1 << 64 or self.deadline_timestamp >= 1 << 64:
            raise ValueError("executor nonce and deadline must fit uint64")
        if any(
            value >= 1 << 128
            for value in (self.amount0_min, self.amount1_min, self.amount0_max, self.amount1_max)
        ):
            raise ValueError("token settlement bounds must fit uint128")
        if self.amount0_min > self.amount0_max or self.amount1_min > self.amount1_max:
            raise ValueError("minimum token amounts cannot exceed maximums")
        if self.mode is LpIntentMode.REMOVE and self.liquidity != 0:
            raise ValueError("remove intent liquidity must be zero")
        if self.mode is LpIntentMode.RECENTER and self.liquidity == 0:
            raise ValueError("recenter intent liquidity must be positive")
        if self.state_block <= 0 or self.deadline_timestamp <= 0:
            raise ValueError("state_block and deadline_timestamp must be positive")
        if not isinstance(self.old_range, TickRange) or not isinstance(self.new_range, TickRange):
            raise TypeError("ranges must be TickRange instances")
        if self.old_range.tick_spacing != self.new_range.tick_spacing:
            raise ValueError("rebalance cannot change pool tick spacing")
        if self.max_fee_pips > 1_000_000:
            raise ValueError("max_fee_pips exceeds canonical LP fee maximum")

    @property
    def intent_hash(self) -> str:
        return sha256_hex(
            canonical_json(
                {
                    "intent_id": self.intent_id,
                    "chain_id": self.chain_id,
                    "pool_id": self.pool_id,
                    "position_token_id": self.position_token_id,
                    "mode": self.mode.value,
                    "target": self.target,
                    "calldata_hash": self.calldata_hash,
                    "old_range": (
                        self.old_range.tick_lower,
                        self.old_range.tick_upper,
                        self.old_range.tick_spacing,
                    ),
                    "new_range": (
                        self.new_range.tick_lower,
                        self.new_range.tick_upper,
                        self.new_range.tick_spacing,
                    ),
                    "liquidity": self.liquidity,
                    "amount0_min": self.amount0_min,
                    "amount1_min": self.amount1_min,
                    "amount0_max": self.amount0_max,
                    "amount1_max": self.amount1_max,
                    "max_swap_input": self.max_swap_input,
                    "max_fee_pips": self.max_fee_pips,
                    "max_gas_units": self.max_gas_units,
                    "nonce": self.nonce,
                    "state_block": self.state_block,
                    "state_hash": self.state_hash,
                    "deadline_timestamp": self.deadline_timestamp,
                    "config_hash": self.config_hash,
                    "code_hashes": self.code_hashes,
                }
            )
        )


@dataclass(frozen=True, slots=True)
class StateTransition:
    state: LpState
    note: str


class LpExecutionStateMachine:
    """In-memory lifecycle model, not a signer or evidence-based authorization gate.

    Transition ordering does not attest simulation, policy, finality, or unwind.
    A live adapter must independently verify those facts and persist recovery state.
    """

    def __init__(self, intent: LpIntent) -> None:
        self.intent = intent
        self.state = LpState.OBSERVED
        self.transaction_hash: str | None = None
        self._history: list[StateTransition] = [StateTransition(LpState.OBSERVED, "intent created")]

    @property
    def history(self) -> tuple[StateTransition, ...]:
        return tuple(self._history)

    @property
    def consumed(self) -> bool:
        """Latch that this lifecycle entered SUBMITTED, even after halt/expiry."""
        return any(item.state is LpState.SUBMITTED for item in self._history)

    def transition(self, target: LpState, note: str = "") -> LpState:
        if target not in VALID_TRANSITIONS[self.state]:
            raise ExecutionBlockedError(
                f"invalid LP state transition {self.state.value} -> {target.value}"
            )
        self.state = target
        self._history.append(StateTransition(target, note))
        return target

    def propose(self, note: str = "") -> LpState:
        return self.transition(LpState.PROPOSED, note)

    def reserve(self, note: str = "") -> LpState:
        return self.transition(LpState.RESERVED, note)

    def pin_simulated(self, ok: bool, note: str = "") -> LpState:
        if type(ok) is not bool:
            raise TypeError("simulation result must be a bool")
        if not ok:
            return self.transition(LpState.HALTED, note or "pinned simulation failed")
        return self.transition(LpState.PINNED_SIMULATED, note)

    def policy_approve(self, allowed: bool, note: str = "") -> LpState:
        if type(allowed) is not bool:
            raise TypeError("policy result must be a bool")
        if not allowed:
            return self.transition(LpState.HALTED, note or "risk policy denied intent")
        return self.transition(LpState.POLICY_APPROVED, note)

    def sign(self, note: str = "") -> LpState:
        return self.transition(LpState.SIGNED, note)

    def submit(self, transaction_hash: str, note: str = "") -> LpState:
        if not transaction_hash:
            raise ValueError("transaction hash is required")
        transaction_hash = normalize_hash32(transaction_hash, "transaction hash")
        result = self.transition(LpState.SUBMITTED, note)
        self.transaction_hash = transaction_hash
        return result

    def soft_confirm(self, note: str = "") -> LpState:
        return self.transition(LpState.SOFT_CONFIRMED, note)

    def reconcile(self, note: str = "") -> LpState:
        return self.transition(LpState.RECONCILING, note)

    def post_l1(self, note: str = "") -> LpState:
        return self.transition(LpState.L1_POSTED, note)

    def finalize(self, note: str = "") -> LpState:
        return self.transition(LpState.FINALIZED, note)

    def close(self, note: str = "") -> LpState:
        return self.transition(LpState.CLOSED, note)

    def expire(self, note: str = "") -> LpState:
        return self.transition(LpState.EXPIRED, note)

    def halt(self, reason: str = "", note: str = "") -> LpState:
        return self.transition(LpState.HALTED, note or f"halted: {reason}")


class LpExecutionLedger:
    """In-process registry rejecting reuse of an identical submitted intent hash.

    This is not durable account/nonce reservation: changing intent_id or
    restarting creates a different registry entry. Never use this model alone
    to authorize retries. An unknown submission must first be reconciled with
    the original account, nonce and transaction; timeout does not prove revert.
    """

    def __init__(self) -> None:
        self._machines: dict[str, LpExecutionStateMachine] = {}

    def create(self, intent: LpIntent) -> LpExecutionStateMachine:
        machine = self._machines.get(intent.intent_hash)
        if machine is not None and machine.consumed:
            raise ExecutionBlockedError("intent already consumed; retries require a fresh nonce")
        if machine is None:
            machine = LpExecutionStateMachine(intent)
            self._machines[intent.intent_hash] = machine
        return machine

    def machine(self, intent_hash: str) -> LpExecutionStateMachine | None:
        return self._machines.get(intent_hash)

    def is_consumed(self, intent_hash: str) -> bool:
        machine = self._machines.get(intent_hash)
        return machine is not None and machine.consumed


__all__ = [
    "VALID_TRANSITIONS",
    "LpExecutionLedger",
    "LpExecutionStateMachine",
    "LpIntent",
    "LpIntentMode",
    "LpState",
    "StateTransition",
    "TickRange",
]
