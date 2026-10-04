"""Join marked position returns to complete direct-account gas without double charging.

All account outer-transaction gas is debited once as an explicit comparison, not
allocated to an LP. Internal cash, reimbursements, other costs and net PnL remain
unresolved. Both components are rebuilt from their raw retained inputs.
"""

from dataclasses import dataclass, field

from ..evidence.ingestion import replay_block_range_evidence
from ..evidence.rpc_validation import strict_integer
from ..evidence.store import EvidenceStore
from ..models.common import ZERO_ADDRESS, EvidenceRef, normalize_address
from .account_gas_coverage import AccountGasReplay, AccountGasRequest, replay_account_gas
from .position_hold_return import (
    PositionHoldRequest,
    PositionHoldReturn,
    replay_position_hold_return,
)
from .position_return_replay import (
    PositionReturnReplay,
    PositionReturnRequest,
    replay_position_return,
)


@dataclass(frozen=True, slots=True)
class PositionAccountGasRequest:
    position: PositionReturnRequest | PositionHoldRequest
    account_gas: AccountGasRequest


@dataclass(frozen=True, slots=True)
class PositionAccountGasReplay:
    position: PositionReturnReplay | PositionHoldReturn
    account_gas: AccountGasReplay
    marked_position_before_gas_usd_micros: int
    overlapping_action_transactions: tuple[str, ...]
    overlapping_action_gas_usd_micros: int
    other_account_gas_usd_micros: int
    marked_position_less_whole_account_gas_usd_micros: int
    evidence: tuple[EvidenceRef, ...]
    scope: str = field(default="position_mark_less_whole_direct_account_gas_once", init=False)
    position_expense_allocation: str = field(default="unresolved", init=False)
    net_pnl: None = field(default=None, init=False)
    out_of_sample_accepted: bool = field(default=False, init=False)
    canary_accepted: bool = field(default=False, init=False)
    execution_eligible: bool = field(default=False, init=False)


def replay_position_account_gas(
    store: EvidenceStore, request: PositionAccountGasRequest
) -> PositionAccountGasReplay:
    position, gas_request = request.position, request.account_gas
    if not isinstance(position, (PositionReturnRequest, PositionHoldRequest)):
        raise TypeError("typed action or hold request required")
    cash = position.cash_window
    providers = tuple(ref.source for ref in cash.opening)
    opening = cash.opening[0].block
    closing = cash.closing[0].block
    if (
        gas_request.opening.ref != opening
        or gas_request.closing.ref != closing
        or gas_request.providers != providers
        or len(providers) != 2
        or len(set(providers)) != 2
    ):
        raise ValueError("account gas must cover the position's exact window and provider pair")
    if (
        isinstance(position, PositionReturnRequest)
        and gas_request.native_policy != position.native_policy
    ):
        raise ValueError("account and action native/USD policies differ")
    if (
        position.currency0_policy.base_token == ZERO_ADDRESS
        and position.currency0_policy != gas_request.native_policy
    ):
        raise ValueError("native inventory and account gas must share one USD policy")
    # Rebuild both complete components. No caller-supplied result totals are accepted.
    gas = replay_account_gas(store, gas_request)
    result = (
        replay_position_return(store, position)
        if isinstance(position, PositionReturnRequest)
        else replay_position_hold_return(store, position)
    )
    if gas.account != result.owner or gas.account != normalize_address(gas_request.account):
        raise ValueError("position owner differs from the direct account gas payer")
    canonical = replay_block_range_evidence(store, cash.core[0]).blocks
    if (
        canonical[0] != gas.opening
        or canonical[-1] != gas.closing
        or tuple(b.plan.header for b in gas_request.blocks) != canonical[1:]
    ):
        raise ValueError("complete account and position header ranges differ")
    by_hash = {row["transaction_hash"]: row for row in gas.transactions}
    blocks = {b.plan.header.ref: b for b in gas_request.blocks}
    overlap = []
    overlap_usd = 0
    if isinstance(position, PositionReturnRequest) and isinstance(result, PositionReturnReplay):
        proofs = {p.transaction_hash: p for p in position.costs}
        prices = {p.block: p for p in position.prices}
        for action in result.quoted_action_costs.actions:
            row = by_hash.get(action.transaction_hash)
            if row is None or row["status"] != 1 or row["total_native"] != action.native_units:
                raise ValueError(
                    "each successful LP action must occur in the complete account gas set"
                )
            block = blocks[action.block]
            # Identical retained cost references bind full receipts, not just amount/hash.
            if proofs[action.transaction_hash].evidence not in block.costs:
                raise ValueError("overlapping action/account cost wire references differ")
            point = prices[action.block]
            native_prices = (
                point.native_prices if point.native_prices is not None else point.currency0
            )
            if native_prices != block.native_prices:
                raise ValueError("overlapping action/account native price references differ")
            overlap.append(action.transaction_hash)
            overlap_usd += strict_integer(row["usd_micros"], "replayed account USD gas")
        if overlap_usd != result.action_gas_usd_micros:
            raise ValueError("overlapping action/account USD gas totals differ")
        gross = result.marked_pnl_before_action_gas_usd_micros
    elif isinstance(result, PositionHoldReturn):
        gross = result.marked_lp_pnl_usd_micros
    else:
        raise TypeError("position replay type differs")
    return PositionAccountGasReplay(
        result,
        gas,
        gross,
        tuple(overlap),
        overlap_usd,
        gas.total_usd_micros - overlap_usd,
        gross - gas.total_usd_micros,
        tuple(dict.fromkeys((*result.evidence, *gas.evidence))),
    )
