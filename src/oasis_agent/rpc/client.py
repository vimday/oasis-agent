from __future__ import annotations

from typing import cast

from ..models.chain import LogRecord
from ..models.common import BlockRef, normalize_address, normalize_hex
from .transport import JsonRpcTransport


class RpcClient:
    def __init__(self, transport: JsonRpcTransport, expected_chain_id: int | None = None) -> None:
        self.transport = transport
        actual = int(cast(str, transport.request("eth_chainId", [])), 16)
        if expected_chain_id is not None and actual != expected_chain_id:
            raise ValueError(f"unexpected chain id: {actual}")
        self._chain_id = actual

    def chain_id(self) -> int:
        return self._chain_id

    @staticmethod
    def _block_arg(block: int | str) -> str:
        return block if isinstance(block, str) else hex(block)

    def get_code(self, address: str, block: int | str = "latest") -> str:
        result = self.transport.request("eth_getCode", [normalize_address(address), self._block_arg(block)])
        return normalize_hex(cast(str, result))

    def call(self, to: str, data: str, block: int | str = "latest") -> str:
        result = self.transport.request("eth_call", [{"to": normalize_address(to), "data": normalize_hex(data)}, self._block_arg(block)])
        return normalize_hex(cast(str, result))

    def get_logs(self, filter_params: dict[str, object]) -> list[LogRecord]:
        values = cast(list[dict[str, object]], self.transport.request("eth_getLogs", [filter_params]))
        return [LogRecord(str(x["address"]), tuple(cast(list[str], x["topics"])), str(x["data"]), int(str(x["blockNumber"]), 16), str(x["transactionHash"]), int(str(x["logIndex"]), 16)) for x in values]
