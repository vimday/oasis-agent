from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..models.evm import FeatureSummary


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
    marker = b"\x69ipfsX\x22\x12\x20"
    index = raw.find(marker)
    if index >= 0 and index + len(marker) + 32 <= len(raw):
        digest = raw[index + len(marker): index + len(marker) + 32]
        ipfs = _cid_v0(b"\x12\x20" + digest)
    solc_marker = b"solcC\x00\x08"
    index = raw.find(solc_marker)
    if index >= 0 and index + 5 <= len(raw):
        solc = f"0.{raw[index + 3]}.{raw[index + 4]}"
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
