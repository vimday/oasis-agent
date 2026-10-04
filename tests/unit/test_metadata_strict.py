import pytest

from oasis_agent.evm.bytecode import extract_cbor_metadata
from oasis_agent.evm.features import scan_features

IPFS = b"\x64ipfs\x58\x22\x12\x20" + bytes(range(32))
SOLC = b"\x64solc\x43\x00\x08\x00"


def runtime(raw: bytes) -> bytes:
    return b"\x60\x00" + raw + len(raw).to_bytes(2, "big")


@pytest.mark.parametrize("fields", [IPFS + SOLC, SOLC + IPFS])
def test_complete_map_without_key_order_assumption(fields: bytes) -> None:
    result = extract_cbor_metadata(runtime(b"\xa2" + fields))
    assert result.ipfs_cid is not None
    assert result.solc_version == "0.8.0"
    assert result.raw_cbor == b"\xa2" + fields


@pytest.mark.parametrize(
    "raw",
    [
        IPFS + SOLC,  # Markers alone are not a map.
        b"\x58" + bytes([len(IPFS + SOLC)]) + IPFS + SOLC,  # Embedded in a bytes value.
        b"\xa1\x65ipfsX\x22\x12\x20" + bytes(range(32)),  # Old regression: wrong key length.
        b"\xa1" + IPFS + SOLC,  # Trailing second field.
        b"\xa2" + SOLC + SOLC,  # Duplicate.
        b"\xa2" + IPFS + b"\x64solc\x43\x00\x08",  # Truncated.
        b"\xa1" + IPFS.replace(b"\x12\x20", b"\x13\x20"),
        b"\xa1\x64ipfs\x58\x21\x12\x20" + bytes(range(31)),
        b"\xa1\x64solc\x44\x00\x08\x00\x01",
        b"\xbf" + IPFS + b"\xff",  # Indefinite map.
        b"\xb8\x01" + IPFS,  # Nonminimal map length.
        b"\xa1\x78\x04ipfs\x58\x22\x12\x20" + bytes(range(32)),
        b"\xa1\x64ipfs\x59\x00\x22\x12\x20" + bytes(range(32)),
        b"\xa2" + SOLC + b"\x65other\xf5",  # Unsupported future profile.
        b"\xa1\x64\xffabc\x43\x00\x08\x00",  # Invalid UTF-8 key.
        b"\xa1\x64solc\xc0\x43\x00\x08\x00",  # Tag.
        b"\xa1\x64solc\x80",  # Nested array.
        b"\xa1\x64solc\x63\xffab",  # Non-ASCII version.
        b"\xa1\x64solc\x60",  # Empty version.
        b"\xa1\x64solc\x62a\n",  # Control character.
        b"\xa1\x6cexperimental\xf4",
        b"\xa2" + IPFS + b"\x65bzzr1\x58\x20" + bytes(32),  # Ambiguous locator.
        b"\xa1\x65bzzr1\x41\x00",
        b"\xa0",
        b"\xa6" + SOLC * 6,
        b"\xa1\x64solc\x7a\x00\x00\x00\x03abc",  # Unsupported length width.
        b"\xa1\x64solc\x7fabc\xff",
        b"\xa1\x64solc\x78\x81" + b"a" * 129,
        b"\xa1\x64solc\x79\xff\xffabc",  # Length overrun.
        bytes(1025),
        b"",
    ],
)
def test_malformed_or_unsupported_trailer_cannot_emit_metadata_claims(raw: bytes) -> None:
    result = extract_cbor_metadata(runtime(raw))
    assert result.ipfs_cid is None
    assert result.solc_version is None
    assert result.bzzr1 is None
    assert result.bzzr0 is None
    assert result.raw_cbor is None
    assert scan_features(runtime(raw)).metadata == {"ipfs_cid": "", "solc_version": ""}


@pytest.mark.parametrize("key", [b"bzzr0", b"bzzr1"])
def test_swarm_hashes_preserve_version_identity(key: bytes) -> None:
    result = extract_cbor_metadata(runtime(b"\xa1\x65" + key + b"\x58\x20" + bytes(32)))
    assert getattr(result, key.decode()) == "00" * 32
    assert getattr(result, "bzzr1" if key == b"bzzr0" else "bzzr0") is None


def test_prerelease_and_experimental_supported_without_claiming_source_authentication() -> None:
    version = b"0.8.0-develop+commit.c7dfd78e.2020.12.16"
    raw = b"\xa2\x64solc\x78" + bytes([len(version)]) + version + b"\x6cexperimental\xf5"
    assert extract_cbor_metadata(runtime(raw)).solc_version == version.decode()


@pytest.mark.parametrize("code", [b"", b"\x00", b"\x01\x02\xff\xff"])
def test_no_complete_length_suffix(code: bytes) -> None:
    assert extract_cbor_metadata(code).raw_cbor is None
