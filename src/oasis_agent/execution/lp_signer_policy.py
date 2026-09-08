"""Independent static policy for bounded Oasis LP executor calldata.

This module has no key and no signing or broadcast interface. A signer sidecar
can call it before exposing its own capability. The strategy cannot override a
denial or widen this frozen policy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from eth_abi.abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_hash.auto import keccak

from ..models.common import (
    BlockRef,
    EvidenceRef,
    canonical_json,
    normalize_address,
    normalize_hash32,
    sha256_hex,
)
from ..models.v4 import PoolKey
from ..uniswap_v4.pool_id import pool_id_from_key
from .lp_state_machine import LpIntent, LpIntentMode

EXECUTE_TYPE = "(uint64,uint64,uint8,(address,address,uint24,int24,address),uint256,int24,int24,uint256,uint128,uint128,uint128,uint128,uint256,bytes)"
EXECUTE_SELECTOR = keccak(f"execute({EXECUTE_TYPE})".encode())[:4]


@dataclass(frozen=True, slots=True)
class LpSignerPolicy:
    chain_id: int
    account: str
    executor: str
    pool_id: str
    config_hash: str
    code_hashes: tuple[str, ...]
    max_liquidity: int
    max_amount0: int
    max_amount1: int
    max_gas_units: int
    max_fee_pips: int
    max_state_age_blocks: int
    max_deadline_seconds: int

    def __post_init__(self) -> None:
        if type(self.chain_id) is not int or self.chain_id <= 0:
            raise ValueError("positive signer chain required")
        for name in ("account", "executor"):
            address = normalize_address(getattr(self, name))
            if int(address, 16) == 0:
                raise ValueError(f"{name} must be nonzero")
            object.__setattr__(self, name, address)
        for name in ("pool_id", "config_hash"):
            object.__setattr__(self, name, normalize_hash32(getattr(self, name)))
        if not self.code_hashes:
            raise ValueError("signer policy requires code hashes")
        object.__setattr__(
            self, "code_hashes", tuple(normalize_hash32(value) for value in self.code_hashes)
        )
        for name in (
            "max_liquidity",
            "max_amount0",
            "max_amount1",
            "max_gas_units",
            "max_fee_pips",
            "max_state_age_blocks",
            "max_deadline_seconds",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.max_fee_pips > 1_000_000:
            raise ValueError("signer fee limit exceeds v4 maximum")
        if self.max_state_age_blocks == 0 or self.max_deadline_seconds == 0:
            raise ValueError("freshness limits must be positive")


@dataclass(frozen=True, slots=True)
class LpSigningRequest:
    intent: LpIntent
    account: str
    target: str
    calldata: bytes
    value: int
    account_nonce: int
    executor_last_nonce: int
    gas_limit: int
    current_block: BlockRef
    simulation_block: BlockRef
    simulation_succeeded: bool
    simulation_calldata_hash: str
    observed_code_hashes: tuple[str, ...]
    risk_approved: bool
    risk_decision_hash: str
    evidence: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.intent, LpIntent):
            raise TypeError("typed LP intent required")
        object.__setattr__(self, "account", normalize_address(self.account))
        object.__setattr__(self, "target", normalize_address(self.target))
        if not isinstance(self.calldata, bytes):
            raise TypeError("calldata must be bytes")
        for name in ("value", "account_nonce", "executor_last_nonce", "gas_limit"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.executor_last_nonce >= 1 << 64:
            raise ValueError("executor_last_nonce must fit uint64")
        for name in ("simulation_succeeded", "risk_approved"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a bool")
        object.__setattr__(
            self, "simulation_calldata_hash", normalize_hash32(self.simulation_calldata_hash)
        )
        object.__setattr__(self, "risk_decision_hash", normalize_hash32(self.risk_decision_hash))
        object.__setattr__(
            self,
            "observed_code_hashes",
            tuple(normalize_hash32(value) for value in self.observed_code_hashes),
        )


@dataclass(frozen=True, slots=True)
class LpSignerDecision:
    authorized: bool
    reasons: tuple[str, ...]
    request_hash: str
    signing_eligible: bool = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "signing_eligible", self.authorized)


def calldata_hash(calldata: bytes) -> str:
    if not isinstance(calldata, bytes):
        raise TypeError("calldata must be bytes")
    return "0x" + keccak(calldata).hex()


def simulation_evidence_hash(request: LpSigningRequest) -> str:
    """Hash the exact state and transaction result providers must agree on."""
    return sha256_hex(
        canonical_json(
            {
                "schema": "oasis.lp-pinned-simulation.v1",
                "block": asdict(request.simulation_block),
                "calldata_hash": request.simulation_calldata_hash,
                "succeeded": request.simulation_succeeded,
                "code_hashes": request.observed_code_hashes,
            }
        )
    )


def preflight_evidence_hash(request: LpSigningRequest) -> str:
    """Hash independent account/executor nonce and runtime observations."""
    return sha256_hex(
        canonical_json(
            {
                "schema": "oasis.lp-execution-preflight.v1",
                "block": asdict(request.current_block),
                "account": request.account,
                "account_nonce": request.account_nonce,
                "executor": request.target,
                "executor_last_nonce": request.executor_last_nonce,
                "code_hashes": request.observed_code_hashes,
            }
        )
    )


def authorize_lp_signing(policy: LpSignerPolicy, request: LpSigningRequest) -> LpSignerDecision:
    reasons: list[str] = []
    intent = request.intent
    actual_calldata_hash = calldata_hash(request.calldata)
    if intent.chain_id != policy.chain_id or request.current_block.chain_id != policy.chain_id:
        reasons.append("chain is not allowed")
    if request.account != policy.account:
        reasons.append("signing account is not allowed")
    if request.target != policy.executor or intent.target != policy.executor:
        reasons.append("transaction target is not the bounded executor")
    if intent.pool_id != policy.pool_id:
        reasons.append("pool is not allowed")
    if intent.config_hash != policy.config_hash:
        reasons.append("intent config is not the frozen signer policy")
    if (
        intent.code_hashes != policy.code_hashes
        or request.observed_code_hashes != policy.code_hashes
    ):
        reasons.append("runtime code hashes do not match policy")
    if actual_calldata_hash != intent.calldata_hash:
        reasons.append("calldata does not match intent")
    if request.simulation_calldata_hash != actual_calldata_hash:
        reasons.append("simulation used different calldata")
    if request.value != 0:
        reasons.append("LP executor transaction value must be zero")
    if intent.nonce <= request.executor_last_nonce:
        reasons.append("executor intent nonce is stale")
    if request.gas_limit == 0:
        reasons.append("gas limit must be positive")
    if request.gas_limit > min(intent.max_gas_units, policy.max_gas_units):
        reasons.append("gas limit exceeds intent or signer policy")
    if intent.max_fee_pips > policy.max_fee_pips:
        reasons.append("fee limit exceeds signer policy")
    if (
        request.simulation_block.chain_id != intent.chain_id
        or request.simulation_block.number != intent.state_block
        or request.simulation_block.block_hash != intent.state_hash
    ):
        reasons.append("simulation is not pinned to intent state")
    if (
        request.current_block.number < intent.state_block
        or request.current_block.number - intent.state_block > policy.max_state_age_blocks
    ):
        reasons.append("intent state is stale or ahead of current head")
    if request.current_block.timestamp is None:
        reasons.append("current block timestamp is unavailable")
    elif (
        intent.deadline_timestamp < request.current_block.timestamp
        or intent.deadline_timestamp - request.current_block.timestamp > policy.max_deadline_seconds
    ):
        reasons.append("intent deadline is expired or too far in the future")
    if not request.simulation_succeeded:
        reasons.append("pinned simulation failed")
    if not request.risk_approved:
        reasons.append("independent risk decision denied the intent")
    simulation_evidence = tuple(
        reference for reference in request.evidence if reference.kind == "pinned-simulation"
    )
    sources = {reference.source for reference in simulation_evidence}
    expected_simulation_hash = simulation_evidence_hash(request)
    interpretations = {
        (reference.content_hash, reference.interpretation_hash) for reference in simulation_evidence
    }
    if (
        len(sources) < 2
        or len(interpretations) != 1
        or {item[0] for item in interpretations} != {expected_simulation_hash}
        or any(
            reference.block != request.simulation_block
            or not reference.request_hash
            or not reference.raw_response_hash
            or not reference.interpretation_hash
            for reference in simulation_evidence
        )
    ):
        reasons.append("simulation evidence lacks two complete agreeing providers")
    preflight_evidence = tuple(
        reference for reference in request.evidence if reference.kind == "execution-preflight"
    )
    preflight_sources = {reference.source for reference in preflight_evidence}
    expected_preflight_hash = preflight_evidence_hash(request)
    preflight_interpretations = {
        (reference.content_hash, reference.interpretation_hash) for reference in preflight_evidence
    }
    if (
        len(preflight_sources) < 2
        or len(preflight_interpretations) != 1
        or {item[0] for item in preflight_interpretations} != {expected_preflight_hash}
        or any(
            reference.block != request.current_block
            or not reference.request_hash
            or not reference.raw_response_hash
            or not reference.interpretation_hash
            for reference in preflight_evidence
        )
    ):
        reasons.append("execution preflight lacks two complete agreeing providers")
    risk_evidence = tuple(
        reference
        for reference in request.evidence
        if reference.kind == "risk-decision"
        and "0x" + reference.content_hash == request.risk_decision_hash
    )
    if len(risk_evidence) != 1 or any(
        reference.block != request.simulation_block
        or not reference.request_hash
        or not reference.raw_response_hash
        or not reference.interpretation_hash
        for reference in risk_evidence
    ):
        reasons.append("risk decision evidence is missing or does not match")

    try:
        if request.calldata[:4] != EXECUTE_SELECTOR:
            raise ValueError("wrong selector")
        decoded = decode([EXECUTE_TYPE], request.calldata[4:])[0]
        if EXECUTE_SELECTOR + encode([EXECUTE_TYPE], [decoded]) != request.calldata:
            raise ValueError("noncanonical calldata")
        (
            nonce,
            deadline,
            mode,
            pool_values,
            old_token_id,
            tick_lower,
            tick_upper,
            liquidity,
            amount0_min,
            amount1_min,
            amount0_max,
            amount1_max,
            max_swap_input,
            hook_data,
        ) = decoded
        pool_key = PoolKey(*pool_values)
        decoded_mode = (
            LpIntentMode.RECENTER if mode == 0 else LpIntentMode.REMOVE if mode == 1 else None
        )
        if (
            nonce != intent.nonce
            or deadline != intent.deadline_timestamp
            or decoded_mode is not intent.mode
            or pool_id_from_key(pool_key) != intent.pool_id
            or old_token_id != intent.position_token_id
            or (tick_lower, tick_upper)
            != (intent.new_range.tick_lower, intent.new_range.tick_upper)
            or liquidity != intent.liquidity
            or amount0_min != intent.amount0_min
            or amount1_min != intent.amount1_min
            or amount0_max != intent.amount0_max
            or amount1_max != intent.amount1_max
            or max_swap_input != intent.max_swap_input
            or hook_data != b""
        ):
            reasons.append("decoded executor calldata does not match bounded intent")
        if (
            liquidity > policy.max_liquidity
            or amount0_max > policy.max_amount0
            or amount1_max > policy.max_amount1
        ):
            reasons.append("decoded capital exceeds signer policy")
    except (DecodingError, ValueError, TypeError, OverflowError):
        reasons.append("executor calldata is malformed or noncanonical")

    identity = canonical_json(
        {
            "intent_hash": intent.intent_hash,
            "calldata_hash": actual_calldata_hash,
            "risk_decision_hash": request.risk_decision_hash,
            "simulation_block": request.simulation_block.block_hash,
            "target": request.target,
            "account": request.account,
            "account_nonce": request.account_nonce,
            "executor_nonce": intent.nonce,
            "executor_last_nonce": request.executor_last_nonce,
        }
    )
    return LpSignerDecision(
        not reasons, tuple(dict.fromkeys(reasons)), "0x" + keccak(identity).hex()
    )


__all__ = [
    "EXECUTE_SELECTOR",
    "EXECUTE_TYPE",
    "LpSignerDecision",
    "LpSignerPolicy",
    "LpSigningRequest",
    "authorize_lp_signing",
    "calldata_hash",
    "preflight_evidence_hash",
    "simulation_evidence_hash",
]
