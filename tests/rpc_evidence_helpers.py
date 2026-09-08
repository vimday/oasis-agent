"""Synthetic wire-format envelopes for offline evidence-boundary tests."""

from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.collector import BlockRangeDataset
from oasis_agent.rpc.transport import RpcExchange


def range_exchanges(dataset: BlockRangeDataset) -> tuple[RpcExchange, ...]:
    items: list[RpcExchange] = []

    def append(method: str, params: list[object], result: object) -> None:
        request_id = len(items) + 1
        items.append(
            RpcExchange(
                method,
                request_id,
                canonical_json(
                    {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
                ),
                canonical_json({"jsonrpc": "2.0", "id": request_id, "result": result}),
                1,
            )
        )

    append("eth_chainId", [], hex(dataset.chain_id))
    headers = []
    for snapshot in dataset.blocks:
        header = {
            "number": hex(snapshot.ref.number),
            "hash": snapshot.ref.block_hash,
            "parentHash": snapshot.parent_hash,
            "timestamp": hex(snapshot.ref.timestamp),
        }
        headers.append(header)
        append("eth_getBlockByNumber", [hex(snapshot.ref.number), False], header)
    append(
        "eth_getLogs",
        [dataset.log_filter],
        [
            {
                "address": log.address,
                "topics": list(log.topics),
                "data": log.data,
                "blockNumber": hex(log.block_number),
                "blockHash": log.block_hash,
                "transactionHash": log.transaction_hash,
                "transactionIndex": hex(log.transaction_index),
                "logIndex": hex(log.log_index),
                "removed": log.removed,
            }
            for log in dataset.logs
        ],
    )
    append("eth_getBlockByNumber", [hex(dataset.end_block), False], headers[-1])
    return tuple(items)
