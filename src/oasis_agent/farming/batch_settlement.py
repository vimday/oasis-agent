"""Complete per-position core claims reconciled against observed PoolManager cash.

Economic claims and physically netted cash are separate outputs. No arbitrary
pro-rata allocation of net transfers, third-party payments, or transaction gas.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import cast

from ..evidence.rpc_validation import decode_exchange
from ..models.common import canonical_json
from ..models.v4 import PoolKey
from ..uniswap_v4.events import MODIFY_LIQUIDITY_TOPIC, decode_modify_liquidity
from ..uniswap_v4.pool_id import pool_id_from_key
from .owner_payments import _abi, _log
from .position_ledger import PositionLedgerEntry
from .position_settlement import (
    MODIFY_SELECTOR,
    MODIFY_TYPES,
    PositionSettlementCapture,
    _mapping,
    _text,
)
from .transaction_payments import TransactionPaymentCapture


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
