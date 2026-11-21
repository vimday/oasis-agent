import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MetadataResult:
    ipfs_cid: str | None
    bzzr1: str | None
    solc_version: str | None
    raw_cbor: bytes | None
    bzzr0: str | None = None


def extract_cbor_metadata(bytecode: bytes) -> MetadataResult:
    """Decode a bounded Solidity metadata profile, never search arbitrary bytes.

    Unsupported or malformed trailers return no claims. Valid CBOR and an embedded
    identifier do not authenticate the referenced document or deployed source.
    This deliberately supports only known Solidity fields, not general CBOR.
    """
    if len(bytecode) < 2:
        return MetadataResult(None, None, None, None)
    size = int.from_bytes(bytecode[-2:], "big")
    start = len(bytecode) - 2 - size
    if start < 0 or not 0 < size <= 1024:
        return MetadataResult(None, None, None, None)
    raw = bytecode[start:-2]
    try:
        fields = _solidity_metadata_fields(raw)
    except ValueError, UnicodeDecodeError:
        return MetadataResult(None, None, None, None)
    ipfs = fields.get("ipfs")
    version = fields.get("solc")
    solc = ".".join(str(part) for part in version) if isinstance(version, bytes) else version
    bzz = fields.get("bzzr1")
    legacy_bzz = fields.get("bzzr0")
    return MetadataResult(
        _cid_v0(ipfs) if isinstance(ipfs, bytes) else None,
        bzz.hex() if isinstance(bzz, bytes) else None,
        solc if isinstance(solc, str) else None,
        raw,
        legacy_bzz.hex() if isinstance(legacy_bzz, bytes) else None,
    )


def _cbor_head(raw: bytes, offset: int, major: int) -> tuple[int, int]:
    if offset >= len(raw) or raw[offset] >> 5 != major:
        raise ValueError("unsupported CBOR type")
    additional = raw[offset] & 31
    offset += 1
    if additional < 24:
        return additional, offset
    # All supported lengths fit in two bytes. No indefinite or nested encoding.
    widths = {24: 1, 25: 2}
    width = widths.get(additional)
    if width is None or offset + width > len(raw):
        raise ValueError("unsupported or truncated CBOR length")
    length = int.from_bytes(raw[offset : offset + width], "big")
    if length < (24 if width == 1 else 256):
        raise ValueError("nonminimal CBOR length")
    return length, offset + width


def _cbor_string(raw: bytes, offset: int, major: int) -> tuple[bytes, int]:
    length, offset = _cbor_head(raw, offset, major)
    end = offset + length
    if end > len(raw):
        raise ValueError("truncated CBOR string")
    return raw[offset:end], end


def _solidity_metadata_fields(raw: bytes) -> dict[str, bytes | str | bool]:
    count, offset = _cbor_head(raw, 0, 5)
    if not 1 <= count <= 5:
        raise ValueError("unsupported metadata map size")
    fields: dict[str, bytes | str | bool] = {}
    for _ in range(count):
        key_bytes, offset = _cbor_string(raw, offset, 3)
        key = key_bytes.decode("utf-8")
        if key in fields or key not in {"ipfs", "bzzr0", "bzzr1", "solc", "experimental"}:
            raise ValueError("duplicate or unsupported metadata key")
        if key == "experimental":
            if offset >= len(raw) or raw[offset] != 0xF5:
                raise ValueError("unsupported experimental flag")
            fields[key] = True
            offset += 1
            continue
        if key == "solc" and offset < len(raw) and raw[offset] >> 5 == 3:
            encoded, offset = _cbor_string(raw, offset, 3)
            version = encoded.decode("ascii")
            if not 1 <= len(version) <= 128 or any(not 33 <= ord(c) <= 126 for c in version):
                raise ValueError("unsupported prerelease version")
            fields[key] = version
            continue
        value, offset = _cbor_string(raw, offset, 2)
        if key == "ipfs":
            if len(value) != 34 or value[:2] != b"\x12\x20":
                raise ValueError("unsupported CID v0 multihash")
        elif len(value) != (3 if key == "solc" else 32):
            raise ValueError("incorrect metadata value width")
        fields[key] = value
    if offset != len(raw):
        raise ValueError("trailing CBOR bytes")
    if sum(key in fields for key in ("ipfs", "bzzr0", "bzzr1")) > 1:
        raise ValueError("ambiguous metadata content reference")
    return fields


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
