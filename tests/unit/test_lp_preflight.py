"""A preflight is only evidence when its own bytes re-derive to what the gate checks.

The signer gate refuses a request unless two independent providers, at the request's
own head, agree on the account nonce, the executor's own last nonce and every runtime
code hash. These tests hold the producer to that: the retained bytes must be exactly the
sequence the verifier re-checks, the payload must hash to the value the gate re-derives,
a provider that disagrees on any observed field must refuse, and every numeric read must
be pinned by block hash rather than to ``latest``.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from eth_hash.auto import keccak

from oasis_agent.evidence.exact_capture import (
    RecordedSession,
    wire_envelope,
)
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.rpc_validation import decode_exchange
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.lp_preflight import (
    LAST_NONCE_SELECTOR,
    PREFLIGHT_KIND,
    LpPreflightQuorum,
    collect_lp_preflight_quorum,
    persist_lp_preflight_evidence,
    replay_lp_preflight_evidence,
)
from oasis_agent.execution.lp_signer_policy import (
    LpSignerPolicy,
    LpSigningRequest,
    authorize_lp_signing,
    calldata_hash,
    preflight_evidence_hash,
    simulation_evidence_hash,
)
from oasis_agent.execution.lp_state_machine import RuntimePin, RuntimeRole
from oasis_agent.models.common import BlockRef, EvidenceRef, canonical_json
from oasis_agent.models.v4 import DYNAMIC_FEE_FLAG, PoolKey
from oasis_agent.rpc.transport import JsonRpcTransport
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key
from tests.unit.test_lp_signer_policy import make_calldata
from tests.unit.test_lp_state_machine import make_intent

PRIMARY = "rpc_a"
SECONDARY = "rpc_b"
CHAIN = 4663
ACCOUNT = "0x" + "45" * 20
EXECUTOR = "0x" + "44" * 20
POSITION_MANAGER = "0x" + "55" * 20
POOL_MANAGER = "0x" + "66" * 20
CONFIG_HASH = "0x" + "ef" * 32
POOL_KEY = PoolKey("0x" + "11" * 20, "0x" + "22" * 20, DYNAMIC_FEE_FLAG, 60, "0x" + "33" * 20)
EXECUTOR_CODE = bytes.fromhex("6001600055")
POSITION_MANAGER_CODE = bytes.fromhex("6002600055")
POOL_MANAGER_CODE = bytes.fromhex("6003600055")
CODES = {
    EXECUTOR: EXECUTOR_CODE,
    POSITION_MANAGER: POSITION_MANAGER_CODE,
    POOL_MANAGER: POOL_MANAGER_CODE,
}
# The pins are derived from the code the fixture serves: a hardcoded hash could not
# agree with any live runtime, so the fixture would prove nothing.
RUNTIME_PINS = (
    RuntimePin(RuntimeRole.EXECUTOR, EXECUTOR, "0x" + keccak(EXECUTOR_CODE).hex()),
    RuntimePin(
        RuntimeRole.POSITION_MANAGER, POSITION_MANAGER, "0x" + keccak(POSITION_MANAGER_CODE).hex()
    ),
    RuntimePin(RuntimeRole.POOL_MANAGER, POOL_MANAGER, "0x" + keccak(POOL_MANAGER_CODE).hex()),
)
HEAD_NUMBER = 101
HEAD_HASH = "0x" + "cd" * 32
OTHER_HASH = "0x" + "ee" * 32
PARENT_HASH = "0x" + "bb" * 32
HEAD_TIMESTAMP = 1_010
HEAD_BLOCK = BlockRef(CHAIN, HEAD_NUMBER, HEAD_HASH, HEAD_TIMESTAMP)
SIMULATION_BLOCK = BlockRef(CHAIN, 100, "0x" + "ab" * 32)
DEADLINE = 1_050
ACCOUNT_NONCE = 91
EXECUTOR_LAST_NONCE = 2
RETRIEVED_AT = "2026-09-25T00:00:00+00:00"
PREFLIGHT_DB = "preflight.db"
WIRE_SEQUENCE = [
    "eth_chainId",
    "eth_getBlockByNumber",
    "eth_getCode",
    "eth_getCode",
    "eth_getCode",
    "eth_call",
    "eth_getTransactionCount",
    "eth_getBlockByNumber",
]


@dataclass
class PreflightFixture:
    """One deterministic provider answer for one pinned head."""

    chain_id: int = CHAIN
    block_number: int = HEAD_NUMBER
    block_hash: str = HEAD_HASH
    parent_hash: str = PARENT_HASH
    timestamp: int = HEAD_TIMESTAMP
    account_nonce: int = ACCOUNT_NONCE
    executor_last_nonce: int = EXECUTOR_LAST_NONCE
    executor_code: bytes = EXECUTOR_CODE
    nonce_word: str | None = None
    header_sequence: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)

    def post(self, payload: bytes) -> bytes:
        request = json.loads(payload)
        self.methods.append(request["method"])
        return canonical_json(
            {"jsonrpc": "2.0", "id": request["id"], "result": self._result(request)}
        )

    def _result(self, request: Mapping[str, object]) -> object:
        method = str(request["method"])
        params = request["params"]
        assert isinstance(params, list)
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "eth_getBlockByNumber":
            return {
                "number": hex(self.block_number),
                "hash": self.header_sequence.pop(0) if self.header_sequence else self.block_hash,
                "parentHash": self.parent_hash,
                "timestamp": hex(self.timestamp),
            }
        if method == "eth_getCode":
            return "0x" + (self.executor_code if params[0] == EXECUTOR else CODES[params[0]]).hex()
        if method == "eth_call":
            assert params[0]["data"] == LAST_NONCE_SELECTOR
            if self.nonce_word is not None:
                return self.nonce_word
            return "0x" + self.executor_last_nonce.to_bytes(32, "big").hex()
        if method == "eth_getTransactionCount":
            return hex(self.account_nonce)
        raise AssertionError(method)


def session(source: str, poster: PreflightFixture) -> RecordedSession:
    """A fresh transport and recorder per provider, exactly as a bounded capture needs."""
    recorder = BoundedExchangeRecorder()
    return (
        JsonRpcTransport("https://fixture.invalid", http=poster, recorder=recorder),
        recorder,
        source,
    )


def make_policy() -> LpSignerPolicy:
    return LpSignerPolicy(
        CHAIN,
        ACCOUNT,
        EXECUTOR,
        pool_id_from_key(POOL_KEY),
        CONFIG_HASH,
        RUNTIME_PINS,
        1_000_000,
        2_000_000,
        3_000_000,
        3_000_000,
        50_000,
        3,
        60,
    )


@dataclass
class Collected:
    quorum: LpPreflightQuorum
    primary: PreflightFixture
    secondary: PreflightFixture


def collect(
    *,
    primary: PreflightFixture | None = None,
    secondary: PreflightFixture | None = None,
) -> Collected:
    left = primary or PreflightFixture()
    right = secondary or PreflightFixture()
    quorum = collect_lp_preflight_quorum(
        session(PRIMARY, left),
        session(SECONDARY, right),
        policy=make_policy(),
        block_number=HEAD_NUMBER,
    )
    return Collected(quorum, left, right)


def retain(tmp_path: Path) -> tuple[Collected, tuple[EvidenceRef, EvidenceRef]]:
    collected = collect()
    store = EvidenceStore(tmp_path / PREFLIGHT_DB)
    try:
        references = persist_lp_preflight_evidence(
            store, collected.quorum, retrieved_at=RETRIEVED_AT
        )
    finally:
        store.close()
    return collected, references


def simulation_references(request: LpSigningRequest) -> tuple[EvidenceRef, EvidenceRef]:
    def reference(source: str, raw: str) -> EvidenceRef:
        return EvidenceRef(
            "pinned-simulation",
            simulation_evidence_hash(request),
            SIMULATION_BLOCK,
            source,
            RETRIEVED_AT,
            "2" * 64,
            raw,
            "4" * 64,
        )

    return reference("rpc-a", "3" * 64), reference("rpc-b", "5" * 64)


def signing_request(
    preflight: Sequence[EvidenceRef],
    *,
    current_block: BlockRef = HEAD_BLOCK,
    account_nonce: int = ACCOUNT_NONCE,
) -> LpSigningRequest:
    """The full request a gate would judge, with only the preflight produced for real."""
    intent = make_intent(
        pool_id=pool_id_from_key(POOL_KEY),
        target=EXECUTOR,
        state_block=SIMULATION_BLOCK.number,
        state_hash=SIMULATION_BLOCK.block_hash,
        deadline_timestamp=DEADLINE,
        config_hash=CONFIG_HASH,
        runtime_pins=RUNTIME_PINS,
    )
    calldata = make_calldata(intent)
    intent = replace(intent, calldata_hash=calldata_hash(calldata))
    provisional = LpSigningRequest(
        intent=intent,
        account=ACCOUNT,
        target=EXECUTOR,
        calldata=calldata,
        value=0,
        account_nonce=account_nonce,
        executor_last_nonce=EXECUTOR_LAST_NONCE,
        gas_limit=2_000_000,
        current_block=current_block,
        simulation_block=SIMULATION_BLOCK,
        simulation_succeeded=True,
        simulation_calldata_hash=calldata_hash(calldata),
        observed_runtime_pins=RUNTIME_PINS,
        risk_approved=True,
        risk_decision_hash="0x" + "67" * 32,
        evidence=(),
    )
    return replace(
        provisional,
        evidence=(
            *simulation_references(provisional),
            *preflight,
            EvidenceRef(
                "risk-decision",
                "67" * 32,
                SIMULATION_BLOCK,
                "risk-engine",
                RETRIEVED_AT,
                "6" * 64,
                "7" * 64,
                "8" * 64,
            ),
        ),
    )


def test_two_providers_must_issue_exactly_the_verifier_sequence() -> None:
    collected = collect()
    assert collected.quorum.left.payload == collected.quorum.right.payload
    assert collected.quorum.left.header.ref == HEAD_BLOCK
    assert collected.primary.methods == WIRE_SEQUENCE
    assert collected.secondary.methods == WIRE_SEQUENCE


def test_every_numeric_read_is_pinned_to_the_observed_head() -> None:
    """``latest`` would let two reads describe different states; a hash pin cannot."""
    pin = {"blockHash": HEAD_HASH, "requireCanonical": True}
    for exchange in collect().quorum.left.exchanges:
        params, _ = decode_exchange(exchange)
        if exchange.method == "eth_chainId":
            assert params == []
        elif exchange.method in {"eth_getCode", "eth_call", "eth_getTransactionCount"}:
            assert params[-1] == pin
        else:
            assert params == [hex(HEAD_NUMBER), False]


def test_retained_preflight_is_exactly_what_the_signer_gate_re_derives(tmp_path) -> None:
    """The producer's hash and the gate's hash must be one definition, not two."""
    _, references = retain(tmp_path)
    request = signing_request(references)
    expected = preflight_evidence_hash(request)
    for reference in references:
        assert reference.content_hash == expected
        assert reference.interpretation_hash == expected
        assert reference.block == request.current_block
        assert reference.request_hash and reference.raw_response_hash
    assert authorize_lp_signing(make_policy(), request).authorized


def test_a_preflight_from_another_head_does_not_authorize(tmp_path) -> None:
    _, references = retain(tmp_path)
    later = BlockRef(CHAIN, HEAD_NUMBER + 1, OTHER_HASH, HEAD_TIMESTAMP)
    decision = authorize_lp_signing(make_policy(), signing_request(references, current_block=later))
    assert "execution preflight lacks two complete agreeing providers" in decision.reasons


def test_the_gate_binds_the_preflight_to_the_declared_account_nonce(tmp_path) -> None:
    _, references = retain(tmp_path)
    decision = authorize_lp_signing(
        make_policy(), signing_request(references, account_nonce=ACCOUNT_NONCE + 1)
    )
    assert not decision.authorized
    assert "execution preflight lacks two complete agreeing providers" in decision.reasons


def test_retained_preflight_replays_to_the_same_observation(tmp_path) -> None:
    collected, references = retain(tmp_path)
    store = EvidenceStore(tmp_path / PREFLIGHT_DB)
    try:
        for reference, provider in zip(references, (PRIMARY, SECONDARY), strict=True):
            assert reference.source == provider
            capture = replay_lp_preflight_evidence(store, reference)
            assert capture.payload == collected.quorum.left.payload
            assert capture.header == collected.quorum.left.header
    finally:
        store.close()


def test_replay_refuses_a_payload_its_own_bytes_contradict(tmp_path) -> None:
    """A payload is a claim; only the retained wire bytes can settle it."""
    collected, _ = retain(tmp_path)
    doctored = canonical_json(
        {**json.loads(collected.quorum.left.payload), "account_nonce": ACCOUNT_NONCE + 1}
    )
    exchanges = collected.quorum.left.exchanges
    store = EvidenceStore(tmp_path / PREFLIGHT_DB)
    try:
        reference = store.put(
            PREFLIGHT_KIND,
            doctored,
            HEAD_BLOCK,
            PRIMARY,
            request=wire_envelope(exchanges, response=False),
            raw_response=wire_envelope(exchanges, response=True),
            interpretation=doctored,
            retrieved_at=RETRIEVED_AT,
            schema_version=2,
        )
        with pytest.raises(ValueError, match="account nonce mismatch"):
            replay_lp_preflight_evidence(store, reference)
    finally:
        store.close()


def test_replay_refuses_another_evidence_kind(tmp_path) -> None:
    _, references = retain(tmp_path)
    store = EvidenceStore(tmp_path / PREFLIGHT_DB)
    try:
        with pytest.raises(ValueError, match="not an execution preflight"):
            replay_lp_preflight_evidence(store, replace(references[0], kind="pinned-simulation"))
    finally:
        store.close()


def test_a_disagreeing_provider_refuses_the_quorum() -> None:
    with pytest.raises(ValueError, match="preflight providers disagree"):
        collect(secondary=PreflightFixture(account_nonce=ACCOUNT_NONCE + 1))


def test_a_provider_that_does_not_see_the_pinned_head_refuses_the_quorum() -> None:
    with pytest.raises(ValueError, match="preflight providers disagree"):
        collect(secondary=PreflightFixture(block_hash=OTHER_HASH))


def test_a_head_that_moves_before_the_closing_read_refuses() -> None:
    """The closing re-read is the only thing that rules out a mid-flight reorg."""
    with pytest.raises(ValueError, match="preflight block changed"):
        collect(primary=PreflightFixture(header_sequence=[HEAD_HASH, OTHER_HASH]))


def test_a_runtime_whose_live_code_differs_from_the_pin_refuses() -> None:
    with pytest.raises(ValueError, match="runtime hash mismatch"):
        collect(primary=PreflightFixture(executor_code=bytes.fromhex("6001600155")))


def test_a_nonce_word_that_is_not_a_uint64_refuses() -> None:
    with pytest.raises(ValueError, match="noncanonical uint64 word"):
        collect(primary=PreflightFixture(nonce_word="0x" + "01" + "00" * 31))


def test_a_chain_that_is_not_the_policy_chain_refuses() -> None:
    with pytest.raises(Exception, match="unexpected chain id"):
        collect(primary=PreflightFixture(chain_id=8453))


def test_the_collector_refuses_a_block_that_is_not_a_nonnegative_integer() -> None:
    with pytest.raises(ValueError, match="nonnegative preflight block"):
        collect_lp_preflight_quorum(
            session(PRIMARY, PreflightFixture()),
            session(SECONDARY, PreflightFixture()),
            policy=make_policy(),
            block_number=-1,
        )
