"""Decode historical log leads without treating an index as canonical coverage."""

import re

from ..arbitrage.cycle_evidence import log_record
from ..arbitrage.cycle_plan import hash32
from ..models.chain import LogRecord
from ..models.common import BlockRef
from .public_receipts import quantity
from .public_rpc_plan import PublicEventIndexPlan
from .rpc_validation import strict_list, strict_object


def index_logs(plan: PublicEventIndexPlan, result: object) -> tuple[LogRecord, ...]:
    entries = strict_list(result, "indexed logs")
    if len(entries) >= 1000:
        raise ValueError("index_may_be_truncated")
    by_position: dict[tuple[int, int], LogRecord] = {}
    block_hashes: dict[int, str] = {}
    account_topic = "0x" + plan.account[2:].zfill(64)
    for entry in entries:
        row = strict_object(entry, "indexed log")
        number = quantity(row.get("blockNumber"), 64)
        tx_index = quantity(row.get("transactionIndex"), 32)
        block_hash = hash32(row.get("blockHash"))
        block = BlockRef(plan.chain_id, number, block_hash)
        log = log_record(row, block, hash32(row.get("transactionHash")), tx_index)
        position = (number, log.log_index)
        if (
            not plan.start_block <= number <= plan.end_block
            or log.address != plan.address
            or len(log.topics) <= plan.account_topic_index
            or log.topics[0] != plan.signature
            or log.topics[plan.account_topic_index] != account_topic
            or log.log_index >= 1 << 32
            or re.fullmatch(r"0x(?:[0-9a-f]{2})*", log.data) is None
            or position in by_position
            or block_hashes.setdefault(number, block_hash) != block_hash
        ):
            raise ValueError("index_log_scope_or_position_mismatch")
        by_position[position] = log
    return tuple(by_position[key] for key in sorted(by_position))
