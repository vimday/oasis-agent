import json
import unittest

from oasis_agent.errors import RpcError
from oasis_agent.models.common import BlockRef
from oasis_agent.rpc.client import RpcClient
from oasis_agent.rpc.transport import JsonRpcTransport, RpcExchange


class SequenceHttp:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.calls = 0
        self.payloads: list[dict[str, object]] = []

    def post(self, payload: bytes) -> bytes:
        self.payloads.append(json.loads(payload))
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return json.dumps(response).encode()


class ExchangeCollector:
    def __init__(self) -> None:
        self.items: list[RpcExchange] = []

    def record(self, exchange: RpcExchange) -> None:
        self.items.append(exchange)


class RpcTests(unittest.TestCase):
    def test_transport_returns_result(self) -> None:
        transport = JsonRpcTransport(
            "https://example.invalid",
            http=SequenceHttp([{"jsonrpc": "2.0", "id": 1, "result": "0x1237"}]),
        )
        self.assertEqual(transport.request("eth_chainId", []), "0x1237")

    def test_transport_records_exact_request_and_response_envelopes(self) -> None:
        recorder = ExchangeCollector()
        http = SequenceHttp([{"jsonrpc": "2.0", "id": 1, "result": "0x1237"}])
        transport = JsonRpcTransport(
            "https://example.invalid",
            http=http,
            recorder=recorder,
        )
        transport.request("eth_chainId", [])
        self.assertEqual(len(recorder.items), 1)
        self.assertEqual(recorder.items[0].request_payload, json.dumps(http.payloads[0]).encode())
        self.assertEqual(
            json.loads(recorder.items[0].response_payload),
            {"jsonrpc": "2.0", "id": 1, "result": "0x1237"},
        )

    def test_transport_raises_rpc_error(self) -> None:
        transport = JsonRpcTransport(
            "https://example.invalid",
            http=SequenceHttp(
                [{"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "boom"}}]
            ),
        )
        with self.assertRaisesRegex(RpcError, "boom"):
            transport.request("eth_chainId", [])

    def test_transport_retries_transient_error(self) -> None:
        http = SequenceHttp(
            [
                {"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "rate limited"}},
                {"jsonrpc": "2.0", "id": 2, "result": "0x1237"},
            ]
        )
        transport = JsonRpcTransport(
            "https://example.invalid", http=http, retries=1, backoff_seconds=0
        )
        self.assertEqual(transport.request("eth_chainId", []), "0x1237")
        self.assertEqual(http.calls, 2)

    def test_client_decodes_receipt(self) -> None:
        receipt = {
            "transactionHash": "0x" + "a" * 64,
            "status": "0x1",
            "blockNumber": "0x2a",
            "logs": [
                {
                    "address": "0x" + "1" * 40,
                    "topics": ["0x" + "2" * 64],
                    "data": "0x",
                    "blockNumber": "0x2a",
                    "transactionHash": "0x" + "a" * 64,
                    "logIndex": "0x0",
                }
            ],
        }
        responses = [
            {"jsonrpc": "2.0", "id": 1, "result": "0x1237"},
            {"jsonrpc": "2.0", "id": 2, "result": receipt},
        ]
        client = RpcClient(
            JsonRpcTransport("https://example.invalid", http=SequenceHttp(responses)),
            expected_chain_id=4663,
        )
        result = client.get_receipt("0x" + "a" * 64)
        self.assertEqual(result.block_number, 42)
        self.assertEqual(result.status, 1)
        self.assertEqual(result.logs[0].log_index, 0)

    def test_client_rejects_receipt_for_different_transaction(self) -> None:
        receipt = {
            "transactionHash": "0x" + "b" * 64,
            "status": "0x1",
            "blockNumber": "0x2a",
            "logs": [],
        }
        responses = [
            {"jsonrpc": "2.0", "id": 1, "result": "0x1237"},
            {"jsonrpc": "2.0", "id": 2, "result": receipt},
        ]
        client = RpcClient(
            JsonRpcTransport("https://example.invalid", http=SequenceHttp(responses)),
            expected_chain_id=4663,
        )
        with self.assertRaisesRegex(ValueError, "different transaction"):
            client.get_receipt("0x" + "a" * 64)

    def test_client_rejects_noncanonical_block_quantity(self) -> None:
        with self.assertRaisesRegex(ValueError, "canonical"):
            RpcClient._block_arg("0x00")
        with self.assertRaises(TypeError):
            RpcClient._block_arg(True)

    def test_client_preserves_complete_block_snapshot(self) -> None:
        block_hash = "0x" + "a" * 64
        parent_hash = "0x" + "b" * 64
        responses = [
            {"jsonrpc": "2.0", "id": 1, "result": "0x1237"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "number": "0x2a",
                    "hash": block_hash,
                    "parentHash": parent_hash,
                    "timestamp": "0x3e8",
                },
            },
        ]
        client = RpcClient(
            JsonRpcTransport("https://example.invalid", http=SequenceHttp(responses)),
            expected_chain_id=4663,
        )
        snapshot = client.get_block_snapshot(42)
        self.assertEqual(snapshot.ref.number, 42)
        self.assertEqual(snapshot.ref.block_hash, block_hash)
        self.assertEqual(snapshot.ref.timestamp, 1_000)
        self.assertEqual(snapshot.parent_hash, parent_hash)

    def test_client_rejects_block_number_mismatch(self) -> None:
        responses = [
            {"jsonrpc": "2.0", "id": 1, "result": "0x1237"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "number": "0x2b",
                    "hash": "0x" + "a" * 64,
                    "parentHash": "0x" + "b" * 64,
                    "timestamp": "0x3e8",
                },
            },
        ]
        client = RpcClient(
            JsonRpcTransport("https://example.invalid", http=SequenceHttp(responses)),
            expected_chain_id=4663,
        )
        with self.assertRaisesRegex(ValueError, "different block number"):
            client.get_block_snapshot(42)

    def test_eth_call_can_require_the_exact_canonical_block_hash(self) -> None:
        http = SequenceHttp(
            [
                {"jsonrpc": "2.0", "id": 1, "result": "0x1237"},
                {"jsonrpc": "2.0", "id": 2, "result": "0x" + "00" * 32},
            ]
        )
        client = RpcClient(
            JsonRpcTransport("https://example.invalid", http=http), expected_chain_id=4663
        )
        block = BlockRef(4663, 42, "0x" + "aa" * 32, 1_000)
        client.call("0x" + "11" * 20, "0x1234", block)
        self.assertEqual(
            http.payloads[-1]["params"],
            [
                {"to": "0x" + "11" * 20, "data": "0x1234"},
                {"blockHash": block.block_hash, "requireCanonical": True},
            ],
        )

    def test_eth_call_rejects_a_block_from_another_chain(self) -> None:
        client = RpcClient(
            JsonRpcTransport(
                "https://example.invalid",
                http=SequenceHttp([{"jsonrpc": "2.0", "id": 1, "result": "0x1237"}]),
            ),
            expected_chain_id=4663,
        )
        with self.assertRaisesRegex(ValueError, "chain IDs disagree"):
            client.call(
                "0x" + "11" * 20,
                "0x1234",
                BlockRef(1, 42, "0x" + "aa" * 32, 1_000),
            )

    def test_get_code_can_require_the_exact_canonical_block_hash(self) -> None:
        http = SequenceHttp(
            [
                {"jsonrpc": "2.0", "id": 1, "result": "0x1237"},
                {"jsonrpc": "2.0", "id": 2, "result": "0x6000"},
            ]
        )
        client = RpcClient(
            JsonRpcTransport("https://example.invalid", http=http), expected_chain_id=4663
        )
        block = BlockRef(4663, 42, "0x" + "aa" * 32, 1_000)
        self.assertEqual(client.get_code("0x" + "11" * 20, block), "0x6000")
        self.assertEqual(
            http.payloads[-1]["params"],
            [
                "0x" + "11" * 20,
                {"blockHash": block.block_hash, "requireCanonical": True},
            ],
        )

    def test_get_code_rejects_a_block_from_another_chain(self) -> None:
        client = RpcClient(
            JsonRpcTransport(
                "https://example.invalid",
                http=SequenceHttp([{"jsonrpc": "2.0", "id": 1, "result": "0x1237"}]),
            ),
            expected_chain_id=4663,
        )
        with self.assertRaisesRegex(ValueError, "chain IDs disagree"):
            client.get_code(
                "0x" + "11" * 20,
                BlockRef(1, 42, "0x" + "aa" * 32, 1_000),
            )


if __name__ == "__main__":
    unittest.main()
