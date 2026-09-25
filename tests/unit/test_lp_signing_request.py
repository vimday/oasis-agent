"""One real intent reaches a signer only when three retained proofs agree byte for byte.

``authorize_lp_signing`` refuses every request unless a pinned simulation, a fresh
execution preflight and exactly one risk decision are all retained evidence whose own
bytes re-derive to the hashes it checks. These tests drive the assembler that produces
that bundle from the real producers -- a plan simulated against two independent
providers, a two-provider preflight at one head, and a decision priced from the same
retained account proofs -- so the first test is the end-to-end claim and the rest hold
each refusal: an unretained reference, a plan outside the frozen policy, a manifest
that never declared the position, a decision priced for another intent, simulation
bytes that are not the plan's own, and a preflight from an undeclared provider all
refuse, while a stale head, a stale executor nonce, an unexplained account and an
exhausted capital ceiling are returned as denials the caller must read.
"""

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import pytest
from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.account_risk_reader import EvidenceReplayAccountRiskReader
from oasis_agent.execution.lp_preflight import (
    collect_lp_preflight_quorum,
    persist_lp_preflight_evidence,
)
from oasis_agent.execution.lp_preparation import LpSimulationPlan
from oasis_agent.execution.lp_risk_decision import (
    LpRiskDecision,
    decide_lp_risk,
    persist_lp_risk_decision,
    replay_lp_risk_decision,
)
from oasis_agent.execution.lp_signer_policy import (
    EXECUTE_SELECTOR,
    EXECUTE_TYPE,
    LpSignerDecision,
    LpSignerPolicy,
    authorize_lp_signing,
    calldata_hash,
    simulation_payload,
)
from oasis_agent.execution.lp_signer_sidecar import (
    LpSidecarConfig,
    LpSidecarRequest,
    sidecar_config_to_json,
    sidecar_limits_from_json,
    sidecar_request_from_json,
    sidecar_request_to_json,
)
from oasis_agent.execution.lp_signing_request import (
    LpPreparedSimulation,
    assemble_lp_signing_request,
    parse_prepared_simulation,
)
from oasis_agent.execution.lp_simulation import (
    POOL_MANAGER_SELECTOR,
    POSITION_MANAGER_SELECTOR,
    SIMULATION_KIND,
    LpSimulationResult,
    simulate_lp_plan,
)
from oasis_agent.execution.lp_state_machine import (
    LpIntent,
    LpIntentMode,
    RuntimePin,
    RuntimeRole,
    TickRange,
)
from oasis_agent.farming.positions import PositionSnapshot
from oasis_agent.models.common import ZERO_ADDRESS, BlockRef, EvidenceRef, canonical_json
from oasis_agent.models.v4 import PoolKey
from oasis_agent.risk.account_risk import GlobalRiskPolicy, PortfolioFrame
from oasis_agent.risk.portfolio_manifest import ChainInventory, PortfolioManifest
from oasis_agent.rpc.transport import JsonRpcTransport
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key
from tests.unit.test_account_risk_reader import (
    ACCOUNT,
    BLOCK_HASH,
    BLOCK_NUMBER,
    BLOCK_TWO,
    CHAIN_ID,
    FEED_MAX_AGE,
    HASH_TWO,
    MAX_SKEW,
    NATIVE_UNITS,
    PARENT_HASH,
    POOL_MANAGER,
    POSITION_ID,
    POSITION_MANAGER,
    SECOND_ACCOUNT,
    TIMESTAMP,
    InventoryFixture,
    PositionSpec,
    chain_inventory,
    observation,
    observe_inventory,
    observe_position,
    position_pool_key,
    position_snapshot,
)
from tests.unit.test_lp_preflight import (
    ACCOUNT_NONCE,
    CONFIG_HASH,
    EXECUTOR,
    EXECUTOR_LAST_NONCE,
    PRIMARY,
    SECONDARY,
    PreflightFixture,
    session,
)

NONCE = 3
GAS_LIMIT = 3_000_000
LIQUIDITY = 100_000
AMOUNT0_MAX = 200_000
AMOUNT1_MAX = 300_000
MAX_SWAP_INPUT = 50_000
MAX_FEE_PIPS = 50_000
MAX_LIQUIDITY = 1_000_000
MAX_AMOUNT0 = 2_000_000
MAX_AMOUNT1 = 3_000_000
MAX_STATE_AGE_BLOCKS = 3
MAX_DEADLINE_SECONDS = 60
CAPITAL_CEILING = 500_000_000
DAILY_LOSS = 200_000_000
CUMULATIVE_LOSS = 500_000_000
MAX_FEE_PER_GAS = 1_000_000_000
PRIORITY_FEE_PER_GAS = 100_000_000
POSITION_LIQUIDITY = 10**8
DECISION_DIGEST = "a" * 64
TICK_SPACING = 60
OLD_RANGE = (-120, 120)
NEW_RANGE = (-180, 60)
HEAD_BLOCK = BLOCK_TWO
HEAD_HASH = HASH_TWO
HEAD_TIMESTAMP = TIMESTAMP + 30
DEADLINE = TIMESTAMP + 40
EXECUTOR_CODE = bytes.fromhex("6001600055")
POSITION_MANAGER_CODE = bytes.fromhex("6004")
POOL_MANAGER_CODE = bytes.fromhex("6006")
RUNTIME_CODES = {
    EXECUTOR: EXECUTOR_CODE,
    POSITION_MANAGER: POSITION_MANAGER_CODE,
    POOL_MANAGER: POOL_MANAGER_CODE,
}
# The pins are derived from the code the fixtures serve: a hardcoded hash could not
# agree with any live runtime, so the bundle would prove nothing.
RUNTIME_PINS = (
    RuntimePin(RuntimeRole.EXECUTOR, EXECUTOR, "0x" + keccak(EXECUTOR_CODE).hex()),
    RuntimePin(
        RuntimeRole.POSITION_MANAGER, POSITION_MANAGER, "0x" + keccak(POSITION_MANAGER_CODE).hex()
    ),
    RuntimePin(RuntimeRole.POOL_MANAGER, POOL_MANAGER, "0x" + keccak(POOL_MANAGER_CODE).hex()),
)
GETTERS = {
    "0x" + POSITION_MANAGER_SELECTOR.hex(): POSITION_MANAGER,
    "0x" + POOL_MANAGER_SELECTOR.hex(): POOL_MANAGER,
}


@dataclass(frozen=True)
class Scenario:
    """One state block, its later preflight head and the account movement between them."""

    name: str
    state_block: int = BLOCK_NUMBER
    state_hash: str = BLOCK_HASH
    state_timestamp: int = TIMESTAMP
    head_block: int = HEAD_BLOCK
    head_hash: str = HEAD_HASH
    head_timestamp: int = HEAD_TIMESTAMP
    deadline: int = DEADLINE
    native_units: int = NATIVE_UNITS
    previous: bool = False


BASE = Scenario("baseline")
# A head further ahead than the policy's own state age: the intent is coherent, the
# state it was priced against is simply too old to sign.
STALE = Scenario(
    "stale-head",
    head_block=BLOCK_NUMBER + MAX_STATE_AGE_BLOCKS + 2,
    head_hash="0x" + "d0" * 32,
    head_timestamp=TIMESTAMP + 90,
    deadline=TIMESTAMP + 100,
)
# One native unit leaves the account with no gas declaration: the frame is complete
# but its inventory claim is unproven, so the ledger's residual is not zero.
UNEXPLAINED = Scenario(
    "unexplained",
    state_block=BLOCK_TWO,
    state_hash=HASH_TWO,
    state_timestamp=TIMESTAMP + 30,
    head_block=BLOCK_TWO + 1,
    head_hash="0x" + "cf" * 32,
    head_timestamp=TIMESTAMP + 60,
    deadline=TIMESTAMP + 70,
    native_units=NATIVE_UNITS - 1,
    previous=True,
)


@dataclass
class SimulationPoster:
    """One provider of the exact wire sequence a pinned LP simulation issues."""

    calldata: bytes
    block_number: int = BLOCK_NUMBER
    block_hash: str = BLOCK_HASH
    timestamp: int = TIMESTAMP
    parent_hash: str = PARENT_HASH
    chain_id: int = CHAIN_ID
    gas_limit: int = GAS_LIMIT
    codes: dict[str, bytes] = field(default_factory=lambda: dict(RUNTIME_CODES))
    methods: list[str] = field(default_factory=list)

    def post(self, payload: bytes) -> bytes:
        request = json.loads(payload)
        self.methods.append(request["method"])
        return canonical_json(
            {"jsonrpc": "2.0", "id": request["id"], "result": self._result(request)}
        )

    def _result(self, request: Mapping[str, object]) -> object:
        method, params = request["method"], request["params"]
        assert isinstance(method, str) and isinstance(params, list)
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "eth_getBlockByNumber":
            return {
                "number": hex(self.block_number),
                "hash": self.block_hash,
                "parentHash": self.parent_hash,
                "timestamp": hex(self.timestamp),
            }
        pin = {"blockHash": self.block_hash, "requireCanonical": True}
        if method == "eth_getCode":
            assert params[1] == pin
            return "0x" + self.codes[params[0]].hex()
        assert method == "eth_call" and params[1] == pin
        call = params[0]
        assert call["to"] == EXECUTOR
        getter = GETTERS.get(call["data"])
        if getter is not None:
            return "0x" + "00" * 12 + getter[2:]
        assert call == {
            "from": ACCOUNT,
            "to": EXECUTOR,
            "value": "0x0",
            "gas": hex(self.gas_limit),
            "data": "0x" + self.calldata.hex(),
        }
        return "0x"


@dataclass
class PolicyPreflight(PreflightFixture):
    """One preflight provider whose runtime codes are this policy's own pins."""

    codes: dict[str, bytes] = field(default_factory=lambda: dict(RUNTIME_CODES))

    def _result(self, request: Mapping[str, object]) -> object:
        if str(request["method"]) == "eth_getCode":
            params = request["params"]
            assert isinstance(params, list)
            return "0x" + self.codes[params[0]].hex()
        return super()._result(request)


@dataclass(frozen=True, slots=True)
class Bundle:
    """One assembled bundle, every retained proof behind it and the gate's verdict."""

    store: EvidenceStore
    policy: LpSignerPolicy
    manifest: PortfolioManifest
    prepared: LpPreparedSimulation
    decision: LpRiskDecision
    decision_reference: EvidenceRef
    preflight: tuple[EvidenceRef, EvidenceRef]
    request: LpSidecarRequest
    verdict: LpSignerDecision


def signer_policy() -> LpSignerPolicy:
    return LpSignerPolicy(
        CHAIN_ID,
        ACCOUNT,
        EXECUTOR,
        pool_id_from_key(position_pool_key()),
        CONFIG_HASH,
        RUNTIME_PINS,
        MAX_LIQUIDITY,
        MAX_AMOUNT0,
        MAX_AMOUNT1,
        GAS_LIMIT,
        MAX_FEE_PIPS,
        MAX_STATE_AGE_BLOCKS,
        MAX_DEADLINE_SECONDS,
    )


def a_manifest(chain: ChainInventory, *, ceiling: int = CAPITAL_CEILING) -> PortfolioManifest:
    return PortfolioManifest(
        policy=GlobalRiskPolicy(
            inventory={chain.key: chain.asset_ids},
            total_capital_usd_micros=ceiling,
            daily_loss_usd_micros=DAILY_LOSS,
            cumulative_loss_usd_micros=CUMULATIVE_LOSS,
            max_age_seconds=FEED_MAX_AGE,
            max_skew_seconds=MAX_SKEW,
        ),
        chains=(chain,),
    )


def two_chain_manifest() -> PortfolioManifest:
    first = chain_inventory(InventoryFixture(positions=(PositionSpec(),)))
    second = chain_inventory(replace(InventoryFixture(), chain_id=8453, account=SECOND_ACCOUNT))
    return PortfolioManifest(
        policy=GlobalRiskPolicy(
            inventory={first.key: first.asset_ids, second.key: second.asset_ids},
            total_capital_usd_micros=CAPITAL_CEILING,
            daily_loss_usd_micros=DAILY_LOSS,
            cumulative_loss_usd_micros=CUMULATIVE_LOSS,
            max_age_seconds=FEED_MAX_AGE,
            max_skew_seconds=MAX_SKEW,
        ),
        chains=(first, second),
    )


def inventory_fixture(scenario: Scenario) -> InventoryFixture:
    return replace(
        InventoryFixture(positions=(PositionSpec(),)),
        block_number=scenario.state_block,
        block_hash=scenario.state_hash,
        timestamp=scenario.state_timestamp,
        native_units=scenario.native_units,
    )


def state_view(scenario: Scenario) -> PositionSnapshot:
    return position_snapshot(
        block_number=scenario.state_block,
        block_hash=scenario.state_hash,
        timestamp=scenario.state_timestamp,
        liquidity=POSITION_LIQUIDITY,
    )


def make_intent(scenario: Scenario, *, nonce: int = NONCE) -> LpIntent:
    return LpIntent(
        "recenter-1",
        CHAIN_ID,
        pool_id_from_key(position_pool_key()),
        POSITION_ID,
        LpIntentMode.RECENTER,
        EXECUTOR,
        "0x" + "00" * 32,
        TickRange(*OLD_RANGE, TICK_SPACING),
        TickRange(*NEW_RANGE, TICK_SPACING),
        LIQUIDITY,
        0,
        0,
        AMOUNT0_MAX,
        AMOUNT1_MAX,
        MAX_SWAP_INPUT,
        MAX_FEE_PIPS,
        GAS_LIMIT,
        nonce,
        scenario.state_block,
        scenario.state_hash,
        scenario.deadline,
        CONFIG_HASH,
        RUNTIME_PINS,
    )


def position_calldata(intent: LpIntent) -> bytes:
    """The bounded executor call for this intent, encoded exactly as the gate decodes it."""
    key = position_pool_key()
    values = (
        intent.nonce,
        intent.deadline_timestamp,
        0 if intent.mode is LpIntentMode.RECENTER else 1,
        (key.currency0, key.currency1, key.fee, key.tick_spacing, key.hooks),
        intent.position_token_id,
        intent.new_range.tick_lower,
        intent.new_range.tick_upper,
        intent.liquidity,
        intent.amount0_min,
        intent.amount1_min,
        intent.amount0_max,
        intent.amount1_max,
        intent.max_swap_input,
        b"",
    )
    return EXECUTE_SELECTOR + encode([EXECUTE_TYPE], [values])


def report_bytes(plan: LpSimulationPlan, result: LpSimulationResult) -> bytes:
    """The plan's own report, once the pinned simulation it demanded actually exists."""
    report = json.loads(plan.payload)
    report["simulation"] = {
        "plan_hash": result.plan_hash,
        "evidence": [asdict(reference) for reference in result.evidence],
        "execution_eligible": False,
    }
    report["unresolved"] = [item for item in report["unresolved"] if item != "pinned_simulation"]
    return canonical_json(report)


def simulation_session(source: str, poster: SimulationPoster) -> tuple:
    recorder = BoundedExchangeRecorder()
    return (
        JsonRpcTransport("https://fixture.invalid", http=poster, recorder=recorder),
        recorder,
        source,
    )


def state_poster(calldata: bytes, scenario: Scenario) -> SimulationPoster:
    return SimulationPoster(
        calldata=calldata,
        block_number=scenario.state_block,
        block_hash=scenario.state_hash,
        timestamp=scenario.state_timestamp,
    )


def head_poster(scenario: Scenario) -> PolicyPreflight:
    return PolicyPreflight(
        block_number=scenario.head_block,
        block_hash=scenario.head_hash,
        timestamp=scenario.head_timestamp,
    )


def baseline_frame(store: EvidenceStore, manifest: PortfolioManifest) -> PortfolioFrame:
    """The account's own first frame, which the second one is reconciled against."""
    fixture = InventoryFixture(positions=(PositionSpec(),))
    key = manifest.chains[0].key
    return EvidenceReplayAccountRiskReader(
        store,
        manifest,
        {
            key: observation(
                observe_inventory(store, fixture),
                positions=observe_position(store, fixture),
            )
        },
    ).reconcile()


def assemble(
    tmp_path: Path,
    scenario: Scenario = BASE,
    *,
    name: str = "assembler",
    ceiling: int = CAPITAL_CEILING,
    nonce: int = NONCE,
    max_fee_per_gas: int = MAX_FEE_PER_GAS,
    max_priority_fee_per_gas: int = PRIORITY_FEE_PER_GAS,
) -> Bundle:
    """Build one bundle from the real producers alone, then run the frozen gate."""
    store = EvidenceStore(tmp_path / f"{name}.db")
    policy = signer_policy()
    fixture = inventory_fixture(scenario)
    manifest = a_manifest(chain_inventory(fixture), ceiling=ceiling)
    entry = manifest.chains[0]
    intent = make_intent(scenario, nonce=nonce)
    calldata = position_calldata(intent)
    intent = replace(intent, calldata_hash=calldata_hash(calldata))
    positions = observe_position(store, fixture, snapshot=state_view(scenario))
    plan = LpSimulationPlan(intent, calldata, ACCOUNT, DECISION_DIGEST, positions[0].snapshots)
    simulated = simulate_lp_plan(
        store,
        plan,
        simulation_session(PRIMARY, state_poster(calldata, scenario)),
        simulation_session(SECONDARY, state_poster(calldata, scenario)),
    )
    prepared = parse_prepared_simulation(report_bytes(plan, simulated))
    reader = EvidenceReplayAccountRiskReader(
        store,
        manifest,
        {
            entry.key: observation(
                observe_inventory(store, fixture),
                positions=positions,
            )
        },
        previous=baseline_frame(store, manifest) if scenario.previous else None,
    )
    decided = decide_lp_risk(
        reader,
        intent=intent,
        account=ACCOUNT,
        calldata=calldata,
        simulation_block=prepared.block,
        simulation_evidence=prepared.evidence,
        gas_limit=intent.max_gas_units,
        max_fee_per_gas=max_fee_per_gas,
        max_priority_fee_per_gas=max_priority_fee_per_gas,
    )
    decision_reference = persist_lp_risk_decision(store, decided)
    preflight = persist_lp_preflight_evidence(
        store,
        collect_lp_preflight_quorum(
            session(PRIMARY, head_poster(scenario)),
            session(SECONDARY, head_poster(scenario)),
            policy=policy,
            block_number=scenario.head_block,
        ),
    )
    request, verdict = assemble_lp_signing_request(
        store,
        policy=policy,
        manifest=manifest,
        prepared=prepared,
        preflight=preflight,
        decision_reference=decision_reference,
        max_fee_per_gas=max_fee_per_gas,
        max_priority_fee_per_gas=max_priority_fee_per_gas,
    )
    return Bundle(
        store,
        policy,
        manifest,
        prepared,
        decided,
        decision_reference,
        preflight,
        request,
        verdict,
    )


def again(built: Bundle, **overrides: object) -> tuple[LpSidecarRequest, LpSignerDecision]:
    """Re-run the gate over one bundle, replacing exactly the arguments under test."""
    arguments: dict[str, object] = {
        "policy": built.policy,
        "manifest": built.manifest,
        "prepared": built.prepared,
        "preflight": built.preflight,
        "decision_reference": built.decision_reference,
        "max_fee_per_gas": MAX_FEE_PER_GAS,
        "max_priority_fee_per_gas": PRIORITY_FEE_PER_GAS,
    }
    arguments.update(overrides)
    return assemble_lp_signing_request(built.store, **arguments)  # type: ignore[arg-type]


def test_a_real_plan_preflight_and_decision_authorize(tmp_path) -> None:
    """The end-to-end claim: three real producers are enough for the frozen gate."""
    built = assemble(tmp_path)
    try:
        signing = built.request.signing
        assert built.decision.approved is True
        assert built.verdict.authorized is True
        assert built.verdict.reasons == ()
        assert signing.current_block == BlockRef(CHAIN_ID, HEAD_BLOCK, HEAD_HASH, HEAD_TIMESTAMP)
        assert signing.simulation_block == built.prepared.block
        assert signing.simulation_block.number == BLOCK_NUMBER
        assert signing.gas_limit == signing.intent.max_gas_units == GAS_LIMIT
        assert signing.value == 0
        assert (signing.account_nonce, signing.executor_last_nonce) == (
            ACCOUNT_NONCE,
            EXECUTOR_LAST_NONCE,
        )
        assert signing.risk_decision_hash == "0x" + built.decision_reference.content_hash
        kinds = [reference.kind for reference in signing.evidence]
        assert kinds.count(SIMULATION_KIND) == 2
        assert kinds.count("execution-preflight") == 2
        assert kinds.count("risk-decision") == 1
    finally:
        built.store.close()


def test_the_offered_request_round_trips_and_still_authorizes(tmp_path) -> None:
    built = assemble(tmp_path)
    try:
        decoded = sidecar_request_from_json(sidecar_request_to_json(built.request))
        assert decoded == built.request
        assert authorize_lp_signing(built.policy, decoded.signing).authorized is True
    finally:
        built.store.close()


def test_the_sidecar_config_caps_are_the_ones_the_decision_priced(tmp_path) -> None:
    """The gas envelope is read from the sidecar, so a cheaper one cannot be smuggled in."""
    built = assemble(tmp_path)
    try:
        config = LpSidecarConfig(
            policy=built.policy,
            max_fee_per_gas=MAX_FEE_PER_GAS,
            max_priority_fee_per_gas=PRIORITY_FEE_PER_GAS,
            journal_path=tmp_path / "sidecar.db",
        )
        policy, max_fee_per_gas, max_priority_fee_per_gas = sidecar_limits_from_json(
            sidecar_config_to_json(config)
        )
        assert policy == built.policy
        assert (max_fee_per_gas, max_priority_fee_per_gas) == (
            MAX_FEE_PER_GAS,
            PRIORITY_FEE_PER_GAS,
        )
        assert (
            built.decision.max_fee_per_gas,
            built.decision.max_priority_fee_per_gas,
        ) == (max_fee_per_gas, max_priority_fee_per_gas)
        with pytest.raises(ValueError, match="does not price this request"):
            again(built, max_fee_per_gas=MAX_FEE_PER_GAS + 1)
    finally:
        built.store.close()


def test_a_reference_that_was_never_retained_is_refused(tmp_path) -> None:
    """A reference is a claim about the store; the store is asked before it is trusted."""
    built = assemble(tmp_path)
    try:
        with pytest.raises(KeyError):
            again(
                built, decision_reference=replace(built.decision_reference, content_hash="f" * 64)
            )
    finally:
        built.store.close()


def test_a_report_that_never_ran_the_pinned_simulation_is_refused(tmp_path) -> None:
    built = assemble(tmp_path)
    try:
        report = json.loads(built.prepared.plan.payload)
        assert "pinned_simulation" in report["unresolved"]
        report["simulation"] = {
            "plan_hash": report["intent_hash"],
            "evidence": [asdict(reference) for reference in built.prepared.evidence],
            "execution_eligible": False,
        }
        with pytest.raises(ValueError, match="carries no retained pinned simulation"):
            parse_prepared_simulation(canonical_json(report))
        report["unresolved"] = [
            item for item in report["unresolved"] if item != "pinned_simulation"
        ]
        assert parse_prepared_simulation(canonical_json(report)).plan == built.prepared.plan
        report.pop("simulation")
        with pytest.raises(ValueError, match="invalid fields"):
            parse_prepared_simulation(canonical_json(report))
    finally:
        built.store.close()


def test_a_plan_outside_the_frozen_policy_is_refused(tmp_path) -> None:
    built = assemble(tmp_path)
    other = pool_id_from_key(PoolKey("0x" + "11" * 20, "0x" + "22" * 20, 500, 10, ZERO_ADDRESS))
    try:
        assert other != built.policy.pool_id
        with pytest.raises(ValueError, match="does not match the frozen signer policy"):
            again(built, policy=replace(built.policy, pool_id=other))
    finally:
        built.store.close()


def test_a_manifest_without_the_rebalanced_position_is_refused(tmp_path) -> None:
    built = assemble(tmp_path)
    try:
        with pytest.raises(ValueError, match="does not declare the position"):
            again(built, manifest=a_manifest(chain_inventory(InventoryFixture())))
    finally:
        built.store.close()


def test_a_multi_chain_manifest_is_refused(tmp_path) -> None:
    """One request pins one block, so two chains cannot share one reconciled frame."""
    built = assemble(tmp_path)
    try:
        with pytest.raises(ValueError, match="exactly one manifest chain"):
            again(built, manifest=two_chain_manifest())
    finally:
        built.store.close()


def test_a_decision_priced_for_another_intent_is_refused(tmp_path) -> None:
    built = assemble(tmp_path)
    try:
        elsewhere = LpPreparedSimulation(
            replace(built.prepared.plan, intent=replace(built.prepared.plan.intent, intent_id="x")),
            built.prepared.evidence,
        )
        with pytest.raises(ValueError, match="does not price this request"):
            again(built, prepared=elsewhere)
    finally:
        built.store.close()


def test_simulation_bytes_that_are_not_the_plan_own_are_refused(tmp_path) -> None:
    """A hash-matched reference is not enough; the retained payload must be re-derived."""
    built = assemble(tmp_path)
    intent = built.prepared.plan.intent
    envelope = simulation_payload(
        block=built.prepared.block,
        account=ACCOUNT,
        target=EXECUTOR,
        value=0,
        gas_limit=intent.max_gas_units + 1,
        calldata_digest=intent.calldata_hash,
        succeeded=True,
        runtime_pins=RUNTIME_PINS,
    )
    doctored = tuple(
        built.store.put(
            SIMULATION_KIND,
            envelope,
            built.prepared.block,
            source,
            request={"encoding": "hex_exact_bytes", "exchanges": []},
            raw_response={"encoding": "hex_exact_bytes", "exchanges": []},
            interpretation=envelope,
            schema_version=2,
        )
        for source in (PRIMARY, SECONDARY)
    )
    try:
        with pytest.raises(ValueError, match="not this plan's own pinned simulation"):
            again(built, prepared=replace(built.prepared, evidence=(doctored[0], doctored[1])))
    finally:
        built.store.close()


def test_a_preflight_from_an_undeclared_provider_is_refused(tmp_path) -> None:
    built = assemble(tmp_path)
    rogue = persist_lp_preflight_evidence(
        built.store,
        collect_lp_preflight_quorum(
            session(PRIMARY, head_poster(BASE)),
            session("rpc_c", head_poster(BASE)),
            policy=built.policy,
            block_number=HEAD_BLOCK,
        ),
    )
    try:
        with pytest.raises(ValueError, match="not the manifest's declared pair"):
            again(built, preflight=rogue)
    finally:
        built.store.close()


def test_a_stale_head_denies_instead_of_refusing(tmp_path) -> None:
    """A coherent bundle on an expired state is a denial, not an assembly error."""
    built = assemble(tmp_path, STALE, name="stale-head")
    try:
        assert built.decision.approved is True
        assert built.verdict.authorized is False
        assert "intent state is stale or ahead of current head" in built.verdict.reasons
    finally:
        built.store.close()


def test_a_stale_executor_nonce_denies_instead_of_refusing(tmp_path) -> None:
    built = assemble(tmp_path, name="stale-nonce", nonce=EXECUTOR_LAST_NONCE)
    try:
        assert built.decision.approved is True
        assert built.verdict.authorized is False
        assert "executor intent nonce is stale" in built.verdict.reasons
    finally:
        built.store.close()


def test_an_unexplained_account_movement_denies_the_intent(tmp_path) -> None:
    """An unexplained residual makes the inventory claim the ceiling rests on unproven."""
    built = assemble(tmp_path, UNEXPLAINED, name="unexplained")
    try:
        assert built.decision.approved is False
        assert "the reconciled account has unresolved operations" in built.decision.reasons
        assert built.verdict.authorized is False
        assert "independent risk decision denied the intent" in built.verdict.reasons
    finally:
        built.store.close()


def test_a_capital_ceiling_that_cannot_absorb_the_worst_case_denies(tmp_path) -> None:
    built = assemble(tmp_path, name="ceiling", ceiling=1_000_000)
    try:
        assert built.decision.approved is False
        assert (
            "the worst-case commitment would exceed the capital ceiling" in built.decision.reasons
        )
        assert (
            built.decision.deployed_usd_micros + built.decision.worst_case_usd_micros
            > built.decision.capital_ceiling_usd_micros
        )
        assert built.verdict.authorized is False
        assert "independent risk decision denied the intent" in built.verdict.reasons
    finally:
        built.store.close()


def test_a_priority_fee_above_the_max_fee_is_refused(tmp_path) -> None:
    built = assemble(tmp_path, name="fees")
    try:
        with pytest.raises(ValueError, match="priority fee cannot exceed the max fee"):
            again(built, max_priority_fee_per_gas=MAX_FEE_PER_GAS + 1)
    finally:
        built.store.close()


def test_a_retained_decision_replays_to_its_own_bytes(tmp_path) -> None:
    built = assemble(tmp_path, name="replay")
    try:
        assert replay_lp_risk_decision(built.store, built.decision_reference) == built.decision
        record = built.store.get_record(built.decision_reference)
        assert record.request_payload == built.decision.request_envelope
        assert record.interpretation_payload == built.decision.payload
    finally:
        built.store.close()
