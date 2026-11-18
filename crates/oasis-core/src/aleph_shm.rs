use std::fs::OpenOptions;
use std::io::Write;
use std::path::Path;
use std::sync::atomic::{AtomicU32, AtomicU64, Ordering, fence};

use memmap2::Mmap;
use serde::{Deserialize, Serialize};
use thiserror::Error;

pub const NUM_SYMBOLS: usize = 2048;
pub const NUM_EXCHANGES: usize = 12;
pub const SHM_MAGIC_V12: [u8; 16] = *b"ALEPHTX_SHM_V12\0";
pub const SLOT_SIZE: usize = 64;
pub const VERSION_SIZE: usize = 8;
const MAX_SEQLOCK_SPINS: u32 = 10_000;

#[derive(Debug, Error)]
pub enum AlephShmError {
    #[error("invalid symbol count {0}: must be in 1..={NUM_SYMBOLS}")]
    InvalidSymbolCount(usize),
    #[error("symbol_id {symbol_id} or exchange_id {exchange_id} out of bounds")]
    OutOfBounds { symbol_id: u16, exchange_id: u8 },
    #[error("shared memory file truncated: expected >= {expected} bytes, got {actual}")]
    Truncated { expected: usize, actual: usize },
    #[error("shared memory mapping base address is not 64-byte aligned")]
    UnalignedMapping,
    #[error("BBO shared memory ABI magic mismatch")]
    MagicMismatch,
    #[error("seqlock stuck (odd seqlock {seq}) on symbol={symbol_id} exch={exchange_id}")]
    SeqlockStuck {
        symbol_id: u16,
        exchange_id: u8,
        seq: u32,
    },
    #[error("seqlock torn read limit exceeded on symbol={symbol_id} exch={exchange_id}")]
    SeqlockTorn { symbol_id: u16, exchange_id: u8 },
    #[error("empty SHM slot (timestamp_ns == 0) for symbol={symbol_id} exch={exchange_id}")]
    EmptySlot { symbol_id: u16, exchange_id: u8 },
    #[error("non-finite floating point field in SHM BBO slot")]
    NonFiniteField,
    #[error("invalid quote or crossed book (bid={bid_price}, ask={ask_price})")]
    InvalidQuoteSpread { bid_price: f64, ask_price: f64 },
    #[error("invalid negative size in SHM BBO slot")]
    NegativeSize,
    #[error(
        "SHM slot identity mismatch: expected (type=1, exch={expected_exch}, sym={expected_sym}), got (type={actual_type}, exch={actual_exch}, sym={actual_sym})"
    )]
    IdentityMismatch {
        expected_exch: u8,
        expected_sym: u16,
        actual_type: u8,
        actual_exch: u8,
        actual_sym: u16,
    },
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
}

#[repr(C, align(64))]
#[derive(Clone, Copy, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct ShmBboMessage {
    pub seqlock: u32,
    pub msg_type: u8,
    pub exchange_id: u8,
    pub symbol_id: u16,
    pub timestamp_ns: u64,
    pub bid_price: f64,
    pub bid_size: f64,
    pub ask_price: f64,
    pub ask_size: f64,
    pub _reserved: [u8; 16],
}

const _: () = {
    assert!(std::mem::size_of::<ShmBboMessage>() == SLOT_SIZE);
    assert!(std::mem::align_of::<ShmBboMessage>() == 64);
    assert!(std::mem::offset_of!(ShmBboMessage, seqlock) == 0);
    assert!(std::mem::offset_of!(ShmBboMessage, msg_type) == 4);
    assert!(std::mem::offset_of!(ShmBboMessage, exchange_id) == 5);
    assert!(std::mem::offset_of!(ShmBboMessage, symbol_id) == 6);
    assert!(std::mem::offset_of!(ShmBboMessage, timestamp_ns) == 8);
    assert!(std::mem::offset_of!(ShmBboMessage, bid_price) == 16);
    assert!(std::mem::offset_of!(ShmBboMessage, bid_size) == 24);
    assert!(std::mem::offset_of!(ShmBboMessage, ask_price) == 32);
    assert!(std::mem::offset_of!(ShmBboMessage, ask_size) == 40);
    assert!(std::mem::offset_of!(ShmBboMessage, _reserved) == 48);
};

#[inline(always)]
#[deny(unsafe_op_in_unsafe_fn)]
unsafe fn load_bbo_payload_relaxed(slot_ptr: *const ShmBboMessage, seqlock: u32) -> ShmBboMessage {
    // SAFETY: `slot_ptr` points to a 64-byte-aligned `ShmBboMessage` within the validated
    // shared mapping. Offset 4 is u32-aligned and offsets 8..48 are u64-aligned.
    unsafe {
        let w = u32::from_le((*slot_ptr.byte_add(4).cast::<AtomicU32>()).load(Ordering::Relaxed));
        let timestamp_ns =
            u64::from_le((*slot_ptr.byte_add(8).cast::<AtomicU64>()).load(Ordering::Relaxed));
        let bid_price = f64::from_bits(u64::from_le(
            (*slot_ptr.byte_add(16).cast::<AtomicU64>()).load(Ordering::Relaxed),
        ));
        let bid_size = f64::from_bits(u64::from_le(
            (*slot_ptr.byte_add(24).cast::<AtomicU64>()).load(Ordering::Relaxed),
        ));
        let ask_price = f64::from_bits(u64::from_le(
            (*slot_ptr.byte_add(32).cast::<AtomicU64>()).load(Ordering::Relaxed),
        ));
        let ask_size = f64::from_bits(u64::from_le(
            (*slot_ptr.byte_add(40).cast::<AtomicU64>()).load(Ordering::Relaxed),
        ));

        ShmBboMessage {
            seqlock,
            msg_type: (w & 0xff) as u8,
            exchange_id: ((w >> 8) & 0xff) as u8,
            symbol_id: (w >> 16) as u16,
            timestamp_ns,
            bid_price,
            bid_size,
            ask_price,
            ask_size,
            _reserved: [0; 16],
        }
    }
}

pub struct AlephShmReader {
    _mmap: Mmap,
    data: *const u8,
    local_versions: [u64; NUM_SYMBOLS],
    max_symbols: usize,
    next_symbol: usize,
    torn_retries: u64,
    exhausted_spins: u64,
}

// SAFETY: `AlephShmReader` owns a read-only `Mmap` whose underlying memory outlives `data`
// and only performs atomic loads against the mapped region.
unsafe impl Send for AlephShmReader {}

impl AlephShmReader {
    pub fn open(path: impl AsRef<Path>, num_symbols: usize) -> Result<Self, AlephShmError> {
        if !(1..=NUM_SYMBOLS).contains(&num_symbols) {
            return Err(AlephShmError::InvalidSymbolCount(num_symbols));
        }
        let file = std::fs::File::open(path)?;
        // SAFETY: The backing file is opened read-only and must not be truncated while mapped.
        let mmap = unsafe { Mmap::map(&file)? };
        let expected_size = NUM_SYMBOLS * VERSION_SIZE + num_symbols * NUM_EXCHANGES * SLOT_SIZE;
        if mmap.len() < expected_size {
            return Err(AlephShmError::Truncated {
                expected: expected_size,
                actual: mmap.len(),
            });
        }
        let data = mmap.as_ptr();
        if !(data as usize).is_multiple_of(64) {
            return Err(AlephShmError::UnalignedMapping);
        }
        let magic_offset = NUM_SYMBOLS * VERSION_SIZE + 48;
        if mmap[magic_offset..magic_offset + 16] != SHM_MAGIC_V12 {
            return Err(AlephShmError::MagicMismatch);
        }
        Ok(Self {
            _mmap: mmap,
            data,
            local_versions: [0u64; NUM_SYMBOLS],
            max_symbols: num_symbols,
            next_symbol: 0,
            torn_retries: 0,
            exhausted_spins: 0,
        })
    }

    #[must_use]
    pub fn load_version(&self, symbol_id: u16) -> u64 {
        if (symbol_id as usize) >= self.max_symbols {
            return 0;
        }
        let offset = (symbol_id as usize) * VERSION_SIZE;
        // SAFETY: `offset` is within the validated `[0, NUM_SYMBOLS * VERSION_SIZE)` header and 8B aligned.
        unsafe {
            let ptr = self.data.add(offset).cast::<AtomicU64>();
            u64::from_le((*ptr).load(Ordering::Acquire))
        }
    }

    pub fn try_poll(&mut self) -> Option<u16> {
        for _ in 0..self.max_symbols {
            let sym = self.next_symbol;
            self.next_symbol = (sym + 1) % self.max_symbols;
            let sym_id = sym as u16;
            let version = self.load_version(sym_id);
            if version != self.local_versions[sym] {
                self.local_versions[sym] = version;
                return Some(sym_id);
            }
        }
        None
    }

    pub fn read_slot(
        &mut self,
        symbol_id: u16,
        exchange_id: u8,
    ) -> Result<ShmBboMessage, AlephShmError> {
        if (symbol_id as usize) >= self.max_symbols || (exchange_id as usize) >= NUM_EXCHANGES {
            return Err(AlephShmError::OutOfBounds {
                symbol_id,
                exchange_id,
            });
        }
        let version = self.load_version(symbol_id);
        self.local_versions[symbol_id as usize] = version;

        let base = NUM_SYMBOLS * VERSION_SIZE;
        let offset =
            base + ((symbol_id as usize) * NUM_EXCHANGES + (exchange_id as usize)) * SLOT_SIZE;
        // SAFETY: Bounds were validated in `open()` and above; mapping and slot offsets are 64B aligned.
        let slot_ptr = unsafe { self.data.add(offset).cast::<ShmBboMessage>() };
        let seq_ptr = slot_ptr.cast::<AtomicU32>();

        let mut spin_count: u32 = 0;
        let msg = loop {
            // SAFETY: `seq_ptr` points to a 64B-aligned `AtomicU32` within the validated mapping.
            let seq1 = u32::from_le(unsafe { (*seq_ptr).load(Ordering::Acquire) });
            if (seq1 & 1) != 0 {
                spin_count += 1;
                if spin_count > MAX_SEQLOCK_SPINS {
                    self.exhausted_spins = self.exhausted_spins.saturating_add(1);
                    return Err(AlephShmError::SeqlockStuck {
                        symbol_id,
                        exchange_id,
                        seq: seq1,
                    });
                }
                std::hint::spin_loop();
                continue;
            }

            // SAFETY: `slot_ptr` is within bounds and 64B aligned.
            let candidate = unsafe { load_bbo_payload_relaxed(slot_ptr, seq1) };
            fence(Ordering::Acquire);
            // SAFETY: `seq_ptr` points to the slot's seqlock word.
            let seq2 = u32::from_le(unsafe { (*seq_ptr).load(Ordering::Acquire) });
            if seq1 == seq2 {
                break candidate;
            }

            self.torn_retries = self.torn_retries.saturating_add(1);
            spin_count += 1;
            if spin_count > MAX_SEQLOCK_SPINS {
                self.exhausted_spins = self.exhausted_spins.saturating_add(1);
                return Err(AlephShmError::SeqlockTorn {
                    symbol_id,
                    exchange_id,
                });
            }
            std::hint::spin_loop();
        };

        if msg.timestamp_ns == 0 {
            return Err(AlephShmError::EmptySlot {
                symbol_id,
                exchange_id,
            });
        }
        if msg.msg_type != 1 || msg.exchange_id != exchange_id || msg.symbol_id != symbol_id {
            return Err(AlephShmError::IdentityMismatch {
                expected_exch: exchange_id,
                expected_sym: symbol_id,
                actual_type: msg.msg_type,
                actual_exch: msg.exchange_id,
                actual_sym: msg.symbol_id,
            });
        }
        if !msg.bid_price.is_finite()
            || !msg.ask_price.is_finite()
            || !msg.bid_size.is_finite()
            || !msg.ask_size.is_finite()
        {
            return Err(AlephShmError::NonFiniteField);
        }
        if msg.bid_price <= 0.0 || msg.ask_price < msg.bid_price {
            return Err(AlephShmError::InvalidQuoteSpread {
                bid_price: msg.bid_price,
                ask_price: msg.ask_price,
            });
        }
        if msg.bid_size < 0.0 || msg.ask_size < 0.0 {
            return Err(AlephShmError::NegativeSize);
        }

        Ok(msg)
    }

    pub fn read_all_exchanges(
        &mut self,
        symbol_id: u16,
    ) -> [(u8, Option<ShmBboMessage>); NUM_EXCHANGES] {
        let mut out = [(0u8, None); NUM_EXCHANGES];
        for (exch, slot) in out.iter_mut().enumerate() {
            let exch_id = exch as u8;
            let val = self.read_slot(symbol_id, exch_id).ok();
            *slot = (exch_id, val);
        }
        out
    }

    #[must_use]
    pub fn local_version(&self, symbol_id: u16) -> u64 {
        self.local_versions
            .get(symbol_id as usize)
            .copied()
            .unwrap_or(0)
    }

    #[must_use]
    pub fn torn_retries(&self) -> u64 {
        self.torn_retries
    }

    #[must_use]
    pub fn exhausted_spins(&self) -> u64 {
        self.exhausted_spins
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct SyntheticBboUpdate {
    pub symbol_id: u16,
    pub exchange_id: u8,
    pub msg_type: u8,
    pub seqlock: u32,
    pub timestamp_ns: u64,
    pub bid_price: f64,
    pub bid_size: f64,
    pub ask_price: f64,
    pub ask_size: f64,
    pub version: u64,
}

/// Write a synthetic `ALEPHTX_SHM_V12\0` shared-memory file for deterministic testing.
pub fn write_synthetic_shm_v12(
    path: impl AsRef<Path>,
    num_symbols: usize,
    updates: &[SyntheticBboUpdate],
) -> Result<(), AlephShmError> {
    if !(1..=NUM_SYMBOLS).contains(&num_symbols) {
        return Err(AlephShmError::InvalidSymbolCount(num_symbols));
    }
    let total_len = NUM_SYMBOLS * VERSION_SIZE + num_symbols * NUM_EXCHANGES * SLOT_SIZE;
    let mut buf = vec![0u8; total_len];
    let magic_offset = NUM_SYMBOLS * VERSION_SIZE + 48;
    buf[magic_offset..magic_offset + 16].copy_from_slice(&SHM_MAGIC_V12);

    for upd in updates {
        if (upd.symbol_id as usize) >= num_symbols || (upd.exchange_id as usize) >= NUM_EXCHANGES {
            return Err(AlephShmError::OutOfBounds {
                symbol_id: upd.symbol_id,
                exchange_id: upd.exchange_id,
            });
        }
        let ver_off = (upd.symbol_id as usize) * VERSION_SIZE;
        buf[ver_off..ver_off + 8].copy_from_slice(&upd.version.to_le_bytes());

        let slot_off = NUM_SYMBOLS * VERSION_SIZE
            + ((upd.symbol_id as usize) * NUM_EXCHANGES + (upd.exchange_id as usize)) * SLOT_SIZE;
        buf[slot_off..slot_off + 4].copy_from_slice(&upd.seqlock.to_le_bytes());
        buf[slot_off + 4] = upd.msg_type;
        buf[slot_off + 5] = upd.exchange_id;
        buf[slot_off + 6..slot_off + 8].copy_from_slice(&upd.symbol_id.to_le_bytes());
        buf[slot_off + 8..slot_off + 16].copy_from_slice(&upd.timestamp_ns.to_le_bytes());
        buf[slot_off + 16..slot_off + 24].copy_from_slice(&upd.bid_price.to_le_bytes());
        buf[slot_off + 24..slot_off + 32].copy_from_slice(&upd.bid_size.to_le_bytes());
        buf[slot_off + 32..slot_off + 40].copy_from_slice(&upd.ask_price.to_le_bytes());
        buf[slot_off + 40..slot_off + 48].copy_from_slice(&upd.ask_size.to_le_bytes());
    }

    let mut file = OpenOptions::new()
        .create(true)
        .truncate(true)
        .write(true)
        .open(path)?;
    file.write_all(&buf)?;
    file.sync_all()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::AtomicBool;
    use std::sync::{Arc, Barrier};

    use super::*;

    fn temp_shm_path(tag: &str) -> std::path::PathBuf {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!("oasis-aleph-shm-{tag}-{nanos}.bin"))
    }

    #[test]
    fn validates_magic_and_rejects_bad_slots() {
        let path = temp_shm_path("validation");
        write_synthetic_shm_v12(
            &path,
            4,
            &[
                SyntheticBboUpdate {
                    symbol_id: 1,
                    exchange_id: 1,
                    msg_type: 1,
                    seqlock: 2,
                    timestamp_ns: 123_456_789,
                    bid_price: 2500.5,
                    bid_size: 4.0,
                    ask_price: 2501.0,
                    ask_size: 5.0,
                    version: 1,
                },
                SyntheticBboUpdate {
                    symbol_id: 1,
                    exchange_id: 2,
                    msg_type: 1,
                    seqlock: 3, // odd seqlock -> stuck
                    timestamp_ns: 123_456_789,
                    bid_price: 2500.5,
                    bid_size: 4.0,
                    ask_price: 2501.0,
                    ask_size: 5.0,
                    version: 1,
                },
                SyntheticBboUpdate {
                    symbol_id: 1,
                    exchange_id: 3,
                    msg_type: 1,
                    seqlock: 2,
                    timestamp_ns: 123_456_789,
                    bid_price: 2505.0,
                    bid_size: 1.0,
                    ask_price: 2500.0, // crossed book!
                    ask_size: 1.0,
                    version: 1,
                },
                SyntheticBboUpdate {
                    symbol_id: 1,
                    exchange_id: 4,
                    msg_type: 1,
                    seqlock: 2,
                    timestamp_ns: 123_456_789,
                    bid_price: f64::NAN,
                    bid_size: 1.0,
                    ask_price: 2500.0,
                    ask_size: 1.0,
                    version: 1,
                },
                SyntheticBboUpdate {
                    symbol_id: 1,
                    exchange_id: 5,
                    msg_type: 2, // wrong msg_type
                    seqlock: 2,
                    timestamp_ns: 123_456_789,
                    bid_price: 2500.0,
                    bid_size: 1.0,
                    ask_price: 2501.0,
                    ask_size: 1.0,
                    version: 1,
                },
            ],
        )
        .unwrap();

        let mut reader = AlephShmReader::open(&path, 4).unwrap();
        let valid = reader.read_slot(1, 1).unwrap();
        assert_eq!(valid.bid_price, 2500.5);
        assert_eq!(valid.ask_price, 2501.0);

        assert!(matches!(
            reader.read_slot(1, 2),
            Err(AlephShmError::SeqlockStuck { .. })
        ));
        assert!(matches!(
            reader.read_slot(1, 3),
            Err(AlephShmError::InvalidQuoteSpread { .. })
        ));
        assert!(matches!(
            reader.read_slot(1, 4),
            Err(AlephShmError::NonFiniteField)
        ));
        assert!(matches!(
            reader.read_slot(1, 5),
            Err(AlephShmError::IdentityMismatch { .. })
        ));
        assert!(matches!(
            reader.read_slot(1, 6),
            Err(AlephShmError::EmptySlot { .. })
        ));

        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn concurrent_seqlock_stress_never_tears() {
        const ITERATIONS: u64 = 10_000;
        const SYM: u16 = 1;
        const EXCH: u8 = 2;

        let path = temp_shm_path("stress");
        write_synthetic_shm_v12(&path, 2, &[]).unwrap();

        let mut reader = AlephShmReader::open(&path, 2).unwrap();
        let barrier = Arc::new(Barrier::new(2));
        let done = Arc::new(AtomicBool::new(false));

        let w_path = path.clone();
        let w_barrier = Arc::clone(&barrier);
        let w_done = Arc::clone(&done);

        let writer = std::thread::spawn(move || {
            let file = OpenOptions::new()
                .read(true)
                .write(true)
                .open(&w_path)
                .unwrap();
            let mut mmap = unsafe { memmap2::MmapMut::map_mut(&file).unwrap() };
            let base_ptr = mmap.as_mut_ptr();
            let slot_offset = NUM_SYMBOLS * VERSION_SIZE
                + ((SYM as usize) * NUM_EXCHANGES + (EXCH as usize)) * SLOT_SIZE;
            w_barrier.wait();

            for step in 1..=ITERATIONS {
                let bid_price = 1000.0 + (step % 500) as f64;
                let bid_size = bid_price * 2.0;
                let ask_price = bid_price + 1.0;
                let ask_size = bid_price * 3.0;
                let ts_ns = step * 10;
                let header = 1u32 | (u32::from(EXCH) << 8) | (u32::from(SYM) << 16);
                unsafe {
                    let slot_ptr = base_ptr.add(slot_offset).cast::<ShmBboMessage>();
                    let seq_ptr = slot_ptr.cast::<AtomicU32>();
                    let prev = (*seq_ptr).fetch_add(1, Ordering::AcqRel);
                    fence(Ordering::Release);
                    (*slot_ptr.byte_add(4).cast::<AtomicU32>())
                        .store(header.to_le(), Ordering::Release);
                    (*slot_ptr.byte_add(8).cast::<AtomicU64>())
                        .store(ts_ns.to_le(), Ordering::Release);
                    (*slot_ptr.byte_add(16).cast::<AtomicU64>())
                        .store(bid_price.to_bits().to_le(), Ordering::Release);
                    (*slot_ptr.byte_add(24).cast::<AtomicU64>())
                        .store(bid_size.to_bits().to_le(), Ordering::Release);
                    (*slot_ptr.byte_add(32).cast::<AtomicU64>())
                        .store(ask_price.to_bits().to_le(), Ordering::Release);
                    (*slot_ptr.byte_add(40).cast::<AtomicU64>())
                        .store(ask_size.to_bits().to_le(), Ordering::Release);
                    (*seq_ptr).store(prev.wrapping_add(2), Ordering::Release);
                    let ver_ptr = base_ptr
                        .add((SYM as usize) * VERSION_SIZE)
                        .cast::<AtomicU64>();
                    (*ver_ptr).fetch_add(1, Ordering::Release);
                }
            }
            w_done.store(true, Ordering::Release);
        });

        barrier.wait();
        while !done.load(Ordering::Acquire) {
            if let Ok(msg) = reader.read_slot(SYM, EXCH) {
                let step = msg.timestamp_ns / 10;
                let expected_bid = 1000.0 + (step % 500) as f64;
                assert_eq!(msg.bid_price, expected_bid);
                assert_eq!(msg.ask_price, msg.bid_price + 1.0);
                assert_eq!(msg.bid_size, msg.bid_price * 2.0);
                assert_eq!(msg.ask_size, msg.bid_price * 3.0);
            }
        }
        writer.join().unwrap();

        let final_msg = reader.read_slot(SYM, EXCH).unwrap();
        assert_eq!(final_msg.timestamp_ns, ITERATIONS * 10);
        std::fs::remove_file(path).unwrap();
    }
}
