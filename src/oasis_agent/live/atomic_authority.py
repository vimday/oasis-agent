"""Fail-closed admission, byte verification, and receipt reconciliation for atomic/intent execution.

This module implements the policy and settlement authority boundary for both
`OasisAtomicExecutor.executeCycle` and `OasisIntentVault.executeReactorFill`.
It holds no private key, performs no signing, and makes no network or RPC calls.
"""

from dataclasses import asdict, dataclass
from typing import Literal

from eth_abi.abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_hash.auto import keccak

from ..execution.atomic_bundle import (
    BPS_DENOMINATOR,
    EXECUTE_CYCLE_SELECTOR,
    EXECUTE_CYCLE_SIGNATURE,
    EXECUTE_CYCLE_TYPE,
    UINT64_MAX,
)
from ..execution.lp_broadcast import decode_signed_transaction
from ..models.common import (
    ZERO_ADDRESS,
    canonical_json,
    normalize_address,
    normalize_hash32,
    sha256_hex,
)
from ..risk.revert_budget import (
    CYCLE_EXECUTED_TOPIC0,
    EARLY_ABORTED_TOPIC0,
    AttemptOutcome,
    ReceiptClassificationInput,
    ReceiptLog,
    RevertBudgetStatus,
    RollingRevertBudgetTracker,
    classify_execution_receipt,
)
from .intent import SubmittedTransaction

EXECUTE_REACTOR_FILL_PLAN_TYPE = (
    "(address,address,uint256,(address,uint160,uint160),"
    "(address,address,address,address,uint256,uint256,bytes)[])"
)
EXECUTE_REACTOR_FILL_PARAM_TYPES: tuple[str, str] = (EXECUTE_REACTOR_FILL_PLAN_TYPE, "bytes")
EXECUTE_REACTOR_FILL_SIGNATURE = f"executeReactorFill({EXECUTE_REACTOR_FILL_PLAN_TYPE},bytes)"
EXECUTE_REACTOR_FILL_SELECTOR: bytes = keccak(EXECUTE_REACTOR_FILL_SIGNATURE.encode())[:4]

EXECUTE_WITH_CALLBACK_SELECTOR: bytes = keccak(b"executeWithCallback((bytes,bytes),bytes)")[:4]
EXECUTE_BATCH_WITH_CALLBACK_SELECTOR: bytes = keccak(
    b"executeBatchWithCallback((bytes,bytes)[],bytes)"
)[:4]
VALID_REACTOR_SELECTORS: frozenset[bytes] = frozenset(
    {EXECUTE_WITH_CALLBACK_SELECTOR, EXECUTE_BATCH_WITH_CALLBACK_SELECTOR}
)
FORBIDDEN_ROUTER_SELECTORS: frozenset[bytes] = frozenset(
    {
        keccak(b"approve(address,uint256)")[:4],
        keccak(b"transfer(address,uint256)")[:4],
        keccak(b"transferFrom(address,address,uint256)")[:4],
    }
)

REACTOR_FILL_EXECUTED_EVENT_SIGNATURE = "ReactorFillExecuted(address,address,uint256,address)"
REACTOR_FILL_EXECUTED_TOPIC0: str = (
    "0x" + keccak(REACTOR_FILL_EXECUTED_EVENT_SIGNATURE.encode()).hex()
)
INTENT_VAULT_EARLY_ABORTED_EVENT_SIGNATURE = "EarlyAborted(address,address,uint160)"
INTENT_VAULT_EARLY_ABORTED_TOPIC0: str = (
    "0x" + keccak(INTENT_VAULT_EARLY_ABORTED_EVENT_SIGNATURE.encode()).hex()
)

MAX_SWAP_STEPS = 3
MAX_STEP_CALLDATA_BYTES = 4096
TargetKind = Literal["atomic_executor", "intent_vault", "rejected"]


def _require_plain_int(
    value: object,
    name: str,
    *,
    low: int | None = None,
    high: int | None = None,
) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be a plain int")
    if low is not None and value < low:
        raise ValueError(f"{name} must be >= {low}")
    if high is not None and value > high:
        raise ValueError(f"{name} must be <= {high}")
    return value


def _require_plain_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be a plain bool")
    return value


def _require_nonzero_address(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string address")
    normalized = normalize_address(value)
    if normalized == ZERO_ADDRESS:
        raise ValueError(f"{name} must be nonzero")
    return normalized


def _require_nonzero_hash32(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a 32-byte hex string")
    normalized = normalize_hash32(value, name)
    if int(normalized, 16) == 0:
        raise ValueError(f"{name} must be nonzero")
    return normalized


def _normalize_address_frozenset(values: object, name: str) -> frozenset[str]:
    if not isinstance(values, frozenset):
        raise TypeError(f"{name} must be a frozenset")
    return frozenset(_require_nonzero_address(item, name) for item in values)


def _require_sha256_hex(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} must be a 64-char sha256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be a 64-char sha256 hex digest") from exc
    return value.lower()


@dataclass(frozen=True, slots=True, kw_only=True)
class AtomicExecutionPolicy:
    """Fail-closed operator policy bounding atomic cycle and UniswapX intent fills."""

    chain_id: int = 8453
    operator_account: str
    executor_address: str
    executor_code_hash: str
    intent_vault_address: str
    intent_vault_code_hash: str
    allowed_pools: frozenset[str]
    allowed_routers: frozenset[str]
    allowed_reactors: frozenset[str]
    allowed_profit_tokens: frozenset[str]
    max_capital_usd_micros: int = 500_000_000
    max_daily_loss_usd_micros: int = 200_000_000
    max_cumulative_loss_usd_micros: int = 500_000_000
    max_gas_limit: int = 650_000
    max_fee_per_gas_wei: int = 5_000_000_000
    max_priority_fee_wei: int = 100_000_000
    max_deadline_horizon_s: int = 24
    release_gate_open: bool = False
    rehearsal_only: bool = False

    def __post_init__(self) -> None:
        _require_plain_int(self.chain_id, "chain_id", low=1, high=(1 << 256) - 1)
        operator = _require_nonzero_address(self.operator_account, "operator_account")
        executor = _require_nonzero_address(self.executor_address, "executor_address")
        vault = _require_nonzero_address(self.intent_vault_address, "intent_vault_address")
        if executor == vault:
            raise ValueError("executor_address and intent_vault_address must differ")
        object.__setattr__(self, "operator_account", operator)
        object.__setattr__(self, "executor_address", executor)
        object.__setattr__(self, "intent_vault_address", vault)
        object.__setattr__(
            self,
            "executor_code_hash",
            _require_nonzero_hash32(self.executor_code_hash, "executor_code_hash"),
        )
        object.__setattr__(
            self,
            "intent_vault_code_hash",
            _require_nonzero_hash32(self.intent_vault_code_hash, "intent_vault_code_hash"),
        )
        object.__setattr__(
            self,
            "allowed_pools",
            _normalize_address_frozenset(self.allowed_pools, "allowed_pools"),
        )
        object.__setattr__(
            self,
            "allowed_routers",
            _normalize_address_frozenset(self.allowed_routers, "allowed_routers"),
        )
        object.__setattr__(
            self,
            "allowed_reactors",
            _normalize_address_frozenset(self.allowed_reactors, "allowed_reactors"),
        )
        object.__setattr__(
            self,
            "allowed_profit_tokens",
            _normalize_address_frozenset(self.allowed_profit_tokens, "allowed_profit_tokens"),
        )
        _require_plain_int(self.max_capital_usd_micros, "max_capital_usd_micros", low=1)
        _require_plain_int(self.max_daily_loss_usd_micros, "max_daily_loss_usd_micros", low=0)
        _require_plain_int(
            self.max_cumulative_loss_usd_micros, "max_cumulative_loss_usd_micros", low=0
        )
        _require_plain_int(self.max_gas_limit, "max_gas_limit", low=1, high=UINT64_MAX)
        _require_plain_int(
            self.max_fee_per_gas_wei, "max_fee_per_gas_wei", low=1, high=(1 << 256) - 1
        )
        _require_plain_int(
            self.max_priority_fee_wei,
            "max_priority_fee_wei",
            low=0,
            high=(1 << 256) - 1,
        )
        _require_plain_int(self.max_deadline_horizon_s, "max_deadline_horizon_s", low=1)
        _require_plain_bool(self.release_gate_open, "release_gate_open")
        _require_plain_bool(self.rehearsal_only, "rehearsal_only")
        if self.release_gate_open and self.rehearsal_only:
            raise ValueError("release_gate_open and rehearsal_only cannot both be true")


@dataclass(frozen=True, slots=True)
class DualSimulationProof:
    """Two-provider pinned pre-submission simulation proof with gas and profit deltas."""

    block_number: int
    block_hash: str
    primary_source: str
    secondary_source: str
    primary_reverted: bool
    secondary_reverted: bool
    primary_early_aborted: bool
    secondary_early_aborted: bool
    primary_gas_used: int
    secondary_gas_used: int
    primary_profit_delta_raw: int
    secondary_profit_delta_raw: int
    l2_gas_fee_wei: int
    l1_data_fee_wei: int
    simulated_profit_value_wei: int

    def __post_init__(self) -> None:
        _require_plain_int(self.block_number, "block_number", low=0)
        object.__setattr__(self, "block_hash", normalize_hash32(self.block_hash, "block_hash"))
        if not isinstance(self.primary_source, str) or not isinstance(self.secondary_source, str):
            raise TypeError("simulation source names must be strings")
        for flag_name in (
            "primary_reverted",
            "secondary_reverted",
            "primary_early_aborted",
            "secondary_early_aborted",
        ):
            _require_plain_bool(getattr(self, flag_name), flag_name)
        _require_plain_int(self.primary_gas_used, "primary_gas_used", low=0)
        _require_plain_int(self.secondary_gas_used, "secondary_gas_used", low=0)
        _require_plain_int(self.primary_profit_delta_raw, "primary_profit_delta_raw")
        _require_plain_int(self.secondary_profit_delta_raw, "secondary_profit_delta_raw")
        _require_plain_int(self.l2_gas_fee_wei, "l2_gas_fee_wei", low=0)
        _require_plain_int(self.l1_data_fee_wei, "l1_data_fee_wei", low=0)
        _require_plain_int(self.simulated_profit_value_wei, "simulated_profit_value_wei")


@dataclass(frozen=True, slots=True)
class EnvelopeProposal:
    """Candidate transaction envelope submitted for independent policy admission."""

    chain_id: int
    account: str
    nonce: int
    target: str
    target_observed_code_hash: str
    value_wei: int
    calldata: bytes
    gas_limit: int
    max_fee_per_gas_wei: int
    max_priority_fee_wei: int
    profit_token: str
    min_net_profit_raw: int
    referenced_pools: tuple[str, ...]
    referenced_routers: tuple[str, ...] = ()
    referenced_reactor: str | None = None
    deadline_s: int = 0

    def __post_init__(self) -> None:
        _require_plain_int(self.chain_id, "chain_id")
        object.__setattr__(self, "account", normalize_address(self.account))
        _require_plain_int(self.nonce, "nonce")
        object.__setattr__(self, "target", normalize_address(self.target))
        object.__setattr__(
            self,
            "target_observed_code_hash",
            normalize_hash32(self.target_observed_code_hash, "target_observed_code_hash"),
        )
        _require_plain_int(self.value_wei, "value_wei")
        if not isinstance(self.calldata, bytes):
            raise TypeError("calldata must be bytes")
        _require_plain_int(self.gas_limit, "gas_limit")
        _require_plain_int(self.max_fee_per_gas_wei, "max_fee_per_gas_wei")
        _require_plain_int(self.max_priority_fee_wei, "max_priority_fee_wei")
        object.__setattr__(self, "profit_token", normalize_address(self.profit_token))
        _require_plain_int(self.min_net_profit_raw, "min_net_profit_raw")
        if isinstance(self.referenced_pools, str) or isinstance(self.referenced_routers, str):
            raise TypeError("referenced_pools and referenced_routers must be address tuples")
        object.__setattr__(
            self,
            "referenced_pools",
            tuple(normalize_address(pool) for pool in self.referenced_pools),
        )
        object.__setattr__(
            self,
            "referenced_routers",
            tuple(normalize_address(router) for router in self.referenced_routers),
        )
        if self.referenced_reactor is not None:
            object.__setattr__(
                self, "referenced_reactor", normalize_address(self.referenced_reactor)
            )
        _require_plain_int(self.deadline_s, "deadline_s")


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """Immutable admission decision committing exact signed envelope fields."""

    admitted: bool
    target_kind: TargetKind
    calldata_sha256: str
    proposal_digest: str
    reasons: tuple[str, ...]
    execution_eligible: bool
    chain_id: int
    account: str
    nonce: int
    target: str
    value_wei: int
    gas_limit: int
    max_fee_per_gas_wei: int
    max_priority_fee_wei: int
    profit_token: str
    min_net_profit_raw: int
    simulation_block_number: int
    simulation_block_hash: str

    def __post_init__(self) -> None:
        _require_plain_bool(self.admitted, "admitted")
        _require_plain_bool(self.execution_eligible, "execution_eligible")
        if self.target_kind not in ("atomic_executor", "intent_vault", "rejected"):
            raise ValueError("invalid target_kind")
        object.__setattr__(
            self, "calldata_sha256", _require_sha256_hex(self.calldata_sha256, "calldata_sha256")
        )
        object.__setattr__(
            self, "proposal_digest", _require_sha256_hex(self.proposal_digest, "proposal_digest")
        )
        if not isinstance(self.reasons, tuple) or any(
            not isinstance(reason, str) or not reason for reason in self.reasons
        ):
            raise ValueError("reasons must be a tuple of non-empty strings")
        for field_name in (
            "chain_id",
            "nonce",
            "value_wei",
            "gas_limit",
            "max_fee_per_gas_wei",
            "max_priority_fee_wei",
            "min_net_profit_raw",
            "simulation_block_number",
        ):
            _require_plain_int(getattr(self, field_name), field_name)
        object.__setattr__(self, "account", normalize_address(self.account))
        object.__setattr__(self, "target", normalize_address(self.target))
        object.__setattr__(self, "profit_token", normalize_address(self.profit_token))
        object.__setattr__(
            self,
            "simulation_block_hash",
            normalize_hash32(self.simulation_block_hash, "simulation_block_hash"),
        )
        if self.admitted:
            if self.target_kind == "rejected" or self.reasons:
                raise ValueError("admitted decision cannot be rejected or carry reasons")
            if self.chain_id <= 0 or self.nonce < 0 or self.value_wei != 0:
                raise ValueError("admitted decision chain/nonce/value invalid")
            if (
                self.gas_limit <= 0
                or self.max_fee_per_gas_wei <= 0
                or not 0 <= self.max_priority_fee_wei <= self.max_fee_per_gas_wei
            ):
                raise ValueError("admitted decision gas/fees invalid")
            if self.min_net_profit_raw <= 0 or self.simulation_block_number < 0:
                raise ValueError("admitted decision profit or block invalid")
            if int(self.simulation_block_hash, 16) == 0:
                raise ValueError("admitted decision requires nonzero simulation_block_hash")
        else:
            if self.target_kind != "rejected" or not self.reasons or self.execution_eligible:
                raise ValueError(
                    "rejected decision must set target_kind='rejected', non-empty reasons, "
                    "and execution_eligible=False"
                )


@dataclass(frozen=True, slots=True)
class DualReceiptObservation:
    """Normalized transaction receipt from one independent RPC provider."""

    source: str
    tx_hash: str
    block_number: int
    block_hash: str
    status: int
    gas_used: int
    effective_gas_price_wei: int
    l1_fee_wei: int
    logs: tuple[ReceiptLog, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source, str):
            raise TypeError("source must be a string")
        object.__setattr__(self, "tx_hash", normalize_hash32(self.tx_hash, "tx_hash"))
        _require_plain_int(self.block_number, "block_number", low=0)
        object.__setattr__(self, "block_hash", normalize_hash32(self.block_hash, "block_hash"))
        _require_plain_int(self.status, "status")
        _require_plain_int(self.gas_used, "gas_used", low=0)
        _require_plain_int(self.effective_gas_price_wei, "effective_gas_price_wei", low=0)
        _require_plain_int(self.l1_fee_wei, "l1_fee_wei", low=0)
        logs = tuple(self.logs)
        if any(not isinstance(item, ReceiptLog) for item in logs):
            raise TypeError("logs must contain ReceiptLog instances")
        object.__setattr__(self, "logs", logs)


@dataclass(frozen=True, slots=True)
class SettlementReconciliation:
    """Result of two-provider receipt, native spend, ERC-20 delta, and revert-budget checks."""

    reconciled: bool
    halt_latch: bool
    outcome: AttemptOutcome
    native_spent_wei: int
    native_expected_wei: int
    native_residual_wei: int
    erc20_profit_delta_raw: int
    revert_budget_status: RevertBudgetStatus
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_plain_bool(self.reconciled, "reconciled")
        _require_plain_bool(self.halt_latch, "halt_latch")
        if not isinstance(self.outcome, AttemptOutcome):
            raise TypeError("outcome must be an AttemptOutcome")
        _require_plain_int(self.native_spent_wei, "native_spent_wei")
        _require_plain_int(self.native_expected_wei, "native_expected_wei", low=0)
        _require_plain_int(self.native_residual_wei, "native_residual_wei")
        _require_plain_int(self.erc20_profit_delta_raw, "erc20_profit_delta_raw")
        if not isinstance(self.revert_budget_status, RevertBudgetStatus):
            raise TypeError("revert_budget_status must be a RevertBudgetStatus")
        if not isinstance(self.reasons, tuple) or any(
            not isinstance(reason, str) or not reason for reason in self.reasons
        ):
            raise ValueError("reasons must be a tuple of non-empty strings")
        if not self.reconciled and (not self.halt_latch or not self.reasons):
            raise ValueError("unreconciled settlement must latch halt with reasons")


def _validate_precheck_tuple(
    precheck: tuple[object, ...],
    *,
    allowed_pools: frozenset[str],
    referenced_pools: frozenset[str],
    label: str,
) -> list[str]:
    reasons: list[str] = []
    if len(precheck) != 3:
        return [f"malformed_{label}_precheck"]
    raw_pool, raw_min, raw_max = precheck
    if not isinstance(raw_pool, str) or type(raw_min) is not int or type(raw_max) is not int:
        return [f"malformed_{label}_precheck"]
    pool = normalize_address(raw_pool)
    if pool == ZERO_ADDRESS:
        if raw_min != 0 or raw_max != 0:
            reasons.append(f"invalid_disabled_{label}_bounds")
        return reasons
    if raw_min > raw_max or raw_max <= 0:
        reasons.append(f"invalid_{label}_bounds")
    if pool not in allowed_pools:
        reasons.append(f"calldata_{label}_pool_not_allowlisted")
    if pool not in referenced_pools:
        reasons.append(f"calldata_{label}_pool_missing_from_proposal")
    return reasons


def _validate_execute_cycle_calldata(
    policy: AtomicExecutionPolicy,
    proposal: EnvelopeProposal,
) -> list[str]:
    reasons: list[str] = []
    if len(proposal.calldata) < 4 or proposal.calldata[:4] != EXECUTE_CYCLE_SELECTOR:
        return ["invalid_executor_selector"]
    try:
        decoded_items = decode([EXECUTE_CYCLE_TYPE], proposal.calldata[4:])
    except (DecodingError, ValueError, TypeError, OverflowError):
        return ["malformed_execute_cycle_calldata"]
    if len(decoded_items) != 1:
        return ["malformed_execute_cycle_calldata"]
    decoded = decoded_items[0]
    try:
        reencoded = EXECUTE_CYCLE_SELECTOR + encode([EXECUTE_CYCLE_TYPE], [decoded])
    except (ValueError, TypeError, OverflowError):
        return ["malformed_execute_cycle_calldata"]
    if reencoded != proposal.calldata:
        return ["noncanonical_execute_cycle_calldata"]

    cycle_nonce, cycle_deadline, raw_profit_token, initial_amount, min_net_profit, guard, legs = (
        decoded
    )
    if type(cycle_nonce) is not int or not 1 <= cycle_nonce <= UINT64_MAX:
        reasons.append("invalid_cycle_nonce")
    if cycle_deadline != proposal.deadline_s:
        reasons.append("calldata_deadline_mismatch")
    profit_token = normalize_address(raw_profit_token)
    if profit_token != proposal.profit_token:
        reasons.append("calldata_profit_token_mismatch")
    if type(initial_amount) is not int or initial_amount <= 0:
        reasons.append("nonpositive_initial_amount")
    if min_net_profit != proposal.min_net_profit_raw:
        reasons.append("calldata_min_net_profit_mismatch")

    referenced_pools = frozenset(proposal.referenced_pools)
    reasons.extend(
        _validate_precheck_tuple(
            guard,
            allowed_pools=policy.allowed_pools,
            referenced_pools=referenced_pools,
            label="guard",
        )
    )
    if not 2 <= len(legs) <= 3:
        reasons.append("invalid_cycle_leg_count")
        return reasons

    leg_pairs: list[tuple[str, str]] = []
    for raw_pool, raw_in, raw_out, _, _, v2_fee_bps in legs:
        pool = normalize_address(raw_pool)
        token_in = normalize_address(raw_in)
        token_out = normalize_address(raw_out)
        if pool == ZERO_ADDRESS or token_in == ZERO_ADDRESS or token_out == ZERO_ADDRESS:
            reasons.append("zero_address_in_cycle_leg")
        if token_in == token_out:
            reasons.append("cycle_leg_same_token")
        if type(v2_fee_bps) is not int or not 0 <= v2_fee_bps < BPS_DENOMINATOR:
            reasons.append("invalid_cycle_v2_fee_bps")
        if pool not in policy.allowed_pools:
            reasons.append("calldata_pool_not_allowlisted")
        if pool not in referenced_pools:
            reasons.append("calldata_pool_missing_from_proposal")
        leg_pairs.append((token_in, token_out))

    contiguous = leg_pairs[0][0] == profit_token and leg_pairs[-1][1] == profit_token
    for idx in range(len(leg_pairs) - 1):
        if leg_pairs[idx][1] != leg_pairs[idx + 1][0]:
            contiguous = False
    if not contiguous:
        reasons.append("discontiguous_cycle_path")
    return reasons


def _validate_execute_reactor_fill_calldata(
    policy: AtomicExecutionPolicy,
    proposal: EnvelopeProposal,
) -> list[str]:
    reasons: list[str] = []
    if len(proposal.calldata) < 4 or proposal.calldata[:4] != EXECUTE_REACTOR_FILL_SELECTOR:
        return ["invalid_intent_vault_selector"]
    try:
        decoded_items = decode(list(EXECUTE_REACTOR_FILL_PARAM_TYPES), proposal.calldata[4:])
    except (DecodingError, ValueError, TypeError, OverflowError):
        return ["malformed_execute_reactor_fill_calldata"]
    if len(decoded_items) != 2:
        return ["malformed_execute_reactor_fill_calldata"]
    try:
        reencoded = EXECUTE_REACTOR_FILL_SELECTOR + encode(
            list(EXECUTE_REACTOR_FILL_PARAM_TYPES), list(decoded_items)
        )
    except (ValueError, TypeError, OverflowError):
        return ["malformed_execute_reactor_fill_calldata"]
    if reencoded != proposal.calldata:
        return ["noncanonical_execute_reactor_fill_calldata"]

    plan_tuple, reactor_calldata = decoded_items
    raw_reactor, raw_profit_token, min_net_profit, precheck, steps = plan_tuple
    reactor = normalize_address(raw_reactor)
    profit_token = normalize_address(raw_profit_token)

    if reactor == ZERO_ADDRESS or reactor not in policy.allowed_reactors:
        reasons.append("calldata_reactor_not_allowlisted")
    if proposal.referenced_reactor is not None and reactor != proposal.referenced_reactor:
        reasons.append("calldata_reactor_mismatch")
    if profit_token != proposal.profit_token:
        reasons.append("calldata_profit_token_mismatch")
    if min_net_profit != proposal.min_net_profit_raw:
        reasons.append("calldata_min_net_profit_mismatch")
    if (
        not isinstance(reactor_calldata, bytes)
        or len(reactor_calldata) < 4
        or reactor_calldata[:4] not in VALID_REACTOR_SELECTORS
    ):
        reasons.append("invalid_reactor_calldata_selector")

    referenced_pools = frozenset(proposal.referenced_pools)
    referenced_routers = frozenset(proposal.referenced_routers)
    reasons.extend(
        _validate_precheck_tuple(
            precheck,
            allowed_pools=policy.allowed_pools,
            referenced_pools=referenced_pools,
            label="precheck",
        )
    )
    if len(steps) > MAX_SWAP_STEPS:
        reasons.append("invalid_reactor_step_count")
        return reasons

    for raw_router, raw_pool, raw_in, raw_out, amount_in, min_out, step_data in steps:
        router = normalize_address(raw_router)
        pool = normalize_address(raw_pool)
        token_in = normalize_address(raw_in)
        token_out = normalize_address(raw_out)
        if ZERO_ADDRESS in (router, pool, token_in, token_out):
            reasons.append("zero_address_in_reactor_step")
        if (
            token_in == token_out
            or type(amount_in) is not int
            or amount_in <= 0
            or type(min_out) is not int
            or min_out <= 0
        ):
            reasons.append("invalid_reactor_swap_step")
        # `step_data` is ignored by the typed adapter call; retain the bound only as a sanity check.
        if not isinstance(step_data, bytes) or len(step_data) > MAX_STEP_CALLDATA_BYTES:
            reasons.append("invalid_reactor_step_calldata")
        elif len(step_data) >= 4:
            selector = step_data[:4]
            if selector == b"\x00\x00\x00\x00" or selector in FORBIDDEN_ROUTER_SELECTORS:
                reasons.append("forbidden_router_selector")
        if router not in policy.allowed_routers:
            reasons.append("calldata_router_not_allowlisted")
        if router not in referenced_routers:
            reasons.append("calldata_router_missing_from_proposal")
        if pool not in policy.allowed_pools:
            reasons.append("calldata_pool_not_allowlisted")
        if pool not in referenced_pools:
            reasons.append("calldata_pool_missing_from_proposal")

    return reasons


def _proposal_digest(
    *,
    policy: AtomicExecutionPolicy,
    proposal: EnvelopeProposal,
    simulation: DualSimulationProof,
    calldata_sha256: str,
    rehearsal_loopback_anvil: bool,
) -> str:
    payload = {
        "schema": "oasis.atomic-intent-admission.v1",
        "policy": {
            "chain_id": policy.chain_id,
            "operator_account": policy.operator_account,
            "executor_address": policy.executor_address,
            "executor_code_hash": policy.executor_code_hash,
            "intent_vault_address": policy.intent_vault_address,
            "intent_vault_code_hash": policy.intent_vault_code_hash,
            "max_capital_usd_micros": policy.max_capital_usd_micros,
            "max_daily_loss_usd_micros": policy.max_daily_loss_usd_micros,
            "max_cumulative_loss_usd_micros": policy.max_cumulative_loss_usd_micros,
            "max_gas_limit": policy.max_gas_limit,
            "max_fee_per_gas_wei": policy.max_fee_per_gas_wei,
            "max_priority_fee_wei": policy.max_priority_fee_wei,
            "max_deadline_horizon_s": policy.max_deadline_horizon_s,
            "release_gate_open": policy.release_gate_open,
        },
        "proposal": {
            "chain_id": proposal.chain_id,
            "account": proposal.account,
            "nonce": proposal.nonce,
            "target": proposal.target,
            "target_observed_code_hash": proposal.target_observed_code_hash,
            "value_wei": proposal.value_wei,
            "calldata_sha256": calldata_sha256,
            "gas_limit": proposal.gas_limit,
            "max_fee_per_gas_wei": proposal.max_fee_per_gas_wei,
            "max_priority_fee_wei": proposal.max_priority_fee_wei,
            "profit_token": proposal.profit_token,
            "min_net_profit_raw": proposal.min_net_profit_raw,
            "referenced_pools": list(proposal.referenced_pools),
            "referenced_routers": list(proposal.referenced_routers),
            "referenced_reactor": proposal.referenced_reactor,
            "deadline_s": proposal.deadline_s,
        },
        "simulation": asdict(simulation),
        "rehearsal_loopback_anvil": rehearsal_loopback_anvil,
    }
    return sha256_hex(canonical_json(payload))


def admit_atomic_or_intent_proposal(
    *,
    policy: AtomicExecutionPolicy,
    proposal: EnvelopeProposal,
    simulation: DualSimulationProof,
    now_s: int,
    daily_loss_usd_micros: int,
    cumulative_loss_usd_micros: int,
    revert_budget_status: RevertBudgetStatus,
    has_unresolved_pending: bool = False,
    rehearsal_loopback_anvil: bool = False,
) -> AdmissionDecision:
    """Fail-closed policy admission for `OasisAtomicExecutor` and `OasisIntentVault`."""
    if not isinstance(policy, AtomicExecutionPolicy):
        raise TypeError("policy must be an AtomicExecutionPolicy")
    if not isinstance(proposal, EnvelopeProposal):
        raise TypeError("proposal must be an EnvelopeProposal")
    if not isinstance(simulation, DualSimulationProof):
        raise TypeError("simulation must be a DualSimulationProof")
    _require_plain_int(now_s, "now_s", low=0)
    _require_plain_int(daily_loss_usd_micros, "daily_loss_usd_micros", low=0)
    _require_plain_int(cumulative_loss_usd_micros, "cumulative_loss_usd_micros", low=0)
    if not isinstance(revert_budget_status, RevertBudgetStatus):
        raise TypeError("revert_budget_status must be a RevertBudgetStatus")
    _require_plain_bool(has_unresolved_pending, "has_unresolved_pending")
    _require_plain_bool(rehearsal_loopback_anvil, "rehearsal_loopback_anvil")

    reasons: list[str] = []

    if not policy.release_gate_open and not rehearsal_loopback_anvil:
        reasons.append("release_gate_closed")
    if revert_budget_status is not RevertBudgetStatus.ACTIVE:
        reasons.append("revert_budget_not_active")
    if has_unresolved_pending:
        reasons.append("unresolved_pending_transaction")
    if daily_loss_usd_micros >= policy.max_daily_loss_usd_micros:
        reasons.append("daily_loss_limit_reached")
    if cumulative_loss_usd_micros >= policy.max_cumulative_loss_usd_micros:
        reasons.append("cumulative_loss_limit_reached")

    if proposal.chain_id != policy.chain_id or proposal.chain_id <= 0:
        reasons.append("chain_id_mismatch")
    if proposal.account != policy.operator_account:
        reasons.append("operator_account_mismatch")
    if not 0 <= proposal.nonce <= UINT64_MAX:
        reasons.append("invalid_nonce")
    if proposal.value_wei != 0:
        reasons.append("nonzero_transaction_value")

    if proposal.gas_limit <= 0 or proposal.gas_limit > policy.max_gas_limit:
        reasons.append("gas_limit_out_of_bounds")
    if (
        proposal.max_fee_per_gas_wei <= 0
        or proposal.max_fee_per_gas_wei > policy.max_fee_per_gas_wei
    ):
        reasons.append("max_fee_per_gas_exceeds_policy")
    if (
        proposal.max_priority_fee_wei < 0
        or proposal.max_priority_fee_wei > policy.max_priority_fee_wei
    ):
        reasons.append("max_priority_fee_exceeds_policy")
    if proposal.max_priority_fee_wei > proposal.max_fee_per_gas_wei:
        reasons.append("priority_fee_exceeds_max_fee")

    if proposal.profit_token not in policy.allowed_profit_tokens:
        reasons.append("profit_token_not_allowlisted")
    if proposal.min_net_profit_raw <= 0:
        reasons.append("nonpositive_min_net_profit")
    if proposal.deadline_s <= now_s or proposal.deadline_s > now_s + policy.max_deadline_horizon_s:
        reasons.append("deadline_out_of_horizon")

    if not proposal.referenced_pools:
        reasons.append("empty_referenced_pools")
    elif any(pool not in policy.allowed_pools for pool in proposal.referenced_pools):
        reasons.append("pool_not_allowlisted")
    if any(router not in policy.allowed_routers for router in proposal.referenced_routers):
        reasons.append("router_not_allowlisted")

    candidate_kind: TargetKind
    if proposal.target == policy.executor_address:
        candidate_kind = "atomic_executor"
        if proposal.target_observed_code_hash != policy.executor_code_hash:
            reasons.append("executor_code_hash_mismatch")
        if proposal.referenced_reactor is not None:
            reasons.append("unexpected_reactor_for_atomic_executor")
        reasons.extend(_validate_execute_cycle_calldata(policy, proposal))
    elif proposal.target == policy.intent_vault_address:
        candidate_kind = "intent_vault"
        if proposal.target_observed_code_hash != policy.intent_vault_code_hash:
            reasons.append("intent_vault_code_hash_mismatch")
        if (
            proposal.referenced_reactor is None
            or proposal.referenced_reactor not in policy.allowed_reactors
        ):
            reasons.append("reactor_not_allowlisted")
        reasons.extend(_validate_execute_reactor_fill_calldata(policy, proposal))
    else:
        candidate_kind = "rejected"
        reasons.append("unallowlisted_target")

    primary_src = simulation.primary_source.strip()
    secondary_src = simulation.secondary_source.strip()
    if not primary_src or not secondary_src or primary_src == secondary_src:
        reasons.append("simulation_sources_not_distinct")
    if int(simulation.block_hash, 16) == 0:
        reasons.append("zero_simulation_block_hash")
    if simulation.primary_reverted or simulation.secondary_reverted:
        reasons.append("simulation_reverted")
    if simulation.primary_early_aborted or simulation.secondary_early_aborted:
        reasons.append("simulation_early_aborted")
    if simulation.primary_profit_delta_raw != simulation.secondary_profit_delta_raw:
        reasons.append("simulation_profit_disagreement")
    elif simulation.primary_profit_delta_raw < proposal.min_net_profit_raw:
        reasons.append("simulation_profit_below_min_net_profit")
    if (
        simulation.simulated_profit_value_wei
        <= simulation.l2_gas_fee_wei + simulation.l1_data_fee_wei
    ):
        reasons.append("simulated_profit_below_l1_l2_gas_cost")

    unique_reasons = tuple(dict.fromkeys(reasons))
    admitted = len(unique_reasons) == 0
    target_kind: TargetKind = candidate_kind if admitted else "rejected"
    execution_eligible = bool(
        admitted and policy.release_gate_open and not rehearsal_loopback_anvil
    )
    calldata_sha256 = sha256_hex(proposal.calldata)
    proposal_digest = _proposal_digest(
        policy=policy,
        proposal=proposal,
        simulation=simulation,
        calldata_sha256=calldata_sha256,
        rehearsal_loopback_anvil=rehearsal_loopback_anvil,
    )

    return AdmissionDecision(
        admitted=admitted,
        target_kind=target_kind,
        calldata_sha256=calldata_sha256,
        proposal_digest=proposal_digest,
        reasons=unique_reasons,
        execution_eligible=execution_eligible,
        chain_id=proposal.chain_id,
        account=proposal.account,
        nonce=proposal.nonce,
        target=proposal.target,
        value_wei=proposal.value_wei,
        gas_limit=proposal.gas_limit,
        max_fee_per_gas_wei=proposal.max_fee_per_gas_wei,
        max_priority_fee_wei=proposal.max_priority_fee_wei,
        profit_token=proposal.profit_token,
        min_net_profit_raw=proposal.min_net_profit_raw,
        simulation_block_number=simulation.block_number,
        simulation_block_hash=simulation.block_hash,
    )


def verify_signed_bytes_match_admission(
    raw_signed_tx: bytes,
    decision: AdmissionDecision,
) -> SubmittedTransaction:
    """Decode signed EIP-1559 bytes and verify exact field/calldata match with admission."""
    if not isinstance(decision, AdmissionDecision):
        raise TypeError("decision must be an AdmissionDecision")
    if not isinstance(raw_signed_tx, bytes):
        raise TypeError("raw_signed_tx must be bytes")
    if not decision.admitted or decision.target_kind == "rejected":
        raise ValueError("cannot verify signed bytes for a rejected admission decision")

    decoded = decode_signed_transaction(raw_signed_tx)
    submitted = SubmittedTransaction.from_signed(raw_signed_tx)

    mismatches: list[str] = []
    if submitted.chain_id != decision.chain_id:
        mismatches.append("chain_id")
    if submitted.account != decision.account:
        mismatches.append("account")
    if submitted.nonce != decision.nonce:
        mismatches.append("nonce")
    if submitted.target != decision.target:
        mismatches.append("target")
    if submitted.value != decision.value_wei:
        mismatches.append("value")
    if sha256_hex(decoded.calldata) != decision.calldata_sha256:
        mismatches.append("calldata")
    if submitted.gas_limit != decision.gas_limit:
        mismatches.append("gas_limit")
    if submitted.max_fee_per_gas != decision.max_fee_per_gas_wei:
        mismatches.append("max_fee_per_gas")
    if submitted.max_priority_fee_per_gas != decision.max_priority_fee_wei:
        mismatches.append("max_priority_fee_per_gas")

    if mismatches:
        raise ValueError(
            "signed transaction bytes diverge from admitted envelope: " + ", ".join(mismatches)
        )
    return submitted


def _classify_receipt_for_decision(
    *,
    decision: AdmissionDecision,
    receipt: DualReceiptObservation,
    provider_disagreement: bool,
    tracker: RollingRevertBudgetTracker,
) -> AttemptOutcome:
    if decision.target_kind == "atomic_executor":
        logs = receipt.logs
    else:
        normalized: list[ReceiptLog] = []
        for entry in receipt.logs:
            if entry.address != decision.target:
                normalized.append(entry)
                continue
            topic0 = entry.topics[0]
            if topic0 == INTENT_VAULT_EARLY_ABORTED_TOPIC0 and len(entry.topics) in (1, 3):
                normalized.append(
                    ReceiptLog(
                        address=entry.address,
                        topics=(EARLY_ABORTED_TOPIC0, *entry.topics[1:]),
                        data=entry.data,
                    )
                )
            elif topic0 == REACTOR_FILL_EXECUTED_TOPIC0 and len(entry.topics) in (1, 4):
                normalized.append(
                    ReceiptLog(
                        address=entry.address,
                        topics=(CYCLE_EXECUTED_TOPIC0, *entry.topics[1:]),
                        data=entry.data,
                    )
                )
            elif topic0 in (EARLY_ABORTED_TOPIC0, CYCLE_EXECUTED_TOPIC0):
                # Ignore executor-specific event signatures when reconciling the intent vault.
                continue
            else:
                normalized.append(entry)
        logs = tuple(normalized)

    return classify_execution_receipt(
        ReceiptClassificationInput(
            executor_address=decision.target,
            status=receipt.status,
            gas_used=receipt.gas_used,
            logs=logs,
            provider_disagreement=provider_disagreement,
        ),
        tracker.policy,
    )


def reconcile_atomic_or_intent_receipt(
    *,
    decision: AdmissionDecision,
    primary_receipt: DualReceiptObservation,
    secondary_receipt: DualReceiptObservation,
    native_balance_before_wei: int,
    native_balance_after_wei: int,
    profit_token_balance_before_raw: int,
    profit_token_balance_after_raw: int,
    gas_cost_usd_micros: int,
    realized_profit_usd_micros: int,
    tracker: RollingRevertBudgetTracker,
    timestamp_s: int,
) -> SettlementReconciliation:
    """Reconcile dual receipts, native gas residual, ERC-20 delta, and revert budget."""
    if not isinstance(decision, AdmissionDecision):
        raise TypeError("decision must be an AdmissionDecision")
    if not decision.admitted or decision.target_kind == "rejected":
        raise ValueError("cannot reconcile receipt for a rejected admission decision")
    if not isinstance(primary_receipt, DualReceiptObservation) or not isinstance(
        secondary_receipt, DualReceiptObservation
    ):
        raise TypeError("receipts must be DualReceiptObservation instances")
    if not isinstance(tracker, RollingRevertBudgetTracker):
        raise TypeError("tracker must be a RollingRevertBudgetTracker")

    _require_plain_int(native_balance_before_wei, "native_balance_before_wei", low=0)
    _require_plain_int(native_balance_after_wei, "native_balance_after_wei", low=0)
    _require_plain_int(profit_token_balance_before_raw, "profit_token_balance_before_raw", low=0)
    _require_plain_int(profit_token_balance_after_raw, "profit_token_balance_after_raw", low=0)
    _require_plain_int(gas_cost_usd_micros, "gas_cost_usd_micros", low=0)
    _require_plain_int(realized_profit_usd_micros, "realized_profit_usd_micros", low=0)
    _require_plain_int(timestamp_s, "timestamp_s", low=0)

    mismatch_reasons: list[str] = []

    primary_src = primary_receipt.source.strip()
    secondary_src = secondary_receipt.source.strip()
    if not primary_src or not secondary_src or primary_src == secondary_src:
        mismatch_reasons.append("receipt_sources_not_distinct")

    receipts_identical = (
        primary_receipt.tx_hash == secondary_receipt.tx_hash
        and primary_receipt.block_number == secondary_receipt.block_number
        and primary_receipt.block_hash == secondary_receipt.block_hash
        and primary_receipt.status == secondary_receipt.status
        and primary_receipt.gas_used == secondary_receipt.gas_used
        and primary_receipt.effective_gas_price_wei == secondary_receipt.effective_gas_price_wei
        and primary_receipt.l1_fee_wei == secondary_receipt.l1_fee_wei
        and primary_receipt.logs == secondary_receipt.logs
    )
    if not receipts_identical:
        mismatch_reasons.append("receipt_provider_disagreement")
    if int(primary_receipt.tx_hash, 16) == 0 or int(primary_receipt.block_hash, 16) == 0:
        mismatch_reasons.append("zero_receipt_identity_hash")

    provider_disagreement = not receipts_identical or "receipt_sources_not_distinct" in (
        mismatch_reasons
    )
    classified_outcome = _classify_receipt_for_decision(
        decision=decision,
        receipt=primary_receipt,
        provider_disagreement=provider_disagreement,
        tracker=tracker,
    )
    if classified_outcome is AttemptOutcome.AMBIGUOUS_HALT and not mismatch_reasons:
        mismatch_reasons.append("ambiguous_receipt_classification")

    native_spent_wei = native_balance_before_wei - native_balance_after_wei
    native_expected_wei = (
        primary_receipt.gas_used * primary_receipt.effective_gas_price_wei
        + primary_receipt.l1_fee_wei
        + decision.value_wei
    )
    native_residual_wei = native_spent_wei - native_expected_wei
    if native_residual_wei != 0:
        mismatch_reasons.append("native_balance_residual_nonzero")

    erc20_profit_delta_raw = profit_token_balance_after_raw - profit_token_balance_before_raw
    if classified_outcome is AttemptOutcome.SUCCESS:
        if erc20_profit_delta_raw < decision.min_net_profit_raw:
            mismatch_reasons.append("erc20_profit_below_min_net_profit")
    elif classified_outcome in (
        AttemptOutcome.EARLY_ABORT_PROBE,
        AttemptOutcome.DEEP_EXECUTION_REVERT,
    ):
        if erc20_profit_delta_raw != 0:
            mismatch_reasons.append("unexpected_erc20_balance_change_on_nonfill")
        if realized_profit_usd_micros != 0:
            mismatch_reasons.append("nonzero_realized_profit_on_nonfill")

    reconciled = len(mismatch_reasons) == 0
    recorded_outcome = classified_outcome if reconciled else AttemptOutcome.AMBIGUOUS_HALT
    recorded_profit_usd_micros = (
        realized_profit_usd_micros if recorded_outcome is AttemptOutcome.SUCCESS else 0
    )

    snapshot = tracker.record(
        timestamp_s=timestamp_s,
        outcome=recorded_outcome,
        gas_cost_usd_micros=gas_cost_usd_micros,
        realized_profit_usd_micros=recorded_profit_usd_micros,
    )
    halt_latch = not reconciled or snapshot.status is RevertBudgetStatus.HALT
    all_reasons = tuple(dict.fromkeys((*mismatch_reasons, *snapshot.halt_reasons)))

    return SettlementReconciliation(
        reconciled=reconciled,
        halt_latch=halt_latch,
        outcome=recorded_outcome,
        native_spent_wei=native_spent_wei,
        native_expected_wei=native_expected_wei,
        native_residual_wei=native_residual_wei,
        erc20_profit_delta_raw=erc20_profit_delta_raw,
        revert_budget_status=snapshot.status,
        reasons=all_reasons,
    )


__all__ = [
    "CYCLE_EXECUTED_TOPIC0",
    "EARLY_ABORTED_TOPIC0",
    "EXECUTE_BATCH_WITH_CALLBACK_SELECTOR",
    "EXECUTE_CYCLE_SELECTOR",
    "EXECUTE_CYCLE_SIGNATURE",
    "EXECUTE_CYCLE_TYPE",
    "EXECUTE_REACTOR_FILL_PARAM_TYPES",
    "EXECUTE_REACTOR_FILL_PLAN_TYPE",
    "EXECUTE_REACTOR_FILL_SELECTOR",
    "EXECUTE_REACTOR_FILL_SIGNATURE",
    "EXECUTE_WITH_CALLBACK_SELECTOR",
    "FORBIDDEN_ROUTER_SELECTORS",
    "INTENT_VAULT_EARLY_ABORTED_EVENT_SIGNATURE",
    "INTENT_VAULT_EARLY_ABORTED_TOPIC0",
    "REACTOR_FILL_EXECUTED_EVENT_SIGNATURE",
    "REACTOR_FILL_EXECUTED_TOPIC0",
    "VALID_REACTOR_SELECTORS",
    "AdmissionDecision",
    "AtomicExecutionPolicy",
    "DualReceiptObservation",
    "DualSimulationProof",
    "EnvelopeProposal",
    "SettlementReconciliation",
    "admit_atomic_or_intent_proposal",
    "reconcile_atomic_or_intent_receipt",
    "verify_signed_bytes_match_admission",
]
