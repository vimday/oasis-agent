from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from ..models.chain import BlockSnapshot, LogRecord, Receipt
from ..models.common import BlockRef, normalize_address, normalize_hash32, normalize_hex
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
        if isinstance(block, int):
            if isinstance(block, bool):
                raise TypeError("block number must be an integer")
            if block < 0:
                raise ValueError("block number must be nonnegative")
            return hex(block)
        if block == "latest":
            return block
        if not block.startswith("0x"):
            raise ValueError("block tag must be latest or a hex quantity")
        digits = block[2:]
        if not digits or (len(digits) > 1 and digits.startswith("0")):
            raise ValueError("block tag must use canonical hex quantity encoding")
        int(digits, 16)
        return block

    def get_block(self, block: int | str = "latest") -> BlockRef:
        return self.get_block_snapshot(block).ref

    def get_block_snapshot(self, block: int | str = "latest") -> BlockSnapshot:
        block_arg = self._block_arg(block)
        value = self.transport.request("eth_getBlockByNumber", [block_arg, False])
        if value is None:
            raise ValueError("block is not available")
        if not isinstance(value, Mapping):
            raise TypeError("block response must be an object")
        result = value
        number = self._response_quantity(result.get("number"), "block number")
        if block_arg != "latest" and number != int(block_arg, 16):
            raise ValueError("RPC returned a different block number")
        ref = BlockRef(
            self._chain_id,
            number,
            self._response_string(result.get("hash"), "block hash"),
            self._response_quantity(result.get("timestamp"), "block timestamp"),
        )
        return BlockSnapshot(ref, self._response_string(result.get("parentHash"), "parent hash"))

    @staticmethod
    def _response_string(value: object, name: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string")
        return value

    @classmethod
    def _response_quantity(cls, value: object, name: str) -> int:
        raw = cls._response_string(value, name)
        try:
            canonical = cls._block_arg(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a canonical hex quantity") from exc
        if canonical == "latest":
            raise ValueError(f"{name} must be a canonical hex quantity")
        return int(canonical, 16)

    def get_code(self, address: str, block: int | str | BlockRef = "latest") -> str:
        if isinstance(block, BlockRef):
            if block.chain_id != self._chain_id:
                raise ValueError("block and RPC chain IDs disagree")
            block_arg: object = {"blockHash": block.block_hash, "requireCanonical": True}
        else:
            block_arg = self._block_arg(block)
        result = self.transport.request("eth_getCode", [normalize_address(address), block_arg])
        return normalize_hex(cast(str, result))

    def call(self, to: str, data: str, block: int | str | BlockRef = "latest") -> str:
        if isinstance(block, BlockRef):
            if block.chain_id != self._chain_id:
                raise ValueError("block and RPC chain IDs disagree")
            block_arg: object = {"blockHash": block.block_hash, "requireCanonical": True}
        else:
            block_arg = self._block_arg(block)
        result = self.transport.request(
            "eth_call", [{"to": normalize_address(to), "data": normalize_hex(data)}, block_arg]
        )
        return normalize_hex(cast(str, result))

    def get_receipt(self, transaction_hash: str) -> Receipt:
        requested_hash = normalize_hash32(transaction_hash, "transaction hash")
        value = self.transport.request("eth_getTransactionReceipt", [requested_hash])
        if value is None:
            raise ValueError("transaction receipt is not available")
        result = cast(dict[str, object], value)
        logs = cast(list[dict[str, object]], result.get("logs", []))
        receipt = Receipt(
            normalize_hex(str(result["transactionHash"])),
            int(str(result["status"]), 16),
            int(str(result["blockNumber"]), 16),
            tuple(self._decode_log(item) for item in logs),
        )
        if receipt.transaction_hash != requested_hash:
            raise ValueError("RPC returned a receipt for a different transaction")
        return receipt

    def _decode_log(self, value: dict[str, object]) -> LogRecord:
        block_hash_value = value.get("blockHash")
        transaction_index_value = value.get("transactionIndex")
        removed_value = value.get("removed", False)
        if not isinstance(removed_value, bool):
            raise TypeError("removed must be a boolean")
        return LogRecord(
            str(value["address"]),
            tuple(cast(list[str], value.get("topics", []))),
            str(value.get("data", "0x")),
            int(str(value["blockNumber"]), 16),
            str(value["transactionHash"]),
            int(str(value["logIndex"]), 16),
            None if block_hash_value is None else str(block_hash_value),
            (
                None
                if transaction_index_value is None
                else self._response_quantity(transaction_index_value, "transaction index")
            ),
            removed_value,
        )

    def get_logs(self, filter_params: dict[str, object]) -> list[LogRecord]:
        values = cast(
            list[dict[str, object]], self.transport.request("eth_getLogs", [filter_params])
        )
        return [self._decode_log(item) for item in values]
