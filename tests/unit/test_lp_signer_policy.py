from dataclasses import replace

import pytest
from eth_abi.abi import encode

from oasis_agent.execution.lp_signer_policy import (
    EXECUTE_SELECTOR,
    EXECUTE_TYPE,
    LpSignerPolicy,
    LpSigningRequest,
    authorize_lp_signing,
    calldata_hash,
    preflight_evidence_hash,
    simulation_evidence_hash,
)
from oasis_agent.models.common import BlockRef, EvidenceRef
from oasis_agent.models.v4 import DYNAMIC_FEE_FLAG, PoolKey
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key
from tests.unit.test_lp_state_machine import make_intent

ACCOUNT = "0x" + "45" * 20
EXECUTOR = "0x" + "44" * 20
CODE_HASHES = ("0x" + "12" * 32, "0x" + "34" * 32)
CONFIG_HASH = "0x" + "ef" * 32
POOL_KEY = PoolKey("0x" + "11" * 20, "0x" + "22" * 20, DYNAMIC_FEE_FLAG, 60, "0x" + "33" * 20)


def make_calldata(intent):
    mode = 0 if intent.mode.value == "recenter" else 1
    values = (
        intent.nonce,
        intent.deadline_timestamp,
        mode,
        (
            POOL_KEY.currency0,
            POOL_KEY.currency1,
            POOL_KEY.fee,
            POOL_KEY.tick_spacing,
            POOL_KEY.hooks,
        ),
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


def fixtures():
    intent = make_intent(
        pool_id=pool_id_from_key(POOL_KEY),
        target=EXECUTOR,
        state_block=100,
        state_hash="0x" + "ab" * 32,
        deadline_timestamp=1_050,
        config_hash=CONFIG_HASH,
        code_hashes=CODE_HASHES,
    )
    calldata = make_calldata(intent)
    intent = replace(intent, calldata_hash=calldata_hash(calldata))
    simulation = BlockRef(4663, 100, "0x" + "ab" * 32)
    policy = LpSignerPolicy(
        4663,
        ACCOUNT,
        EXECUTOR,
        intent.pool_id,
        CONFIG_HASH,
        CODE_HASHES,
        1_000_000,
        2_000_000,
        3_000_000,
        3_000_000,
        50_000,
        3,
        60,
    )
    request = LpSigningRequest(
        intent=intent,
        account=ACCOUNT,
        target=EXECUTOR,
        calldata=calldata,
        value=0,
        account_nonce=91,
        executor_last_nonce=intent.nonce - 1,
        gas_limit=2_000_000,
        current_block=BlockRef(4663, 101, "0x" + "cd" * 32, 1_010),
        simulation_block=simulation,
        simulation_succeeded=True,
        simulation_calldata_hash=calldata_hash(calldata),
        observed_code_hashes=CODE_HASHES,
        risk_approved=True,
        risk_decision_hash="0x" + "67" * 32,
        evidence=(),
    )
    simulation_references = tuple(
        EvidenceRef(
            "pinned-simulation",
            simulation_evidence_hash(request),
            simulation,
            source,
            "2026-09-07T00:00:00+00:00",
            "2" * 64,
            raw,
            "4" * 64,
        )
        for source, raw in (("rpc-a", "3" * 64), ("rpc-b", "5" * 64))
    )
    preflight_references = tuple(
        EvidenceRef(
            "execution-preflight",
            preflight_evidence_hash(request),
            request.current_block,
            source,
            "2026-09-07T00:00:00+00:00",
            "a" * 64,
            raw,
            "b" * 64,
        )
        for source, raw in (("rpc-a", "c" * 64), ("rpc-b", "d" * 64))
    )
    references = (
        simulation_references
        + preflight_references
        + (
            EvidenceRef(
                "risk-decision",
                "67" * 32,
                simulation,
                "risk-engine",
                "2026-09-07T00:00:00+00:00",
                "6" * 64,
                "7" * 64,
                "8" * 64,
            ),
        )
    )
    request = replace(request, evidence=references)
    return policy, request


def test_complete_bounded_request_is_authorized_without_signing_capability():
    policy, request = fixtures()
    decision = authorize_lp_signing(policy, request)
    assert decision.authorized
    assert decision.signing_eligible
    assert not decision.reasons
    assert len(decision.request_hash) == 66
    assert not hasattr(policy, "sign")


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("target", "0x" + "99" * 20, "target"),
        ("value", 1, "value"),
        ("executor_last_nonce", 3, "intent nonce"),
        ("account_nonce", 92, "execution preflight"),
        ("gas_limit", 0, "positive"),
        ("gas_limit", 4_000_000, "gas"),
        ("simulation_succeeded", False, "simulation failed"),
        ("risk_approved", False, "risk decision"),
        ("observed_code_hashes", ("0x" + "99" * 32,), "code hashes"),
    ],
)
def test_policy_rejects_each_independent_boundary(field, value, reason):
    policy, request = fixtures()
    decision = authorize_lp_signing(policy, replace(request, **{field: value}))
    assert not decision.authorized
    assert any(reason in item for item in decision.reasons)


def test_calldata_is_decoded_and_must_match_intent_and_simulation():
    policy, request = fixtures()
    altered = request.calldata[:-1] + bytes([request.calldata[-1] ^ 1])
    decision = authorize_lp_signing(policy, replace(request, calldata=altered))
    assert not decision.authorized
    assert "calldata does not match intent" in decision.reasons
    assert any("malformed" in item or "decoded" in item for item in decision.reasons)


def test_stale_state_deadline_and_single_provider_fail_closed():
    policy, request = fixtures()
    stale = replace(request, current_block=BlockRef(4663, 104, "0x" + "cd" * 32, 1_010))
    assert (
        "intent state is stale or ahead of current head"
        in authorize_lp_signing(policy, stale).reasons
    )
    late = replace(request, current_block=BlockRef(4663, 101, "0x" + "cd" * 32, 1_051))
    assert any("deadline" in reason for reason in authorize_lp_signing(policy, late).reasons)
    one_source = replace(
        request,
        evidence=(request.evidence[0], *request.evidence[2:]),
    )
    assert any(
        "two complete" in reason for reason in authorize_lp_signing(policy, one_source).reasons
    )


def test_missing_risk_evidence_and_provider_disagreement_fail_closed():
    policy, request = fixtures()
    missing_risk = replace(request, evidence=request.evidence[:2])
    assert any(
        "risk decision evidence" in reason
        for reason in authorize_lp_signing(policy, missing_risk).reasons
    )
    disagreeing = replace(
        request,
        evidence=(
            request.evidence[0],
            replace(request.evidence[1], interpretation_hash="9" * 64),
            request.evidence[2],
        ),
    )
    assert any(
        "agreeing providers" in reason
        for reason in authorize_lp_signing(policy, disagreeing).reasons
    )


def test_preflight_provider_disagreement_fails_closed():
    policy, request = fixtures()
    disagreeing = replace(
        request,
        evidence=(
            *request.evidence[:3],
            replace(request.evidence[3], interpretation_hash="9" * 64),
            request.evidence[4],
        ),
    )
    assert any(
        "execution preflight" in reason
        for reason in authorize_lp_signing(policy, disagreeing).reasons
    )
