from __future__ import annotations

import unittest

from oasis_agent.farming.inventory import position_inventory
from oasis_agent.farming.positions import PositionSnapshot
from oasis_agent.models.common import BlockRef
from oasis_agent.models.v4 import DYNAMIC_FEE_FLAG, PoolKey
from oasis_agent.uniswap_v4.liquidity_math import sqrt_price_at_tick
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key


class PositionInventoryTests(unittest.TestCase):
    def test_inventory_keeps_principal_and_uncollected_fees_separate(self) -> None:
        key = PoolKey(
            "0x" + "11" * 20,
            "0x" + "22" * 20,
            DYNAMIC_FEE_FLAG,
            60,
            "0x" + "33" * 20,
        )
        snapshot = PositionSnapshot(
            BlockRef(4663, 10, "0x" + "aa" * 32, 1_000),
            "0x" + "44" * 20,
            7,
            "0x" + "55" * 20,
            "0x" + "44" * 20,
            key,
            pool_id_from_key(key),
            "0x" + "66" * 32,
            -60,
            60,
            False,
            1_000_000,
            0,
            0,
            0,
            0,
            11,
            13,
        )
        inventory = position_inventory(snapshot, sqrt_price_at_tick(0))
        self.assertTrue(inventory.in_range)
        self.assertEqual((inventory.principal0, inventory.principal1), (2995, 2995))
        self.assertEqual((inventory.total0, inventory.total1), (3006, 3008))


if __name__ == "__main__":
    unittest.main()
