"""Strict, incremental public log-range validation shared by capture and replay."""

import re
from dataclasses import dataclass

from ..arbitrage.cycle_evidence import header
from ..arbitrage.cycle_plan import address, hash32, utc
from ..models.chain import BlockSnapshot, LogRecord
from ..models.common import BlockRef
from ..rpc.collector import BlockRangeDataset
from .public_rpc_plan import PublicRangePlan
from .range_validation import _decode_log
from .rpc_validation import rpc_quantity


@dataclass(frozen=True, slots=True)
class RangeContext:
    blocks: tuple[BlockSnapshot, ...]
    logs: tuple[LogRecord, ...] = ()

    @property
    def ref(self) -> BlockRef:
        return self.blocks[-1].ref

    def dataset(self, plan: PublicRangePlan) -> BlockRangeDataset:
        return BlockRangeDataset(plan.source, self.blocks, self.logs, plan.address, plan.topics)


def range_context(
    plan: PublicRangePlan, index: int, result: object, prior: RangeContext | None
) -> RangeContext:
    count = plan.end_block - plan.start_block + 1
    if 1 <= index <= count:
        block = header(result, plan.chain_id, plan.start_block + index - 1)
        parent = prior.ref.block_hash if prior else plan.parent_hash
        if block.parent_hash != parent:
            raise ValueError("range_ancestry_mismatch")
        if (
            plan.expected_hashes is not None
            and block.ref.block_hash != plan.expected_hashes[index - 1]
        ):
            raise ValueError("range_block_hash_changed")
        if block.ref.timestamp is None or block.ref.timestamp <= int(
            utc(plan.locked_utc).timestamp()
        ):
            raise ValueError("range_predates_declared_lock")
        if plan.parent_timestamp is not None and block.ref.timestamp < plan.parent_timestamp:
            raise ValueError("range_predates_parent")
        context = RangeContext((*prior.blocks, block) if prior else (block,))
        context.dataset(plan)  # Contiguity, monotone timestamps and bounded domain.
        return context
    if prior is None or len(prior.blocks) != count:
        raise ValueError("range_headers_missing")
    if index == count + 1:
        if not isinstance(result, list) or len(result) > 8192:
            raise ValueError("range_log_limit")
        logs = []
        for item in result:
            if not isinstance(item, dict) or not isinstance(item.get("topics"), list):
                raise ValueError("range_log_shape")
            address(item.get("address"))
            for value in (item.get("blockHash"), item.get("transactionHash"), *item["topics"]):
                hash32(value)
            data = item.get("data")
            if not isinstance(data, str) or re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", data) is None:
                raise ValueError("range_log_data")
            if any(
                rpc_quantity(item.get(key)) >= 1 << 32 for key in ("logIndex", "transactionIndex")
            ):
                raise ValueError("range_log_position_overflow")
            logs.append(_decode_log(item))
        context = RangeContext(prior.blocks, tuple(logs))
        context.dataset(plan)  # Exact prefix filter, block/hash membership and unique ordering.
        return context
    if index != count + 2 or header(result, plan.chain_id, plan.end_block) != prior.blocks[-1]:
        raise ValueError("range_closing_header_changed")
    return prior
