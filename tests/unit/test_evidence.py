import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.models.common import BlockRef, EvidenceRef, canonical_json


class EvidenceTests(unittest.TestCase):
    def test_same_content_has_same_hash_but_distinct_facts_remain_retrievable(self) -> None:
        with TemporaryDirectory() as directory:
            store = EvidenceStore(Path(directory) / "evidence.sqlite3")
            first_block = BlockRef(4663, 12, "0x" + "a" * 64)
            second_block = BlockRef(4663, 13, "0x" + "b" * 64)
            first = store.put("code", b"abc", first_block, "rpc-a")
            second = store.put("code", b"abc", second_block, "rpc-b")
            self.assertEqual(first.content_hash, second.content_hash)
            self.assertEqual(store.get(first), b"abc")
            self.assertEqual(store.get(second), b"abc")

    def test_get_requires_the_complete_evidence_identity(self) -> None:
        with TemporaryDirectory() as directory:
            store = EvidenceStore(Path(directory) / "evidence.sqlite3")
            block = BlockRef(4663, 12, "0x" + "a" * 64)
            reference = store.put("code", b"abc", block, "rpc-a")
            wrong_source = type(reference)(reference.kind, reference.content_hash, block, "rpc-b")
            with self.assertRaises(KeyError):
                store.get(wrong_source)

    def test_provenance_is_part_of_identity(self) -> None:
        with TemporaryDirectory() as directory:
            store = EvidenceStore(Path(directory) / "evidence.sqlite3")
            block = BlockRef(4663, 12, "0x" + "a" * 64)
            first = store.put(
                "rpc",
                b"raw",
                block,
                "rpc-a",
                request={"method": "eth_getBlockByNumber", "params": ["0xc", True]},
                raw_response={"number": "0xc"},
                interpretation={"number": 12},
                retrieved_at="2026-09-03T00:00:00+00:00",
            )
            second = store.put(
                "rpc",
                b"raw",
                block,
                "rpc-a",
                request={"params": ["0xc", True], "method": "eth_getBlockByNumber"},
                raw_response={"number": "0xc"},
                interpretation={"number": 12},
                retrieved_at="2026-09-03T00:00:01+00:00",
            )
            self.assertEqual(store.get(first), b"raw")
            self.assertEqual(store.get(second), b"raw")
            self.assertNotEqual(first.request_hash, "")
            self.assertEqual(first.request_hash, second.request_hash)

    def test_canonical_json_is_order_independent(self) -> None:
        self.assertEqual(canonical_json({"b": 2, "a": 1}), b'{"a":1,"b":2}')

    def test_tampered_retrieval_timestamp_cannot_read_fact(self) -> None:
        with TemporaryDirectory() as directory:
            store = EvidenceStore(Path(directory) / "evidence.sqlite3")
            reference = store.put(
                "code", b"abc", None, "fixture", retrieved_at="2026-09-03T00:00:00+00:00"
            )
            tampered = EvidenceRef(
                reference.kind,
                reference.content_hash,
                reference.block,
                reference.source,
                "1900-01-01T00:00:00+00:00",
                reference.request_hash,
                reference.raw_response_hash,
                reference.interpretation_hash,
                reference.schema_version,
            )
            with self.assertRaises(KeyError):
                store.get(tampered)

    def test_legacy_migration_keeps_reference_identity_retrievable(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE evidence (content_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                "payload BLOB NOT NULL, chain_id INTEGER, block_number INTEGER, "
                "block_hash TEXT, source TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
                    "code",
                    b"abc",
                    4663,
                    12,
                    "0x" + "a" * 64,
                    "legacy-rpc",
                ),
            )
            connection.commit()
            connection.close()

            store = EvidenceStore(path)
            migrated = EvidenceRef(
                "code",
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
                BlockRef(4663, 12, "0x" + "a" * 64),
                "legacy-rpc",
                "1970-01-01T00:00:00+00:00",
                "",
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
                "",
                1,
            )
            self.assertEqual(store.get(migrated), b"abc")

    def test_block_reference_rejects_noncanonical_identity(self) -> None:
        for chain_id, number, block_hash in (
            (0, 1, "0x" + "a" * 64),
            (4663, -1, "0x" + "a" * 64),
            (4663, 1, "0xabc"),
        ):
            with (
                self.subTest(chain_id=chain_id, number=number, block_hash=block_hash),
                self.assertRaises(ValueError),
            ):
                BlockRef(chain_id, number, block_hash)

    def test_retrieval_rejects_tampered_payload(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.sqlite3"
            store = EvidenceStore(path)
            reference = store.put(
                "code",
                b"abc",
                None,
                "fixture",
                retrieved_at="2026-09-03T00:00:00+00:00",
            )
            connection = sqlite3.connect(path)
            connection.execute("UPDATE evidence SET payload = ?", (b"tampered",))
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                store.get(reference)

    def test_evidence_reference_requires_valid_hashes_and_timestamp(self) -> None:
        with self.assertRaisesRegex(ValueError, "content_hash"):
            EvidenceRef("code", "not-a-hash", None, "fixture")
        with self.assertRaisesRegex(ValueError, "timezone"):
            EvidenceRef(
                "code",
                "a" * 64,
                None,
                "fixture",
                "2026-09-03T00:00:00",
            )

    def test_complete_raw_envelope_survives_reopen_and_is_verified(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.sqlite3"
            store = EvidenceStore(path)
            reference = store.put(
                "rpc",
                b"payload",
                BlockRef(4663, 12, "0x" + "a" * 64, 1_000),
                "rpc-a",
                request={"method": "eth_getBlockByNumber", "params": ["0xc", False]},
                raw_response=b'{"jsonrpc":"2.0","result":{"number":"0xc"}}',
                interpretation={"number": 12},
                retrieved_at="2026-09-03T00:00:00+00:00",
            )
            store.close()

            reopened = EvidenceStore(path)
            record = reopened.get_record(reference)
            self.assertEqual(record.payload, b"payload")
            self.assertEqual(
                record.request_payload,
                b'{"method":"eth_getBlockByNumber","params":["0xc",false]}',
            )
            self.assertEqual(
                record.raw_response_payload,
                b'{"jsonrpc":"2.0","result":{"number":"0xc"}}',
            )
            self.assertEqual(record.interpretation_payload, b'{"number":12}')

            connection = sqlite3.connect(path)
            connection.execute(
                "UPDATE evidence SET raw_response_payload = ?",
                (b"tampered",),
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(ValueError, "raw response hash mismatch"):
                reopened.get_record(reference)
