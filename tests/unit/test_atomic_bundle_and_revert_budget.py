"""Unit tests for atomic bundle ABI encoding and rolling revert-budget risk controls."""

import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from eth_abi.abi import decode
from eth_hash.auto import keccak

from oasis_agent.execution.atomic_bundle import (
    EXECUTE_CYCLE_SELECTOR,
    EXECUTE_CYCLE_SIGNATURE,
    EXECUTE_CYCLE_TYPE,
    AtomicCycleIntent,
    PrecheckGuardSpec,
    SwapLegSpec,
    encode_execute_cycle_calldata,
)
from oasis_agent.models.common import ZERO_ADDRESS, sha256_hex
from oasis_agent.risk.revert_budget import (
    CYCLE_EXECUTED_TOPIC0,
    EARLY_ABORTED_TOPIC0,
    AttemptOutcome,
    ReceiptClassificationInput,
    ReceiptLog,
    RevertBudgetStatus,
    RollingRevertBudgetPolicy,
    RollingRevertBudgetTracker,
    classify_execution_receipt,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPILED_EXECUTOR_ABI = (
    REPO_ROOT / "contracts" / "out" / "OasisAtomicExecutor.sol" / "OasisAtomicExecutor.json"
)

EXECUTOR = "0x" + "11" * 20
EXECUTOR_CODE_HASH = "0x" + "aa" * 32
TOKEN_A = "0x" + "22" * 20
TOKEN_B = "0x" + "33" * 20
TOKEN_C = "0x" + "44" * 20
POOL_V3 = "0x" + "55" * 20
POOL_V2 = "0x" + "66" * 20
POOL_V3_ALT = "0x" + "77" * 20
OTHER_CONTRACT = "0x" + "88" * 20
UINT160_SQRT_1 = 1 << 96


def _canonical_param_type(param: dict[str, Any]) -> str:
    raw_type: str = param["type"]
    if not raw_type.startswith("tuple"):
        return raw_type
    suffix = raw_type[len("tuple") :]
    inner = ",".join(_canonical_param_type(c) for c in param["components"])
    return f"({inner}){suffix}"


def _sample_intent(*, three_hop: bool = False) -> AtomicCycleIntent:
    legs: tuple[SwapLegSpec, ...]
    if three_hop:
        legs = (
            SwapLegSpec(
                pool=POOL_V3,
                token_in=TOKEN_A,
                token_out=TOKEN_B,
                zero_for_one=True,
                sqrt_price_limit_x96=UINT160_SQRT_1 - 1,
                v2_fee_bps=0,
            ),
            SwapLegSpec(
                pool=POOL_V3_ALT,
                token_in=TOKEN_B,
                token_out=TOKEN_C,
                zero_for_one=False,
                sqrt_price_limit_x96=UINT160_SQRT_1 + 1,
                v2_fee_bps=0,
            ),
            SwapLegSpec(
                pool=POOL_V2,
                token_in=TOKEN_C,
                token_out=TOKEN_A,
                zero_for_one=True,
                sqrt_price_limit_x96=0,
                v2_fee_bps=30,
            ),
        )
    else:
        legs = (
            SwapLegSpec(
                pool=POOL_V3,
                token_in=TOKEN_A,
                token_out=TOKEN_B,
                zero_for_one=True,
                sqrt_price_limit_x96=UINT160_SQRT_1 - 1,
                v2_fee_bps=0,
            ),
            SwapLegSpec(
                pool=POOL_V2,
                token_in=TOKEN_B,
                token_out=TOKEN_A,
                zero_for_one=False,
                sqrt_price_limit_x96=0,
                v2_fee_bps=30,
            ),
        )
    return AtomicCycleIntent(
        chain_id=8453,
        executor_address=EXECUTOR,
        executor_code_hash=EXECUTOR_CODE_HASH,
        nonce=7,
        deadline=1_700_000_060,
        profit_token=TOKEN_A,
        initial_amount=10**19,
        min_net_profit=10**16,
        guard=PrecheckGuardSpec(
            pool=POOL_V3,
            min_sqrt_price_x96=UINT160_SQRT_1 - 100,
            max_sqrt_price_x96=UINT160_SQRT_1 + 100,
        ),
        legs=legs,
    )


def test_compiled_abi_matches_selector_and_event_topics() -> None:
    artifact = json.loads(COMPILED_EXECUTOR_ABI.read_text())
    abi = artifact["abi"]

    fn_entry = next(
        item for item in abi if item["type"] == "function" and item["name"] == "executeCycle"
    )
    compiled_sig = f"executeCycle({','.join(_canonical_param_type(p) for p in fn_entry['inputs'])})"
    assert compiled_sig == EXECUTE_CYCLE_SIGNATURE
    compiled_selector = keccak(compiled_sig.encode())[:4]
    assert compiled_selector == EXECUTE_CYCLE_SELECTOR
    assert artifact["methodIdentifiers"][compiled_sig] == EXECUTE_CYCLE_SELECTOR.hex()

    early_entry = next(
        item for item in abi if item["type"] == "event" and item["name"] == "EarlyAborted"
    )
    early_sig = f"EarlyAborted({','.join(_canonical_param_type(p) for p in early_entry['inputs'])})"
    assert "0x" + keccak(early_sig.encode()).hex() == EARLY_ABORTED_TOPIC0

    exec_entry = next(
        item for item in abi if item["type"] == "event" and item["name"] == "CycleExecuted"
    )
    exec_sig = f"CycleExecuted({','.join(_canonical_param_type(p) for p in exec_entry['inputs'])})"
    assert "0x" + keccak(exec_sig.encode()).hex() == CYCLE_EXECUTED_TOPIC0


def test_encode_execute_cycle_calldata_round_trip_and_hash() -> None:
    intent = _sample_intent(three_hop=True)
    raw, digest = encode_execute_cycle_calldata(intent)

    assert raw[:4] == EXECUTE_CYCLE_SELECTOR
    assert digest == sha256_hex(raw)
    assert len(digest) == 64

    (decoded,) = decode([EXECUTE_CYCLE_TYPE], raw[4:])
    nonce, deadline, profit_token, initial_amount, min_net_profit, guard, legs = decoded
    assert nonce == intent.nonce
    assert deadline == intent.deadline
    assert profit_token.lower() == intent.profit_token
    assert initial_amount == intent.initial_amount
    assert min_net_profit == intent.min_net_profit
    assert guard == (
        intent.guard.pool,
        intent.guard.min_sqrt_price_x96,
        intent.guard.max_sqrt_price_x96,
    )
    assert len(legs) == 3
    assert legs[2][0].lower() == POOL_V2
    assert legs[2][5] == 30


def test_atomic_bundle_specs_normalize_and_enforce_invariants() -> None:
    upper_executor = "0x" + "AB" * 20
    upper_token_a = "0x" + "CD" * 20
    upper_token_b = "0x" + "EF" * 20
    intent = AtomicCycleIntent(
        chain_id=8453,
        executor_address=upper_executor,
        executor_code_hash="0x" + "AB" * 32,
        nonce=1,
        deadline=100,
        profit_token=upper_token_a,
        initial_amount=1000,
        min_net_profit=0,
        guard=PrecheckGuardSpec(pool=ZERO_ADDRESS),
        legs=(
            SwapLegSpec(
                pool=POOL_V3, token_in=upper_token_a, token_out=upper_token_b, zero_for_one=True
            ),
            SwapLegSpec(
                pool=POOL_V2,
                token_in=upper_token_b,
                token_out=upper_token_a,
                zero_for_one=False,
                v2_fee_bps=30,
            ),
        ),
    )
    assert intent.executor_address == upper_executor.lower()
    assert intent.profit_token == upper_token_a.lower()
    assert intent.executor_code_hash == "0x" + "ab" * 32
    assert len(intent.intent_hash) == 64

    with pytest.raises(FrozenInstanceError):
        intent.nonce = 2  # type: ignore[misc]

    # Single leg or four legs rejected.
    with pytest.raises(ValueError, match="2 or 3 swap legs"):
        AtomicCycleIntent(
            chain_id=8453,
            executor_address=EXECUTOR,
            executor_code_hash=EXECUTOR_CODE_HASH,
            nonce=1,
            deadline=100,
            profit_token=TOKEN_A,
            initial_amount=100,
            min_net_profit=0,
            guard=PrecheckGuardSpec(pool=ZERO_ADDRESS),
            legs=(
                SwapLegSpec(pool=POOL_V3, token_in=TOKEN_A, token_out=TOKEN_B, zero_for_one=True),
            ),
        )

    # Discontiguous path rejected.
    with pytest.raises(ValueError, match="contiguous"):
        AtomicCycleIntent(
            chain_id=8453,
            executor_address=EXECUTOR,
            executor_code_hash=EXECUTOR_CODE_HASH,
            nonce=1,
            deadline=100,
            profit_token=TOKEN_A,
            initial_amount=100,
            min_net_profit=0,
            guard=PrecheckGuardSpec(pool=ZERO_ADDRESS),
            legs=(
                SwapLegSpec(pool=POOL_V3, token_in=TOKEN_A, token_out=TOKEN_B, zero_for_one=True),
                SwapLegSpec(pool=POOL_V2, token_in=TOKEN_C, token_out=TOKEN_A, zero_for_one=False),
            ),
        )

    # Non-positive initial_amount, negative min_net_profit, bad v2_fee_bps, zero code hash rejected.
    with pytest.raises(ValueError, match="initial_amount"):
        AtomicCycleIntent(
            chain_id=8453,
            executor_address=EXECUTOR,
            executor_code_hash=EXECUTOR_CODE_HASH,
            nonce=1,
            deadline=100,
            profit_token=TOKEN_A,
            initial_amount=0,
            min_net_profit=0,
            guard=PrecheckGuardSpec(pool=ZERO_ADDRESS),
            legs=_sample_intent().legs,
        )

    with pytest.raises(ValueError, match="min_net_profit"):
        AtomicCycleIntent(
            chain_id=8453,
            executor_address=EXECUTOR,
            executor_code_hash=EXECUTOR_CODE_HASH,
            nonce=1,
            deadline=100,
            profit_token=TOKEN_A,
            initial_amount=10,
            min_net_profit=-1,
            guard=PrecheckGuardSpec(pool=ZERO_ADDRESS),
            legs=_sample_intent().legs,
        )

    with pytest.raises(ValueError, match="v2_fee_bps"):
        SwapLegSpec(
            pool=POOL_V2,
            token_in=TOKEN_A,
            token_out=TOKEN_B,
            zero_for_one=True,
            v2_fee_bps=10_000,
        )

    with pytest.raises(ValueError, match="executor_code_hash"):
        AtomicCycleIntent(
            chain_id=8453,
            executor_address=EXECUTOR,
            executor_code_hash="0x" + "00" * 32,
            nonce=1,
            deadline=100,
            profit_token=TOKEN_A,
            initial_amount=10,
            min_net_profit=0,
            guard=PrecheckGuardSpec(pool=ZERO_ADDRESS),
            legs=_sample_intent().legs,
        )


def test_classify_execution_receipt_distinguishes_early_abort_reverts_and_ambiguity() -> None:
    policy = RollingRevertBudgetPolicy(
        max_hourly_gas_burn_usd_micros=5_000_000,
        max_deep_reverts_per_hour=3,
        max_early_abort_gas=65_000,
    )

    early_log = ReceiptLog(
        address=EXECUTOR,
        topics=(EARLY_ABORTED_TOPIC0, "0x" + "00" * 31 + "01", "0x" + "00" * 12 + "55" * 20),
    )
    probe_receipt = ReceiptClassificationInput(
        executor_address=EXECUTOR,
        status=1,
        gas_used=65_000,
        logs=(early_log,),
    )
    assert classify_execution_receipt(probe_receipt, policy) is AttemptOutcome.EARLY_ABORT_PROBE

    # Over-budget early abort does not qualify as cheap EARLY_ABORT_PROBE.
    oversized_probe = ReceiptClassificationInput(
        executor_address=EXECUTOR,
        status=1,
        gas_used=65_001,
        logs=(early_log,),
    )
    assert (
        classify_execution_receipt(oversized_probe, policy) is AttemptOutcome.DEEP_EXECUTION_REVERT
    )

    # EarlyAborted spoofed from an unrelated contract fails closed as AMBIGUOUS_HALT.
    spoofed_log = ReceiptLog(address=OTHER_CONTRACT, topics=early_log.topics)
    spoofed_receipt = ReceiptClassificationInput(
        executor_address=EXECUTOR,
        status=1,
        gas_used=55_000,
        logs=(spoofed_log,),
    )
    assert classify_execution_receipt(spoofed_receipt, policy) is AttemptOutcome.AMBIGUOUS_HALT

    # CycleExecuted -> SUCCESS.
    exec_log = ReceiptLog(
        address=EXECUTOR,
        topics=(
            CYCLE_EXECUTED_TOPIC0,
            "0x" + "00" * 31 + "01",
            "0x" + "00" * 12 + "22" * 20,
            "0x" + "00" * 12 + "11" * 20,
        ),
    )
    success_receipt = ReceiptClassificationInput(
        executor_address=EXECUTOR,
        status=1,
        gas_used=190_000,
        logs=(exec_log,),
    )
    assert classify_execution_receipt(success_receipt, policy) is AttemptOutcome.SUCCESS

    # Reverted tx (status == 0) -> DEEP_EXECUTION_REVERT.
    reverted = ReceiptClassificationInput(executor_address=EXECUTOR, status=0, gas_used=120_000)
    assert classify_execution_receipt(reverted, policy) is AttemptOutcome.DEEP_EXECUTION_REVERT

    # Missing / disagreeing / empty status-1 receipts -> AMBIGUOUS_HALT.
    missing = ReceiptClassificationInput(
        executor_address=EXECUTOR, status=None, gas_used=None, receipt_missing=True
    )
    disagree = ReceiptClassificationInput(
        executor_address=EXECUTOR, status=1, gas_used=100_000, provider_disagreement=True
    )
    no_logs = ReceiptClassificationInput(executor_address=EXECUTOR, status=1, gas_used=50_000)
    assert classify_execution_receipt(missing, policy) is AttemptOutcome.AMBIGUOUS_HALT
    assert classify_execution_receipt(disagree, policy) is AttemptOutcome.AMBIGUOUS_HALT
    assert classify_execution_receipt(no_logs, policy) is AttemptOutcome.AMBIGUOUS_HALT


def test_rolling_budget_tolerates_50_early_aborts_under_budget_and_slides_window() -> None:
    policy = RollingRevertBudgetPolicy(
        max_hourly_gas_burn_usd_micros=5_000_000,
        max_deep_reverts_per_hour=2,
        max_early_abort_gas=65_000,
        window_seconds=3_600,
    )
    tracker = RollingRevertBudgetTracker(policy)
    early_log = ReceiptLog(
        address=EXECUTOR,
        topics=(EARLY_ABORTED_TOPIC0, "0x" + "00" * 31 + "01", "0x" + "00" * 12 + "55" * 20),
    )

    # 50 early aborts at $0.05 each = $2.50 total (< $5.00 hourly cap) stay ACTIVE.
    snap = None
    for idx in range(50):
        snap = tracker.record_receipt(
            timestamp_s=10_000 + idx * 10,
            receipt=ReceiptClassificationInput(
                executor_address=EXECUTOR,
                status=1,
                gas_used=55_000,
                logs=(early_log,),
            ),
            gas_cost_usd_micros=50_000,
        )
    assert snap is not None
    assert snap.status is RevertBudgetStatus.ACTIVE
    assert snap.is_active
    assert snap.early_abort_count == 50
    assert snap.deep_revert_count == 0
    assert snap.net_gas_burn_usd_micros == 2_500_000

    # Profitable execution offsets earlier gas burn so another 50 early aborts remain ACTIVE.
    snap = tracker.record(
        timestamp_s=10_600,
        outcome=AttemptOutcome.SUCCESS,
        gas_cost_usd_micros=100_000,
        realized_profit_usd_micros=4_000_000,
    )
    assert snap.status is RevertBudgetStatus.ACTIVE
    assert snap.net_gas_burn_usd_micros == 0

    for idx in range(50):
        snap = tracker.record(
            timestamp_s=10_610 + idx * 10,
            outcome=AttemptOutcome.EARLY_ABORT_PROBE,
            gas_cost_usd_micros=50_000,
        )
    assert snap.status is RevertBudgetStatus.ACTIVE
    assert snap.early_abort_count == 100
    assert snap.gross_gas_spend_usd_micros == 5_100_000
    assert snap.net_gas_burn_usd_micros == 1_100_000

    # Advance 3,600s past all prior events -> sliding window clears and stays ACTIVE.
    later = tracker.snapshot(now_s=15_000)
    assert later.status is RevertBudgetStatus.ACTIVE
    assert later.window_attempt_count == 0
    assert later.net_gas_burn_usd_micros == 0


def test_rolling_budget_halts_on_gas_burn_deep_reverts_and_ambiguous_halt() -> None:
    # (a) Hourly net gas burn > max_hourly_gas_burn_usd_micros -> sticky HALT.
    gas_tracker = RollingRevertBudgetTracker(
        RollingRevertBudgetPolicy(
            max_hourly_gas_burn_usd_micros=1_000_000, max_deep_reverts_per_hour=5
        )
    )
    assert (
        gas_tracker.record(
            timestamp_s=1_000,
            outcome=AttemptOutcome.EARLY_ABORT_PROBE,
            gas_cost_usd_micros=1_000_000,
        ).status
        is RevertBudgetStatus.ACTIVE
    )
    breach = gas_tracker.record(
        timestamp_s=1_005,
        outcome=AttemptOutcome.EARLY_ABORT_PROBE,
        gas_cost_usd_micros=1,
    )
    assert breach.status is RevertBudgetStatus.HALT
    assert not breach.is_active
    assert "net gas burn" in breach.halt_reasons[0]
    # Sticky even after sliding window expires.
    assert gas_tracker.snapshot(now_s=10_000).status is RevertBudgetStatus.HALT

    # (b) Deep execution revert count > max_deep_reverts_per_hour -> sticky HALT.
    deep_tracker = RollingRevertBudgetTracker(
        RollingRevertBudgetPolicy(
            max_hourly_gas_burn_usd_micros=5_000_000, max_deep_reverts_per_hour=2
        )
    )
    assert (
        deep_tracker.record(
            timestamp_s=2_000,
            outcome=AttemptOutcome.DEEP_EXECUTION_REVERT,
            gas_cost_usd_micros=100_000,
        ).status
        is RevertBudgetStatus.ACTIVE
    )
    assert (
        deep_tracker.record(
            timestamp_s=2_010,
            outcome=AttemptOutcome.DEEP_EXECUTION_REVERT,
            gas_cost_usd_micros=100_000,
        ).status
        is RevertBudgetStatus.ACTIVE
    )
    deep_halt = deep_tracker.record(
        timestamp_s=2_020, outcome=AttemptOutcome.DEEP_EXECUTION_REVERT, gas_cost_usd_micros=100_000
    )
    assert deep_halt.status is RevertBudgetStatus.HALT
    assert "deep execution reverts" in deep_halt.halt_reasons[0]

    # (c) Any AMBIGUOUS_HALT immediately freezes fail-closed.
    amb_tracker = RollingRevertBudgetTracker(
        RollingRevertBudgetPolicy(
            max_hourly_gas_burn_usd_micros=5_000_000, max_deep_reverts_per_hour=5
        )
    )
    amb_halt = amb_tracker.record(
        timestamp_s=3_000, outcome=AttemptOutcome.AMBIGUOUS_HALT, gas_cost_usd_micros=0
    )
    assert amb_halt.status is RevertBudgetStatus.HALT
    assert "ambiguous execution outcome" in amb_halt.halt_reasons[0]
    assert amb_tracker.snapshot(now_s=10_000).status is RevertBudgetStatus.HALT

    # Explicit operator reconciliation clears HALT cleanly.
    cleared = amb_tracker.clear_halt(operator_reason="reconciled pending receipt", now_s=10_001)
    assert cleared.status is RevertBudgetStatus.ACTIVE
    assert cleared.halt_reasons == ()
