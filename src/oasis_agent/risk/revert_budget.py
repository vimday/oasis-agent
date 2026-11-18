"""Rolling revert-budget and execution receipt classifier for OasisAtomicExecutor.

An early-abort slot0 guard hit emits `EarlyAborted` with `status == 1` and bounded
gas (`<= max_early_abort_gas`). It is a cheap on-chain probe (`EARLY_ABORT_PROBE`),
not an execution failure. By contrast, deep EVM reverts (`DEEP_EXECUTION_REVERT`),
missing/unmatched receipts (`AMBIGUOUS_HALT`), and excessive 1-hour net gas burn
trip a sticky `HALT` until operator inspection clears the condition.
"""

from collections import deque
from dataclasses import dataclass
from enum import StrEnum

from eth_hash.auto import keccak

from ..models.common import ZERO_ADDRESS, normalize_address, normalize_hash32, normalize_hex

EARLY_ABORTED_EVENT_SIGNATURE = "EarlyAborted(uint64,address,uint160)"
EARLY_ABORTED_TOPIC0: str = "0x" + keccak(EARLY_ABORTED_EVENT_SIGNATURE.encode()).hex()

CYCLE_EXECUTED_EVENT_SIGNATURE = "CycleExecuted(uint64,address,uint256,uint256,address)"
CYCLE_EXECUTED_TOPIC0: str = "0x" + keccak(CYCLE_EXECUTED_EVENT_SIGNATURE.encode()).hex()

DEFAULT_WINDOW_SECONDS = 3_600
DEFAULT_MAX_EARLY_ABORT_GAS = 65_000


class AttemptOutcome(StrEnum):
    """Normalized execution attempt outcome consumed by the rolling revert budget."""

    SUCCESS = "success"
    EARLY_ABORT_PROBE = "early_abort_probe"
    DEEP_EXECUTION_REVERT = "deep_execution_revert"
    AMBIGUOUS_HALT = "ambiguous_halt"


class RevertBudgetStatus(StrEnum):
    """Operating state of the rolling revert-budget gate."""

    ACTIVE = "active"
    HALT = "halt"


def _require_nonneg_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _require_pos_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class ReceiptLog:
    """Normalized EVM log entry for receipt classification."""

    address: str
    topics: tuple[str, ...]
    data: str = "0x"

    def __post_init__(self) -> None:
        address = normalize_address(self.address)
        if address == ZERO_ADDRESS:
            raise ValueError("log address must be nonzero")
        topics = tuple(normalize_hash32(topic, "log topic") for topic in self.topics)
        if not topics:
            raise ValueError("log requires at least topic0")
        object.__setattr__(self, "address", address)
        object.__setattr__(self, "topics", topics)
        object.__setattr__(self, "data", normalize_hex(self.data))


@dataclass(frozen=True, slots=True)
class ReceiptClassificationInput:
    """Observed receipt envelope bound to the expected atomic executor address."""

    executor_address: str
    status: int | None
    gas_used: int | None
    logs: tuple[ReceiptLog, ...] = ()
    receipt_missing: bool = False
    provider_disagreement: bool = False
    ambiguous_reason: str = ""

    def __post_init__(self) -> None:
        executor_address = normalize_address(self.executor_address)
        if executor_address == ZERO_ADDRESS:
            raise ValueError("executor_address must be nonzero")
        if self.status is not None and type(self.status) is not int:
            raise TypeError("status must be an integer or None")
        if self.gas_used is not None and (type(self.gas_used) is not int or self.gas_used < 0):
            raise ValueError("gas_used must be a nonnegative integer or None")
        if type(self.receipt_missing) is not bool or type(self.provider_disagreement) is not bool:
            raise TypeError("ambiguity flags must be booleans")
        logs = tuple(self.logs)
        if any(not isinstance(log, ReceiptLog) for log in logs):
            raise TypeError("all receipt logs must be ReceiptLog instances")
        object.__setattr__(self, "executor_address", executor_address)
        object.__setattr__(self, "logs", logs)


@dataclass(frozen=True, slots=True)
class RollingRevertBudgetPolicy:
    """Fail-closed thresholds for the 3,600-second rolling execution budget."""

    max_hourly_gas_burn_usd_micros: int
    max_deep_reverts_per_hour: int
    max_early_abort_gas: int = DEFAULT_MAX_EARLY_ABORT_GAS
    window_seconds: int = DEFAULT_WINDOW_SECONDS

    def __post_init__(self) -> None:
        _require_nonneg_int(
            self.max_hourly_gas_burn_usd_micros,
            "max_hourly_gas_burn_usd_micros",
        )
        _require_nonneg_int(self.max_deep_reverts_per_hour, "max_deep_reverts_per_hour")
        _require_pos_int(self.max_early_abort_gas, "max_early_abort_gas")
        _require_pos_int(self.window_seconds, "window_seconds")


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """Single timestamped execution attempt inside the rolling budget window."""

    timestamp_s: int
    outcome: AttemptOutcome
    gas_cost_usd_micros: int
    realized_profit_usd_micros: int = 0

    def __post_init__(self) -> None:
        _require_nonneg_int(self.timestamp_s, "timestamp_s")
        if not isinstance(self.outcome, AttemptOutcome):
            raise TypeError("outcome must be an AttemptOutcome")
        _require_nonneg_int(self.gas_cost_usd_micros, "gas_cost_usd_micros")
        _require_nonneg_int(self.realized_profit_usd_micros, "realized_profit_usd_micros")
        if self.outcome is not AttemptOutcome.SUCCESS and self.realized_profit_usd_micros != 0:
            raise ValueError("non-success attempt cannot credit realized profit")


@dataclass(frozen=True, slots=True)
class RevertBudgetSnapshot:
    """Immutable view of the rolling 1h revert budget and current gate verdict."""

    status: RevertBudgetStatus
    window_attempt_count: int
    early_abort_count: int
    deep_revert_count: int
    success_count: int
    ambiguous_count: int
    gross_gas_spend_usd_micros: int
    realized_profit_usd_micros: int
    net_gas_burn_usd_micros: int
    halt_reasons: tuple[str, ...]

    @property
    def is_active(self) -> bool:
        return self.status is RevertBudgetStatus.ACTIVE


def _matches_executor_event(
    log: ReceiptLog,
    *,
    executor_address: str,
    topic0: str,
    expected_topic_count: int,
) -> bool:
    return (
        log.address == executor_address
        and log.topics[0] == topic0
        and len(log.topics) in (1, expected_topic_count)
    )


def classify_execution_receipt(
    receipt: ReceiptClassificationInput,
    policy: RollingRevertBudgetPolicy,
) -> AttemptOutcome:
    """Classify an observed transaction receipt into a typed `AttemptOutcome`.

    An `EarlyAborted` event emitted by `executor_address` on a successful (`status == 1`)
    transaction whose `gas_used <= policy.max_early_abort_gas` is classified as
    `EARLY_ABORT_PROBE`. Any missing receipt, provider disagreement, malformed status, or
    unexplained `status == 1` receipt fails closed as `AMBIGUOUS_HALT`.
    """
    if not isinstance(receipt, ReceiptClassificationInput):
        raise TypeError("receipt must be a ReceiptClassificationInput")
    if not isinstance(policy, RollingRevertBudgetPolicy):
        raise TypeError("policy must be a RollingRevertBudgetPolicy")

    if (
        receipt.receipt_missing
        or receipt.provider_disagreement
        or bool(receipt.ambiguous_reason)
        or receipt.status not in (0, 1)
        or receipt.gas_used is None
    ):
        return AttemptOutcome.AMBIGUOUS_HALT

    if receipt.status == 0:
        return AttemptOutcome.DEEP_EXECUTION_REVERT

    early_aborted = any(
        _matches_executor_event(
            log,
            executor_address=receipt.executor_address,
            topic0=EARLY_ABORTED_TOPIC0,
            expected_topic_count=3,
        )
        for log in receipt.logs
    )
    cycle_executed = any(
        _matches_executor_event(
            log,
            executor_address=receipt.executor_address,
            topic0=CYCLE_EXECUTED_TOPIC0,
            expected_topic_count=4,
        )
        for log in receipt.logs
    )

    if early_aborted and cycle_executed:
        return AttemptOutcome.AMBIGUOUS_HALT
    if early_aborted:
        if receipt.gas_used <= policy.max_early_abort_gas:
            return AttemptOutcome.EARLY_ABORT_PROBE
        return AttemptOutcome.DEEP_EXECUTION_REVERT
    if cycle_executed:
        return AttemptOutcome.SUCCESS
    return AttemptOutcome.AMBIGUOUS_HALT


class RollingRevertBudgetTracker:
    """Sliding 3,600-second revert-budget gate with sticky fail-closed `HALT`."""

    def __init__(self, policy: RollingRevertBudgetPolicy) -> None:
        if not isinstance(policy, RollingRevertBudgetPolicy):
            raise TypeError("policy must be a RollingRevertBudgetPolicy")
        self._policy = policy
        self._records: deque[AttemptRecord] = deque()
        self._last_timestamp_s: int | None = None
        self._halted = False
        self._halt_reasons: list[str] = []

    @property
    def policy(self) -> RollingRevertBudgetPolicy:
        return self._policy

    @property
    def status(self) -> RevertBudgetStatus:
        return RevertBudgetStatus.HALT if self._halted else RevertBudgetStatus.ACTIVE

    @property
    def halt_reasons(self) -> tuple[str, ...]:
        return tuple(self._halt_reasons)

    def _evict_expired(self, now_s: int) -> None:
        cutoff = now_s - self._policy.window_seconds
        while self._records and self._records[0].timestamp_s <= cutoff:
            self._records.popleft()

    def _build_snapshot(self) -> RevertBudgetSnapshot:
        early_abort_count = 0
        deep_revert_count = 0
        success_count = 0
        ambiguous_count = 0
        gross_gas = 0
        realized_profit = 0

        for entry in self._records:
            gross_gas += entry.gas_cost_usd_micros
            realized_profit += entry.realized_profit_usd_micros
            if entry.outcome is AttemptOutcome.EARLY_ABORT_PROBE:
                early_abort_count += 1
            elif entry.outcome is AttemptOutcome.DEEP_EXECUTION_REVERT:
                deep_revert_count += 1
            elif entry.outcome is AttemptOutcome.SUCCESS:
                success_count += 1
            else:
                ambiguous_count += 1

        net_burn = max(0, gross_gas - realized_profit)
        return RevertBudgetSnapshot(
            status=self.status,
            window_attempt_count=len(self._records),
            early_abort_count=early_abort_count,
            deep_revert_count=deep_revert_count,
            success_count=success_count,
            ambiguous_count=ambiguous_count,
            gross_gas_spend_usd_micros=gross_gas,
            realized_profit_usd_micros=realized_profit,
            net_gas_burn_usd_micros=net_burn,
            halt_reasons=tuple(self._halt_reasons),
        )

    def snapshot(self, now_s: int) -> RevertBudgetSnapshot:
        """Advance the sliding window to `now_s` and return current budget state."""
        _require_nonneg_int(now_s, "now_s")
        if self._last_timestamp_s is not None and now_s < self._last_timestamp_s:
            raise ValueError("timestamps must be monotonically non-decreasing")
        self._last_timestamp_s = now_s
        self._evict_expired(now_s)
        return self._build_snapshot()

    def record(
        self,
        *,
        timestamp_s: int,
        outcome: AttemptOutcome,
        gas_cost_usd_micros: int,
        realized_profit_usd_micros: int = 0,
    ) -> RevertBudgetSnapshot:
        """Record one attempt, prune expired entries, and evaluate halt rules."""
        entry = AttemptRecord(
            timestamp_s=timestamp_s,
            outcome=outcome,
            gas_cost_usd_micros=gas_cost_usd_micros,
            realized_profit_usd_micros=realized_profit_usd_micros,
        )
        if self._last_timestamp_s is not None and entry.timestamp_s < self._last_timestamp_s:
            raise ValueError("timestamps must be monotonically non-decreasing")
        self._last_timestamp_s = entry.timestamp_s
        self._evict_expired(entry.timestamp_s)
        self._records.append(entry)

        snapshot = self._build_snapshot()
        reasons = list(self._halt_reasons)
        if entry.outcome is AttemptOutcome.AMBIGUOUS_HALT:
            reason = f"ambiguous execution outcome at {entry.timestamp_s}"
            if reason not in reasons:
                reasons.append(reason)
        if snapshot.net_gas_burn_usd_micros > self._policy.max_hourly_gas_burn_usd_micros:
            reason = (
                "hourly net gas burn exceeded budget: "
                f"{snapshot.net_gas_burn_usd_micros} > "
                f"{self._policy.max_hourly_gas_burn_usd_micros}"
            )
            if reason not in reasons:
                reasons.append(reason)
        if snapshot.deep_revert_count > self._policy.max_deep_reverts_per_hour:
            reason = (
                "hourly deep execution reverts exceeded limit: "
                f"{snapshot.deep_revert_count} > "
                f"{self._policy.max_deep_reverts_per_hour}"
            )
            if reason not in reasons:
                reasons.append(reason)

        if reasons:
            self._halted = True
            self._halt_reasons = reasons

        return self._build_snapshot()

    def record_receipt(
        self,
        *,
        timestamp_s: int,
        receipt: ReceiptClassificationInput,
        gas_cost_usd_micros: int,
        realized_profit_usd_micros: int = 0,
    ) -> RevertBudgetSnapshot:
        """Classify a receipt and record it into the rolling budget window."""
        outcome = classify_execution_receipt(receipt, self._policy)
        return self.record(
            timestamp_s=timestamp_s,
            outcome=outcome,
            gas_cost_usd_micros=gas_cost_usd_micros,
            realized_profit_usd_micros=realized_profit_usd_micros,
        )

    def clear_halt(self, *, operator_reason: str, now_s: int) -> RevertBudgetSnapshot:
        """Explicitly clear a latched `HALT` after operator reconciliation."""
        if not isinstance(operator_reason, str) or not operator_reason.strip():
            raise ValueError("explicit operator_reason required to clear HALT")
        _require_nonneg_int(now_s, "now_s")
        if self._last_timestamp_s is not None and now_s < self._last_timestamp_s:
            raise ValueError("timestamps must be monotonically non-decreasing")
        self._last_timestamp_s = now_s
        self._evict_expired(now_s)
        self._halted = False
        self._halt_reasons = []
        self._records.clear()
        return self._build_snapshot()


__all__ = [
    "CYCLE_EXECUTED_EVENT_SIGNATURE",
    "CYCLE_EXECUTED_TOPIC0",
    "DEFAULT_MAX_EARLY_ABORT_GAS",
    "DEFAULT_WINDOW_SECONDS",
    "EARLY_ABORTED_EVENT_SIGNATURE",
    "EARLY_ABORTED_TOPIC0",
    "AttemptOutcome",
    "AttemptRecord",
    "ReceiptClassificationInput",
    "ReceiptLog",
    "RevertBudgetSnapshot",
    "RevertBudgetStatus",
    "RollingRevertBudgetPolicy",
    "RollingRevertBudgetTracker",
    "classify_execution_receipt",
]
