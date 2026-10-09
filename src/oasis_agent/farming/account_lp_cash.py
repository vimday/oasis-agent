"""Raw ERC-20 wallet/owned-position cash joins; no native-purpose inference.

The first supported interval is one block, with stable NFT ownership and complete
exclusive direct-owner settlement for every declared position. Every nonzero LP
currency Transfer must match a retained receipt individually, never just by net.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import TypeAdapter

from ..evidence.exact_capture import require_code_hash
from ..evidence.ingestion import replay_block_range_evidence
from ..evidence.rpc_validation import (
    decode_exchange,
    restore_exchanges,
    strict_json_object,
    strict_list,
    strict_object,
    strict_string,
)
from ..evidence.store import EvidenceStore
from ..models.common import EvidenceRef, canonical_json
from ..risk.account_risk import AccountFrame
from ..risk.portfolio_manifest import ChainInventory
from .account_flows import TRANSFER_TOPIC, TokenFlow, TransferLog, _log_from_result
from .account_gas_coverage import AccountGasRequest, replay_account_gas, replay_account_gas_block
from .cash_window import CashWindowRequest, replay_cash_window
from .position_settlement import replay_position_settlement_evidence
from .transaction_costs import replay_transaction_cost


@dataclass(frozen=True, slots=True)
class AccountLpCashEvidence:
    asset_id: str
    window: CashWindowRequest

    def __post_init__(self) -> None:
        if not isinstance(self.asset_id, str) or not self.asset_id.strip():
            raise ValueError("LP cash requires a position asset identifier")
        if not isinstance(self.window, CashWindowRequest):
            raise TypeError("LP cash requires a typed raw window request")


def replay_account_lp_cash(
    store: EvidenceStore,
    *,
    entry: ChainInventory,
    previous: AccountFrame,
    closing_units: Mapping[str, int],
    closing_snapshots: Mapping[str, tuple[EvidenceRef, EvidenceRef]],
    requests: tuple[AccountLpCashEvidence, ...],
    flows: Mapping[str, TokenFlow],
    gas_request: AccountGasRequest,
) -> frozenset[TransferLog]:
    """Return only raw-verified internal transfers; refuse all partial joins.

    Gas proves the owner sent each action and binds its exact full receipt and
    input. It is still a whole-account expense, not an LP allocation. Token and
    protocol runtime pins are checked at the action block. This function writes
    nothing and grants no source/token admission, finality or release authority.
    """
    positions = {p.asset_id: p for p in entry.positions}
    if (
        not requests
        or len(requests) != len(positions)
        or {r.asset_id for r in requests} != set(positions)
        or set(closing_snapshots) != set(positions)
        or previous.key != entry.key
        or gas_request.account != entry.account
        or gas_request.providers != (entry.primary_source, entry.secondary_source)
        or gas_request.closing.ref.number != previous.block_number + 1
        or gas_request.opening.ref.number != previous.block_number
        or gas_request.opening.ref.block_hash != previous.block_hash
        or gas_request.opening.ref.timestamp != previous.timestamp
        or gas_request.opening.ref.chain_id != entry.chain_id
    ):
        raise ValueError("LP cash requires complete single-block account/position scope")
    for position in entry.positions:
        if any(entry.asset(c).is_native for c in (position.currency0_id, position.currency1_id)):
            raise ValueError("native LP cash requires whole-window internal cash coverage")
    gas = replay_account_gas(store, gas_request)
    block = gas_request.blocks[0]
    facts = replay_account_gas_block(store, block.blocks[0], block.plan)
    transactions = {
        strict_string(t["hash"], "transaction hash"): t
        for item in strict_list(facts["account_transactions"], "account transactions")
        for t in (strict_object(item, "transaction"),)
    }
    # Every cost has already passed complete-account replay; keep the exact receipt
    # for each provider so a separately consistent but different LP receipt refuses.
    receipts: dict[tuple[str, str], bytes] = {}
    for pair in block.costs:
        for ref in pair:
            record = store.get_record(ref)
            if record.request_payload is None:
                raise ValueError("LP cash/gas raw requests missing")
            xs = restore_exchanges(record.request_payload, record.raw_response_payload)
            receipt = strict_object(decode_exchange(xs[2])[1]["result"], "gas receipt")
            digest = strict_string(receipt["transactionHash"], "transaction hash")
            capture = replay_transaction_cost(
                store, ref, header=gas.closing, transaction_hash=digest
            )
            receipts[(digest, ref.source)] = canonical_json(
                decode_exchange(capture.exchanges[2])[1]["result"]
            )
    accepted: set[TransferLog] = set()
    seen_transactions: set[str] = set()
    previous_assets = {a.asset_id: a for a in previous.assets}
    for request in requests:
        position = positions[request.asset_id]
        window = request.window
        if (
            window.pool_manager != position.pool_manager
            or window.closing != closing_snapshots[request.asset_id]
            or tuple(ref.source for ref in window.opening) != gas_request.providers
            or any(ref.block != gas.opening.ref for ref in window.opening)
            or any(ref.block != gas.closing.ref for ref in window.closing)
            or window.opening[0].content_hash != previous_assets[position.asset_id].evidence_hash
        ):
            raise ValueError("LP cash boundary differs from counted account inventory")
        result = replay_cash_window(store, window)
        ledger = result.ledger
        currencies = (entry.asset(position.currency0_id), entry.asset(position.currency1_id))
        for snapshot in (ledger.opening, ledger.closing):
            if (
                snapshot.nft_owner != entry.account
                or snapshot.position_manager != position.position_manager
                or snapshot.token_id != position.token_id
                or snapshot.pool_id != position.pool_id
                or (snapshot.pool_key.currency0, snapshot.pool_key.currency1)
                != tuple(c.address for c in currencies)
            ):
                raise ValueError("LP cash identity differs from owned manifest position")
        if ledger.ownership_replay is None or ledger.ownership_replay.transfers:
            raise ValueError("LP cash bridge requires unchanged ownership throughout")
        for ref in window.core:
            if replay_block_range_evidence(store, ref).blocks != (gas.opening, gas.closing):
                raise ValueError("LP cash and complete gas header lineage differ")
        for payment in result.payments:
            if tuple(t.decimals for t in (payment.amounts.token0, payment.amounts.token1)) != tuple(
                c.decimals for c in currencies
            ):
                raise ValueError("LP cash token decimals differ from manifest inventory")
            digest = payment.entry.transaction_hash
            if digest in seen_transactions or payment.owner != entry.account:
                raise ValueError("LP cash action reused or owner differs")
            seen_transactions.add(digest)
            tx = transactions.get(digest)
            if tx is None:
                raise ValueError("LP cash action missing from complete owner gas coverage")
            peer_logs: list[tuple[TransferLog, ...]] = []
            for ref in payment.evidence:
                record = store.get_record(ref)
                payload = strict_json_object(record.payload)
                core_ref = TypeAdapter(EvidenceRef).validate_python(payload["core_evidence"])
                core = replay_position_settlement_evidence(
                    store,
                    core_ref,
                    header=gas.closing,
                    pool_manager=position.pool_manager,
                    pool_key=ledger.opening.pool_key,
                    entry=payment.entry,
                )
                reconstructed = core.reconstruct()
                if (
                    reconstructed.pool_manager_code_hash != position.pool_manager_runtime_keccak
                    or reconstructed.position_manager_code_hash
                    != position.position_manager_runtime_keccak
                ):
                    raise ValueError("LP cash protocol runtime differs from manifest pin")
                raw_receipt = strict_object(
                    decode_exchange(core.exchanges[2])[1]["result"], "LP receipt"
                )
                if canonical_json(raw_receipt) != receipts.get((digest, ref.source)):
                    raise ValueError("LP cash and gas full receipts differ")
                root = strict_object(decode_exchange(core.exchanges[3])[1]["result"], "trace")
                if root.get("input") != tx["input"] or tx["value"] != 0:
                    raise ValueError("LP cash trace differs from enumerated transaction")
                if record.request_payload is None:
                    raise ValueError("LP cash raw requests missing")
                xs = restore_exchanges(record.request_payload, record.raw_response_payload)
                for i, currency in enumerate(currencies):
                    if currency.runtime_keccak is None:
                        raise ValueError("ERC-20 runtime pin required")
                    for offset in (3 + 5 * i, 4 + 5 * i):
                        require_code_hash(
                            decode_exchange(xs[offset])[1]["result"], currency.runtime_keccak
                        )
                    for offset, units in (
                        (6 + 5 * i, previous_assets[currency.asset_id].units),
                        (7 + 5 * i, closing_units[currency.asset_id]),
                    ):
                        if decode_exchange(xs[offset])[1]["result"] != f"0x{units:064x}":
                            raise ValueError("LP cash absolute balance differs from inventory")
                logs = []
                for raw_value in strict_list(raw_receipt["logs"], "receipt logs"):
                    raw = strict_object(raw_value, "receipt log")
                    for currency in currencies:
                        if raw["address"] == currency.address and strict_list(
                            raw.get("topics"), "topics"
                        )[:1] == [TRANSFER_TOPIC]:
                            log = _log_from_result(raw, token=currency.address)
                            if entry.account in (log.from_address, log.to_address):
                                logs.append(log)
                peer_logs.append(tuple(logs))
            if peer_logs[0] != peer_logs[1]:
                raise ValueError("LP cash providers disagree on individual owner transfers")
            for log in peer_logs[0]:
                if log in accepted:
                    raise ValueError("LP cash transfer reused by more than one position")
                accepted.add(log)
            for currency, amount in zip(
                currencies, (payment.amounts.amount0, payment.amounts.amount1), strict=True
            ):
                flow = flows.get(currency.asset_id)
                matching = [log for log in peer_logs[0] if log.token == currency.address]
                if flow is None or flow.decimals != currency.decimals:
                    raise ValueError("LP cash requires complete currency movement evidence")
                if any(log not in flow.logs for log in matching):
                    raise ValueError("LP cash receipt transfer absent from account movement set")
                delta = sum(
                    (log.units if log.to_address == entry.account else 0)
                    - (log.units if log.from_address == entry.account else 0)
                    for log in matching
                )
                if delta != amount:
                    raise ValueError("LP cash receipt amount differs from reconciled payment")
    return frozenset(accepted)
