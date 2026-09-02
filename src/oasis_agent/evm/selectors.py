from __future__ import annotations

from dataclasses import dataclass

from ..models.evm import SelectorObservation


@dataclass(frozen=True, slots=True)
class SelectorTable:
    selectors: tuple[SelectorObservation, ...]


def extract_push4_selectors(bytecode: bytes) -> tuple[SelectorObservation, ...]:
    result: list[SelectorObservation] = []
    i = 0
    while i < len(bytecode):
        offset = i
        opcode = bytecode[i]
        i += 1
        if 0x60 <= opcode <= 0x7F:
            size = opcode - 0x5F
            if opcode == 0x63 and i + 4 <= len(bytecode):
                result.append(SelectorObservation("0x" + bytecode[i:i + 4].hex(), offset, None))
            i += size
        elif opcode == 0x63 and i + 4 <= len(bytecode):
            result.append(SelectorObservation("0x" + bytecode[i:i + 4].hex(), offset, None))
            i += 4
    return tuple(result)
