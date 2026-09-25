"""Assemble one signer request from retained simulation, preflight and risk proofs.

The frozen signer gate (``authorize_lp_signing``) authorizes an LP intent only when
a pinned simulation, a fresh execution preflight and exactly one risk decision are
all present as retained evidence whose bytes re-derive to the hashes it checks.
Nothing assembled those three into one request, so a real intent could never reach a
signer. This module is that assembler.

It holds no key, has no signing or broadcast interface, reserves no capital and never
opens the loss ledger. Every reference it submits is re-read from the evidence store
first, so a fabricated reference cannot authorize anything, and the plan, the
preflight and the decision must all describe the same intent, the same state block
and the same priced gas envelope. It returns the gate's own verdict, so a caller
enforces ``authorized`` instead of re-deriving the policy.

A bundle that cannot describe one coherent request at all is refused outright; a
bundle that is coherent but violates the frozen policy is returned as a denial the
caller must read.
"""

from dataclasses import dataclass

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
    evidence_ref_from_json,
    normalize_address,
    normalize_hex,
)
from ..risk.portfolio_manifest import PortfolioManifest
from .lp_preflight import LpPreflightQuorum, replay_lp_preflight_evidence
from .lp_preparation import SIMULATION_PLAN_SCHEMA, LpSimulationPlan
from .lp_risk_decision import LpRiskDecision, replay_lp_risk_decision
from .lp_signer_policy import (
    LpSignerDecision,
    LpSignerPolicy,
    LpSigningRequest,
    authorize_lp_signing,
    calldata_hash,
    simulation_payload,
)
from .lp_signer_sidecar import LpSidecarRequest, intent_from_json
from .lp_simulation import SIMULATION_KIND
from .lp_state_machine import LpIntent, RuntimePin

POSITION_KIND = "position_snapshot"
_PLAN_FIELDS = {
    "schema",
    "mode",
    "execution_eligible",
    "intent",
    "intent_hash",
    "calldata",
    "account",
    "value",
    "decision_digest",
    "position_evidence",
    "unresolved",
    "simulation",
}
_INTENT_FIELDS = {
    "intent_id",
    "chain_id",
    "pool_id",
    "position_token_id",
    "mode",
    "target",
    "calldata_hash",
    "old_range",
    "new_range",
    "liquidity",
    "amount0_min",
    "amount1_min",
    "amount0_max",
    "amount1_max",
    "max_swap_input",
    "max_fee_pips",
    "max_gas_units",
    "nonce",
    "state_block",
    "state_hash",
    "deadline_timestamp",
    "config_hash",
    "runtime_pins",
}

type EvidencePair = tuple[EvidenceRef, EvidenceRef]


@dataclass(frozen=True, slots=True)
class LpPreparedSimulation:
    """One simulated plan together with the two providers that retained its call."""

    plan: LpSimulationPlan
    evidence: EvidencePair

    @property
    def block(self) -> BlockRef:
        """The plan's own pinned state block, which its simulation and frame share."""
        block = self.plan.position_evidence[0].block
        if block is None:
            raise ValueError("a prepared simulation requires a pinned state block")
        return block


def _bytes(value: object, name: str) -> bytes:
    text = strict_string(value, name)
    return bytes.fromhex(normalize_hex(text)[2:])


def _intent(value: object) -> LpIntent:
    data = strict_object(value, "LP intent")
    if set(data) != _INTENT_FIELDS:
        raise ValueError("LP intent has invalid fields")
    try:
        return intent_from_json(data)
    except (TypeError, ValueError) as exc:
        raise ValueError("LP intent is malformed") from exc


def parse_prepared_simulation(payload: bytes) -> LpPreparedSimulation:
    """Rebuild the plan and its retained simulation from one report's exact bytes.

    A report that never ran the pinned simulation, that carries a different intent
    hash than the intent it embeds, or whose providers are not the two distinct
    sources the gate needs is refused instead of being repaired.
    """
    if not isinstance(payload, bytes):
        raise TypeError("prepared simulation payload must be bytes")
    data = strict_json_object(payload)
    if set(data) != _PLAN_FIELDS:
        raise ValueError("prepared simulation report has invalid fields")
    if data["schema"] != SIMULATION_PLAN_SCHEMA:
        raise ValueError("unsupported LP simulation plan schema")
    if data["mode"] != "simulate" or strict_boolean(
        data["execution_eligible"], "execution eligibility"
    ):
        raise ValueError("a prepared simulation must be a simulation-only plan")
    if strict_integer(data["value"], "execution value") != 0:
        raise ValueError("an LP executor plan must carry a zero value")
    intent = _intent(data["intent"])
    if strict_string(data["intent_hash"], "intent hash") != intent.intent_hash:
        raise ValueError("the plan intent hash does not match its own intent")
    calldata = _bytes(data["calldata"], "calldata")
    if calldata_hash(calldata) != intent.calldata_hash:
        raise ValueError("the plan calldata does not match its own intent hash")
    account = normalize_address(strict_string(data["account"], "plan account"))
    decision_digest = strict_digest(data["decision_digest"], "decision digest")
    unresolved = tuple(
        strict_string(item, "unresolved item")
        for item in strict_list(data["unresolved"], "unresolved items")
    )
    if "pinned_simulation" in unresolved:
        raise ValueError("the plan carries no retained pinned simulation")
    positions = tuple(
        evidence_ref_from_json(item)
        for item in strict_list(data["position_evidence"], "position evidence")
    )
    if len(positions) != 2 or len({reference.source for reference in positions}) != 2:
        raise ValueError("a plan binds exactly two independent position providers")
    block = positions[0].block
    if (
        block is None
        or block.timestamp is None
        or positions[1].block != block
        or any(
            reference.kind != POSITION_KIND or reference.schema_version != 1
            for reference in positions
        )
    ):
        raise ValueError("position evidence is not two agreeing pinned snapshots")
    simulation = strict_object(data["simulation"], "simulation result")
    if set(simulation) != {"plan_hash", "evidence", "execution_eligible"}:
        raise ValueError("simulation result has invalid fields")
    if strict_boolean(simulation["execution_eligible"], "simulation eligibility"):
        raise ValueError("a retained simulation is never execution eligible")
    if strict_string(simulation["plan_hash"], "plan hash") != intent.intent_hash:
        raise ValueError("the retained simulation is not this plan's own simulation")
    evidence = tuple(
        evidence_ref_from_json(item)
        for item in strict_list(simulation["evidence"], "simulation evidence")
    )
    if (
        len(evidence) != 2
        or len({reference.source for reference in evidence}) != 2
        or any(
            reference.kind != SIMULATION_KIND or reference.block != block for reference in evidence
        )
    ):
        raise ValueError("simulation evidence is not two independent providers at the plan block")
    return LpPreparedSimulation(
        LpSimulationPlan(intent, calldata, account, decision_digest, (positions[0], positions[1])),
        (evidence[0], evidence[1]),
    )


def _simulation_envelope(prepared: LpPreparedSimulation, block: BlockRef) -> bytes:
    """The exact retained payload this plan's own simulation must consist of."""
    plan = prepared.plan
    return simulation_payload(
        block=block,
        account=plan.account,
        target=plan.intent.target,
        value=0,
        gas_limit=plan.intent.max_gas_units,
        calldata_digest=plan.intent.calldata_hash,
        succeeded=True,
        runtime_pins=plan.intent.runtime_pins,
    )


def _require_simulation_evidence(
    store: EvidenceStore, prepared: LpPreparedSimulation, block: BlockRef
) -> None:
    """Prove both simulation references are this plan's own retained simulation bytes."""
    envelope = _simulation_envelope(prepared, block)
    for reference in prepared.evidence:
        record = store.get_record(reference)
        if (
            record.payload != envelope
            or record.interpretation_payload != envelope
            or record.request_payload is None
        ):
            raise ValueError("the retained simulation is not this plan's own pinned simulation")


def _require_preflight(
    store: EvidenceStore,
    preflight: EvidencePair,
    *,
    policy: LpSignerPolicy,
) -> tuple[BlockRef, int, int, tuple[RuntimePin, ...]]:
    """Replay both preflight providers and bind the head they observed to the policy."""
    if len(preflight) != 2:
        raise ValueError("two independent preflight providers are required")
    left = replay_lp_preflight_evidence(store, preflight[0])
    right = replay_lp_preflight_evidence(store, preflight[1])
    quorum = LpPreflightQuorum(left, right)
    current_block = quorum.left.block
    if (
        current_block.chain_id != policy.chain_id
        or quorum.left.account != policy.account
        or quorum.left.executor != policy.executor
        or quorum.left.runtime_pins != policy.runtime_pins
    ):
        raise ValueError("the preflight observes a different account, executor or runtime")
    return (
        current_block,
        quorum.left.account_nonce,
        quorum.left.executor_last_nonce,
        quorum.left.runtime_pins,
    )


def _require_risk_decision(
    store: EvidenceStore,
    decision_reference: EvidenceRef,
    *,
    intent: LpIntent,
    prepared: LpPreparedSimulation,
    block: BlockRef,
    manifest: PortfolioManifest,
    max_fee_per_gas: int,
    max_priority_fee_per_gas: int,
) -> LpRiskDecision:
    """Prove the retained decision priced exactly this intent, block and gas envelope."""
    decided = replay_lp_risk_decision(store, decision_reference)
    expected_hashes = tuple(sorted(reference.content_hash for reference in prepared.evidence))
    if (
        decision_reference.block != block
        or decided.intent_hash != intent.intent_hash
        or decided.chain_id != intent.chain_id
        or decided.account != prepared.plan.account
        or decided.calldata_hash != intent.calldata_hash
        or decided.simulation_block != block
        or decided.simulation_evidence_hashes != expected_hashes
        or decided.gas_limit != intent.max_gas_units
        or decided.max_fee_per_gas != max_fee_per_gas
        or decided.max_priority_fee_per_gas != max_priority_fee_per_gas
        or decided.manifest_digest != manifest.digest
        or decided.capital_ceiling_usd_micros != manifest.policy.total_capital_usd_micros
    ):
        raise ValueError("the retained risk decision does not price this request")
    return decided


def assemble_lp_signing_request(
    store: EvidenceStore,
    *,
    policy: LpSignerPolicy,
    manifest: PortfolioManifest,
    prepared: LpPreparedSimulation,
    preflight: EvidencePair,
    decision_reference: EvidenceRef,
    max_fee_per_gas: int,
    max_priority_fee_per_gas: int,
) -> tuple[LpSidecarRequest, LpSignerDecision]:
    """Bind one prepared simulation, one preflight and one risk decision to a policy.

    The returned request carries the gate's own verdict. A caller must refuse to
    write, offer or sign the request unless ``decision.authorized`` is true, and a
    signature produced elsewhere is still not an execution permit.
    """
    if not isinstance(store, EvidenceStore):
        raise TypeError("typed evidence store required")
    if not isinstance(policy, LpSignerPolicy):
        raise TypeError("typed signer policy required")
    if not isinstance(manifest, PortfolioManifest):
        raise TypeError("typed portfolio manifest required")
    if not isinstance(prepared, LpPreparedSimulation):
        raise TypeError("typed prepared simulation required")
    if not isinstance(decision_reference, EvidenceRef):
        raise TypeError("typed risk decision reference required")
    for name, value in (
        ("max fee per gas", max_fee_per_gas),
        ("priority fee per gas", max_priority_fee_per_gas),
    ):
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if max_priority_fee_per_gas > max_fee_per_gas:
        raise ValueError("priority fee cannot exceed the max fee")

    plan = prepared.plan
    intent = plan.intent
    if (plan.account, intent.chain_id, intent.target) != (
        policy.account,
        policy.chain_id,
        policy.executor,
    ):
        raise ValueError("the prepared plan is not the signed account, chain and executor")
    if (
        intent.runtime_pins != policy.runtime_pins
        or intent.pool_id != policy.pool_id
        or intent.config_hash != policy.config_hash
    ):
        raise ValueError("the prepared intent does not match the frozen signer policy")
    if calldata_hash(plan.calldata) != intent.calldata_hash:
        raise ValueError("the prepared calldata does not match its own intent")
    if (
        intent.liquidity > policy.max_liquidity
        or intent.amount0_max > policy.max_amount0
        or intent.amount1_max > policy.max_amount1
        or intent.max_fee_pips > policy.max_fee_pips
        or intent.max_gas_units > policy.max_gas_units
    ):
        raise ValueError("the prepared intent exceeds the signer policy capital or fee bounds")

    block = prepared.block
    if (
        block.chain_id != intent.chain_id
        or block.number != intent.state_block
        or block.block_hash != intent.state_hash
    ):
        raise ValueError("the plan's position evidence is not its own intent state block")

    if len(manifest.chains) != 1:
        # One request carries one block, one account and one chain, so a multi-chain
        # manifest cannot be reconciled into the single frame the ceiling is checked on.
        raise ValueError("one signing request reconciles exactly one manifest chain")
    entry = manifest.chains[0]
    if entry.key != f"{intent.chain_id}:{plan.account}":
        raise ValueError("the manifest does not declare the plan's chain and account")
    declared = tuple(
        position
        for position in entry.positions
        if position.pool_id == intent.pool_id and position.token_id == intent.position_token_id
    )
    if len(declared) != 1:
        # Without this the ceiling would be checked against a position the intent does
        # not rebalance, and the declared valuation would describe another position.
        raise ValueError("the manifest does not declare the position the intent rebalances")

    # Every reference is re-read, so a reference that was never retained cannot be
    # smuggled in as evidence the gate would accept by hash alone.
    for reference in (*plan.position_evidence, *prepared.evidence, *preflight, decision_reference):
        store.get_record(reference)
    _require_simulation_evidence(store, prepared, block)
    if {reference.source for reference in preflight} != {
        entry.primary_source,
        entry.secondary_source,
    }:
        raise ValueError("the preflight providers are not the manifest's declared pair")
    current_block, account_nonce, executor_last_nonce, runtime_pins = _require_preflight(
        store, preflight, policy=policy
    )
    decided = _require_risk_decision(
        store,
        decision_reference,
        intent=intent,
        prepared=prepared,
        block=block,
        manifest=manifest,
        max_fee_per_gas=max_fee_per_gas,
        max_priority_fee_per_gas=max_priority_fee_per_gas,
    )

    signing = LpSigningRequest(
        intent=intent,
        account=policy.account,
        target=policy.executor,
        calldata=plan.calldata,
        value=0,
        account_nonce=account_nonce,
        executor_last_nonce=executor_last_nonce,
        gas_limit=intent.max_gas_units,
        current_block=current_block,
        simulation_block=block,
        simulation_succeeded=True,
        simulation_calldata_hash=intent.calldata_hash,
        observed_runtime_pins=runtime_pins,
        risk_approved=decided.approved,
        risk_decision_hash="0x" + decision_reference.content_hash,
        evidence=(*prepared.evidence, *preflight, decision_reference),
    )
    request = LpSidecarRequest(
        signing=signing,
        max_fee_per_gas=max_fee_per_gas,
        max_priority_fee_per_gas=max_priority_fee_per_gas,
    )
    return request, authorize_lp_signing(policy, signing)


__all__ = [
    "POSITION_KIND",
    "LpPreparedSimulation",
    "assemble_lp_signing_request",
    "parse_prepared_simulation",
]
