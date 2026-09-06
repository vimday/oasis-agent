"""Bind a bounded block/log dataset to its retained RPC exchange contents."""

from __future__ import annotations

from ..models.chain import LogRecord
from ..models.common import normalize_address, normalize_hash32, normalize_hex
from ..rpc.collector import BlockRangeDataset
from ..rpc.transport import RpcExchange
from .rpc_validation import decode_exchange, rpc_quantity


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("range evidence requires string data")
    return value


def _decode_log(value: object) -> LogRecord:
    if not isinstance(value, dict):
        raise TypeError("range evidence log must be an object")
    topics = value.get("topics")
    if not isinstance(topics, list):
        raise TypeError("range evidence log topics must be an array")
    if len(topics) > 4 or value.get("removed") is not False:
        raise ValueError("range evidence log is removed or has invalid topic count")
    return LogRecord(
        normalize_address(_string(value.get("address"))),
        tuple(normalize_hash32(_string(topic), "event topic") for topic in topics),
        normalize_hex(_string(value.get("data"))),
        rpc_quantity(value.get("blockNumber")),
        normalize_hash32(_string(value.get("transactionHash")), "transaction hash"),
        rpc_quantity(value.get("logIndex")),
        normalize_hash32(_string(value.get("blockHash")), "block hash"),
        rpc_quantity(value.get("transactionIndex")),
        False,
    )


def validate_block_range_exchanges(
    dataset: BlockRangeDataset, exchanges: tuple[RpcExchange, ...]
) -> None:
    """Require successful chain/header/log coverage with exactly the declared filter.

    Failed RPC attempts are retained but do not count as coverage. This validates
    consistency of the provider's replies, not provider honesty or chain finality.
    """
    snapshots = {item.ref.number: item for item in dataset.blocks}
    seen_ids: set[int] = set()
    headers: set[int] = set()
    chain_seen = False
    logs_seen = False
    closing_seen = False
    for exchange in exchanges:
        params, response = decode_exchange(exchange)
        if exchange.request_id in seen_ids:
            raise ValueError("range evidence repeats an RPC request ID")
        seen_ids.add(exchange.request_id)
        if exchange.method not in {"eth_chainId", "eth_getBlockByNumber", "eth_getLogs"}:
            raise ValueError("unexpected method in block range evidence")
        if "error" in response:
            continue
        result = response["result"]
        if exchange.method == "eth_chainId":
            if params or rpc_quantity(result) != dataset.chain_id:
                raise ValueError("range evidence chain ID mismatch")
            chain_seen = True
            continue
        if not chain_seen:
            raise ValueError("range evidence requires chain ID before data reads")
        if exchange.method == "eth_getBlockByNumber":
            if len(params) != 2 or params[1] is not False:
                raise ValueError("range evidence has invalid block request")
            number = rpc_quantity(params[0])
            snapshot = snapshots.get(number)
            if snapshot is None or not isinstance(result, dict):
                raise ValueError("range evidence block is outside dataset or unavailable")
            if (
                rpc_quantity(result.get("number")) != number
                or result.get("hash") != snapshot.ref.block_hash
                or result.get("parentHash") != snapshot.parent_hash
                or rpc_quantity(result.get("timestamp")) != snapshot.ref.timestamp
            ):
                raise ValueError("range evidence block header does not match dataset")
            headers.add(number)
            if logs_seen and number == dataset.end_block:
                closing_seen = True
        else:
            if headers != set(snapshots):
                raise ValueError("range evidence is missing block headers before log acquisition")
            if params != [dataset.log_filter]:
                raise ValueError("range evidence log filter does not match dataset scope")
            if logs_seen:
                raise ValueError("range evidence has ambiguous repeated successful log queries")
            if not isinstance(result, list):
                raise TypeError("range evidence log result must be an array")
            if tuple(_decode_log(item) for item in result) != dataset.logs:
                raise ValueError("range evidence logs do not match dataset")
            logs_seen = True
    if not chain_seen or headers != set(snapshots) or not logs_seen or not closing_seen:
        raise ValueError("range evidence lacks successful chain/header/log/closing coverage")


__all__ = ["validate_block_range_exchanges"]
