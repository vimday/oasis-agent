import unittest

from eth_abi import encode
from eth_hash.auto import keccak

from oasis_agent.farming.position_rpc import RpcPositionStateSource
from oasis_agent.farming.positions import read_position_snapshot
from oasis_agent.models.common import BlockRef


class FakeCallRpc:
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


class RpcPositionStateSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rpc = FakeCallRpc()
        self.manager = "0x" + "11" * 20
        self.view = "0x" + "22" * 20
        self.block = BlockRef(4663, 99, "0x" + "aa" * 32, 1_000)
        self.source = RpcPositionStateSource(self.rpc, self.manager, self.view)

    def test_reads_every_surface_at_exact_block(self) -> None:
        owner = "0x" + "33" * 20
        currency0 = "0x" + "44" * 20
        currency1 = "0x" + "55" * 20
        hooks = "0x" + "66" * 20
        pool_id = "0x" + "77" * 32
        position_id = "0x" + "88" * 32
        self.rpc.responses = {
            selector("ownerOf(uint256)"): result(["address"], [owner]),
            selector("getPoolAndPositionInfo(uint256)"): result(
                ["(address,address,uint24,int24,address)", "uint256"],
                [(currency0, currency1, 0x800000, 60, hooks), 9],
            ),
            selector("getPositionLiquidity(uint256)"): result(["uint128"], [123]),
            selector("getPositionInfo(bytes32,bytes32)"): result(
                ["uint128", "uint256", "uint256"], [123, 456, 789]
            ),
            selector("getFeeGrowthInside(bytes32,int24,int24)"): result(
                ["uint256", "uint256"], [1_000, 2_000]
            ),
        }

        self.assertEqual(self.source.owner_of(7, self.block), owner)
        key, packed = self.source.pool_and_position_info(7, self.block)
        self.assertEqual(
            (key.currency0, key.currency1, key.tick_spacing), (currency0, currency1, 60)
        )
        self.assertEqual(packed, 9)
        self.assertEqual(self.source.position_liquidity(7, self.block), 123)
        self.assertEqual(
            self.source.core_position(
                pool_id, position_id, self.block
            ).fee_growth_inside1_last_x128,
            789,
        )
        self.assertEqual(
            self.source.fee_growth_inside(pool_id, -120, 120, self.block), (1_000, 2_000)
        )
        self.assertEqual({call[2] for call in self.rpc.calls}, {self.block})

    def test_rejects_cross_chain_block_before_call(self) -> None:
        with self.assertRaisesRegex(ValueError, "chain IDs disagree"):
            self.source.owner_of(7, BlockRef(1, 99, "0x" + "aa" * 32, 1_000))
        self.assertEqual(self.rpc.calls, [])

    def test_rejects_trailing_or_short_abi_data(self) -> None:
        key = selector("ownerOf(uint256)")
        for malformed in ("0x" + "00" * 31, "0x" + "00" * 64):
            with self.subTest(malformed=malformed):
                self.rpc.responses[key] = malformed
                with self.assertRaisesRegex(ValueError, "expected 32"):
                    self.source.owner_of(7, self.block)

    def test_robinhood_target_pool_position_vector(self) -> None:
        """Pinned single-provider research vector captured at block 54,324,388."""
        block = BlockRef(
            4663,
            54_324_388,
            "0xfca8bbb3fb1affca4785f77e222ddaeaee825fba4d209ecabbe34153124f8227",
            int("6a9ad0cc", 16),
        )
        manager = "0x58daec3116aae6d93017baaea7749052e8a04fa7"
        source = RpcPositionStateSource(self.rpc, manager, self.view)
        self.rpc.responses = {
            selector("ownerOf(uint256)"): "0x"
            + "0" * 24
            + "8b31bd449a740f87b768bfa2160f49a350c50883",
            selector("getPositionLiquidity(uint256)"): "0x" + "0" * 64,
            selector("getPoolAndPositionInfo(uint256)"): (
                "0x00000000000000000000000039dbed3a2bd333467115de45665cc57f813c4571"
                "0000000000000000000000005fc5360d0400a0fd4f2af552add042d716f1d168"
                "0000000000000000000000000000000000000000000000000000000000800000"
                "000000000000000000000000000000000000000000000000000000000000003c"
                "0000000000000000000000001191ac2561817686efe4296b37ba5a5419eaa880"
                "374531f25cfb1b92f6d03b946598d53dd934fba6e138bcece1fbb360fbb05400"
            ),
            selector("getPositionInfo(bytes32,bytes32)"): (
                "0x"
                + "0" * 128
                + "0000000000000000000000000000000000000000230e2bb2906242c5d377f130"
            ),
            selector("getFeeGrowthInside(bytes32,int24,int24)"): "0x" + "0" * 128,
        }

        snapshot = read_position_snapshot(manager, int("18e932", 16), block, source)
        self.assertEqual(
            snapshot.pool_id,
            "0x374531f25cfb1b92f6d03b946598d53dd934fba6e138bcece153a52113aa73a5",
        )
        self.assertEqual((snapshot.tick_lower, snapshot.tick_upper), (-282_540, -281_760))
        self.assertEqual(snapshot.liquidity, 0)
        self.assertEqual(snapshot.uncollected_fee0, 0)
        self.assertEqual(snapshot.uncollected_fee1, 0)


if __name__ == "__main__":
    unittest.main()
