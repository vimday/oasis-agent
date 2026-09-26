"""Assemble an evidence-bound LP shadow cycle from a declared economic model.

``run_lp_shadow.py`` consumes ``LpCycle`` records, but nothing in this repository
produced one: candidates and the rebalance gate existed only inside test fixtures.
This module is the deterministic core of that missing producer.

It does not estimate economics, because a fee/LVR/inventory estimate is a trading
hypothesis and this repository requires the owner to lock that model before it is
implemented. Every candidate term is therefore an explicit, digest-bound operator
declaration. The assembler applies the normative blueprint formula from
``execution/range_controller.py`` to the declared terms and refuses a declaration
that contradicts the verified state it is derived with.

The parts that are not declarations are bound rather than trusted:

* the header must be the decision-block header that two agreeing providers signed;
* the market window must have been built from those same slot0 captures;
* inventory and reference price must be two-provider agreed values at the decision
  block from that same provider pair;
* volatility, toxicity and the gate's marginal value are taken from the verified
  feature window, never from the declaration, so a declared gate can never
  disagree with the evidence it claims to describe;
* every retained record is re-read from the evidence store before anything is
  assembled, so a fabricated reference fails closed.

The declaration itself is retained as its own block-pinned record so the operator's
reasoning is auditable next to the decision it produced.

This module holds no key, signs nothing, broadcasts nothing and cannot authorize
anything: ``execution_eligible`` is never asserted here.
"""

import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from ..evidence.rpc_validation import (
    strict_boolean,
    strict_integer,
    strict_list,
    strict_object,
    strict_string,
)
from ..evidence.store import EvidenceRecord, EvidenceStore
from ..execution.lp_state_machine import TickRange
from ..execution.range_controller import (
    LpAction,
    LpActionType,
    MarginalValueInputs,
    RebalanceGateInput,
    select_action,
)
from ..models.chain import BlockSnapshot
from ..models.common import BlockRef, EvidenceRef, canonical_json, sha256_hex
from ..models.position_scope import LpPositionScope
from ..models.v4 import SwapDirection
from ..shadow.features import AgreedInteger, produce_pool_state
from ..shadow.market_window import MarketWindow
from ..shadow.pool_state_capture import (
    Slot0WindowCapture,
    Slot0WindowQuorum,
    replay_slot0_window_evidence,
)
from ..shadow.service import LpCycle, _unique_object, serializable

CYCLE_DECLARATION_SCHEMA: Final[str] = "oasis.lp-cycle-declaration.v1"
HEADER_KIND: Final[str] = "shadow_lp_header"
CYCLE_INPUT_KIND: Final[str] = "shadow_lp_cycle_input"
DECLARATION_KIND: Final[str] = "lp-cycle-declaration"
#: Gate conditions no verifier in this repository derives from the feature window.
#: They stay operator declarations, are named as such in the retained
#: interpretation, and are the fields an operator is asserting on their own
#: authority rather than on retained evidence.
DECLARED_GATE_NAMES: Final[tuple[str, ...]] = (
    "price_left_buffer",
    "dwell_met",
    "reference_fresh",
    "route_fresh",
    "nonce_fresh",
    "balances_fresh",
    "approvals_fresh",
    "gas_reserve_fresh",
    "post_action_limits_ok",
    "pin_simulated_ok",
    "cooldown_met",
    "daily_limit_ok",
)
DERIVED_GATE_NAMES: Final[tuple[str, ...]] = ("volatility", "toxicity", "marginal_value")
_CANDIDATE_TERMS: Final[tuple[str, ...]] = (
    "expected_incremental_fees",
    "expected_incremental_lvr",
    "inventory_risk_charge",
    "swap_rebalance_gas_mev_cost",
    "failure_and_model_uncertainty_buffer",
)
_DECLARATION_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema",
        "chain_id",
        "pool_id",
        "block_number",
        "block_hash",
        "direction",
        "amount_specified",
        "exact_input",
        "benign_flow",
        "min_benefit",
        "hysteresis_threshold",
        "max_volatility",
        "max_toxicity",
        "candidates",
        *DECLARED_GATE_NAMES,
    }
)
_CANDIDATE_FIELDS: Final[frozenset[str]] = frozenset(
    {"action_type", "target_range", *_CANDIDATE_TERMS}
)
_RANGE_FIELDS: Final[frozenset[str]] = frozenset({"tick_lower", "tick_upper", "tick_spacing"})
#: Placeholder carried only while the input payload is computed; the input payload
#: does not contain the input reference, so replacing it cannot change the bytes.
_PENDING: Final[EvidenceRef] = EvidenceRef("pending-input-evidence", "0" * 64, None, "pending")


def _finite(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    return value


def _decimal(value: object, name: str) -> Decimal:
    text = strict_string(value, name)
    try:
        parsed = Decimal(text)
    except ArithmeticError as error:  # decimal.InvalidOperation is not a ValueError
        raise ValueError(f"{name} must be an explicit decimal string") from error
    return _finite(parsed, name)


@dataclass(frozen=True, slots=True)
class CandidateDeclaration:
    """Operator-declared blueprint 6.3 terms for one non-hold candidate action.

    The five terms are quote-currency amounts over the declared horizon, in the
    same unit as ``LpCycleDeclaration.min_benefit``. They are the trading
    hypothesis, not an inference: nothing in this repository accepts them unless
    the operator states them explicitly.
    """

    action_type: LpActionType
    target_range: TickRange
    expected_incremental_fees: Decimal
    expected_incremental_lvr: Decimal
    inventory_risk_charge: Decimal
    swap_rebalance_gas_mev_cost: Decimal
    failure_and_model_uncertainty_buffer: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.action_type, LpActionType):
            raise TypeError("candidate action type must be an LpActionType")
        if self.action_type is LpActionType.HOLD:
            raise ValueError("HOLD is the implicit baseline and cannot be declared")
        if not isinstance(self.target_range, TickRange):
            raise TypeError("every non-hold candidate must name its tick range")
        for name in _CANDIDATE_TERMS:
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a finite nonnegative Decimal")

    @property
    def inputs(self) -> MarginalValueInputs:
        return MarginalValueInputs(
            self.expected_incremental_fees,
            self.expected_incremental_lvr,
            self.inventory_risk_charge,
            self.swap_rebalance_gas_mev_cost,
            self.failure_and_model_uncertainty_buffer,
        )


@dataclass(frozen=True, slots=True)
class LpCycleDeclaration:
    """One operator decision input, bound to a chain, pool and pinned block."""

    chain_id: int
    pool_id: str
    block_number: int
    block_hash: str
    direction: SwapDirection
    amount_specified: int
    exact_input: bool
    benign_flow: bool
    min_benefit: Decimal
    hysteresis_threshold: Decimal
    max_volatility: Decimal
    max_toxicity: Decimal
    price_left_buffer: bool
    dwell_met: bool
    reference_fresh: bool
    route_fresh: bool
    nonce_fresh: bool
    balances_fresh: bool
    approvals_fresh: bool
    gas_reserve_fresh: bool
    post_action_limits_ok: bool
    pin_simulated_ok: bool
    cooldown_met: bool
    daily_limit_ok: bool
    candidates: tuple[CandidateDeclaration, ...]

    def __post_init__(self) -> None:
        if type(self.chain_id) is not int or self.chain_id <= 0:
            raise ValueError("positive chain id required")
        if not self.pool_id.startswith("0x") or len(self.pool_id) != 66:
            raise ValueError("a canonical pool id is required")
        if type(self.block_number) is not int or self.block_number < 0:
            raise ValueError("nonnegative pinned block number required")
        if not self.block_hash.startswith("0x") or len(self.block_hash) != 66:
            raise ValueError("a canonical pinned block hash is required")
        if not isinstance(self.direction, SwapDirection):
            raise TypeError("typed swap direction required")
        if (
            type(self.amount_specified) is not int
            or self.amount_specified == 0
            or not -(1 << 255) <= self.amount_specified < 1 << 255
        ):
            raise ValueError("amount_specified must be a nonzero int256")
        for name in ("exact_input", "benign_flow", *DECLARED_GATE_NAMES):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a bool")
        for name in ("min_benefit", "hysteresis_threshold", "max_volatility", "max_toxicity"):
            _finite(getattr(self, name), name)
        for name in ("hysteresis_threshold", "max_volatility", "max_toxicity"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative")
        if not isinstance(self.candidates, tuple) or not self.candidates:
            raise ValueError("at least one non-hold candidate is required")
        seen: set[LpActionType] = set()
        for candidate in self.candidates:
            if not isinstance(candidate, CandidateDeclaration):
                raise TypeError("candidates must be CandidateDeclaration instances")
            if candidate.action_type in seen:
                raise ValueError("one candidate per action type is required")
            seen.add(candidate.action_type)

    @property
    def gate_conditions(self) -> dict[str, bool]:
        return {name: getattr(self, name) for name in DECLARED_GATE_NAMES}

    @property
    def payload(self) -> bytes:
        """The declared bytes, without any verification metadata.

        ``execution_eligible`` and the note that the economics are operator supplied
        live in the retained interpretation, not here: the payload is exactly the
        fields ``parse_lp_cycle_declaration`` accepts, so a retained declaration
        round-trips.
        """
        return canonical_json(
            serializable(
                {
                    "schema": CYCLE_DECLARATION_SCHEMA,
                    **self.gate_conditions,
                    "chain_id": self.chain_id,
                    "pool_id": self.pool_id,
                    "block_number": self.block_number,
                    "block_hash": self.block_hash,
                    "direction": self.direction,
                    "amount_specified": self.amount_specified,
                    "exact_input": self.exact_input,
                    "benign_flow": self.benign_flow,
                    "min_benefit": self.min_benefit,
                    "hysteresis_threshold": self.hysteresis_threshold,
                    "max_volatility": self.max_volatility,
                    "max_toxicity": self.max_toxicity,
                    "candidates": self.candidates,
                }
            )
        )

    @property
    def digest(self) -> str:
        return sha256_hex(self.payload)


def _declared_range(raw: object) -> TickRange | None:
    if raw is None:
        return None
    data = strict_object(raw, "candidate target range")
    if set(data) != _RANGE_FIELDS:
        raise ValueError("candidate target range has invalid fields")
    return TickRange(
        strict_integer(data["tick_lower"], "tick_lower"),
        strict_integer(data["tick_upper"], "tick_upper"),
        strict_integer(data["tick_spacing"], "tick_spacing"),
    )


def parse_lp_cycle_declaration(raw: bytes) -> LpCycleDeclaration:
    """Parse one declared cycle input; unknown keys, floats and coercion are refused."""
    values = json.loads(raw, object_pairs_hook=_unique_object)
    data = strict_object(values, "cycle declaration")
    if set(data) != _DECLARATION_FIELDS:
        raise ValueError("cycle declaration requires exactly the declared fields")
    if data["schema"] != CYCLE_DECLARATION_SCHEMA:
        raise ValueError("cycle declaration has the wrong schema")
    # Canonical serialization rejects binary floats anywhere, including unknown keys.
    serializable(data)
    candidates: list[CandidateDeclaration] = []
    for item in strict_list(data["candidates"], "candidates"):
        entry = strict_object(item, "candidate")
        if set(entry) != _CANDIDATE_FIELDS:
            raise ValueError("candidate requires exactly the declared fields")
        target = _declared_range(entry["target_range"])
        if target is None:
            raise ValueError("every declared candidate must name its tick range")
        candidates.append(
            CandidateDeclaration(
                LpActionType(strict_string(entry["action_type"], "action_type")),
                target,
                *(_decimal(entry[name], name) for name in _CANDIDATE_TERMS),
            )
        )
    return LpCycleDeclaration(
        chain_id=strict_integer(data["chain_id"], "chain_id"),
        pool_id=strict_string(data["pool_id"], "pool_id"),
        block_number=strict_integer(data["block_number"], "block_number"),
        block_hash=strict_string(data["block_hash"], "block_hash"),
        direction=SwapDirection(strict_string(data["direction"], "direction")),
        amount_specified=strict_integer(data["amount_specified"], "amount_specified"),
        exact_input=strict_boolean(data["exact_input"], "exact_input"),
        benign_flow=strict_boolean(data["benign_flow"], "benign_flow"),
        min_benefit=_decimal(data["min_benefit"], "min_benefit"),
        hysteresis_threshold=_decimal(data["hysteresis_threshold"], "hysteresis_threshold"),
        max_volatility=_decimal(data["max_volatility"], "max_volatility"),
        max_toxicity=_decimal(data["max_toxicity"], "max_toxicity"),
        candidates=tuple(candidates),
        **{name: strict_boolean(data[name], name) for name in DECLARED_GATE_NAMES},
    )


@dataclass(frozen=True, slots=True)
class AssembledLpCycle:
    """A retained cycle, the declaration record behind it, and its selected action."""

    cycle: LpCycle
    declaration_evidence: EvidenceRef
    action: LpAction


def _record(store: EvidenceStore, reference: EvidenceRef) -> EvidenceRecord:
    try:
        return store.get_record(reference)
    except KeyError as error:
        raise ValueError("a referenced evidence record is not retained") from error


def _wire_bytes(store: EvidenceStore, reference: EvidenceRef) -> tuple[bytes, bytes]:
    """The retained wire request and response a derived record borrows.

    The shadow consumer requires a request payload and an interpretation on the
    records it accepts, so a slot0 capture that retained neither cannot be the
    source of a cycle.
    """
    record = _record(store, reference)
    if record.request_payload is None or record.interpretation_payload is None:
        raise ValueError("slot0 evidence retained no wire request for reuse")
    return record.request_payload, record.raw_response_payload


def _ordered_captures(
    store: EvidenceStore,
    window: MarketWindow,
    references: tuple[EvidenceRef, EvidenceRef],
) -> tuple[Slot0WindowQuorum, tuple[tuple[EvidenceRef, Slot0WindowCapture], ...]]:
    """Replay both slot0 captures and prove the window was built from them."""
    if len(references) != 2 or references[0].source == references[1].source:
        raise ValueError("two distinct slot0 captures are required")
    captures: tuple[Slot0WindowCapture, Slot0WindowCapture] = (
        replay_slot0_window_evidence(store, references[0]),
        replay_slot0_window_evidence(store, references[1]),
    )
    quorum = Slot0WindowQuorum(captures[0], captures[1])
    if not set(references) <= set(window.input_references):
        raise ValueError("the market window was not built from these slot0 captures")
    for reference, capture in zip(references, captures, strict=True):
        if reference.source != capture.source:
            raise ValueError("a slot0 reference and its capture disagree on the source")
    if quorum.left.headers[-1].ref != window.decision_block:
        raise ValueError("the slot0 captures do not end at the window decision block")
    ordered = tuple(sorted(zip(references, captures, strict=True), key=lambda pair: pair[0].source))
    return quorum, ordered


def _require_agreed(
    store: EvidenceStore,
    value: object,
    name: str,
    block: BlockRef,
    sources: frozenset[str],
) -> AgreedInteger:
    if not isinstance(value, AgreedInteger):
        raise TypeError(f"{name} must be a two-provider agreed integer")
    if value.name != name:
        raise ValueError(f"{name} must be named for its role")
    if value.block != block:
        raise ValueError(f"{name} must be agreed at the pinned decision block")
    for reference in value.evidence:
        _record(store, reference)
    if frozenset(reference.source for reference in value.evidence) != sources:
        raise ValueError(f"{name} must come from the window's own two providers")
    return value


def _retain_header(
    store: EvidenceStore,
    *,
    header: BlockSnapshot,
    primary: tuple[EvidenceRef, Slot0WindowCapture],
    parents: tuple[EvidenceRef, ...],
    retrieved_at: str,
) -> EvidenceRef:
    """Retain the decision-block header, borrowing the bytes that pinned it.

    The request and response bytes are the slot0 capture's own wire envelope: it is
    the capture that read this header twice (opening and closing) and pinned the
    state view to it. The interpretation says so explicitly rather than implying
    the envelope is a header-only exchange.
    """
    reference, capture = primary
    request, raw = _wire_bytes(store, reference)
    return store.put(
        HEADER_KIND,
        canonical_json(asdict(header)),
        header.ref,
        capture.source,
        request=request,
        raw_response=raw,
        interpretation={
            "schema": "oasis.lp-shadow-header.v1",
            "execution_eligible": False,
            "bytes_kind": "slot0_window_envelope_that_pinned_this_header",
            "block": asdict(header.ref),
            "parents": [asdict(item) for item in parents],
        },
        retrieved_at=retrieved_at,
    )


def _retain_cycle(
    store: EvidenceStore,
    *,
    cycle: LpCycle,
    declaration: LpCycleDeclaration,
    action: LpAction,
    primary: tuple[EvidenceRef, Slot0WindowCapture],
    parents: tuple[EvidenceRef, ...],
    retrieved_at: str,
) -> LpCycle:
    reference, capture = primary
    request, raw = _wire_bytes(store, reference)
    interpretation = {
        "schema": "oasis.lp-cycle-input-interpretation.v1",
        "execution_eligible": False,
        "pool_id": declaration.pool_id,
        "chain_id": declaration.chain_id,
        "block": asdict(cycle.header.ref),
        "state_digest": sha256_hex(canonical_json(serializable(asdict(cycle.state)))),
        "candidate_action": action.action_type.value,
        "declaration_digest": declaration.digest,
        "derived_economics": list(DERIVED_GATE_NAMES),
        "declared_gate_conditions": list(DECLARED_GATE_NAMES),
        "parents": [asdict(item) for item in parents],
    }
    inputs = store.put(
        CYCLE_INPUT_KIND,
        cycle.input_payload,
        cycle.header.ref,
        capture.source,
        request=request,
        raw_response=raw,
        interpretation=interpretation,
        retrieved_at=retrieved_at,
    )
    return replace(cycle, input_evidence=inputs)


def _retain_declaration(
    store: EvidenceStore,
    declaration: LpCycleDeclaration,
    block: BlockRef,
    primary: tuple[EvidenceRef, Slot0WindowCapture],
    retrieved_at: str,
) -> EvidenceRef:
    return store.put(
        DECLARATION_KIND,
        declaration.payload,
        block,
        primary[1].source,
        interpretation={
            "schema": CYCLE_DECLARATION_SCHEMA,
            "execution_eligible": False,
            "declared_economics": "operator_supplied_not_estimated",
            "declared_gate_conditions": list(DECLARED_GATE_NAMES),
        },
        retrieved_at=retrieved_at,
    )


def build_lp_cycle(
    store: EvidenceStore,
    declaration: LpCycleDeclaration,
    *,
    window: MarketWindow,
    slot0_references: tuple[EvidenceRef, EvidenceRef],
    inventory0: AgreedInteger,
    inventory1: AgreedInteger,
    reference_price_x192: AgreedInteger,
    position_scope: LpPositionScope | None = None,
    retrieved_at: str | None = None,
) -> AssembledLpCycle:
    """Retain the declaration, header and input records, then return the cycle.

    Every check precedes the first write, so a refused declaration leaves no
    partially retained cycle behind. The returned cycle is exactly what
    ``LpShadowService.consume`` validates and must not be treated as authorization.
    """
    if not isinstance(store, EvidenceStore):
        raise TypeError("an evidence store is required")
    if not isinstance(declaration, LpCycleDeclaration):
        raise TypeError("a declared cycle input is required")
    if not isinstance(window, MarketWindow):
        raise TypeError("a verified market window is required")
    if position_scope is not None and not isinstance(position_scope, LpPositionScope):
        raise TypeError("typed LP position scope required")
    quorum, ordered = _ordered_captures(store, window, slot0_references)
    header = quorum.left.headers[-1]
    block = header.ref
    if declaration.chain_id != window.chain_id or declaration.chain_id != block.chain_id:
        raise ValueError("the declared chain, market window and header must agree")
    if declaration.pool_id != window.pool_id:
        raise ValueError("the declared pool must be the market window's pool")
    if declaration.block_number != block.number or declaration.block_hash != block.block_hash:
        raise ValueError("the declared block must be the pinned decision block")
    if position_scope is not None and (
        position_scope.chain_id != declaration.chain_id
        or position_scope.pool_id != declaration.pool_id
    ):
        raise ValueError("the declared position scope disagrees with the chain or pool")
    sources = frozenset(reference.source for reference in window.prices[-1].evidence)
    accounts = (
        _require_agreed(store, inventory0, "inventory0", block, sources),
        _require_agreed(store, inventory1, "inventory1", block, sources),
        _require_agreed(store, reference_price_x192, "reference_price_x192", block, sources),
    )
    feature = window.feature_window(
        inventory0=accounts[0], inventory1=accounts[1], reference_price_x192=accounts[2]
    )
    state = produce_pool_state(feature)
    if state.block != block:
        raise ValueError("the derived pool state is not the pinned decision block")
    if state.observed_at_timestamp != block.timestamp:
        raise ValueError("the derived pool state is not timestamped by the decision block")
    candidates = {
        item.action_type: (item.inputs, item.target_range) for item in declaration.candidates
    }
    action = select_action(candidates, declaration.min_benefit)
    gate = RebalanceGateInput(
        volatility=state.volatility,
        max_volatility=declaration.max_volatility,
        toxicity=state.adverse_selection,
        max_toxicity=declaration.max_toxicity,
        marginal_value=action.marginal_value,
        hysteresis_threshold=declaration.hysteresis_threshold,
        **declaration.gate_conditions,
    )
    pending = LpCycle(
        header=header,
        header_evidence=_PENDING,
        input_evidence=_PENDING,
        state=state,
        direction=declaration.direction,
        amount_specified=declaration.amount_specified,
        exact_input=declaration.exact_input,
        benign_flow=declaration.benign_flow,
        candidates=candidates,
        gate_input=gate,
        position_scope=position_scope,
    )
    timestamp = retrieved_at or datetime.now(UTC).isoformat()
    parents = tuple(reference for reference, _ in ordered)
    declaration_evidence = _retain_declaration(store, declaration, block, ordered[0], timestamp)
    header_evidence = _retain_header(
        store,
        header=header,
        primary=ordered[0],
        parents=parents,
        retrieved_at=timestamp,
    )
    cycle = _retain_cycle(
        store,
        cycle=replace(pending, header_evidence=header_evidence),
        declaration=declaration,
        action=action,
        primary=ordered[0],
        parents=parents,
        retrieved_at=timestamp,
    )
    return AssembledLpCycle(cycle, declaration_evidence, action)


__all__ = [
    "CYCLE_DECLARATION_SCHEMA",
    "CYCLE_INPUT_KIND",
    "DECLARATION_KIND",
    "DECLARED_GATE_NAMES",
    "DERIVED_GATE_NAMES",
    "HEADER_KIND",
    "AssembledLpCycle",
    "CandidateDeclaration",
    "LpCycleDeclaration",
    "build_lp_cycle",
    "parse_lp_cycle_declaration",
]
