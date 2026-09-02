import json
import unittest

from oasis_agent.errors import RpcError
from oasis_agent.rpc.transport import JsonRpcTransport


class FakeHttp:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response

    def post(self, payload: bytes) -> bytes:
        return json.dumps(self.response).encode()


class RpcTests(unittest.TestCase):
    def test_transport_returns_result(self) -> None:
        transport = JsonRpcTransport("https://example.invalid", http=FakeHttp({"jsonrpc": "2.0", "id": 1, "result": "0x1237"}))
        self.assertEqual(transport.request("eth_chainId", []), "0x1237")

    def test_transport_raises_rpc_error(self) -> None:
        transport = JsonRpcTransport("https://example.invalid", http=FakeHttp({"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "boom"}}))
        with self.assertRaisesRegex(RpcError, "boom"):
            transport.request("eth_chainId", [])


if __name__ == "__main__":
    unittest.main()
