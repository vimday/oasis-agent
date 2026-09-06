import unittest
from dataclasses import replace

from oasis_agent.models.chain import BlockSnapshot, LogRecord
from oasis_agent.models.common import BlockRef
from oasis_agent.rpc.collector import collect_block_range, require_provider_agreement


class FakeRangeRpc:
    def __init__(
        self,
        blocks: tuple[BlockSnapshot, ...],
        logs: tuple[LogRecord, ...],
    ) -> None:
        self.blocks = {item.ref.number: item for item in blocks}
        self.logs = logs
        self.filters: list[dict[str, object]] = []

    def chain_id(self) -> int:
        return 4663

    def get_block_snapshot(self, block: int | str = "latest") -> BlockSnapshot:
        assert isinstance(block, int)
        return self.blocks[block]

    def get_logs(self, filter_params: dict[str, object]) -> list[LogRecord]:
        self.filters.append(filter_params)
        return list(self.logs)


def snapshots() -> tuple[BlockSnapshot, ...]:
    h9 = "0x" + "9" * 64
    h10 = "0x" + "a" * 64
    h11 = "0x" + "b" * 64
    return (
        BlockSnapshot(BlockRef(4663, 10, h10, 1_000), h9),
        BlockSnapshot(BlockRef(4663, 11, h11, 1_002), h10),
    )


def logs() -> tuple[LogRecord, ...]:
    blocks = snapshots()
    return (
        LogRecord(
            "0x" + "1" * 40,
            ("0x" + "2" * 64,),
            "0x01",
            10,
            "0x" + "3" * 64,
            2,
            blocks[0].ref.block_hash,
            1,
        ),
        LogRecord(
            "0x" + "1" * 40,
            ("0x" + "2" * 64,),
            "0x02",
            11,
            "0x" + "4" * 64,
            0,
            blocks[1].ref.block_hash,
            0,
        ),
    )


class RpcCollectorTests(unittest.TestCase):
    def test_equal_logs_under_different_filters_cannot_form_quorum(self) -> None:
        first = collect_block_range(
            FakeRangeRpc(snapshots(), ()),
            source="rpc-a",
            start_block=10,
            end_block=11,
            address="0x" + "1" * 40,
        )
        second = replace(first, source="rpc-b", topics=("0x" + "2" * 64,))
        with self.assertRaisesRegex(ValueError, "disagree"):
            require_provider_agreement((first, second))

    def test_reorg_after_empty_log_response_is_rejected(self) -> None:
        class ReorgRpc(FakeRangeRpc):
            def get_logs(self, filter_params: dict[str, object]) -> list[LogRecord]:
                block = self.blocks[11]
                self.blocks[11] = replace(block, ref=replace(block.ref, block_hash="0x" + "c" * 64))
                return []

        with self.assertRaisesRegex(ValueError, "closing block changed"):
            collect_block_range(
                ReorgRpc(snapshots(), ()),
                source="rpc-a",
                start_block=10,
                end_block=11,
            )

    def test_collects_contiguous_block_pinned_dataset(self) -> None:
        rpc = FakeRangeRpc(snapshots(), logs())
        dataset = collect_block_range(
            rpc,
            source="rpc-a",
            start_block=10,
            end_block=11,
            address="0x" + "1" * 40,
            topics=("0x" + "2" * 64,),
        )
        self.assertEqual(dataset.start_block, 10)
        self.assertEqual(dataset.end_block, 11)
        self.assertEqual(len(dataset.logs), 2)
        self.assertEqual(len(dataset.content_hash), 64)
        self.assertEqual(
            rpc.filters,
            [
                {
                    "fromBlock": "0xa",
                    "toBlock": "0xb",
                    "address": "0x" + "1" * 40,
                    "topics": ["0x" + "2" * 64],
                }
            ],
        )

    def test_rejects_unpinned_or_out_of_order_logs(self) -> None:
        unpinned = LogRecord(
            "0x" + "1" * 40,
            (),
            "0x",
            10,
            "0x" + "3" * 64,
            0,
        )
        with self.assertRaisesRegex(ValueError, "block hash"):
            collect_block_range(
                FakeRangeRpc(snapshots(), (unpinned,)),
                source="rpc-a",
                start_block=10,
                end_block=11,
            )
        ordered = logs()
        with self.assertRaisesRegex(ValueError, "strict order"):
            collect_block_range(
                FakeRangeRpc(snapshots(), tuple(reversed(ordered))),
                source="rpc-a",
                start_block=10,
                end_block=11,
            )

    def test_range_is_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "maximum span"):
            collect_block_range(
                FakeRangeRpc(snapshots(), ()),
                source="rpc-a",
                start_block=10,
                end_block=12,
                max_blocks=2,
            )

    def test_provider_agreement_requires_distinct_matching_sources(self) -> None:
        first = collect_block_range(
            FakeRangeRpc(snapshots(), logs()),
            source="rpc-a",
            start_block=10,
            end_block=11,
        )
        second = collect_block_range(
            FakeRangeRpc(snapshots(), logs()),
            source="rpc-b",
            start_block=10,
            end_block=11,
        )
        agreement = require_provider_agreement((first, second))
        self.assertEqual(agreement.sources, ("rpc-a", "rpc-b"))
        self.assertEqual(agreement.content_hash, first.content_hash)

        with self.assertRaisesRegex(ValueError, "distinct providers"):
            require_provider_agreement((first, first))

        changed_logs = list(logs())
        original = changed_logs[-1]
        changed_logs[-1] = LogRecord(
            original.address,
            original.topics,
            "0x03",
            original.block_number,
            original.transaction_hash,
            original.log_index,
            original.block_hash,
            original.transaction_index,
        )
        conflicting = collect_block_range(
            FakeRangeRpc(snapshots(), tuple(changed_logs)),
            source="rpc-c",
            start_block=10,
            end_block=11,
        )
        with self.assertRaisesRegex(ValueError, "disagree"):
            require_provider_agreement((first, conflicting))


if __name__ == "__main__":
    unittest.main()
