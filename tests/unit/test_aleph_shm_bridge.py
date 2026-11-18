import os
import struct
import subprocess
from pathlib import Path

import pytest

from oasis_agent.fastpath import (
    AUDIT_FILE_SIZE,
    AUDIT_MAGIC,
    AUDIT_RING_CAPACITY,
    AUDIT_SLOT_SIZE,
    AuditRingError,
    FastpathBridge,
    FastpathCallError,
    OasisAuditRingConsumer,
    PoolKind,
    PoolNode,
    is_halted,
    read_audit_events,
    scan_cycles,
    trigger_halt,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEPH_NUM_SYMBOLS = 2048
ALEPH_NUM_EXCHANGES = 12
ALEPH_SLOT_SIZE = 64
ALEPH_VERSION_SIZE = 8
ALEPH_MAGIC = b"ALEPHTX_SHM_V12\x00"


def _addr(byte_hex: str) -> str:
    return "0x" + byte_hex * 20


def _ensure_release_cdylib() -> Path:
    crates_dir = REPO_ROOT / "crates"
    lib_name = "liboasis_core.dylib" if os.uname().sysname == "Darwin" else "liboasis_core.so"
    release_lib = crates_dir / "target" / "release" / lib_name
    if not release_lib.is_file():
        subprocess.run(
            [
                "cargo",
                "build",
                "--offline",
                "--manifest-path",
                str(crates_dir / "Cargo.toml"),
                "--release",
            ],
            cwd=REPO_ROOT,
            check=True,
        )
    return release_lib


@pytest.fixture(scope="module")
def bridge() -> FastpathBridge:
    return FastpathBridge(lib_path=_ensure_release_cdylib())


def _write_python_aleph_shm(
    path: Path,
    *,
    num_symbols: int,
    magic: bytes = ALEPH_MAGIC,
    slots: list[tuple[int, int, int, int, int, float, float, float, float, int]],
) -> None:
    total = (
        ALEPH_NUM_SYMBOLS * ALEPH_VERSION_SIZE + num_symbols * ALEPH_NUM_EXCHANGES * ALEPH_SLOT_SIZE
    )
    buf = bytearray(total)
    magic_offset = ALEPH_NUM_SYMBOLS * ALEPH_VERSION_SIZE + 48
    buf[magic_offset : magic_offset + 16] = magic

    for (
        sym_id,
        exch_id,
        msg_type,
        seqlock,
        ts_ns,
        bid_p,
        bid_s,
        ask_p,
        ask_s,
        ver,
    ) in slots:
        struct.pack_into("<Q", buf, sym_id * ALEPH_VERSION_SIZE, ver)
        slot_offset = (
            ALEPH_NUM_SYMBOLS * ALEPH_VERSION_SIZE
            + (sym_id * ALEPH_NUM_EXCHANGES + exch_id) * ALEPH_SLOT_SIZE
        )
        struct.pack_into(
            "<IBBHQdddd16s",
            buf,
            slot_offset,
            seqlock,
            msg_type,
            exch_id,
            sym_id,
            ts_ns,
            bid_p,
            bid_s,
            ask_p,
            ask_s,
            b"\x00" * 16,
        )
    path.write_bytes(buf)


def test_aleph_shm_binary_compatibility_and_rejections(
    tmp_path: Path, bridge: FastpathBridge
) -> None:
    shm_path = tmp_path / "aleph_bbo.shm"
    _write_python_aleph_shm(
        shm_path,
        num_symbols=4,
        slots=[
            # Valid slot: sym=1, exch=2, type=1, even seqlock=4
            (1, 2, 1, 4, 1_760_000_000_123, 3010.25, 12.5, 3010.75, 8.0, 9),
            # Odd seqlock (writer active / stuck)
            (1, 3, 1, 5, 1_760_000_000_124, 3010.0, 1.0, 3011.0, 1.0, 9),
            # Crossed book (ask < bid)
            (1, 4, 1, 2, 1_760_000_000_125, 3012.0, 1.0, 3010.0, 1.0, 9),
            # Non-positive bid price
            (1, 5, 1, 2, 1_760_000_000_126, 0.0, 1.0, 3010.0, 1.0, 9),
            # NaN price
            (1, 6, 1, 2, 1_760_000_000_127, float("nan"), 1.0, 3010.0, 1.0, 9),
            # Wrong msg_type
            (1, 7, 9, 2, 1_760_000_000_128, 3010.0, 1.0, 3011.0, 1.0, 9),
            # Zero timestamp (empty/invalidated)
            (1, 8, 1, 2, 0, 3010.0, 1.0, 3011.0, 1.0, 9),
        ],
    )

    valid = bridge.read_aleph_bbo(shm_path, 4, symbol_id=1, exchange_id=2)
    assert valid["seqlock"] == 4
    assert valid["msg_type"] == 1
    assert valid["exchange_id"] == 2
    assert valid["symbol_id"] == 1
    assert valid["timestamp_ns"] == 1_760_000_000_123
    assert valid["bid_price"] == pytest.approx(3010.25)
    assert valid["bid_size"] == pytest.approx(12.5)
    assert valid["ask_price"] == pytest.approx(3010.75)
    assert valid["ask_size"] == pytest.approx(8.0)

    with pytest.raises(FastpathCallError, match="seqlock stuck"):
        bridge.read_aleph_bbo(shm_path, 4, symbol_id=1, exchange_id=3)
    with pytest.raises(FastpathCallError, match="crossed book"):
        bridge.read_aleph_bbo(shm_path, 4, symbol_id=1, exchange_id=4)
    with pytest.raises(FastpathCallError, match="crossed book"):
        bridge.read_aleph_bbo(shm_path, 4, symbol_id=1, exchange_id=5)
    with pytest.raises(FastpathCallError, match="non-finite"):
        bridge.read_aleph_bbo(shm_path, 4, symbol_id=1, exchange_id=6)
    with pytest.raises(FastpathCallError, match="identity mismatch"):
        bridge.read_aleph_bbo(shm_path, 4, symbol_id=1, exchange_id=7)
    with pytest.raises(FastpathCallError, match="empty SHM slot"):
        bridge.read_aleph_bbo(shm_path, 4, symbol_id=1, exchange_id=8)

    # Reverse ABI check: Rust writes synthetic SHM, Python validates raw bytes and reads back
    rust_shm = tmp_path / "rust_written.shm"
    written = bridge.write_synthetic_aleph_shm(
        rust_shm,
        2,
        [
            {
                "symbol_id": 0,
                "exchange_id": 1,
                "msg_type": 1,
                "seqlock": 8,
                "timestamp_ns": 555_000,
                "bid_price": 1999.5,
                "bid_size": 2.5,
                "ask_price": 2000.5,
                "ask_size": 3.5,
                "version": 42,
            }
        ],
    )
    assert written == 1
    raw = rust_shm.read_bytes()
    magic_offset = ALEPH_NUM_SYMBOLS * ALEPH_VERSION_SIZE + 48
    assert raw[magic_offset : magic_offset + 16] == ALEPH_MAGIC
    slot_offset = ALEPH_NUM_SYMBOLS * ALEPH_VERSION_SIZE + ALEPH_SLOT_SIZE
    seqlock, msg_type, exch_id, sym_id, ts_ns, bid_p, bid_s, ask_p, ask_s, _ = struct.unpack_from(
        "<IBBHQdddd16s", raw, slot_offset
    )
    assert (seqlock, msg_type, exch_id, sym_id, ts_ns) == (8, 1, 1, 0, 555_000)
    assert (bid_p, bid_s, ask_p, ask_s) == (1999.5, 2.5, 2000.5, 3.5)

    # Bad magic rejected
    bad_magic_shm = tmp_path / "bad_magic.shm"
    _write_python_aleph_shm(bad_magic_shm, num_symbols=2, magic=b"WRONG_MAGIC_V01\x00", slots=[])
    with pytest.raises(FastpathCallError, match="magic mismatch"):
        bridge.read_aleph_bbo(bad_magic_shm, 2, symbol_id=0, exchange_id=1)


def test_oasis_audit_ring_consumer_and_halt_trip(tmp_path: Path, bridge: FastpathBridge) -> None:
    ring_path = tmp_path / "oasis_audit.ring"
    hdr_init = bridge.audit_ring_init(ring_path)
    assert hdr_init["capacity"] == AUDIT_RING_CAPACITY
    assert hdr_init["slot_size"] == AUDIT_SLOT_SIZE
    assert hdr_init["halt_flag"] == 0
    assert ring_path.stat().st_size == AUDIT_FILE_SIZE
    assert ring_path.read_bytes()[:16] == AUDIT_MAGIC

    consumer = OasisAuditRingConsumer()
    assert not is_halted(ring_path)
    assert consumer.read_events(ring_path) == ()

    # Build a 2-pool profitable V2 cycle and scan with audit_ring_path attached
    weth = _addr("11")
    usdc = _addr("22")
    pools = [
        PoolNode(
            chain_id=8453,
            pool_id=_addr("b1"),
            kind=PoolKind.V2,
            token0=weth,
            token1=usdc,
            decimals0=18,
            decimals1=6,
            fee_pips=500,
            reserve0=100 * 10**18,
            reserve1=300_000 * 10**6,
        ),
        PoolNode(
            chain_id=8453,
            pool_id=_addr("b2"),
            kind=PoolKind.V2,
            token0=weth,
            token1=usdc,
            decimals0=18,
            decimals1=6,
            fee_pips=500,
            reserve0=100 * 10**18,
            reserve1=260_000 * 10**6,
        ),
    ]
    report = scan_cycles(
        pools,
        [weth],
        max_hops=2,
        min_amount_in=10**16,
        max_amount_in=5 * 10**18,
        gas_cost_in_base=10**14,
        l1_fee_in_base=2 * 10**13,
        min_net_profit=10**14,
        audit_ring_path=ring_path,
        block_number=21_000_001,
        timestamp_ns=1_760_000_000_999,
        bridge=bridge,
    )
    assert len(report.candidates) == 1
    cand = report.candidates[0]

    events = read_audit_events(ring_path)
    assert len(events) == 1
    ev = events[0]
    assert ev.seq_id == 1
    assert ev.event_kind == 1
    assert ev.hop_count == 2
    assert ev.chain_id == 8453
    assert ev.block_number == 21_000_001
    assert ev.timestamp_ns == 1_760_000_000_999
    assert ev.amount_in == cand.optimal_amount_in
    assert ev.amount_out == cand.amount_out
    assert ev.net_profit == cand.net_profit

    # Emit a second scan and verify monotonic seq_id and limit slicing
    scan_cycles(
        pools,
        [weth],
        max_hops=2,
        min_amount_in=10**16,
        max_amount_in=5 * 10**18,
        gas_cost_in_base=10**14,
        l1_fee_in_base=2 * 10**13,
        min_net_profit=10**14,
        audit_ring_path=ring_path,
        block_number=21_000_002,
        timestamp_ns=1_760_000_001_000,
        bridge=bridge,
    )
    all_events = read_audit_events(ring_path)
    assert [e.seq_id for e in all_events] == [1, 2]
    latest_only = read_audit_events(ring_path, limit=1)
    assert [e.seq_id for e in latest_only] == [2]

    # Trip atomic halt_flag from Python consumer and verify Rust fails closed
    trigger_halt(ring_path, reason_code=9)
    assert is_halted(ring_path)
    assert consumer.read_header(ring_path).halt_flag == 9

    with pytest.raises(FastpathCallError, match="halted"):
        scan_cycles(
            pools,
            [weth],
            max_hops=2,
            min_amount_in=10**16,
            max_amount_in=5 * 10**18,
            gas_cost_in_base=10**14,
            l1_fee_in_base=2 * 10**13,
            min_net_profit=10**14,
            audit_ring_path=ring_path,
            block_number=21_000_003,
            timestamp_ns=1_760_000_001_001,
            bridge=bridge,
        )

    # Confirm no new audit slots were emitted after halt
    assert consumer.read_header(ring_path).write_idx == 2

    # Corrupt/truncated ring rejected
    bad_ring = tmp_path / "bad.ring"
    bad_ring.write_bytes(b"\x00" * 128)
    with pytest.raises(AuditRingError):
        consumer.read_header(bad_ring)
