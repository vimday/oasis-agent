"""Independent risk decision for one bounded LP intent; never a signature.

`authorize_lp_signing` refuses every request unless exactly one retained
`risk-decision` record, at that request's own simulation block, has a content
hash equal to the request's `risk_decision_hash`. Nothing in this repository
produced such a record, so no real request could ever be authorized. This module
is that producer.

The decision prices the intent's own worst case from the same retained,
two-provider proofs the signer's admission boundary replays: it rebuilds the
complete reconciled frame at the intent's state block and floors the maximum
principal, the pricier pool currency's swap cap and the exact gas envelope. It
refuses an account with an unresolved operation, a reconciled block that is not
the intent state block, and a worst case that would cross the policy's capital
ceiling.

It holds no key, has no signing or broadcast interface, reserves no capital and
never opens the loss ledger. The ledger's halt latch and its outstanding
commitments stay `AccountRiskGuard`'s admission at signing time, so this decision
is a state-based precondition rather than a permit, and the two checks are
deliberately not the same one.

A decision observes no provider, so it has no RPC request to retain. Its request
envelope is the exact input projection the verdict was computed from, and both
that projection and the verdict are canonical bytes a verifier rebuilds.
"""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from ..evidence.rpc_validation import (
    strict_boolean,
    strict_digest,
    strict_integer,
    strict_json_object,
    strict_list,
    strict_object,
    strict_string,
)
from ..evidence.store import EvidenceStore
from ..models.common import (
    BlockRef,
    EvidenceRef,
    canonical_json,
    normalize_address,
    normalize_hash32,
)
from .account_risk_reader import EvidenceReplayAccountRiskReader
from .lp_signer_policy import LpSigningRequest, calldata_hash
from .lp_signer_sidecar import LpSidecarRequest
from .lp_simulation import SIMULATION_KIND
from .lp_state_machine import LpIntent

RISK_DECISION_KIND = "risk-decision"
_DECISION_SCHEMA = "oasis.lp-risk-decision.v1"
_INPUT_SCHEMA = "oasis.lp-risk-decision-input.v1"


@dataclass(frozen=True, slots=True)
class LpRiskDecision:
    """One verdict, bound to the intent, the frame and the gas envelope it priced."""

    approved: bool
    reasons: tuple[str, ...]
    intent_hash: str
    chain_id: int
    account: str
    calldata_hash: str
    simulation_block: BlockRef
    simulation_evidence_hashes: tuple[str, str]
    frame_block: BlockRef
    inventory_evidence_hash: str
    worst_case_usd_micros: int
    gas_limit: int
    max_fee_per_gas: int
    max_priority_fee_per_gas: int
    manifest_digest: str
    capital_ceiling_usd_micros: int
    deployed_usd_micros: int

    def __post_init__(self) -> None:
        if type(self.approved) is not bool:
            raise TypeError("approval must be a boolean")
        object.__setattr__(self, "reasons", tuple(self.reasons))
        if any(not isinstance(reason, str) or not reason for reason in self.reasons):
            raise ValueError("every denial reason must be a nonempty string")
        if self.approved == bool(self.reasons):
            # An approval that names a reason, or a denial that names none, is an
            # unreadable verdict; refuse it instead of guessing which half is true.
            raise ValueError("an approval carries no reason and a denial carries at least one")
        object.__setattr__(self, "account", normalize_address(self.account))
        for name in ("intent_hash", "inventory_evidence_hash", "manifest_digest"):
            label = name.replace("_", " ")
            object.__setattr__(self, name, strict_digest(getattr(self, name), label))
        object.__setattr__(
            self, "calldata_hash", normalize_hash32(self.calldata_hash, "calldata hash")
        )
        if type(self.chain_id) is not int or self.chain_id <= 0:
            raise ValueError("a positive chain ID is required")
        for name in ("simulation_block", "frame_block"):
            block = getattr(self, name)
            if not isinstance(block, BlockRef) or block.timestamp is None:
                raise ValueError(f"a timestamped {name.replace('_', ' ')} is required")
        if self.frame_block != self.simulation_block:
            raise ValueError("a decision prices the frame at the intent state block")
        hashes = tuple(self.simulation_evidence_hashes)
        if len(hashes) != 2:
            raise ValueError("a decision binds exactly two simulation providers")
        object.__setattr__(
            self,
            "simulation_evidence_hashes",
            tuple(sorted(strict_digest(item, "simulation evidence hash") for item in hashes)),
        )
        for name in (
            "worst_case_usd_micros",
            "gas_limit",
            "max_fee_per_gas",
            "max_priority_fee_per_gas",
            "capital_ceiling_usd_micros",
            "deployed_usd_micros",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.worst_case_usd_micros == 0 or self.gas_limit == 0:
            raise ValueError("a priced commitment and its gas envelope must be positive")
        if self.max_fee_per_gas == 0 or self.max_priority_fee_per_gas > self.max_fee_per_gas:
            raise ValueError("a positive max fee and a bounded priority fee are required")
        if self.capital_ceiling_usd_micros == 0:
            raise ValueError("a positive capital ceiling is required")

    def _facts(self) -> dict[str, object]:
        """Every input the verdict was computed from, and no verdict of its own."""
        return {
            "intent_hash": self.intent_hash,
            "chain_id": self.chain_id,
            "account": self.account,
            "calldata_hash": self.calldata_hash,
            "simulation_block": asdict(self.simulation_block),
            "simulation_evidence_hashes": list(self.simulation_evidence_hashes),
            "frame_block": asdict(self.frame_block),
            "inventory_evidence_hash": self.inventory_evidence_hash,
            "worst_case_usd_micros": self.worst_case_usd_micros,
            "gas_limit": self.gas_limit,
            "max_fee_per_gas": self.max_fee_per_gas,
            "max_priority_fee_per_gas": self.max_priority_fee_per_gas,
            "manifest_digest": self.manifest_digest,
            "capital_ceiling_usd_micros": self.capital_ceiling_usd_micros,
            "deployed_usd_micros": self.deployed_usd_micros,
        }

    @property
    def payload(self) -> bytes:
        """The verdict together with the facts behind it, as one retained statement."""
        return canonical_json(
            {
                "schema": _DECISION_SCHEMA,
                **self._facts(),
                "approved": self.approved,
                "reasons": list(self.reasons),
            }
        )

    @property
    def request_envelope(self) -> bytes:
        """The bound inputs alone, retained as this decision's request evidence.

        A decision issues no call, so the signer gate's requirement that every
        evidence record carry a complete request hash is met with the projection
        the verdict was reached from, not with a fabricated observation.
        """
        return canonical_json({"schema": _INPUT_SCHEMA, **self._facts()})


def _pricing_envelope(
    *,
    intent: LpIntent,
    account: str,
    calldata: bytes,
    simulation_block: BlockRef,
    gas_limit: int,
    max_fee_per_gas: int,
    max_priority_fee_per_gas: int,
) -> LpSidecarRequest:
    """A never-signed request shape, used only to reuse the reviewed pricing path.

    `EvidenceReplayAccountRiskReader` prices exactly one thing: a request whose
    `current_block` is the frame's own block. This envelope points that block at
    the intent state, carries no evidence and no approval, and is never hashed,
    retained, authorized or offered to a signer.
    """
    return LpSidecarRequest(
        signing=LpSigningRequest(
            intent=intent,
            account=account,
            target=intent.target,
            calldata=calldata,
            value=0,
            account_nonce=0,
            executor_last_nonce=0,
            gas_limit=gas_limit,
            current_block=simulation_block,
            simulation_block=simulation_block,
            simulation_succeeded=True,
            simulation_calldata_hash=intent.calldata_hash,
            observed_runtime_pins=intent.runtime_pins,
            risk_approved=False,
            risk_decision_hash="0x" + "00" * 32,
            evidence=(),
        ),
        max_fee_per_gas=max_fee_per_gas,
        max_priority_fee_per_gas=max_priority_fee_per_gas,
    )


def decide_lp_risk(
    reader: EvidenceReplayAccountRiskReader,
    *,
    intent: LpIntent,
    account: str,
    calldata: bytes,
    simulation_block: BlockRef,
    simulation_evidence: tuple[EvidenceRef, EvidenceRef],
    gas_limit: int,
    max_fee_per_gas: int,
    max_priority_fee_per_gas: int,
) -> LpRiskDecision:
    """Price one intent against the reader's retained proofs and decide exactly once."""
    if not isinstance(reader, EvidenceReplayAccountRiskReader):
        raise TypeError("typed account risk reader required")
    if not isinstance(intent, LpIntent):
        raise TypeError("typed LP intent required")
    if not isinstance(calldata, bytes):
        raise TypeError("calldata must be bytes")
    account = normalize_address(account)
    if calldata_hash(calldata) != intent.calldata_hash:
        raise ValueError("calldata does not match the bounded intent")
    if gas_limit != intent.max_gas_units:
        raise ValueError("the gas envelope must be the one the plan simulated")
    if not isinstance(simulation_block, BlockRef) or simulation_block.timestamp is None:
        raise ValueError("a timestamped simulation block is required")
    if simulation_block != BlockRef(
        intent.chain_id, intent.state_block, intent.state_hash, simulation_block.timestamp
    ):
        raise ValueError("the simulation block is not the intent state block")
    evidence = tuple(simulation_evidence)
    if len(evidence) != 2 or len({reference.source for reference in evidence}) != 2:
        raise ValueError("two independent simulation providers are required")
    if any(
        reference.kind != SIMULATION_KIND or reference.block != simulation_block
        for reference in evidence
    ):
        raise ValueError("simulation evidence is not this plan's own simulation at its block")
    key = f"{intent.chain_id}:{account}"
    if key not in reader.manifest.keys:
        raise ValueError("the signing account is not a reconciled manifest account")
    frame, worst_case = reader.reconcile_and_price(
        _pricing_envelope(
            intent=intent,
            account=account,
            calldata=calldata,
            simulation_block=simulation_block,
            gas_limit=gas_limit,
            max_fee_per_gas=max_fee_per_gas,
            max_priority_fee_per_gas=max_priority_fee_per_gas,
        )
    )
    signed = next((item for item in frame.accounts if item.key == key), None)
    if signed is None:
        raise ValueError("the signing account is not a reconciled manifest account")
    frame_block = BlockRef(
        signed.chain_id, signed.block_number, signed.block_hash, signed.timestamp
    )
    if frame_block != simulation_block:
        raise ValueError("the reconciled frame is not at the intent state block")
    deployed = sum(
        max(0, asset.value)
        for item in frame.accounts
        for asset in item.assets
        if asset.role == "investment"
    )
    reasons: list[str] = []
    if signed.unresolved_operations:
        # An unexplained residual makes the frame's own inventory claim unproven,
        # so the deployed figure the ceiling is checked against is not trustworthy.
        reasons.append("the reconciled account has unresolved operations")
    if deployed + worst_case > reader.manifest.policy.total_capital_usd_micros:
        reasons.append("the worst-case commitment would exceed the capital ceiling")
    return LpRiskDecision(
        approved=not reasons,
        reasons=tuple(reasons),
        intent_hash=intent.intent_hash,
        chain_id=intent.chain_id,
        account=account,
        calldata_hash=intent.calldata_hash,
        simulation_block=simulation_block,
        simulation_evidence_hashes=(evidence[0].content_hash, evidence[1].content_hash),
        frame_block=frame_block,
        inventory_evidence_hash=signed.inventory_evidence_hash,
        worst_case_usd_micros=worst_case,
        gas_limit=gas_limit,
        max_fee_per_gas=max_fee_per_gas,
        max_priority_fee_per_gas=max_priority_fee_per_gas,
        manifest_digest=reader.manifest.digest,
        capital_ceiling_usd_micros=reader.manifest.policy.total_capital_usd_micros,
        deployed_usd_micros=deployed,
    )


def persist_lp_risk_decision(
    store: EvidenceStore,
    decision: LpRiskDecision,
    *,
    retrieved_at: str | None = None,
) -> EvidenceRef:
    """Retain one decision under its own reconciled account and simulation block.

    The signer gate matches a decision by content hash, so the bytes written here
    and the bytes it re-derives must be the same object: the payload is retained
    as its own interpretation, and the input projection as its request.
    """
    if not isinstance(decision, LpRiskDecision):
        raise TypeError("typed risk decision required")
    return store.put(
        RISK_DECISION_KIND,
        decision.payload,
        decision.simulation_block,
        decision.account,
        request=decision.request_envelope,
        interpretation=decision.payload,
        retrieved_at=retrieved_at or datetime.now(UTC).isoformat(),
        schema_version=1,
    )


def _block(value: object, name: str) -> BlockRef:
    data = strict_object(value, name)
    if set(data) != {"chain_id", "number", "block_hash", "timestamp"}:
        raise ValueError(f"{name} has invalid fields")
    return BlockRef(
        strict_integer(data["chain_id"], "chain ID"),
        strict_integer(data["number"], "block number"),
        strict_string(data["block_hash"], "block hash"),
        strict_integer(data["timestamp"], "block timestamp"),
    )


def _hashes(value: object) -> tuple[str, str]:
    """Exactly the two simulation evidence hashes a decision binds."""
    items = tuple(
        strict_string(item, "simulation evidence hash")
        for item in strict_list(value, "simulation evidence hashes")
    )
    if len(items) != 2:
        raise ValueError("a risk decision binds exactly two simulation providers")
    return (items[0], items[1])


def _parse_decision(payload: bytes) -> LpRiskDecision:
    data = strict_json_object(payload)
    expected = {
        "schema",
        "intent_hash",
        "chain_id",
        "account",
        "calldata_hash",
        "simulation_block",
        "simulation_evidence_hashes",
        "frame_block",
        "inventory_evidence_hash",
        "worst_case_usd_micros",
        "gas_limit",
        "max_fee_per_gas",
        "max_priority_fee_per_gas",
        "manifest_digest",
        "capital_ceiling_usd_micros",
        "deployed_usd_micros",
        "approved",
        "reasons",
    }
    if set(data) != expected:
        raise ValueError("risk decision has invalid fields")
    if data["schema"] != _DECISION_SCHEMA:
        raise ValueError("unsupported risk decision schema")
    return LpRiskDecision(
        approved=strict_boolean(data["approved"], "approval"),
        reasons=tuple(
            strict_string(item, "denial reason")
            for item in strict_list(data["reasons"], "denial reasons")
        ),
        intent_hash=strict_string(data["intent_hash"], "intent hash"),
        chain_id=strict_integer(data["chain_id"], "chain ID"),
        account=strict_string(data["account"], "account"),
        calldata_hash=strict_string(data["calldata_hash"], "calldata hash"),
        simulation_block=_block(data["simulation_block"], "simulation block"),
        simulation_evidence_hashes=_hashes(data["simulation_evidence_hashes"]),
        frame_block=_block(data["frame_block"], "reconciled frame block"),
        inventory_evidence_hash=strict_string(
            data["inventory_evidence_hash"], "inventory evidence hash"
        ),
        worst_case_usd_micros=strict_integer(
            data["worst_case_usd_micros"], "worst-case commitment"
        ),
        gas_limit=strict_integer(data["gas_limit"], "gas limit"),
        max_fee_per_gas=strict_integer(data["max_fee_per_gas"], "max fee per gas"),
        max_priority_fee_per_gas=strict_integer(
            data["max_priority_fee_per_gas"], "priority fee per gas"
        ),
        manifest_digest=strict_string(data["manifest_digest"], "manifest digest"),
        capital_ceiling_usd_micros=strict_integer(
            data["capital_ceiling_usd_micros"], "capital ceiling"
        ),
        deployed_usd_micros=strict_integer(data["deployed_usd_micros"], "deployed investment"),
    )


def replay_lp_risk_decision(store: EvidenceStore, reference: EvidenceRef) -> LpRiskDecision:
    """Rebuild one decision from its retained bytes; a changed byte refuses."""
    if reference.kind != RISK_DECISION_KIND:
        raise ValueError("reference is not a risk decision")
    record = store.get_record(reference)
    if record.request_payload is None or record.interpretation_payload is None:
        raise ValueError("risk decision lacks complete retained input evidence")
    decision = _parse_decision(record.payload)
    if decision.request_envelope != record.request_payload:
        raise ValueError("risk decision inputs differ from their raw reconstruction")
    if decision.payload != record.payload:
        raise ValueError("risk decision differs from its own bytes")
    return decision


__all__ = [
    "RISK_DECISION_KIND",
    "LpRiskDecision",
    "decide_lp_risk",
    "persist_lp_risk_decision",
    "replay_lp_risk_decision",
]
