"""Complete per-position core claims reconciled against observed PoolManager cash.

Economic claims and physically netted cash are separate outputs. No arbitrary
pro-rata allocation of net transfers, third-party payments, or transaction gas.
Per-position owner cash is only emitted when the physical attribution is
unique: each currency must belong to exactly one pool in the batch and every
non-zero transaction participant must be the owner or the PoolManager.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import cast

from ..evidence.rpc_validation import decode_exchange
from ..models.common import ZERO_ADDRESS, canonical_json, normalize_address
from ..models.v4 import PoolKey
from ..uniswap_v4.events import MODIFY_LIQUIDITY_TOPIC, decode_modify_liquidity
from ..uniswap_v4.pool_id import pool_id_from_key
from .owner_payments import _abi, _log
from .position_ledger import PositionLedgerEntry
from .position_settlement import (
    MODIFY_SELECTOR,
    MODIFY_TYPES,
    PositionCoreSettlement,
    PositionSettlementCapture,
    _mapping,
    _text,
)
from .transaction_payments import TransactionPaymentCapture


class UnsupportedBatchAllocation(ValueError):
    """Batch data outside the unique physical per-position payment policy."""


def reconstruct_all_positions(
    core: PositionSettlementCapture,
) -> tuple[PositionSettlementCapture, ...]:
    core.reconstruct()
    trace = _mapping(decode_exchange(core.exchanges[3])[1].get("result"))
    keys: dict[str, PoolKey] = {}
    calls = 0
    stack = [trace]
    while stack:
        frame = stack.pop()
        stack.extend(_mapping(x) for x in cast(list[object], frame.get("calls", [])))
        data = bytes.fromhex(_text(frame["input"])[2:])
        if frame.get("to") != core.pool_manager or data[:4] != MODIFY_SELECTOR:
            continue
        key_raw, _, _ = _abi(MODIFY_TYPES, data[4:].hex())
        values = cast(tuple[str, str, int, int, str], key_raw)
        key = PoolKey(*values)
        keys[pool_id_from_key(key)] = key
        calls += 1
    receipt = _mapping(decode_exchange(core.exchanges[2])[1]["result"])
    logs = [
        _log(x)
        for x in cast(list[object], receipt["logs"])
        if _log(x).address == core.pool_manager and _log(x).topics[:1] == (MODIFY_LIQUIDITY_TOPIC,)
    ]
    if not logs or len(logs) != calls:
        raise ValueError("complete modification call/event coverage required")
    result = []
    for log in logs:
        obs = decode_modify_liquidity(log)
        if obs.sender != core.entry.observation.sender:
            raise ValueError("additional PositionManager needs separate runtime evidence")
        if log.transaction_index is None:
            raise ValueError("modification transaction index missing")
        entry = PositionLedgerEntry(
            core.header.ref, log.transaction_hash, log.transaction_index, log.log_index, obs
        )
        result.append(replace(core, pool_key=keys[obs.pool_id], entry=entry))
    return tuple(result)


def reconcile_batch_settlement(payments: TransactionPaymentCapture) -> bytes:
    captures = reconstruct_all_positions(payments.core)
    cash = payments.reconstruct()
    claims: dict[str, int] = {}
    positions = []
    for capture in captures:
        result = capture.reconstruct()
        delta = result.caller_delta
        if delta is None:
            raise ValueError("batch needs complete core return data")
        for token, amount in (
            (capture.pool_key.currency0, delta.amount0),
            (capture.pool_key.currency1, delta.amount1),
        ):
            claims[token] = claims.get(token, 0) + amount
        positions.append(
            {
                "pool_id": capture.entry.observation.pool_id,
                "token_id": result.token_id,
                "log_index": capture.entry.log_index,
                "currency0": capture.pool_key.currency0,
                "currency1": capture.pool_key.currency1,
                "economic_caller_delta": asdict(delta),
                "fees_accrued": asdict(result.fees_accrued)
                if result.fees_accrued is not None
                else None,
                "owner_cash_allocation": None,
            }
        )
    manager = payments.core.pool_manager
    actual = {x.token: x.net for x in cash if x.account == manager}
    if any(actual.get(token) != -amount for token, amount in claims.items()) or any(
        amount != 0 and token not in claims for token, amount in actual.items()
    ):
        raise ValueError("core position claims/PoolManager actual cash residual")
    return canonical_json(
        {
            "schema": "oasis.batch-core-cash.v1",
            "block": asdict(payments.core.header.ref),
            "transaction_hash": payments.core.entry.transaction_hash,
            "positions": positions,
            "actual_accounts": [asdict(x) for x in cash],
            "pool_manager_residual_by_token": {
                token: actual[token] + amount for token, amount in claims.items()
            },
            "core_cash_reconciled": True,
            "individual_owner_cash_allocation": "unresolved",
            "execution_eligible": False,
        }
    )


@dataclass(frozen=True, slots=True)
class BatchOwnerCash:
    """Physically unique owner cash attribution for one position in the batch."""

    pool_id: str
    token_id: int
    log_index: int
    currency0: str
    currency1: str
    owner_amount0: int
    owner_amount1: int
    fees_accrued0: int
    fees_accrued1: int


def resolve_batch_owner_cash(
    payments: TransactionPaymentCapture, owner: str
) -> tuple[BatchOwnerCash, ...]:
    """Attribute owner cash per position when and only when the physics are unique.

    Triple cross-validation is inherited from the independently reconstructing
    sources:

    - call traces: per-position ``modifyLiquidity`` return deltas (core),
    - transfer logs: receipt plus full-block Transfer coverage,
    - balance changes: per-account block balance deltas reconciled to those
      logs and to total supply (``TransactionPaymentCapture``).

    The batch fails closed with :class:`UnsupportedBatchAllocation` when any
    currency belongs to more than one pool in the batch (inter-position
    netting has no unique physical payment), when a non-zero participant is
    neither the owner nor the PoolManager (third-party payment/refund), or
    when a pool uses native currency (requires native payment attribution).
    """
    owner = normalize_address(owner)
    if owner == ZERO_ADDRESS:
        raise ValueError("owner cannot be zero")
    cashflows = payments.reconstruct()
    captures = reconstruct_all_positions(payments.core)
    manager = payments.core.pool_manager
    if any(
        currency == ZERO_ADDRESS
        for capture in captures
        for currency in (capture.pool_key.currency0, capture.pool_key.currency1)
    ):
        raise UnsupportedBatchAllocation("native currency requires native payment attribution")
    accounts = {x.account for x in cashflows}
    if owner not in accounts:
        raise UnsupportedBatchAllocation("owner did not participate in this transaction")
    third_party = {
        (x.token, x.account) for x in cashflows if x.account not in {owner, manager} and x.net != 0
    }
    if third_party:
        raise UnsupportedBatchAllocation("third-party netting in batch payment is unsupported")
    currency_positions: dict[str, int] = {}
    for capture in captures:
        for currency in (capture.pool_key.currency0, capture.pool_key.currency1):
            currency_positions[currency] = currency_positions.get(currency, 0) + 1
    claim: dict[str, int] = {}
    settlements: list[tuple[PositionSettlementCapture, PositionCoreSettlement]] = []
    for capture in captures:
        core = capture.reconstruct()
        if core.caller_delta is None:
            raise ValueError("batch needs complete core return data")
        for currency, amount in (
            (capture.pool_key.currency0, core.caller_delta.amount0),
            (capture.pool_key.currency1, core.caller_delta.amount1),
        ):
            claim[currency] = claim.get(currency, 0) + amount
        settlements.append((capture, core))
    owner_flows = {x.token: x.net for x in cashflows if x.account == owner}
    if claim.keys() != owner_flows.keys():
        raise ValueError("owner flow scope differs from position claim scope")
    for currency, amount in claim.items():
        if owner_flows[currency] != amount:
            raise ValueError("owner balance/core claim residual per token")
    result: list[BatchOwnerCash] = []
    for capture, settlement in settlements:
        for currency in (capture.pool_key.currency0, capture.pool_key.currency1):
            if currency_positions[currency] != 1:
                raise UnsupportedBatchAllocation(
                    "shared token pair between positions cannot be physically attributed"
                )
        if settlement.caller_delta is None:
            raise ValueError("batch needs complete core return data")
        caller_delta = settlement.caller_delta
        fees = settlement.fees_accrued
        result.append(
            BatchOwnerCash(
                capture.entry.observation.pool_id,
                settlement.token_id,
                capture.entry.log_index,
                capture.pool_key.currency0,
                capture.pool_key.currency1,
                caller_delta.amount0,
                caller_delta.amount1,
                fees.amount0 if fees is not None else 0,
                fees.amount1 if fees is not None else 0,
            )
        )
    return tuple(result)


def batch_owner_cash_payload(payments: TransactionPaymentCapture, owner: str) -> bytes:
    """Evidence payload for a uniquely attributed batch owner cash allocation."""
    allocations = resolve_batch_owner_cash(payments, owner)
    return canonical_json(
        {
            "schema": "oasis.batch-owner-cash.v1",
            "block": asdict(payments.core.header.ref),
            "transaction_hash": payments.core.entry.transaction_hash,
            "owner": normalize_address(owner),
            "positions": [asdict(x) for x in allocations],
            "basis": "core_delta_transfer_log_block_balance_triple_reconciliation",
            "physical_allocation": "unique_per_currency_per_pool",
            "execution_eligible": False,
            "unresolved": [
                "native_currency_and_gas_attribution",
                "independent_provider_finality",
                "token_runtime_attestation",
            ],
        }
    )
