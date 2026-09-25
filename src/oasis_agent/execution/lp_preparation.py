"""Deterministic LP simulation preparation; no execution or signing authority."""

from dataclasses import asdict, dataclass, field, replace

from eth_abi.abi import encode

from ..evidence.store import EvidenceStore
from ..farming.position_quorum import PositionQuorum
from ..models.common import EvidenceRef, canonical_json, sha256_hex
from ..models.position_scope import LpPositionScope
from ..shadow.journal import ShadowJournal
from ..shadow.service import LpCycle, serializable
from .lp_signer_policy import EXECUTE_SELECTOR, EXECUTE_TYPE, LpSignerPolicy, calldata_hash
from .lp_state_machine import LpIntent, LpIntentMode, RuntimeRole, TickRange

SIMULATION_PLAN_SCHEMA = "oasis.lp-simulation-plan.v2"


@dataclass(frozen=True, slots=True)
class LpPlanBounds:
    executor_nonce: int
    deadline: int
    liquidity: int
    amount0_min: int
    amount1_min: int
    amount0_max: int
    amount1_max: int
    gas_limit: int
    max_age_seconds: int

    def __post_init__(self) -> None:
        for name, bits in (
            ("executor_nonce", 64),
            ("deadline", 64),
            ("liquidity", 128),
            ("amount0_min", 128),
            ("amount1_min", 128),
            ("amount0_max", 128),
            ("amount1_max", 128),
            ("gas_limit", 256),
            ("max_age_seconds", 256),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value < 1 << bits:
                raise ValueError(f"{name} must fit uint{bits}")
        if not all((self.executor_nonce, self.deadline, self.gas_limit, self.max_age_seconds)):
            raise ValueError("positive nonce, deadline, gas and age required")
        if self.amount0_min > self.amount0_max or self.amount1_min > self.amount1_max:
            raise ValueError("settlement minimum exceeds maximum")


@dataclass(frozen=True, slots=True)
class LpSimulationPlan:
    intent: LpIntent
    calldata: bytes
    account: str
    decision_digest: str
    position_evidence: tuple[EvidenceRef, EvidenceRef]
    execution_eligible: bool = field(default=False, init=False)

    @property
    def payload(self) -> bytes:
        return canonical_json(
            {
                "schema": SIMULATION_PLAN_SCHEMA,
                "mode": "simulate",
                "execution_eligible": False,
                "intent": asdict(self.intent),
                "intent_hash": self.intent.intent_hash,
                "calldata": "0x" + self.calldata.hex(),
                "account": self.account,
                "value": 0,
                "decision_digest": self.decision_digest,
                "position_evidence": [asdict(ref) for ref in self.position_evidence],
                "unresolved": [
                    "deployment_identity",
                    "independent_risk",
                    "pinned_simulation",
                    "account_nonce_reservation",
                    "release_approval",
                    "settlement",
                ],
            }
        )


def build_lp_simulation_plan(
    store: EvidenceStore,
    journal: ShadowJournal,
    cycle: LpCycle,
    positions: PositionQuorum,
    policy: LpSignerPolicy,
    bounds: LpPlanBounds,
    *,
    now: int,
) -> LpSimulationPlan:
    """Prepare only the latest scoped proposal against reconstructed position truth."""
    positions.validate()
    snapshot = positions.left.snapshot
    scope = LpPositionScope(
        snapshot.block.chain_id,
        snapshot.position_manager,
        snapshot.token_id,
        snapshot.pool_id,
        snapshot.nft_owner,
    )
    if cycle.position_scope is None or cycle.position_scope != scope:
        raise ValueError("scoped cycle and reconstructed position disagree")
    if cycle.header != positions.left.header:
        raise ValueError("decision and position must use the same header")
    if (policy.chain_id, policy.account, policy.pool_id) != (
        scope.chain_id,
        scope.owner,
        scope.pool_id,
    ):
        raise ValueError("position does not match frozen account/chain/pool policy")
    if snapshot.position_manager != next(
        pin.address for pin in policy.runtime_pins if pin.role is RuntimeRole.POSITION_MANAGER
    ):
        raise ValueError("position snapshot does not match frozen PositionManager runtime")
    if snapshot.has_subscriber or snapshot.liquidity == 0:
        raise ValueError("empty or subscribed position is unsupported")
    timestamp = snapshot.block.timestamp
    if (
        type(now) is not int
        or timestamp is None
        or not 0 <= now - timestamp <= bounds.max_age_seconds
    ):
        raise ValueError("stale or future position snapshot")
    if not now < bounds.deadline <= timestamp + policy.max_deadline_seconds:
        raise ValueError("deadline expired or exceeds policy")
    if (
        bounds.liquidity > policy.max_liquidity
        or bounds.amount0_max > policy.max_amount0
        or bounds.amount1_max > policy.max_amount1
        or bounds.gas_limit > policy.max_gas_units
    ):
        raise ValueError("capital or gas bounds exceed policy")
    inputs = store.get_record(cycle.input_evidence)
    header = store.get_record(cycle.header_evidence)
    if (
        inputs.payload != cycle.input_payload
        or header.payload != canonical_json(asdict(cycle.header))
        or cycle.input_evidence.block != snapshot.block
        or cycle.header_evidence.block != snapshot.block
        or any(
            record.request_payload is None or record.interpretation_payload is None
            for record in (inputs, header)
        )
    ):
        raise ValueError("retained cycle evidence mismatch")
    # Hold one SQLite read transaction across health and decision-chain validation.
    with journal.atomic(write=False):
        records = journal.decision_records()
        health = journal._last()
        if not records or health is None:
            raise ValueError("latest decision missing")
        record = records[-1]
        if (
            record.get("health_digest") != health["digest"]
            or health["decision"] != "HOLD"
            or record.get("cycle_digest") != sha256_hex(canonical_json(serializable(cycle)))
            or record.get("execution_eligible") is not False
        ):
            raise ValueError("latest health/decision mismatch or halt")
        if (
            record.get("decision") not in {"recenter", "remove"}
            or record.get("rebalance_allowed") is not True
        ):
            raise ValueError("latest action is unsupported or rebalance gate denied")
        action, fee = record.get("range_action"), record.get("fee_quote")
        if not isinstance(action, dict) or not isinstance(fee, dict):
            raise ValueError("range and fee proposal required")
        if action.get("action_type") != record["decision"] or not isinstance(
            action.get("target_range"), dict
        ):
            raise ValueError("action/range mismatch")
        effective_fee = fee.get("effective_fee")
        expiry = fee.get("valid_until_timestamp")
        if (
            type(effective_fee) is not int
            or not 0 <= effective_fee <= policy.max_fee_pips
            or type(expiry) is not int
            or now > expiry
        ):
            raise ValueError("fee proposal stale or exceeds policy")
        target_range = TickRange(**action["target_range"])
        decision_digest = str(record["digest"])
        mode = LpIntentMode(str(record["decision"]))
    old_range = TickRange(snapshot.tick_lower, snapshot.tick_upper, snapshot.pool_key.tick_spacing)
    if mode is LpIntentMode.RECENTER and target_range == old_range:
        raise ValueError("recenter requires a different range")
    if mode is LpIntentMode.REMOVE and bounds.liquidity != 0:
        raise ValueError("remove requires zero replacement liquidity")
    references = positions.retain(store)
    identity = sha256_hex(
        canonical_json(
            {
                "decision": decision_digest,
                "scope": asdict(scope),
                "policy": asdict(policy),
                "bounds": asdict(bounds),
                "position_evidence": [asdict(ref) for ref in references],
            }
        )
    )
    intent = LpIntent(
        identity,
        policy.chain_id,
        policy.pool_id,
        snapshot.token_id,
        mode,
        policy.executor,
        "0x" + "00" * 32,
        old_range,
        target_range,
        bounds.liquidity,
        bounds.amount0_min,
        bounds.amount1_min,
        bounds.amount0_max,
        bounds.amount1_max,
        0,
        policy.max_fee_pips,
        bounds.gas_limit,
        bounds.executor_nonce,
        snapshot.block.number,
        snapshot.block.block_hash,
        bounds.deadline,
        policy.config_hash,
        policy.runtime_pins,
    )
    key = snapshot.pool_key
    values = (
        intent.nonce,
        intent.deadline_timestamp,
        0 if mode is LpIntentMode.RECENTER else 1,
        (key.currency0, key.currency1, key.fee, key.tick_spacing, key.hooks),
        intent.position_token_id,
        target_range.tick_lower,
        target_range.tick_upper,
        intent.liquidity,
        intent.amount0_min,
        intent.amount1_min,
        intent.amount0_max,
        intent.amount1_max,
        0,
        b"",
    )
    calldata = EXECUTE_SELECTOR + encode([EXECUTE_TYPE], [values])
    intent = replace(intent, calldata_hash=calldata_hash(calldata))
    return LpSimulationPlan(intent, calldata, policy.account, decision_digest, references)
