"""Bounded static proxy indicators, never a clean token or security approval.

PUSH payloads are not instructions. Valid dispatch shapes and storage-slot
operations are syntax observations, not control-flow reachability, ownership,
signature validity, implementation authentication or source reproduction.
"""

from dataclasses import dataclass, field

from eth_hash.auto import keccak

_SIGNATURES = (
    "implementation()",
    "admin()",
    "changeAdmin(address)",
    "upgradeTo(address)",
    "upgradeToAndCall(address,bytes)",
)
_SLOTS = {
    name: keccak(f"org.zeppelinos.proxy.{name}".encode()) for name in ("implementation", "admin")
}


@dataclass(frozen=True, slots=True)
class DispatchIndicator:
    selector: str
    candidate_signature: str
    push_offset: int
    destination: int


@dataclass(frozen=True, slots=True)
class StorageIndicator:
    slot_name: str
    slot: str
    push_offset: int
    operation: str


@dataclass(frozen=True, slots=True)
class ProxyIndicators:
    runtime_keccak: str
    runtime_length: int
    delegatecall_offsets: tuple[int, ...]
    dispatch: tuple[DispatchIndicator, ...]
    legacy_slot_operations: tuple[StorageIndicator, ...]
    truncated_push_offsets: tuple[int, ...]
    token_admitted: bool = field(default=False, init=False)
    execution_eligible: bool = field(default=False, init=False)


def inspect_proxy_indicators(code: bytes) -> ProxyIndicators:
    if type(code) is not bytes or not 1 <= len(code) <= 24576:
        raise ValueError("bounded nonempty runtime bytes required")
    instructions: list[tuple[int, int, bytes]] = []
    truncated = []
    offset = 0
    while offset < len(code):
        opcode = code[offset]
        width = opcode - 0x5F if 0x60 <= opcode <= 0x7F else 0
        end = offset + 1 + width
        if end > len(code):
            truncated.append(offset)
            break
        instructions.append((offset, opcode, code[offset + 1 : end]))
        offset = end
    boundaries = {pc: op for pc, op, _ in instructions}
    selectors = {keccak(s.encode())[:4]: s for s in _SIGNATURES}
    dispatch = []
    storage = []
    for index, (pc, op, data) in enumerate(instructions):
        tail = instructions[index + 1 : index + 4]
        if op == 0x63 and data in selectors and len(tail) == 3:
            eq, push, jump = tail
            destination = int.from_bytes(push[2])
            if (
                eq[1] == 0x14
                and 0x60 <= push[1] <= 0x7F
                and jump[1] == 0x57
                and boundaries.get(destination) == 0x5B
            ):
                dispatch.append(
                    DispatchIndicator("0x" + data.hex(), selectors[data], pc, destination)
                )
        if op == 0x7F and tail and tail[0][1] in (0x54, 0x55):
            for name, slot in _SLOTS.items():
                if data == slot:
                    storage.append(
                        StorageIndicator(
                            name, "0x" + slot.hex(), pc, "SLOAD" if tail[0][1] == 0x54 else "SSTORE"
                        )
                    )
    return ProxyIndicators(
        "0x" + keccak(code).hex(),
        len(code),
        tuple(pc for pc, op, _ in instructions if op == 0xF4),
        tuple(dispatch),
        tuple(storage),
        tuple(truncated),
    )
