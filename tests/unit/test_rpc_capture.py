import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from oasis_agent.evidence.range_validation import validate_block_range_exchanges
from oasis_agent.evidence.rpc_capture import (
    BoundedExchangeRecorder,
    persist_block_range_evidence,
)
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.models.chain import BlockSnapshot, LogRecord
from oasis_agent.models.common import BlockRef
from oasis_agent.rpc.collector import BlockRangeDataset
from oasis_agent.rpc.transport import RpcExchange
from tests.rpc_evidence_helpers import range_exchanges


class RpcCaptureTests(unittest.TestCase):
    def exchange(self, method: str, request_id: int) -> RpcExchange:
        request = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": []}
        ).encode()
        response = json.dumps({"jsonrpc": "2.0", "id": request_id, "result": []}).encode()
        return RpcExchange(method, request_id, request, response, 1)

    def dataset(self) -> BlockRangeDataset:
        block = BlockRef(4663, 10, "0x" + "aa" * 32, 1_000)
        snapshot = BlockSnapshot(block, "0x" + "bb" * 32)
        log = LogRecord(
            "0x" + "11" * 20,
            ("0x" + "22" * 32,),
            "0x",
            10,
            "0x" + "33" * 32,
            1,
            block.block_hash,
            0,
        )
        return BlockRangeDataset("provider-a", (snapshot,), (log,))

    def test_persists_exact_exchange_envelopes_with_dataset(self) -> None:
        exchanges = range_exchanges(self.dataset())
        with tempfile.TemporaryDirectory() as directory:
            store = EvidenceStore(Path(directory) / "evidence.db")
            reference = persist_block_range_evidence(
                store,
                self.dataset(),
                exchanges,
                retrieved_at="2026-09-04T00:00:00+00:00",
            )
            record = store.get_record(reference)
            self.assertEqual(record.payload, self.dataset().payload)
            assert record.raw_response_payload is not None
            raw = json.loads(record.raw_response_payload)
            self.assertEqual(
                bytes.fromhex(raw["exchanges"][1]["payload"]),
                exchanges[1].response_payload,
            )
            store.close()

    def test_rejects_placeholder_envelopes_with_correct_method_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = EvidenceStore(Path(directory) / "evidence.db")
            with self.assertRaises((ValueError, TypeError)):
                persist_block_range_evidence(
                    store,
                    self.dataset(),
                    (
                        self.exchange("eth_getBlockByNumber", 1),
                        self.exchange("eth_getLogs", 2),
                    ),
                )
            store.close()

    def test_bounded_recorder_fails_closed_on_count_or_bytes(self) -> None:
        exchange = self.exchange("eth_getLogs", 1)
        count_limited = BoundedExchangeRecorder(max_exchanges=1, max_bytes=10_000)
        count_limited.record(exchange)
        with self.assertRaisesRegex(ValueError, "capacity"):
            count_limited.record(exchange)
        byte_limited = BoundedExchangeRecorder(max_exchanges=2, max_bytes=1)
        with self.assertRaisesRegex(ValueError, "capacity"):
            byte_limited.record(exchange)

    def test_refuses_incomplete_raw_rpc_surface(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = EvidenceStore(Path(directory) / "evidence.db")
            with self.assertRaisesRegex(ValueError, "headers and logs"):
                persist_block_range_evidence(
                    store,
                    self.dataset(),
                    (self.exchange("eth_getLogs", 1),),
                )
            store.close()

    def test_rejects_changed_or_missing_raw_values_before_insertion(self) -> None:
        dataset = self.dataset()
        original = range_exchanges(dataset)
        wire_log = json.loads(original[-2].response_payload)["result"][0]
        for field, value in (
            ("data", "0x01"),
            ("blockHash", "0x" + "ff" * 32),
            ("transactionHash", "0x" + "dd" * 32),
            ("transactionIndex", "0x2"),
            ("logIndex", "0x2"),
            ("removed", True),
            ("removed", 0),
            ("topics", []),
            ("blockNumber", "0x0a"),
        ):
            response = json.loads(original[-2].response_payload)
            response["result"] = [{**wire_log, field: value}]
            mutated = replace(original[-2], response_payload=json.dumps(response).encode())
            with self.subTest(field=field, value=value), self.assertRaises((ValueError, TypeError)):
                validate_block_range_exchanges(dataset, (*original[:-2], mutated, original[-1]))

        omitted = replace(dataset, logs=())
        with self.assertRaisesRegex(ValueError, "logs do not match"):
            validate_block_range_exchanges(omitted, original)
        with self.assertRaisesRegex(ValueError, "coverage"):
            validate_block_range_exchanges(dataset, original[:-1])
        with self.assertRaisesRegex(ValueError, "chain ID"):
            validate_block_range_exchanges(dataset, original[1:])

    def test_empty_result_requires_exact_query_scope_and_success(self) -> None:
        dataset = replace(self.dataset(), logs=(), address="0x" + "11" * 20)
        original = range_exchanges(dataset)
        validate_block_range_exchanges(dataset, original)
        unfiltered = replace(dataset, address=None)
        self.assertNotEqual(dataset.content_hash, unfiltered.content_hash)
        with self.assertRaisesRegex(ValueError, "filter"):
            validate_block_range_exchanges(unfiltered, original)

        response = {
            "jsonrpc": "2.0",
            "id": original[-2].request_id,
            "error": {"code": -32005, "message": "rate limited"},
        }
        failed = replace(original[-2], response_payload=json.dumps(response).encode())
        with self.assertRaisesRegex(ValueError, "coverage"):
            validate_block_range_exchanges(dataset, (*original[:-2], failed, original[-1]))

    def test_multiblock_capture_requires_every_header_and_exact_logs(self) -> None:
        first = self.dataset()
        next_block = BlockSnapshot(
            BlockRef(4663, 11, "0x" + "cc" * 32, 1_002), first.blocks[-1].ref.block_hash
        )
        dataset = replace(first, blocks=(*first.blocks, next_block))
        exchanges = range_exchanges(dataset)
        validate_block_range_exchanges(dataset, exchanges)
        with self.assertRaisesRegex(ValueError, "missing block headers"):
            validate_block_range_exchanges(dataset, (exchanges[0], *exchanges[2:]))


if __name__ == "__main__":
    unittest.main()
