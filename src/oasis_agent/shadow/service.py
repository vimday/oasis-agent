"""Evidence-bound LP shadow consumer with restart-stable code/config identity.

Consumes acquired cycle inputs; does not invent volatility, reference prices,
route depth or settlement. Every feature and candidate must match retained
input evidence. No signing interface exists in this service.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, fields, is_dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Self

from ..evidence.store import EvidenceStore
from ..execution.lp_state_machine import TickRange
from ..execution.range_controller import LpActionType, MarginalValueInputs, RebalanceGateInput
from ..farming.strategy import Band, BandTableFeeStrategy
from ..models.chain import BlockSnapshot
from ..models.common import BlockRef, EvidenceRef, canonical_json
from ..models.finance import PoolState
from ..models.position_scope import LpPositionScope
from ..models.v4 import SwapDirection
from ..risk.lp_policy import CanaryRiskLimits
from .journal import ShadowJournal
from .lp_runner import LpShadowDecision, LpShadowRunner, _decimal_str


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def serializable(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("finite shadow amount required")
        return _decimal_str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return serializable(asdict(value))
    if isinstance(value, dict):
        return {serializable(key): serializable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(child) for child in value]
    if isinstance(value, float):
        raise TypeError("binary float is not a shadow input")
    return value


def _decimal(value: object) -> Decimal:
    if not isinstance(value, str):
        raise TypeError("decimal inputs must be explicit strings")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError("finite decimal required")
    return result


def _ref(value: dict[str, Any]) -> EvidenceRef:
    return EvidenceRef(**{**value, "block": BlockRef(**value["block"])})


def _decimal_fields(cls: Any, values: dict[str, Any]) -> Any:
    parsed = dict(values)
    for field in fields(cls):
        if field.type in (Decimal, "Decimal") and field.name in parsed:
            parsed[field.name] = _decimal(parsed[field.name])
    return cls(**parsed)


@dataclass(frozen=True, slots=True)
class LpCycle:
    header: BlockSnapshot
    header_evidence: EvidenceRef
    input_evidence: EvidenceRef
    state: PoolState
    direction: SwapDirection
    amount_specified: int
    exact_input: bool
    benign_flow: bool
    candidates: dict[LpActionType, tuple[MarginalValueInputs, TickRange]]
    gate_input: RebalanceGateInput | None
    position_scope: LpPositionScope | None = None

    def __post_init__(self) -> None:
        if self.position_scope is not None and not isinstance(self.position_scope, LpPositionScope):
            raise TypeError("typed LP position scope required")

    @property
    def input_payload(self) -> bytes:
        return canonical_json(
            serializable(
                {
                    **({"position_scope": self.position_scope} if self.position_scope else {}),
                    "header_evidence": self.header_evidence,
                    "state": self.state,
                    "direction": self.direction,
                    "amount_specified": self.amount_specified,
                    "exact_input": self.exact_input,
                    "benign_flow": self.benign_flow,
                    "candidates": self.candidates,
                    "gate_input": self.gate_input,
                }
            )
        )

    @classmethod
    def from_json(cls, raw: bytes) -> Self:
        data = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(data, dict):
            raise TypeError("shadow cycle must be an object")
        # Canonical serialization rejects floats anywhere, including ignored/unknown keys.
        serializable(data)
        header = data.pop("header")
        state = data.pop("state")
        state["block"] = BlockRef(**state["block"])
        raw_candidates = data.pop("candidates")
        if any(not isinstance(pair, list) or len(pair) != 2 for pair in raw_candidates.values()):
            raise ValueError("each candidate requires exactly inputs and range")
        candidates = {
            LpActionType(key): (_decimal_fields(MarginalValueInputs, pair[0]), TickRange(**pair[1]))
            for key, pair in raw_candidates.items()
        }
        gate = data.pop("gate_input")
        scope = data.pop("position_scope", None)
        result = cls(
            position_scope=None if scope is None else LpPositionScope(**scope),
            header=BlockSnapshot(ref=BlockRef(**header.pop("ref")), **header),
            header_evidence=_ref(data.pop("header_evidence")),
            input_evidence=_ref(data.pop("input_evidence")),
            state=_decimal_fields(PoolState, state),
            direction=SwapDirection(data.pop("direction")),
            candidates=candidates,
            gate_input=None if gate is None else _decimal_fields(RebalanceGateInput, gate),
            **data,
        )
        if type(result.exact_input) is not bool or type(result.benign_flow) is not bool:
            raise TypeError("strict shadow booleans required")
        if type(result.amount_specified) is not int:
            raise TypeError("integer specified amount required")
        return result


def parse_policy(raw: bytes) -> dict[str, Any]:
    values: dict[str, Any] = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(values, dict):
        raise TypeError("shadow policy must be an object")
    expected = {"chain_id", "max_age_seconds", "strategy", "min_benefit", "canary_limits"}
    if set(values) != expected:
        raise ValueError("shadow policy requires exactly the declared policy fields")
    for name in ("chain_id", "max_age_seconds"):
        if type(values[name]) is not int or not 0 < values[name] < 2**256:
            raise ValueError("positive bounded chain and age limits required")
    serializable(values)
    strategy = values.pop("strategy")
    for name in (
        "volatility_bands",
        "impact_bands0",
        "impact_bands1",
        "toxicity_bands",
        "inventory_bands",
    ):
        strategy[name] = tuple(Band(**band) for band in strategy[name])
    values["strategy"] = _decimal_fields(BandTableFeeStrategy, strategy)
    values["min_benefit"] = _decimal(values["min_benefit"])
    canary = values.pop("canary_limits")
    values["canary_limits"] = None if canary is None else _decimal_fields(CanaryRiskLimits, canary)
    return values


@dataclass(frozen=True, slots=True)
class LpConsumption:
    duplicate: bool
    record: dict[str, object]
    halted: bool


def serialize_cycle(cycle: LpCycle) -> bytes:
    """The canonical JSON record a producer appends and this consumer re-parses.

    ``LpCycle.from_json`` is the inverse. Defining the producer side here keeps one
    wire definition: the same bytes are the idempotency digest of the decision.
    """
    if not isinstance(cycle, LpCycle):
        raise TypeError("typed LP cycle required")
    return canonical_json(serializable(cycle))


class LpShadowService:
    def __init__(
        self,
        store: EvidenceStore,
        journal: ShadowJournal,
        strategy: BandTableFeeStrategy,
        *,
        min_benefit: Decimal,
        canary_limits: CanaryRiskLimits | None,
    ) -> None:
        self.store, self.journal = store, journal
        self.runner = LpShadowRunner(
            journal, strategy, min_benefit=min_benefit, canary_limits=canary_limits
        )
        package = Path(__file__).resolve().parents[1]
        code = {
            str(path.relative_to(package)): hashlib.sha256(path.read_bytes()).hexdigest()
            for relative in (
                "shadow/service.py",
                "shadow/features.py",
                "shadow/lp_runner.py",
                "shadow/journal.py",
                "farming/strategy.py",
                "execution/range_controller.py",
                "execution/lp_state_machine.py",
                "risk/lp_policy.py",
                "models/finance.py",
                "models/position_scope.py",
                "models/common.py",
            )
            for path in (package / relative,)
        }
        policy = canonical_json(
            serializable(
                {
                    "code": code,
                    "strategy": strategy,
                    "min_benefit": min_benefit,
                    "canary": canary_limits,
                }
            )
        )
        journal.db.execute(
            "CREATE TABLE IF NOT EXISTS lp_policy "
            "(id INTEGER PRIMARY KEY CHECK(id=1), payload BLOB NOT NULL)"
        )
        with journal.db:
            journal.db.execute("INSERT OR IGNORE INTO lp_policy VALUES (1, ?)", (policy,))
            if journal.db.execute("SELECT payload FROM lp_policy WHERE id=1").fetchone() != (
                policy,
            ):
                raise ValueError("shadow code/config changed; start a new reviewed journal")

    def consume_once(self, cycle: LpCycle, *, now: int) -> LpConsumption:
        """Atomically consume an input once; a retry is historical, not authorization.

        The canonical full cycle digest is part of the hash-linked decision.
        No separate mutable cursor can mark an uncommitted decision consumed.
        Latest HALT status is returned even when an older input is replayed.
        """
        if type(now) is not int or now < 0:
            raise ValueError("integer observation time required")
        digest = hashlib.sha256(serialize_cycle(cycle)).hexdigest()
        try:
            with self.journal.atomic(cycle_digest=digest):
                records = self.journal.decision_records()
                matches = [record for record in records if record.get("cycle_digest") == digest]
                if len(matches) > 1:
                    raise ValueError("duplicate retained cycle identity")
                if matches:
                    record = matches[0]
                else:
                    self.consume(cycle, now=now)
                    record = self.journal.decision_records()[-1]
                health = self.journal._last()
                latest = self.journal._last_decision()
                halted = bool(
                    (health and health["decision"] == "HALT")
                    or (latest and latest["decision"] == "halt")
                )
                return LpConsumption(bool(matches), record, halted)
        except ValueError, TypeError, KeyError:
            self.journal.fault(now=now)
            raise ValueError("LP cycle failed; halt retained") from None

    def consume(self, cycle: LpCycle, *, now: int) -> LpShadowDecision:
        try:
            header = self.store.get_record(cycle.header_evidence)
            inputs = self.store.get_record(cycle.input_evidence)
            if (
                (
                    cycle.position_scope is not None
                    and cycle.position_scope.chain_id != cycle.header.ref.chain_id
                )
                or cycle.header_evidence.block != cycle.header.ref
                or cycle.input_evidence.block != cycle.header.ref
                or header.payload != canonical_json(asdict(cycle.header))
                or inputs.payload != cycle.input_payload
                or header.request_payload is None
                or header.interpretation_payload is None
                or inputs.request_payload is None
                or inputs.interpretation_payload is None
            ):
                raise ValueError("shadow input evidence binding mismatch")
        except ValueError, TypeError, KeyError:
            self.journal.fault(now=now)
            raise ValueError("shadow input validation failed; halt retained") from None
        return self.runner.decide(
            header=cycle.header,
            evidence=cycle.input_evidence,
            state=cycle.state,
            direction=cycle.direction,
            amount_specified=cycle.amount_specified,
            exact_input=cycle.exact_input,
            benign_flow=cycle.benign_flow,
            now=now,
            candidates=cycle.candidates,
            gate_input=cycle.gate_input,
        )
