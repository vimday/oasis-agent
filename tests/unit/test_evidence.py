import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.models.common import BlockRef


class EvidenceTests(unittest.TestCase):
    def test_same_content_has_same_hash(self) -> None:
        with TemporaryDirectory() as directory:
            store = EvidenceStore(Path(directory) / "evidence.sqlite3")
            block = BlockRef(4663, 12, "0xabc")
            first = store.put("code", b"abc", block, "fixture")
            second = store.put("code", b"abc", block, "fixture")
            self.assertEqual(first.content_hash, second.content_hash)
            self.assertEqual(store.get(first), b"abc")


if __name__ == "__main__":
    unittest.main()
