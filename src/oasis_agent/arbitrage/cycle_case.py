"""After-the-fact receipt decoding. Its outputs never feed the quote engine."""

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

from ..evidence.rpc_validation import rpc_quantity, strict_list, strict_object
from ..models.chain import BlockSnapshot, LogRecord
from ..models.common import ZERO_ADDRESS
from ..uniswap_v4.events import SWAP_TOPIC, decode_swap
from ..uniswap_v4.liquidity_math import MAX_SQRT_PRICE, MAX_TICK, MIN_SQRT_PRICE, MIN_TICK
from .cycle_evidence import Export, Reads, Unavailable, header, log_record
from .cycle_plan import Case, Plan, Pool, address, hash32
from .cycle_protocols import (
    UINT256,
    abi,
    data_bytes,
    topic,
    v4_key,
    verify_asset,
    verify_code,
    verify_pool,
)
from .cycle_research import Route, routes

V3_SWAP = topic("Swap(address,address,int256,int256,uint160,uint128,int24)")
SOLIDLY_SWAP = topic("Swap(address,uint256,uint256,uint256,uint256,address)")
SOLIDLY_SYNC = topic("Sync(uint256,uint256)")
SOLIDLY_FEES = topic("Fees(address,uint256,uint256)")
TRANSFER = topic("Transfer(address,address,uint256)")
WITHDRAWAL = topic("Withdrawal(address,uint256)")
TOKEN_SWAP = topic(
    "swapExactTokensForTokens(uint256,uint256,(address,address,bool)[],address,uint256)"
)[:10]
NATIVE_SWAP = topic("swapExactETHForTokens(uint256,(address,address,bool)[],address,uint256)")[:10]
ROUTE_ABI = "(address,address,bool)[]"


@dataclass(frozen=True, slots=True)
class Included:
    block: BlockSnapshot
    tx: dict[str, object]
    receipt: dict[str, object]
    logs: tuple[LogRecord, ...]
    prefix_types: tuple[int, ...]
    evidence: tuple[str, ...]


def _quantity(value: object) -> int:
    result = rpc_quantity(value)
    if result >= UINT256:
        raise ValueError("receipt quantity overflow")
    return result


def _inclusion(
    reads: Reads,
    chain_id: int,
    number: int,
    index: int,
    tx_hash: str,
    *,
    last: bool,
    expected_hash: str | None,
) -> Included:
    reads.chain(chain_id)
    raw, block_refs = reads.get("eth_getBlockByNumber", hex(number), False)
    block = header(raw, chain_id, number)
    if expected_hash is not None and block.ref.block_hash != expected_hash:
        raise Unavailable("planned_block_hash_mismatch")
    reads.bind(block.ref)
    raw_full, full_refs = reads.get("eth_getBlockByNumber", hex(number), True)
    if header(raw_full, chain_id, number) != block:
        raise Unavailable("full_block_header_mismatch")
    full = strict_object(raw_full, "full block")
    transactions = strict_list(full.get("transactions"), "transactions")
    if index >= len(transactions) or (last and index != len(transactions) - 1):
        raise Unavailable("unexpected_case_transaction_position")
    tx = strict_object(transactions[index], "transaction")
    raw_receipt, receipt_refs = reads.get("eth_getTransactionReceipt", tx_hash)
    receipt = strict_object(raw_receipt, "receipt")
    for obj, hash_field in ((tx, "hash"), (receipt, "transactionHash")):
        if (
            hash32(obj.get(hash_field)) != tx_hash
            or hash32(obj.get("blockHash")) != block.ref.block_hash
            or _quantity(obj.get("blockNumber")) != number
            or _quantity(obj.get("transactionIndex")) != index
        ):
            raise Unavailable("case_transaction_or_receipt_identity_mismatch")
    if (
        address(tx.get("from")) != address(receipt.get("from"))
        or address(tx.get("to")) != address(receipt.get("to"))
        or ("chainId" in tx and _quantity(tx["chainId"]) != chain_id)
    ):
        raise Unavailable("case_transaction_roles_or_chain_mismatch")
    if _quantity(receipt.get("status")) != 1:
        raise Unavailable("case_receipt_unsuccessful")
    raw_logs = strict_list(receipt.get("logs"), "logs")
    logs = tuple(log_record(log, block.ref, tx_hash, index) for log in raw_logs)
    indices = [log.log_index for log in logs]
    if indices != sorted(set(indices)):
        raise Unavailable("case_duplicate_or_unordered_logs")
    prefix_types: tuple[int, ...] = ()
    if not last:
        prefix = [strict_object(item, "prefix transaction") for item in transactions[:index]]
        seen = {tx_hash}
        for position, item in enumerate(prefix):
            item_hash = hash32(item.get("hash"))
            if (
                item_hash in seen
                or _quantity(item.get("transactionIndex")) != position
                or _quantity(item.get("blockNumber")) != number
                or hash32(item.get("blockHash")) != block.ref.block_hash
            ):
                raise Unavailable("case_prefix_transaction_identity_mismatch")
            seen.add(item_hash)
        prefix_types = tuple(_quantity(item.get("type")) for item in prefix)
        if any(tx_type > 255 for tx_type in prefix_types):
            raise ValueError("case prefix transaction type overflow")
    return Included(block, tx, receipt, logs, prefix_types, block_refs + full_refs + receipt_refs)


def _event(
    inclusion: Included,
    target: str,
    signature: str,
    pool_id: str | None = None,
) -> LogRecord:
    matches = [
        log
        for log in inclusion.logs
        if log.address == target
        and log.topics[:1] == (signature,)
        and (pool_id is None or log.topics[1:2] == (pool_id,))
    ]
    if len(matches) != 1:
        raise Unavailable("case_required_event_missing_or_ambiguous")
    return matches[0]


def _indexed(log: LogRecord, count: int) -> tuple[str, ...]:
    if len(log.topics) != count + 1:
        raise ValueError("unexpected ABI topic count")
    return tuple(abi(("address",), data_bytes(word))[0] for word in log.topics[1:])


def _signed_swap(
    amounts: tuple[int, int],
    zero_for_one: bool,
    *,
    trader_delta: bool,
) -> tuple[int, int]:
    delta_in, delta_out = amounts if zero_for_one else amounts[::-1]
    amount_in, amount_out = (-delta_in, delta_out) if trader_delta else (delta_in, -delta_out)
    if min(amount_in, amount_out) <= 0:
        raise Unavailable("case_signed_swap_direction_mismatch")
    return amount_in, amount_out


def _solidly_swap(
    inclusion: Included,
    pool: Pool,
    token_in: str,
) -> tuple[int, int, LogRecord, str, str]:
    log = _event(inclusion, pool.address, SOLIDLY_SWAP)
    sender, recipient = _indexed(log, 2)
    in0, in1, out0, out1 = abi(("uint256",) * 4, data_bytes(log.data))
    in_raw, out_raw, wrong_in, wrong_out = (
        (in0, out1, in1, out0) if token_in == pool.token0 else (in1, out0, in0, out1)
    )
    if min(in_raw, out_raw) <= 0 or wrong_in or wrong_out:
        raise Unavailable("case_solidly_swap_direction_mismatch")
    return in_raw, out_raw, log, sender, recipient


def _sync(inclusion: Included, pool: Pool) -> dict[str, Any] | None:
    if not any(
        log.address == pool.address and log.topics[:1] == (SOLIDLY_SYNC,) for log in inclusion.logs
    ):
        return None
    log = _event(inclusion, pool.address, SOLIDLY_SYNC)
    _indexed(log, 0)
    reserve0, reserve1 = abi(("uint256", "uint256"), data_bytes(log.data))
    return {"reserve0_raw": reserve0, "reserve1_raw": reserve1, "log_index": log.log_index}


def _trigger(
    case: Case,
    route: Route,
    inclusion: Included,
    plan: Plan,
) -> dict[str, Any]:
    pool = route.pools[2]
    token_out = route.currencies[2]
    if address(inclusion.tx["to"]) != case.router:
        raise Unavailable("case_trigger_router_mismatch")
    raw = data_bytes(inclusion.tx.get("input"))
    selector = "0x" + raw[:4].hex()
    value = _quantity(inclusion.tx.get("value"))
    if selector == TOKEN_SWAP:
        specified, minimum, path, receiver, deadline = abi(
            ("uint256", "uint256", ROUTE_ABI, "address", "uint256"),
            raw[4:],
        )
        if value:
            raise Unavailable("case_unexpected_trigger_native_value")
    elif selector == NATIVE_SWAP:
        minimum, path, receiver, deadline = abi(
            ("uint256", ROUTE_ABI, "address", "uint256"),
            raw[4:],
        )
        specified = value
    else:
        raise Unavailable("case_trigger_router_abi_unsupported")
    if path != ((plan.wrapped_native, token_out, False),):
        raise Unavailable("case_trigger_router_path_mismatch")
    amount, output, log, sender, recipient = _solidly_swap(inclusion, pool, plan.wrapped_native)
    if (
        specified != amount
        or output < minimum
        or recipient != receiver
        or sender != case.router
        or deadline < (inclusion.block.ref.timestamp or 0)
    ):
        raise Unavailable("case_trigger_calldata_and_swap_mismatch")
    try:
        fees = _fee_transfers(inclusion, pool, plan.wrapped_native, sender, log.log_index)
        fee_raw = sum(fee.raw for fee in fees)
        if fee_raw >= amount or any(fee.recipient == receiver for fee in fees):
            raise Unavailable("case_trigger_fee_target_or_amount_invalid")
        expected = Counter(
            {
                (plan.wrapped_native, pool.address): amount - fee_raw,
                (token_out, pool.address): -output,
                (token_out, receiver): output,
            }
        )
        for fee in fees:
            expected[plan.wrapped_native, fee.recipient] += fee.raw
        actual: Counter[tuple[str, str]] = Counter()
        for transfer in inclusion.logs:
            if transfer.topics[0] != TRANSFER or transfer.address not in (
                plan.wrapped_native,
                token_out,
            ):
                continue
            source, destination = _indexed(transfer, 2)
            if pool.address not in (source, destination):
                continue
            if (
                transfer.address == plan.wrapped_native
                and destination == pool.address
                and source
                not in (
                    case.router,
                    address(inclusion.tx["from"]),
                )
            ):
                raise Unavailable("case_trigger_unknown_input_source")
            (transferred,) = abi(("uint256",), data_bytes(transfer.data))
            if source == pool.address or transfer.address == token_out:
                actual[transfer.address, source] -= transferred
            actual[transfer.address, destination] += transferred
        actual.subtract(expected)
        transfers_reconciled = not any(actual.values())
        flow_issue = None if transfers_reconciled else "case_trigger_transfer_residual"
    except Unavailable as exc:
        transfers_reconciled, flow_issue, fees = False, exc.reason, ()
    return {
        "router": case.router,
        "router_selector": selector,
        "sender": address(inclusion.tx["from"]),
        "recipient": recipient,
        "input_asset": plan.wrapped_native,
        "input_raw": amount,
        "output_asset": token_out,
        "output_raw": output,
        "amount_out_min_raw": minimum,
        "min_out_discount_bps_floor": (output - minimum) * 10_000 // output,
        "swap_log_index": log.log_index,
        "sync_event": _sync(inclusion, pool),
        "transaction_native_value_raw": value,
        "erc20_transfers_reconciled": transfers_reconciled,
        "erc20_flow_issue": flow_issue,
        "fee_transfers": [asdict(fee) for fee in fees],
    }


@dataclass(frozen=True, slots=True)
class FeeTransfer:
    recipient: str
    raw: int
    log_index: int


def _fee_transfers(
    inclusion: Included,
    pool: Pool,
    token_in: str,
    sender: str,
    swap_index: int,
) -> tuple[FeeTransfer, ...]:
    matches = [
        (position, log)
        for position, log in enumerate(inclusion.logs)
        if log.address == pool.address and log.topics[:1] == (SOLIDLY_FEES,)
    ]
    if not matches:
        return ()
    if len(matches) != 1:
        raise Unavailable("case_solidly_fee_event_ambiguous")
    position, log = matches[0]
    (payer,) = _indexed(log, 1)
    fee0, fee1 = abi(("uint256", "uint256"), data_bytes(log.data))
    total, wrong_side = (fee0, fee1) if token_in == pool.token0 else (fee1, fee0)
    if payer != sender or wrong_side or log.log_index >= swap_index:
        raise Unavailable("case_solidly_fee_event_mismatch")
    fees: list[FeeTransfer] = []
    for transfer in reversed(inclusion.logs[:position]):
        if transfer.address != token_in or transfer.topics[:1] != (TRANSFER,):
            break
        source, recipient = _indexed(transfer, 2)
        if source != pool.address:
            break
        (raw,) = abi(("uint256",), data_bytes(transfer.data))
        if not raw or recipient in (ZERO_ADDRESS, pool.address, payer) or len(fees) >= 8:
            raise Unavailable("case_solidly_fee_transfer_invalid")
        fees.append(FeeTransfer(recipient, raw, transfer.log_index))
    if sum(fee.raw for fee in fees) != total:
        raise Unavailable("case_solidly_fee_transfers_do_not_match_event")
    return tuple(reversed(fees))


def _wrapper_exit(
    inclusion: Included,
    wrapper: str,
    executor: str,
    expected: int,
    swap_index: int,
) -> tuple[LogRecord, str]:
    withdrawals = []
    burns = []
    for log in inclusion.logs:
        if log.address != wrapper:
            continue
        if log.topics[:1] == (WITHDRAWAL,):
            (withdrawer,) = _indexed(log, 1)
            if withdrawer != executor:
                raise Unavailable("case_wrapper_withdrawer_mismatch")
            withdrawals.append(log)
        elif log.topics[:1] == (TRANSFER,) and _indexed(log, 2) == (executor, ZERO_ADDRESS):
            burns.append(log)
    for group in (withdrawals, burns):
        if len(group) > 1 or any(
            log.log_index <= swap_index or abi(("uint256",), data_bytes(log.data))[0] != expected
            for log in group
        ):
            raise Unavailable("case_wrapper_amount_or_order_mismatch")
    if withdrawals:
        return withdrawals[0], "withdrawal_event"
    if burns:
        return burns[0], "wrapped_burn_only"
    raise Unavailable("case_wrapper_exit_missing")


def _erc20_residuals(
    inclusion: Included,
    route: Route,
    executor: str,
    amounts: tuple[int, int, int],
    fees: tuple[FeeTransfer, ...],
) -> list[dict[str, object]]:
    amount1, amount2, amount3 = amounts
    first, second, third = (pool.address for pool in route.pools)
    _, token_a, token_b, wrapped = route.currencies
    fee_raw = sum(fee.raw for fee in fees)
    expected: Counter[tuple[str, str]] = Counter(
        {
            (token_a, first): -amount1,
            (token_a, second): amount1,
            (token_b, second): -amount2,
            (token_b, third): amount2 - fee_raw,
            (wrapped, third): -amount3,
            (wrapped, executor): amount3,
        }
    )
    for fee in fees:
        expected[token_b, fee.recipient] += fee.raw
    allowed = {
        (token_a, first, executor),
        (token_a, executor, second),
        (token_a, first, second),
        (token_b, second, executor),
        (token_b, executor, third),
        (token_b, second, third),
        (wrapped, third, executor),
    }
    allowed.update((token_b, third, fee.recipient) for fee in fees)
    transfers: Counter[tuple[str, str]] = Counter()
    for log in inclusion.logs:
        if log.topics[:1] != (TRANSFER,):
            continue
        if log.address not in route.currencies[1:]:
            if len(log.topics) > 2 and any(
                word[-40:] in {executor[2:], address(inclusion.tx["from"])[2:]}
                for word in log.topics[1:3]
            ):
                raise Unavailable("case_nonroute_asset_flow")
            continue
        source, destination = _indexed(log, 2)
        (amount,) = abi(("uint256",), data_bytes(log.data))
        if (log.address, source, destination) == (wrapped, executor, ZERO_ADDRESS):
            continue  # Validated independently by _wrapper_exit.
        if (log.address, source, destination) not in allowed:
            raise Unavailable("case_unattributed_erc20_transfer")
        transfers[log.address, source] -= amount
        transfers[log.address, destination] += amount
    transfers.subtract(expected)
    return [
        {"token": token, "address": account, "unexplained_raw": delta}
        for (token, account), delta in sorted(transfers.items())
        if delta
    ]


def _winner(case: Case, route: Route, inclusion: Included) -> dict[str, Any]:
    if address(inclusion.tx["from"]) != case.sender or address(inclusion.tx["to"]) != case.executor:
        raise Unavailable("case_winner_role_mismatch")
    if case.prefix_system_type is not None and inclusion.prefix_types[:1] != (
        case.prefix_system_type,
    ):
        raise Unavailable("case_winner_system_prefix_mismatch")
    first, second, third = route.pools
    native, token_a, token_b, wrapped = route.currencies
    v4_log = _event(inclusion, first.address, SWAP_TOPIC, first.pool_id)
    v4 = decode_swap(v4_log)
    if v4.sender != case.executor:
        raise Unavailable("case_v4_caller_mismatch")
    principal, out1 = _signed_swap((v4.amount0, v4.amount1), True, trader_delta=True)
    v3_log = _event(inclusion, second.address, V3_SWAP)
    v3_sender, v3_recipient = _indexed(v3_log, 2)
    a0, a1, sqrt_price, _, tick = abi(
        ("int256", "int256", "uint160", "uint128", "int24"),
        data_bytes(v3_log.data),
    )
    assert v4.sqrt_price_x96 is not None and v4.fee is not None
    if (
        not MIN_SQRT_PRICE <= sqrt_price < MAX_SQRT_PRICE
        or not MIN_TICK <= tick <= MAX_TICK
        or not MIN_SQRT_PRICE <= v4.sqrt_price_x96 < MAX_SQRT_PRICE
        or not MIN_TICK <= v4.tick <= MAX_TICK
        or v4.fee > 1_000_000
    ):
        raise Unavailable("case_concentrated_swap_state_invalid")
    in2, out2 = _signed_swap((a0, a1), token_a == second.token0, trader_delta=False)
    in3, out3, solid_log, solid_sender, solid_recipient = _solidly_swap(inclusion, third, token_b)
    exit_log, exit_kind = _wrapper_exit(
        inclusion, wrapped, case.executor, out3, solid_log.log_index
    )
    withdrawn = out3 if exit_kind == "withdrawal_event" else None
    if (
        not v4_log.log_index < v3_log.log_index < solid_log.log_index
        or (out1, out2) != (in2, in3)
        or solid_sender != case.executor
        or solid_recipient != case.executor
        or v3_sender != case.executor
        or v3_recipient not in (case.executor, third.address)
    ):
        raise Unavailable("case_winner_route_or_unwrap_discontinuity")
    gas_used = _quantity(inclusion.receipt.get("gasUsed"))
    gas_price = _quantity(inclusion.receipt.get("effectiveGasPrice"))
    gas = gas_used * gas_price
    if gas_used == 0 or gas >= UINT256:
        raise Unavailable("case_receipt_gas_invalid")
    value = _quantity(inclusion.tx.get("value"))
    flow_issue = None
    try:
        fees = _fee_transfers(inclusion, third, token_b, solid_sender, solid_log.log_index)
        if sum(fee.raw for fee in fees) >= in3 or any(
            fee.recipient == case.executor for fee in fees
        ):
            raise Unavailable("case_solidly_fee_exceeds_input")
        residuals = _erc20_residuals(inclusion, route, case.executor, (out1, out2, out3), fees)
    except Unavailable as exc:
        fees, residuals, flow_issue = (), None, exc.reason
    legs = [
        {
            "protocol": protocol,
            "asset_in": src,
            "asset_out": dst,
            "input_raw": amount_in,
            "output_raw": amount_out,
            "log_index": log.log_index,
        }
        for protocol, src, dst, amount_in, amount_out, log in (
            ("v4", native, token_a, principal, out1, v4_log),
            ("v3", token_a, token_b, in2, out2, v3_log),
            ("solidly_volatile", token_b, wrapped, in3, out3, solid_log),
            (
                "unwrap" if withdrawn is not None else "wrapped_burn",
                wrapped,
                native if withdrawn is not None else None,
                out3,
                withdrawn,
                exit_log,
            ),
        )
    ]
    return {
        "sender": case.sender,
        "executor": case.executor,
        "legs": legs,
        "transaction_native_value_raw": value,
        "principal_native_raw": principal,
        "principal_exceeding_tx_value_raw": max(0, principal - value),
        "route_output_native_raw": withdrawn,
        "wrapped_output_raw": out3,
        "wrapper_exit_evidence": exit_kind,
        "gas_payer": case.sender,
        "receipt_gas_used": gas_used,
        "receipt_effective_gas_price_raw": gas_price,
        "receipt_gas_product_native_raw": gas,
        "additional_receipt_fee_fields_uninterpreted": {
            field: _quantity(inclusion.receipt[field])
            for field in ("gasUsedForL1", "l1Fee", "l1GasUsed", "l1GasPrice")
            if field in inclusion.receipt
        },
        "route_output_less_principal_and_receipt_gas_raw": (
            withdrawn - principal - gas if withdrawn is not None else None
        ),
        "receipt_conditional_net_at_parity_raw": out3 - principal - gas,
        "fully_reconciled_net_raw": None,
        "inventory_valuation_change_raw": None,
        "external_funding_and_internal_transfers_raw": None,
        "builder_payment_raw": None,
        "additional_protocol_or_chain_fee_raw": None,
        "window_failed_attempt_cost_raw": None,
        "solidly_input_fee": (
            {
                "token": token_b,
                "raw": sum(fee.raw for fee in fees),
                "recipient": fees[0].recipient if len(fees) == 1 else None,
                "transfers": [asdict(fee) for fee in fees],
                "already_in_swap_amounts": True,
            }
            if fees
            else None
        ),
        "erc20_residuals": residuals,
        "erc20_flow_issue": flow_issue,
        "sync_event": _sync(inclusion, third),
        "prefix_transaction_types": inclusion.prefix_types,
        "prestate_replayed": False,
    }


def _identities(
    reads: Reads,
    plan: Plan,
    case: Case,
    route: Route,
    *,
    trigger: bool,
) -> dict[str, dict[str, object]]:
    checks: dict[str, dict[str, object]] = {}
    relevant_currencies = route.currencies[2:] if trigger else route.currencies
    for asset in plan.assets:
        if asset.currency in relevant_currencies:
            try:
                checks[asset.currency] = {"evidence": verify_asset(reads, asset)}
            except (ValueError, TypeError) as exc:
                checks[asset.currency] = {
                    "reason": exc.reason
                    if isinstance(exc, Unavailable)
                    else "invalid_asset_identity",
                }
    for pool in route.pools[2:] if trigger else route.pools:
        try:
            evidence = verify_pool(reads, pool)
            if pool.protocol == "v4" and v4_key(pool).hooks != ZERO_ADDRESS:
                raise Unavailable("unsupported_v4_hook")
            checks[f"pool:{pool.name}"] = {"evidence": evidence}
        except (ValueError, TypeError) as exc:
            checks[f"pool:{pool.name}"] = {
                "reason": exc.reason if isinstance(exc, Unavailable) else "invalid_pool_identity",
            }
    role, target, code_hash = (
        ("router", case.router, case.router_hash)
        if trigger
        else ("executor", case.executor, case.executor_hash)
    )
    try:
        checks[role] = {"evidence": verify_code(reads, target, code_hash)}
    except (ValueError, TypeError) as exc:
        checks[role] = {
            "reason": exc.reason
            if isinstance(exc, Unavailable)
            else "invalid_case_target_identity",
        }
    return checks


def review_case(plan: Plan, records: tuple[Export, ...]) -> dict[str, Any] | None:
    case = plan.case
    if case is None:
        return None
    result: dict[str, Any] = {
        "status": "unresolved",
        "settlement_status": "unresolved",
        "first_observed_utc": None,
        "exact_winner_prestate": None,
        "atomic_simulation": "unavailable",
        "execution_eligible": False,
        "missing": [
            "complete_native_trace_and_transaction_balance_deltas",
            "full_chain_fee_and_builder_attribution",
            "window_accounting",
            "historical_first_observation",
            "winner_prefix_execution_environment",
        ],
        "trigger": None,
        "winner": None,
        "block_boundary_consistent": None,
    }
    route = next((item for item in routes(plan) if item.names == case.route), None)
    if route is None:
        result["missing"].append("case_route_not_closed")
        return result
    blocks = []
    for trigger in (True, False):
        role = "trigger" if trigger else "winner"
        tx_hash = case.trigger_hash if trigger else case.winner_hash
        number = case.trigger_block if trigger else case.winner_block
        index = case.trigger_index if trigger else case.winner_index
        reads = Reads(records, plan.sources, plan.max_queries_per_window)
        entry: dict[str, Any] = {
            "hash": tx_hash,
            "requested_block": number,
            "transaction_index": index,
            "queries": reads.queries,
            "receipt_path_status": "unresolved",
        }
        try:
            if len({p.address for p in route.pools} | {plan.wrapped_native}) != 4:
                raise Unavailable("case_repeated_or_shared_target")
            expected_hash = next(
                (window.expected_hash for window in plan.windows if window.block == number),
                None,
            )
            included = _inclusion(
                reads,
                plan.chain_id,
                number,
                index,
                tx_hash,
                last=trigger,
                expected_hash=expected_hash,
            )
            entry.update(block=asdict(included.block), evidence=included.evidence)
            blocks.append(included.block)
            decoded = (
                _trigger(case, route, included, plan) if trigger else _winner(case, route, included)
            )
            identities = _identities(reads, plan, case, route, trigger=trigger)
            entry.update(decoded=decoded, identity_checks=identities)
            entry["receipt_path_status"] = "abi_decoded"
            flows_reconciled = (
                decoded["erc20_transfers_reconciled"]
                if trigger
                else decoded["erc20_residuals"] == []
            )
            entry["receipt_flows_reconciled"] = flows_reconciled
            if (
                flows_reconciled
                and all("reason" not in value for value in identities.values())
                and (trigger or decoded["wrapper_exit_evidence"] == "withdrawal_event")
            ):
                entry["receipt_path_status"] = "identity_bound_receipt_path"
        except (ValueError, TypeError) as exc:
            entry["reason"] = (
                exc.reason if isinstance(exc, Unavailable) else "invalid_case_evidence"
            )
        result[role] = entry
    if len(blocks) == 2:
        left, right = blocks
        consistent = (
            right.ref.number == left.ref.number + 1
            and right.parent_hash == left.ref.block_hash
            and (right.ref.timestamp or 0) >= (left.ref.timestamp or 0)
        )
        result["block_boundary_consistent"] = consistent
        if consistent and all(
            result[role].get("receipt_flows_reconciled") for role in ("trigger", "winner")
        ):
            result["status"] = (
                "receipt_path_reconciled"
                if all(
                    result[role]["receipt_path_status"] == "identity_bound_receipt_path"
                    for role in ("trigger", "winner")
                )
                else "receipt_token_flows_reconciled"
            )
    return result
