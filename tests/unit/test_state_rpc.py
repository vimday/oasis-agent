from __future__ import annotations

import unittest

from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.models.common import BlockRef
from oasis_agent.uniswap_v4.state_rpc import RpcV4StateSource


class FakeStateRpc:
    def __init__(self) -> None:
        self.responses: dict[str, str] = {}
        self.calls: list[tuple[str, str, int | str | BlockRef]] = []

    def chain_id(self) -> int:
        return 4663

    def call(self, to: str, data: str, block: int | str | BlockRef = "latest") -> str:
        self.calls.append((to, data, block))
        return self.responses[data[:10]]


def selector(signature: str) -> str:
    return "0x" + keccak(signature.encode())[:4].hex()


def result(types: list[str], values: list[object]) -> str:
    return "0x" + encode(types, values).hex()


class StateRpcTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rpc = FakeStateRpc()
        self.view = "0x" + "11" * 20
        self.source = RpcV4StateSource(self.rpc, self.view)
        self.block = BlockRef(4663, 10, "0x" + "aa" * 32, 1_000)
        self.pool_id = "0x" + "22" * 32

    def test_reads_replay_checkpoint_surfaces_at_exact_hash(self) -> None:
        self.rpc.responses = {
            selector("getSlot0(bytes32)"): result(
                ["uint160", "int24", "uint24", "uint24"], [1 << 96, -1, 0, 2_000]
            ),
            selector("getLiquidity(bytes32)"): result(["uint128"], [123]),
            selector("getFeeGrowthGlobals(bytes32)"): result(["uint256", "uint256"], [4, 5]),
            selector("getTickInfo(bytes32,int24)"): result(
                ["uint128", "int128", "uint256", "uint256"], [100, -20, 6, 7]
            ),
        }
        slot0 = self.source.get_slot0(self.pool_id, self.block)
        self.assertEqual((slot0.sqrt_price_x96, slot0.tick, slot0.lp_fee), (1 << 96, -1, 2_000))
        self.assertEqual(self.source.get_liquidity(self.pool_id, self.block), 123)
        self.assertEqual(self.source.get_fee_growth_globals(self.pool_id, self.block), (4, 5))
        self.assertEqual(
            self.source.get_tick_info(self.pool_id, -60, self.block).liquidity_net, -20
        )
        self.assertEqual({call[2] for call in self.rpc.calls}, {self.block})

    def test_rejects_uninitialized_slot_and_wrong_chain(self) -> None:
        self.rpc.responses[selector("getSlot0(bytes32)")] = result(
            ["uint160", "int24", "uint24", "uint24"], [0, 0, 0, 0]
        )
        with self.assertRaisesRegex(ValueError, "initialized"):
            self.source.get_slot0(self.pool_id, self.block)
        with self.assertRaisesRegex(ValueError, "chain IDs disagree"):
            self.source.get_liquidity(self.pool_id, BlockRef(1, 10, "0x" + "aa" * 32, 1_000))


if __name__ == "__main__":
    unittest.main()
