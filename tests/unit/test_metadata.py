import unittest

from oasis_agent.evm.bytecode import extract_cbor_metadata


class MetadataTests(unittest.TestCase):
    def test_truncated_metadata_is_ignored(self) -> None:
        self.assertIsNone(extract_cbor_metadata(b"\x01\x02\xff\xff").raw_cbor)

    def test_metadata_length_cannot_include_length_suffix(self) -> None:
        raw = b"\xa1\x64ipfsX\x22\x12\x20" + b"\x11" * 32
        bytecode = b"\x60\x00" + raw + len(raw).to_bytes(2, "big")
        result = extract_cbor_metadata(bytecode)
        self.assertEqual(result.raw_cbor, raw)
        self.assertTrue(result.ipfs_cid)


if __name__ == "__main__":
    unittest.main()
