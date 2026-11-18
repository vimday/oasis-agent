import mmap
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from oasis_agent.errors import OasisError

from .bridge import require_int

AUDIT_MAGIC: Final[bytes] = b"OASIS_AUDIT_V01\x00"
AUDIT_RING_CAPACITY: Final[int] = 1024
AUDIT_HEADER_SIZE: Final[int] = 64
AUDIT_SLOT_SIZE: Final[int] = 128
AUDIT_FILE_SIZE: Final[int] = AUDIT_HEADER_SIZE + AUDIT_RING_CAPACITY * AUDIT_SLOT_SIZE
_HALT_FLAG_OFFSET: Final[int] = 32
_HEADER_STRUCT: Final[struct.Struct] = struct.Struct("<16sQQIIII16s")
_SLOT_STRUCT: Final[struct.Struct] = struct.Struct("<IHHQQQQ32s16s16s16sII")


class AuditRingError(OasisError):
    """Raised when reading or updating the OasisAuditRing mmap fails validation."""


@dataclass(frozen=True, slots=True)
class AuditRingHeader:
    write_idx: int
    generation: int
    halt_flag: int
    capacity: int
    slot_size: int


@dataclass(frozen=True, slots=True)
class AuditEvent:
    seqlock: int
    event_kind: int
    hop_count: int
    seq_id: int
    timestamp_ns: int
    chain_id: int
    block_number: int
    route_hash_hex: str
    amount_in: int
    amount_out: int
    net_profit: int
    flags: int
    reason_code: int


def _decode_header(mm: mmap.mmap) -> AuditRingHeader:
    if len(mm) < AUDIT_FILE_SIZE:
        raise AuditRingError(
            f"audit ring file truncated: expected >= {AUDIT_FILE_SIZE} bytes, got {len(mm)}"
        )
    magic, write_idx, generation, halt_flag, capacity, slot_size, _pad, _reserved = (
        _HEADER_STRUCT.unpack_from(mm, 0)
    )
    if magic != AUDIT_MAGIC:
        raise AuditRingError("audit ring ABI magic mismatch")
    if capacity != AUDIT_RING_CAPACITY or slot_size != AUDIT_SLOT_SIZE:
        raise AuditRingError(
            f"audit ring geometry mismatch: capacity={capacity}, slot_size={slot_size}"
        )
    return AuditRingHeader(
        write_idx=write_idx,
        generation=generation,
        halt_flag=halt_flag,
        capacity=capacity,
        slot_size=slot_size,
    )


def _decode_slot(mm: mmap.mmap, slot_idx: int) -> AuditEvent | None:
    offset = AUDIT_HEADER_SIZE + slot_idx * AUDIT_SLOT_SIZE
    for _ in range(100):
        seq1 = struct.unpack_from("<I", mm, offset)[0]
        if seq1 & 1:
            continue
        if seq1 == 0:
            return None
        raw = _SLOT_STRUCT.unpack_from(mm, offset)
        seq2 = struct.unpack_from("<I", mm, offset)[0]
        if seq1 != seq2:
            continue
        (
            seqlock,
            event_kind,
            hop_count,
            seq_id,
            timestamp_ns,
            chain_id,
            block_number,
            route_hash,
            amount_in_raw,
            amount_out_raw,
            net_profit_raw,
            flags,
            reason_code,
        ) = raw
        return AuditEvent(
            seqlock=seqlock,
            event_kind=event_kind,
            hop_count=hop_count,
            seq_id=seq_id,
            timestamp_ns=timestamp_ns,
            chain_id=chain_id,
            block_number=block_number,
            route_hash_hex="0x" + route_hash.hex(),
            amount_in=int.from_bytes(amount_in_raw, "little", signed=False),
            amount_out=int.from_bytes(amount_out_raw, "little", signed=False),
            net_profit=int.from_bytes(net_profit_raw, "little", signed=True),
            flags=flags,
            reason_code=reason_code,
        )
    raise AuditRingError(f"audit ring slot {slot_idx} remained contended across retries")


class OasisAuditRingConsumer:
    """Pure-Python mmap reader and fail-closed halt controller for `OasisAuditRing`."""

    def read_header(self, path: Path) -> AuditRingHeader:
        try:
            with path.open("rb") as handle:
                mm = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
                try:
                    return _decode_header(mm)
                finally:
                    mm.close()
        except OSError as exc:
            raise AuditRingError(f"failed to read audit ring {path}: {exc}") from exc
        except ValueError as exc:
            raise AuditRingError(f"invalid audit ring mapping {path}: {exc}") from exc

    def is_halted(self, path: Path) -> bool:
        return self.read_header(path).halt_flag != 0

    def trigger_halt(self, path: Path, *, reason_code: int = 1) -> None:
        code = require_int(reason_code, "reason_code", non_negative=True)
        if code == 0:
            code = 1
        try:
            with path.open("r+b") as handle:
                mm = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_WRITE)
                try:
                    _decode_header(mm)
                    struct.pack_into("<I", mm, _HALT_FLAG_OFFSET, code)
                    mm.flush()
                finally:
                    mm.close()
        except OSError as exc:
            raise AuditRingError(f"failed to trip halt on {path}: {exc}") from exc
        except ValueError as exc:
            raise AuditRingError(f"invalid audit ring mapping {path}: {exc}") from exc

    def read_events(self, path: Path, *, limit: int | None = None) -> tuple[AuditEvent, ...]:
        if limit is not None:
            require_int(limit, "limit", non_negative=True)
        try:
            with path.open("rb") as handle:
                mm = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
                try:
                    header = _decode_header(mm)
                    if header.write_idx == 0:
                        return ()
                    start_idx = max(0, header.write_idx - AUDIT_RING_CAPACITY)
                    events: list[AuditEvent] = []
                    for idx in range(start_idx, header.write_idx):
                        slot = _decode_slot(mm, idx % AUDIT_RING_CAPACITY)
                        if slot is not None and slot.seq_id == idx + 1:
                            events.append(slot)
                    if limit is not None and limit < len(events):
                        return tuple(events[-limit:])
                    return tuple(events)
                finally:
                    mm.close()
        except OSError as exc:
            raise AuditRingError(f"failed to read audit events from {path}: {exc}") from exc
        except ValueError as exc:
            raise AuditRingError(f"invalid audit ring mapping {path}: {exc}") from exc


def is_halted(path: Path) -> bool:
    """Return True if the OasisAuditRing `halt_flag` at `path` is non-zero."""
    return OasisAuditRingConsumer().is_halted(path)


def trigger_halt(path: Path, *, reason_code: int = 1) -> None:
    """Atomically set `halt_flag` in the OasisAuditRing header at `path`."""
    OasisAuditRingConsumer().trigger_halt(path, reason_code=reason_code)


def read_audit_events(path: Path, *, limit: int | None = None) -> tuple[AuditEvent, ...]:
    """Decode committed audit slots from an OasisAuditRing file."""
    return OasisAuditRingConsumer().read_events(path, limit=limit)
