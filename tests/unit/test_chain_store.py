import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from oasis_agent.evidence.chain_store import CanonicalChainStore
from oasis_agent.models.chain import BlockSnapshot, LogRecord
from oasis_agent.models.common import BlockRef
from oasis_agent.rpc.collector import BlockRangeDataset


class ChainStoreTests(unittest.TestCase):
    @staticmethod
    def block(number: int, value: str, parent: str) -> BlockSnapshot:
        return BlockSnapshot(BlockRef(4663, number, "0x" + value * 64, 1_000 + number), parent)

    @staticmethod
    def log(block: BlockSnapshot, value: str, index: int = 0) -> LogRecord:
        return LogRecord(
            "0x" + "11" * 20,
            ("0x" + "22" * 32,),
            "0x",
            block.ref.number,
            "0x" + value * 64,
            index,
            block.ref.block_hash,
            0,
        )

    def initial(self) -> tuple[BlockSnapshot, BlockSnapshot, BlockSnapshot]:
        b10 = self.block(10, "a", "0x" + "9" * 64)
        b11 = self.block(11, "b", b10.ref.block_hash)
        b12 = self.block(12, "c", b11.ref.block_hash)
        return b10, b11, b12

    def test_appends_and_atomically_rolls_back_all_streams_on_reorg(self) -> None:
        b10, b11, b12 = self.initial()
        b13 = self.block(13, "d", b12.ref.block_hash)
        alt12 = self.block(12, "e", b11.ref.block_hash)
        alt13 = self.block(13, "f", alt12.ref.block_hash)
        with tempfile.TemporaryDirectory() as directory:
            store = CanonicalChainStore(Path(directory) / "chain.db")
            first = store.apply(
                "pool-a",
                BlockRangeDataset("provider-a", (b10, b11, b12), (self.log(b12, "1"),)),
            )
            self.assertEqual(first.inserted_blocks, 3)
            appended = store.apply(
                "pool-b",
                BlockRangeDataset("provider-a", (b12, b13), (self.log(b13, "2"),)),
            )
            self.assertEqual(appended.canonical_tip.number, 13)

            reorged = store.apply(
                "pool-a",
                BlockRangeDataset(
                    "provider-a",
                    (b11, alt12, alt13),
                    (self.log(alt13, "3"),),
                ),
            )
            self.assertEqual(reorged.reorg_from, 12)
            self.assertEqual(reorged.removed_blocks, 2)
            self.assertEqual(
                tuple(block.ref.block_hash for block in store.blocks(4663)),
                (
                    b10.ref.block_hash,
                    b11.ref.block_hash,
                    alt12.ref.block_hash,
                    alt13.ref.block_hash,
                ),
            )
            self.assertEqual(store.logs("pool-b", 4663), ())
            self.assertEqual(store.logs("pool-a", 4663)[0].block_hash, alt13.ref.block_hash)
            store.close()

    def test_requires_common_ancestor_and_rejects_forward_gap(self) -> None:
        b10, b11, b12 = self.initial()
        alt12 = self.block(12, "e", b11.ref.block_hash)
        b14 = self.block(14, "f", b12.ref.block_hash)
        with tempfile.TemporaryDirectory() as directory:
            store = CanonicalChainStore(Path(directory) / "chain.db")
            store.apply("pool-a", BlockRangeDataset("provider-a", (b10, b11, b12), ()))
            with self.assertRaisesRegex(ValueError, "common ancestor"):
                store.apply("pool-a", BlockRangeDataset("provider-a", (alt12,), ()))
            with self.assertRaisesRegex(ValueError, "gap"):
                store.apply("pool-a", BlockRangeDataset("provider-a", (b14,), ()))
            store.close()

    def test_is_idempotent_for_same_range_and_stream(self) -> None:
        b10, b11, b12 = self.initial()
        dataset = BlockRangeDataset("provider-a", (b10, b11, b12), (self.log(b12, "1"),))
        with tempfile.TemporaryDirectory() as directory:
            store = CanonicalChainStore(Path(directory) / "chain.db")
            store.apply("pool-a", dataset)
            result = store.apply("pool-a", dataset)
            self.assertEqual(result.inserted_blocks, 0)
            self.assertIsNone(result.reorg_from)
            self.assertEqual(store.logs("pool-a", 4663), dataset.logs)
            store.close()

    def test_narrower_query_cannot_erase_a_streams_retained_events(self) -> None:
        blocks = self.initial()
        dataset = BlockRangeDataset("provider-a", blocks, (self.log(blocks[-1], "1"),))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chain.db"
            store = CanonicalChainStore(path)
            store.apply("pool-a", dataset)
            store.close()
            store = CanonicalChainStore(path)
            narrower = replace(dataset, logs=(), topics=("0x" + "99" * 32,))
            with self.assertRaisesRegex(ValueError, "scope"):
                store.apply("pool-a", narrower)
            self.assertEqual(store.logs("pool-a", 4663), dataset.logs)
            store.close()

    def test_same_block_hash_cannot_silently_replace_timestamp(self) -> None:
        blocks = self.initial()
        dataset = BlockRangeDataset("provider-a", blocks, ())
        with tempfile.TemporaryDirectory() as directory:
            store = CanonicalChainStore(Path(directory) / "chain.db")
            store.apply("pool-a", dataset)
            changed = replace(blocks[-1], ref=replace(blocks[-1].ref, timestamp=2_000))
            with self.assertRaisesRegex(ValueError, "header"):
                store.apply("pool-a", replace(dataset, blocks=(*blocks[:-1], changed)))
            self.assertEqual(store.blocks(4663), blocks)
            store.close()


if __name__ == "__main__":
    unittest.main()


def test_filter_coverage_does_not_inherit_chain_tip(tmp_path):
    case = ChainStoreTests()
    b10, b11, b12 = case.initial()
    b13 = case.block(13, "d", b12.ref.block_hash)
    path = tmp_path / "chain.db"
    store = CanonicalChainStore(path)
    store.apply("a", BlockRangeDataset("provider-a", (b10, b11, b12), ()))
    store.apply("b", BlockRangeDataset("provider-a", (b10,), ()))
    store.apply("b", BlockRangeDataset("provider-a", (b13,), ()))
    store.close()
    store = CanonicalChainStore(path)
    assert store.tip(4663).number == 13
    assert store.coverage("b", 4663, 10, 13)["missing_blocks"] == [11, 12]
    assert store.coverage("b", 4663, 10, 13)["contiguous_through"] == 10
    store.apply("b", BlockRangeDataset("provider-a", (b11, b12), ()))
    assert store.coverage("b", 4663, 10, 13)["status"] == "complete"
    assert store.coverage("unknown", 4663, 10, 13)["contiguous_through"] is None
    store.close()


def test_reorg_invalidates_other_filter_coverage_but_preserves_provenance(tmp_path):
    case = ChainStoreTests()
    b10, b11, b12 = case.initial()
    alt12 = case.block(12, "e", b11.ref.block_hash)
    store = CanonicalChainStore(tmp_path / "chain.db")
    dataset = BlockRangeDataset("provider-a", (b10, b11, b12), ())
    store.apply("a", dataset)
    store.apply("b", dataset)
    before = store.captures("b", 4663)
    store.apply("a", BlockRangeDataset("provider-a", (b11, alt12), ()))
    assert store.coverage("b", 4663, 10, 12)["missing_blocks"] == [12]
    assert store.captures("b", 4663) == before
    assert store.reorgs(4663)[0]["from_block"] == 12
    assert store.coverage("a", 4663, 10, 12)["status"] == "complete"
    store.close()


def test_conflicting_same_hash_logs_cannot_erase_and_partial_batch_rolls_back(tmp_path):
    case = ChainStoreTests()
    blocks = case.initial()
    dataset = BlockRangeDataset("provider-a", blocks, (case.log(blocks[-1], "1"),))
    store = CanonicalChainStore(tmp_path / "chain.db")
    store.apply("a", dataset)
    with pytest.raises(ValueError, match="coverage conflicts"), store.atomic():
        store.apply("b", dataset)
        store.apply("a", replace(dataset, logs=()))
    assert store.logs("a", 4663) == dataset.logs
    assert store.coverage("b", 4663, 10, 12)["missing_blocks"] == [10, 11, 12]
    assert store.captures("b", 4663) == ()
    store.close()


def test_dedup_keeps_distinct_sources_and_legacy_coverage_unknown(tmp_path):
    case = ChainStoreTests()
    blocks = case.initial()
    dataset = BlockRangeDataset("provider-a", blocks, ())
    store = CanonicalChainStore(tmp_path / "chain.db")
    store.apply("a", dataset)
    store.apply("a", dataset)
    store.apply("a", replace(dataset, source="provider-b"))
    assert len(store.captures("a", 4663)) == 2
    store._connection.execute("DELETE FROM canonical_coverage")
    store._connection.commit()
    assert store.coverage("a", 4663, 10, 12)["status"] == "coverage_incomplete"
    store.apply("a", dataset)
    assert store.coverage("a", 4663, 10, 12)["status"] == "complete"
    store.close()


def test_competing_writers_validate_tip_after_lock(tmp_path):
    case = ChainStoreTests()
    b10, b11, b12 = case.initial()
    path = tmp_path / "chain.db"
    store = CanonicalChainStore(path)
    store.apply("a", BlockRangeDataset("provider-a", (b10,), ()))
    store.close()
    barrier = Barrier(2)

    def write(name):
        db = CanonicalChainStore(path)
        barrier.wait(timeout=5)
        db.apply(name, BlockRangeDataset("provider-a", (b11, b12), ()))
        db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, ("a", "b")))
    store = CanonicalChainStore(path)
    assert store.coverage("a", 4663, 10, 12)["status"] == "complete"
    assert store.coverage("b", 4663, 10, 12)["missing_blocks"] == [10]
    store.close()


def test_legacy_logs_cannot_be_erased_during_coverage_backfill(tmp_path):
    case = ChainStoreTests()
    blocks = case.initial()
    dataset = BlockRangeDataset("provider-a", blocks, (case.log(blocks[-1], "1"),))
    store = CanonicalChainStore(tmp_path / "chain.db")
    store.apply("a", dataset)
    store._connection.execute("DELETE FROM canonical_coverage")
    store._connection.commit()
    with pytest.raises(ValueError, match="legacy same-block"):
        store.apply("a", replace(dataset, logs=()))
    assert store.logs("a", 4663) == dataset.logs
    assert store.coverage("a", 4663, 10, 12)["status"] == "coverage_incomplete"
    store.close()


def test_process_exit_cannot_advance_partial_batch(tmp_path):
    path = tmp_path / "chain.db"
    store = CanonicalChainStore(path)
    store.close()
    script = """
import os, sys
from pathlib import Path
from oasis_agent.evidence.chain_store import CanonicalChainStore
from oasis_agent.rpc.collector import BlockRangeDataset
from tests.unit.test_chain_store import ChainStoreTests
store = CanonicalChainStore(Path(sys.argv[1]))
with store.atomic():
    store.apply('partial', BlockRangeDataset('synthetic', ChainStoreTests().initial(), ()))
    os._exit(9)
"""
    result = subprocess.run([sys.executable, "-c", script, str(path)], check=False, timeout=10)
    assert result.returncode == 9
    store = CanonicalChainStore(path)
    assert store.tip(4663) is None
    assert store.captures("partial", 4663) == ()
    assert store.coverage("partial", 4663, 10, 12)["missing_blocks"] == [10, 11, 12]
    store.close()
