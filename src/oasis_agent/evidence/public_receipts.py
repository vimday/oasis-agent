"""Strict block-bound receipt decoding; gas component is not complete transaction PnL."""

import re
from dataclasses import dataclass

from ..arbitrage.cycle_evidence import header, log_record
from ..arbitrage.cycle_plan import address, hash32
from ..models.chain import BlockSnapshot, LogRecord
from ..models.common import BlockRef
from .rpc_validation import rpc_quantity, strict_list, strict_object


def quantity(value: object, bits: int = 256) -> int:
    n = rpc_quantity(value)
    if n >= 1 << bits:
        raise ValueError("receipt_quantity_overflow")
    return n


@dataclass(frozen=True, slots=True)
class ObservedReceipt:
    block: BlockRef
    transaction_hash: str
    transaction_index: int
    sender: str
    recipient: str | None
    status: int
    gas_used: int
    effective_gas_price: int
    logs: tuple[LogRecord, ...]


@dataclass(frozen=True, slots=True)
class ReceiptBlockSnapshot(BlockSnapshot):
    receipt_positions: tuple[tuple[str, int], ...]


def receipt_header(
    value: object, chain_id: int, number: int, transactions: tuple[str, ...]
) -> ReceiptBlockSnapshot:
    base = header(value, chain_id, number)
    raw = strict_list(strict_object(value, "block").get("transactions"), "transactions")
    if len(raw) > 16384:
        raise ValueError("block_transaction_count_limit")
    hashes = [hash32(tx) for tx in raw]
    if len(hashes) != len(set(hashes)) or set(transactions) - set(hashes):
        raise ValueError("receipt_not_in_block_transaction_list")
    return ReceiptBlockSnapshot(
        base.ref, base.parent_hash, tuple((tx, hashes.index(tx)) for tx in transactions)
    )


def decode_receipt(
    value: object, block: BlockRef, tx_hash: str, expected_index: int | None = None
) -> ObservedReceipt:
    obj = strict_object(value, "receipt")
    if (
        hash32(obj.get("transactionHash")) != tx_hash
        or hash32(obj.get("blockHash")) != block.block_hash
        or quantity(obj.get("blockNumber"), 64) != block.number
    ):
        raise ValueError("receipt_inclusion_mismatch")
    index = quantity(obj.get("transactionIndex"), 32)
    if expected_index is not None and index != expected_index:
        raise ValueError("receipt_index_differs_from_block")
    status = quantity(obj.get("status"), 8)
    if status not in (0, 1):
        raise ValueError("invalid_receipt_status")
    raw_logs = strict_list(obj.get("logs"), "logs")
    if len(raw_logs) > 4096 or (not status and raw_logs):
        raise ValueError("receipt_log_count_or_failed_transaction_logs")
    logs = tuple(log_record(v, block, tx_hash, index) for v in raw_logs)
    if any(re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", log.data) is None for log in logs):
        raise ValueError("receipt_log_data_not_hex_bytes")
    indices = [log.log_index for log in logs]
    if indices != sorted(set(indices)) or any(n >= 1 << 32 for n in indices):
        raise ValueError("receipt_duplicate_or_unordered_logs")
    gas, price = quantity(obj.get("gasUsed")), quantity(obj.get("effectiveGasPrice"))
    if gas * price >= 1 << 256:
        raise ValueError("receipt_gas_product_overflow")
    return ObservedReceipt(
        block,
        tx_hash,
        index,
        address(obj.get("from")),
        address(obj["to"]) if obj.get("to") is not None else None,
        status,
        gas,
        price,
        logs,
    )
