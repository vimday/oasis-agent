"""Pure-Python AlephTX ALEPHTX_SHM_V12 mmap seqlock reader and read-only lakehouse probe."""

import math
import mmap
import re
import struct
import subprocess  # nosec B404 - fixed argv, shell=False, validated ssh_host
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Final, Self

NUM_SYMBOLS: Final[int] = 2048
NUM_EXCHANGES: Final[int] = 12
VERSION_SIZE: Final[int] = 8
SLOT_SIZE: Final[int] = 64
HEADER_BYTES: Final[int] = NUM_SYMBOLS * VERSION_SIZE  # 16,384 bytes
SHM_MAGIC_V12: Final[bytes] = b"ALEPHTX_SHM_V12\x00"
MAGIC_OFFSET: Final[int] = HEADER_BYTES + 48

SLOT_STRUCT: Final[struct.Struct] = struct.Struct("<IBBHQdddd16s")
SEQ_STRUCT: Final[struct.Struct] = struct.Struct("<I")
VERSION_STRUCT: Final[struct.Struct] = struct.Struct("<Q")

EXCHANGE_SLOT_MAP: Final[dict[int, str]] = {
    1: "hyperliquid",
    2: "lighter",
    6: "binance_usdm",
    7: "arcus",
    8: "lighter_robinhood",
    9: "entropy",
    10: "tradexyz",
    11: "extended",
}

_USD_MICRO_SCALE: Final[Decimal] = Decimal(1000000)
_SSH_HOST_RE: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}\Z")
_LAKEHOUSE_PROBE_CMD: Final[str] = "find ~/aleph-tx-lakehouse -mindepth 1 -maxdepth 2"


def _float_to_usd_micros(value: float, *, rounding: str) -> int:
    scaled = Decimal(str(value)) * _USD_MICRO_SCALE
    return int(scaled.to_integral_value(rounding=rounding))


@dataclass(frozen=True, slots=True)
class ShmBboQuote:
    """Validated BBO observation read from an AlephTX shared-memory matrix slot."""

    symbol_id: int
    exchange_id: int
    venue: str
    timestamp_ns: int
    age_ns: int
    seqlock: int
    bid_usd_micros: int
    ask_usd_micros: int
    bid_size: Decimal
    ask_size: Decimal

    @property
    def mid_usd_micros(self) -> int:
        return (self.bid_usd_micros + self.ask_usd_micros) // 2


@dataclass(frozen=True, slots=True)
class ShmReadResult:
    """Slot read outcome including explicit rejection status when invalid or stale."""

    status: str
    quote: ShmBboQuote | None
    age_ns: int | None = None


@dataclass(frozen=True, slots=True)
class SyntheticBboSlot:
    """Input record for building deterministic ALEPHTX_SHM_V12 test files."""

    exchange_id: int
    symbol_id: int
    timestamp_ns: int
    bid_price: float
    bid_size: float
    ask_price: float
    ask_size: float
    seqlock: int = 2
    msg_type: int = 1
    slot_exchange_id: int | None = None
    slot_symbol_id: int | None = None
    symbol_version: int = 1


@dataclass(frozen=True, slots=True)
class LakehouseProbeResult:
    """Read-only summary of remote ~/aleph-tx-lakehouse subdirectory entry counts."""

    ssh_host: str
    reachable: bool
    subdirectories: dict[str, int]
    error: str | None = None


def write_synthetic_aleph_shm_v12(
    path: str | Path,
    slots: Sequence[SyntheticBboSlot],
    *,
    num_symbols: int = NUM_SYMBOLS,
) -> Path:
    """Create a deterministic ALEPHTX_SHM_V12 shared-memory backing file for tests."""
    if type(num_symbols) is not int or not 1 <= num_symbols <= NUM_SYMBOLS:
        raise ValueError(f"num_symbols must be in 1..{NUM_SYMBOLS}")

    target = Path(path)
    total_bytes = HEADER_BYTES + num_symbols * NUM_EXCHANGES * SLOT_SIZE
    buf = bytearray(total_bytes)

    for slot in slots:
        if not 0 <= slot.symbol_id < num_symbols:
            raise ValueError(f"symbol_id {slot.symbol_id} out of range 0..{num_symbols - 1}")
        if not 0 <= slot.exchange_id < NUM_EXCHANGES:
            raise ValueError(f"exchange_id {slot.exchange_id} out of range 0..{NUM_EXCHANGES - 1}")
        slot_offset = HEADER_BYTES + (slot.symbol_id * NUM_EXCHANGES + slot.exchange_id) * SLOT_SIZE
        reserved = (
            SHM_MAGIC_V12 if (slot.symbol_id == 0 and slot.exchange_id == 0) else b"\x00" * 16
        )
        eff_exch = slot.exchange_id if slot.slot_exchange_id is None else slot.slot_exchange_id
        eff_sym = slot.symbol_id if slot.slot_symbol_id is None else slot.slot_symbol_id
        SLOT_STRUCT.pack_into(
            buf,
            slot_offset,
            slot.seqlock,
            slot.msg_type,
            eff_exch,
            eff_sym,
            slot.timestamp_ns,
            slot.bid_price,
            slot.bid_size,
            slot.ask_price,
            slot.ask_size,
            reserved,
        )
        ver_offset = slot.symbol_id * VERSION_SIZE
        existing_ver = int(VERSION_STRUCT.unpack_from(buf, ver_offset)[0])
        VERSION_STRUCT.pack_into(buf, ver_offset, max(existing_ver, slot.symbol_version))

    # Always stamp magic at BboMatrix[0][0]._Reserved (bytes HEADER_BYTES+48 .. +64).
    buf[MAGIC_OFFSET : MAGIC_OFFSET + len(SHM_MAGIC_V12)] = SHM_MAGIC_V12
    target.write_bytes(buf)
    return target


class AlephShmReader:
    """Read-only mmap seqlock consumer for AlephTX ALEPHTX_SHM_V12 matrices."""

    def __init__(self, path: str | Path, *, num_symbols: int = NUM_SYMBOLS) -> None:
        if type(num_symbols) is not int or not 1 <= num_symbols <= NUM_SYMBOLS:
            raise ValueError(f"num_symbols must be in 1..{NUM_SYMBOLS}")
        self._num_symbols = num_symbols
        expected_size = HEADER_BYTES + num_symbols * NUM_EXCHANGES * SLOT_SIZE
        with Path(path).open("rb") as fh:
            self._mmap = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        self._closed = False
        if len(self._mmap) < expected_size:
            self.close()
            raise ValueError("BBO shared memory is truncated")
        if self._mmap[MAGIC_OFFSET : MAGIC_OFFSET + len(SHM_MAGIC_V12)] != SHM_MAGIC_V12:
            self.close()
            raise ValueError("BBO shared memory ABI magic mismatch")

    @classmethod
    def open(cls, path: str | Path, *, num_symbols: int = NUM_SYMBOLS) -> Self:
        return cls(path, num_symbols=num_symbols)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if not getattr(self, "_closed", True):
            self._mmap.close()
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValueError("AlephShmReader is closed")

    def read_symbol_version(self, symbol_id: int) -> int:
        self._ensure_open()
        if type(symbol_id) is not int or not 0 <= symbol_id < self._num_symbols:
            raise ValueError("symbol_id out of bounds")
        return int(VERSION_STRUCT.unpack_from(self._mmap, symbol_id * VERSION_SIZE)[0])

    def read_slot_with_status(
        self,
        symbol_id: int,
        exchange_id: int,
        *,
        now_ns: int,
        max_age_ns: int,
        max_future_skew_ns: int = 1_000_000_000,
        max_retries: int = 8,
    ) -> ShmReadResult:
        self._ensure_open()
        if type(now_ns) is not int or now_ns < 0:
            raise ValueError("now_ns must be a nonnegative integer")
        if type(max_age_ns) is not int or max_age_ns < 0:
            raise ValueError("max_age_ns must be a nonnegative integer")
        if type(max_future_skew_ns) is not int or max_future_skew_ns < 0:
            raise ValueError("max_future_skew_ns must be a nonnegative integer")
        if type(max_retries) is not int or max_retries <= 0:
            raise ValueError("max_retries must be positive")
        if type(symbol_id) is not int or not 0 <= symbol_id < self._num_symbols:
            return ShmReadResult(status="invalid_symbol", quote=None)
        if type(exchange_id) is not int or exchange_id not in EXCHANGE_SLOT_MAP:
            return ShmReadResult(status="unknown_exchange", quote=None)

        offset = HEADER_BYTES + (symbol_id * NUM_EXCHANGES + exchange_id) * SLOT_SIZE
        last_status = "torn_read"
        for _ in range(max_retries):
            seq1 = int(SEQ_STRUCT.unpack_from(self._mmap, offset)[0])
            if seq1 & 1 != 0:
                last_status = "odd_seqlock"
                continue
            (
                raw_seq,
                msg_type,
                slot_exch,
                slot_sym,
                timestamp_ns,
                bid_price,
                bid_size,
                ask_price,
                ask_size,
                _reserved,
            ) = SLOT_STRUCT.unpack_from(self._mmap, offset)
            seq2 = int(SEQ_STRUCT.unpack_from(self._mmap, offset)[0])
            if raw_seq != seq1 or seq1 != seq2 or seq2 & 1 != 0:
                last_status = "torn_read"
                continue

            if timestamp_ns == 0:
                return ShmReadResult(status="empty", quote=None)
            if msg_type != 1 or slot_exch != exchange_id or slot_sym != symbol_id:
                return ShmReadResult(status="identity_mismatch", quote=None)
            if any(
                not math.isfinite(val) or val <= 0.0
                for val in (bid_price, bid_size, ask_price, ask_size)
            ):
                return ShmReadResult(status="invalid_float", quote=None)
            if ask_price < bid_price:
                return ShmReadResult(status="crossed_book", quote=None)
            if timestamp_ns > now_ns + max_future_skew_ns:
                return ShmReadResult(status="future_timestamp", quote=None)

            age_ns = max(0, now_ns - timestamp_ns)
            if age_ns > max_age_ns:
                return ShmReadResult(status="stale", quote=None, age_ns=age_ns)

            bid_usd_micros = _float_to_usd_micros(bid_price, rounding=ROUND_FLOOR)
            ask_usd_micros = _float_to_usd_micros(ask_price, rounding=ROUND_CEILING)
            if bid_usd_micros <= 0 or ask_usd_micros <= 0:
                return ShmReadResult(status="invalid_price_precision", quote=None)

            quote = ShmBboQuote(
                symbol_id=symbol_id,
                exchange_id=exchange_id,
                venue=EXCHANGE_SLOT_MAP[exchange_id],
                timestamp_ns=timestamp_ns,
                age_ns=age_ns,
                seqlock=seq1,
                bid_usd_micros=bid_usd_micros,
                ask_usd_micros=ask_usd_micros,
                bid_size=Decimal(str(bid_size)),
                ask_size=Decimal(str(ask_size)),
            )
            return ShmReadResult(status="ok", quote=quote, age_ns=age_ns)

        return ShmReadResult(status=last_status, quote=None)

    def read_slot(
        self,
        symbol_id: int,
        exchange_id: int,
        *,
        now_ns: int,
        max_age_ns: int,
        max_future_skew_ns: int = 1_000_000_000,
        max_retries: int = 8,
    ) -> ShmBboQuote | None:
        return self.read_slot_with_status(
            symbol_id,
            exchange_id,
            now_ns=now_ns,
            max_age_ns=max_age_ns,
            max_future_skew_ns=max_future_skew_ns,
            max_retries=max_retries,
        ).quote

    def read_all_exchanges(
        self,
        symbol_id: int,
        *,
        now_ns: int,
        max_age_ns: int,
        max_future_skew_ns: int = 1_000_000_000,
    ) -> dict[str, ShmBboQuote]:
        quotes: dict[str, ShmBboQuote] = {}
        for exchange_id, venue in EXCHANGE_SLOT_MAP.items():
            quote = self.read_slot(
                symbol_id,
                exchange_id,
                now_ns=now_ns,
                max_age_ns=max_age_ns,
                max_future_skew_ns=max_future_skew_ns,
            )
            if quote is not None:
                quotes[venue] = quote
        return quotes


def probe_remote_aleph_lakehouse(
    ssh_host: str = "aleph", timeout_s: int = 5
) -> LakehouseProbeResult:
    """Strictly read-only inspection of ~/aleph-tx-lakehouse subdirectory names and counts."""
    if not isinstance(ssh_host, str) or _SSH_HOST_RE.fullmatch(ssh_host) is None:
        raise ValueError("invalid ssh_host alias")
    if type(timeout_s) is not int or not 1 <= timeout_s <= 60:
        raise ValueError("timeout_s must be an integer in 1..60")

    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={timeout_s}",
        ssh_host,
        _LAKEHOUSE_PROBE_CMD,
    ]
    try:
        proc = subprocess.run(  # nosec B603 - validated host alias, fixed read-only command
            argv,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s + 2,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return LakehouseProbeResult(
            ssh_host=ssh_host,
            reachable=False,
            subdirectories={},
            error=type(exc).__name__,
        )

    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:200] or f"ssh_exit_{proc.returncode}"
        return LakehouseProbeResult(
            ssh_host=ssh_host,
            reachable=False,
            subdirectories={},
            error=err,
        )

    counts: dict[str, int] = {}
    marker = "aleph-tx-lakehouse/"
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if marker not in line:
            continue
        rel = line.split(marker, 1)[1].strip("/")
        if not rel:
            continue
        if "/" not in rel:
            counts.setdefault(rel, 0)
        else:
            subdir, _, _child = rel.partition("/")
            if subdir:
                counts[subdir] = counts.get(subdir, 0) + 1

    return LakehouseProbeResult(
        ssh_host=ssh_host,
        reachable=True,
        subdirectories=dict(sorted(counts.items())),
        error=None,
    )


__all__ = [
    "EXCHANGE_SLOT_MAP",
    "HEADER_BYTES",
    "MAGIC_OFFSET",
    "NUM_EXCHANGES",
    "NUM_SYMBOLS",
    "SHM_MAGIC_V12",
    "SLOT_SIZE",
    "VERSION_SIZE",
    "AlephShmReader",
    "LakehouseProbeResult",
    "ShmBboQuote",
    "ShmReadResult",
    "SyntheticBboSlot",
    "probe_remote_aleph_lakehouse",
    "write_synthetic_aleph_shm_v12",
]
