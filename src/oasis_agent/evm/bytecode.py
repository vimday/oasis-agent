import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MetadataResult:
    ipfs_cid: str | None
    bzzr1: str | None
    solc_version: str | None
    raw_cbor: bytes | None


def extract_cbor_metadata(bytecode: bytes) -> MetadataResult:
    if len(bytecode) < 2:
        return MetadataResult(None, None, None, None)
    size = int.from_bytes(bytecode[-2:], "big")
    start = len(bytecode) - 2 - size
    if start < 0:
        return MetadataResult(None, None, None, None)
    raw = bytecode[start:-2]
    ipfs = None
    bzz = None
    solc = None
    marker = b"ipfs"
    index = raw.find(marker)
    if index >= 0:
        value_start = index + len(marker)
        if (
            value_start + 2 + 2 + 32 <= len(raw)
            and raw[value_start : value_start + 2] == b"X\x22"
            and raw[value_start + 2 : value_start + 4] == b"\x12\x20"
        ):
            digest = raw[value_start + 4 : value_start + 36]
            ipfs = _cid_v0(b"\x12\x20" + digest)
    solc_marker = b"solcC"
    index = raw.find(solc_marker)
    if index >= 0 and index + len(solc_marker) + 3 <= len(raw):
        solc = f"{raw[index + 5]}.{raw[index + 6]}.{raw[index + 7]}"
    return MetadataResult(ipfs, bzz, solc, raw)


def _cid_v0(multihash: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = int.from_bytes(multihash, "big")
    result = ""
    while number:
        number, remainder = divmod(number, 58)
        result = alphabet[remainder] + result
    return result or "1"


def bytecode_hash(bytecode: bytes) -> str:
    return hashlib.sha256(bytecode).hexdigest()
