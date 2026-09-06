from __future__ import annotations

from collections import Counter

from ..models.common import EvidenceLevel
from ..models.evm import FeatureObservation, FeatureSummary
from .bytecode import bytecode_hash, extract_cbor_metadata
from .selectors import extract_push4_selectors

_OPCODES = {
    0x54: "SLOAD",
    0x55: "SSTORE",
    0xF1: "CALL",
    0xF2: "CALLCODE",
    0xF4: "DELEGATECALL",
    0xFA: "STATICCALL",
    0xF0: "CREATE",
    0xFF: "SELFDESTRUCT",
}


def scan_features(bytecode: bytes) -> FeatureSummary:
    counts: Counter[str] = Counter()
    addresses: set[str] = set()
    i = 0
    while i < len(bytecode):
        opcode = bytecode[i]
        i += 1
        name = _OPCODES.get(opcode)
        if name:
            counts[name] += 1
        if opcode == 0x73:
            if i + 20 <= len(bytecode):
                addresses.add("0x" + bytecode[i : i + 20].hex())
                i += 20
            else:
                i = len(bytecode)
        elif 0x60 <= opcode <= 0x7F:
            i = min(len(bytecode), i + opcode - 0x5F)
    metadata = extract_cbor_metadata(bytecode)
    observations: list[FeatureObservation] = []
    if counts["SSTORE"]:
        observations.append(
            FeatureObservation("state mutation", EvidenceLevel.CONFIRMED, "SSTORE present")
        )
    if counts["DELEGATECALL"]:
        observations.append(
            FeatureObservation("delegate call", EvidenceLevel.CONFIRMED, "DELEGATECALL present")
        )
    if counts["CALL"] or counts["STATICCALL"]:
        observations.append(
            FeatureObservation("external call", EvidenceLevel.CONFIRMED, "CALL-like opcode present")
        )
    if bytecode.find(bytes.fromhex("400000")) >= 0:
        observations.append(
            FeatureObservation(
                "fee override indicator", EvidenceLevel.INFERRED, "override flag constant present"
            )
        )
    return FeatureSummary(
        opcode_counts=dict(counts),
        hardcoded_addresses=tuple(sorted(addresses)),
        selectors=extract_push4_selectors(bytecode),
        observations=tuple(observations),
        metadata={"ipfs_cid": metadata.ipfs_cid or "", "solc_version": metadata.solc_version or ""},
        bytecode_hash=bytecode_hash(bytecode),
        byte_length=len(bytecode),
    )
