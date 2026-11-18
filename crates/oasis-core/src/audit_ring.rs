use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::Path;
use std::sync::atomic::{AtomicU32, AtomicU64, Ordering, fence};

use memmap2::{Mmap, MmapMut};
use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::graph_arb::CycleCandidate;

pub const MAGIC: [u8; 16] = *b"OASIS_AUDIT_V01\0";
pub const AUDIT_RING_CAPACITY: usize = 1024;
pub const AUDIT_HEADER_SIZE: usize = 64;
pub const AUDIT_SLOT_SIZE: usize = 128;
pub const AUDIT_FILE_SIZE: usize = AUDIT_HEADER_SIZE + AUDIT_RING_CAPACITY * AUDIT_SLOT_SIZE;

pub const EVENT_EXECUTION_CANDIDATE: u16 = 1;
pub const EVENT_CANDIDATE_REJECTED: u16 = 2;
pub const EVENT_HALT_TRIGGERED: u16 = 3;

#[derive(Debug, Error)]
pub enum AuditRingError {
    #[error("audit ring is halted (halt_flag={flag}); execution candidate emission blocked")]
    Halted { flag: u32 },
    #[error("audit ring file truncated: expected >= {expected} bytes, got {actual}")]
    Truncated { expected: usize, actual: usize },
    #[error("audit ring mapping is not 64-byte aligned")]
    UnalignedMapping,
    #[error("audit ring ABI magic mismatch")]
    MagicMismatch,
    #[error("audit ring header mismatch: capacity={capacity}, slot_size={slot_size}")]
    HeaderMismatch { capacity: u32, slot_size: u32 },
    #[error("audit ring slot index {0} out of bounds")]
    SlotOutOfBounds(usize),
    #[error("audit ring slot {0} seqlock torn or stuck")]
    SlotContended(usize),
    #[error("audit ring slot {0} is empty")]
    EmptySlot(usize),
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
}

#[repr(C, align(64))]
pub struct AuditRingHeader {
    pub magic: [u8; 16],
    pub write_idx: AtomicU64,
    pub generation: AtomicU64,
    pub halt_flag: AtomicU32,
    pub capacity: u32,
    pub slot_size: u32,
    pub _pad: u32,
    pub _reserved: [u8; 16],
}

const _: () = {
    assert!(std::mem::size_of::<AuditRingHeader>() == AUDIT_HEADER_SIZE);
    assert!(std::mem::align_of::<AuditRingHeader>() == 64);
    assert!(std::mem::offset_of!(AuditRingHeader, magic) == 0);
    assert!(std::mem::offset_of!(AuditRingHeader, write_idx) == 16);
    assert!(std::mem::offset_of!(AuditRingHeader, generation) == 24);
    assert!(std::mem::offset_of!(AuditRingHeader, halt_flag) == 32);
    assert!(std::mem::offset_of!(AuditRingHeader, capacity) == 36);
    assert!(std::mem::offset_of!(AuditRingHeader, slot_size) == 40);
    assert!(std::mem::offset_of!(AuditRingHeader, _pad) == 44);
    assert!(std::mem::offset_of!(AuditRingHeader, _reserved) == 48);
};

#[repr(C, align(64))]
#[derive(Clone, Copy, Debug)]
pub struct AuditEventSlot {
    pub seqlock: u32,
    pub event_kind: u16,
    pub hop_count: u16,
    pub seq_id: u64,
    pub timestamp_ns: u64,
    pub chain_id: u64,
    pub block_number: u64,
    pub route_hash: [u8; 32],
    pub amount_in_le: [u8; 16],
    pub amount_out_le: [u8; 16],
    pub net_profit_le: [u8; 16],
    pub flags: u32,
    pub reason_code: u32,
}

const _: () = {
    assert!(std::mem::size_of::<AuditEventSlot>() == AUDIT_SLOT_SIZE);
    assert!(std::mem::align_of::<AuditEventSlot>() == 64);
    assert!(std::mem::offset_of!(AuditEventSlot, seqlock) == 0);
    assert!(std::mem::offset_of!(AuditEventSlot, event_kind) == 4);
    assert!(std::mem::offset_of!(AuditEventSlot, hop_count) == 6);
    assert!(std::mem::offset_of!(AuditEventSlot, seq_id) == 8);
    assert!(std::mem::offset_of!(AuditEventSlot, timestamp_ns) == 16);
    assert!(std::mem::offset_of!(AuditEventSlot, chain_id) == 24);
    assert!(std::mem::offset_of!(AuditEventSlot, block_number) == 32);
    assert!(std::mem::offset_of!(AuditEventSlot, route_hash) == 40);
    assert!(std::mem::offset_of!(AuditEventSlot, amount_in_le) == 72);
    assert!(std::mem::offset_of!(AuditEventSlot, amount_out_le) == 88);
    assert!(std::mem::offset_of!(AuditEventSlot, net_profit_le) == 104);
    assert!(std::mem::offset_of!(AuditEventSlot, flags) == 120);
    assert!(std::mem::offset_of!(AuditEventSlot, reason_code) == 124);
};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AuditHeaderSnapshot {
    pub write_idx: u64,
    pub generation: u64,
    pub halt_flag: u32,
    pub capacity: u32,
    pub slot_size: u32,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AuditEventRecord {
    pub seqlock: u32,
    pub event_kind: u16,
    pub hop_count: u16,
    pub seq_id: u64,
    pub timestamp_ns: u64,
    pub chain_id: u64,
    pub block_number: u64,
    pub route_hash_hex: String,
    pub amount_in: String,
    pub amount_out: String,
    pub net_profit: String,
    pub flags: u32,
    pub reason_code: u32,
}

fn validate_header_bytes(buf: &[u8]) -> Result<(), AuditRingError> {
    if buf.len() < AUDIT_FILE_SIZE {
        return Err(AuditRingError::Truncated {
            expected: AUDIT_FILE_SIZE,
            actual: buf.len(),
        });
    }
    if !(buf.as_ptr() as usize).is_multiple_of(64) {
        return Err(AuditRingError::UnalignedMapping);
    }
    if buf[0..16] != MAGIC {
        return Err(AuditRingError::MagicMismatch);
    }
    let capacity = u32::from_le_bytes(buf[36..40].try_into().expect("slice len 4"));
    let slot_size = u32::from_le_bytes(buf[40..44].try_into().expect("slice len 4"));
    if capacity != AUDIT_RING_CAPACITY as u32 || slot_size != AUDIT_SLOT_SIZE as u32 {
        return Err(AuditRingError::HeaderMismatch {
            capacity,
            slot_size,
        });
    }
    Ok(())
}

pub struct OasisAuditRing {
    mmap: MmapMut,
}

// SAFETY: `OasisAuditRing` owns an exclusive `MmapMut` and performs atomic header/slot updates.
unsafe impl Send for OasisAuditRing {}

impl OasisAuditRing {
    pub fn create_or_open(path: impl AsRef<Path>) -> Result<Self, AuditRingError> {
        let path_ref = path.as_ref();
        let needs_init = match std::fs::metadata(path_ref) {
            Ok(meta) => meta.len() == 0,
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => true,
            Err(err) => return Err(AuditRingError::Io(err)),
        };

        if needs_init {
            let mut file = OpenOptions::new()
                .create(true)
                .truncate(true)
                .read(true)
                .write(true)
                .open(path_ref)?;
            let mut buf = vec![0u8; AUDIT_FILE_SIZE];
            buf[0..16].copy_from_slice(&MAGIC);
            buf[24..32].copy_from_slice(&1u64.to_le_bytes()); // generation = 1
            buf[36..40].copy_from_slice(&(AUDIT_RING_CAPACITY as u32).to_le_bytes());
            buf[40..44].copy_from_slice(&(AUDIT_SLOT_SIZE as u32).to_le_bytes());
            file.write_all(&buf)?;
            file.sync_all()?;
        }

        let file = OpenOptions::new().read(true).write(true).open(path_ref)?;
        // SAFETY: File is sized to `AUDIT_FILE_SIZE` and mapped for shared ring updates.
        let mmap = unsafe { MmapMut::map_mut(&file)? };
        validate_header_bytes(&mmap)?;
        Ok(Self { mmap })
    }

    fn header(&self) -> &AuditRingHeader {
        // SAFETY: `validate_header_bytes` verified size >= AUDIT_FILE_SIZE and 64B alignment.
        unsafe { &*self.mmap.as_ptr().cast::<AuditRingHeader>() }
    }

    #[must_use]
    pub fn header_snapshot(&self) -> AuditHeaderSnapshot {
        let hdr = self.header();
        AuditHeaderSnapshot {
            write_idx: u64::from_le(hdr.write_idx.load(Ordering::Acquire)),
            generation: u64::from_le(hdr.generation.load(Ordering::Acquire)),
            halt_flag: u32::from_le(hdr.halt_flag.load(Ordering::Acquire)),
            capacity: u32::from_le(hdr.capacity),
            slot_size: u32::from_le(hdr.slot_size),
        }
    }

    /// Fails closed whenever `halt_flag != 0`.
    pub fn check_not_halted(&self) -> Result<(), AuditRingError> {
        let flag = u32::from_le(self.header().halt_flag.load(Ordering::Acquire));
        if flag != 0 {
            return Err(AuditRingError::Halted { flag });
        }
        Ok(())
    }

    pub fn trigger_halt(&mut self, reason_code: u32) -> Result<u64, AuditRingError> {
        let code = reason_code.max(1);
        self.header()
            .halt_flag
            .store(code.to_le(), Ordering::Release);
        fence(Ordering::SeqCst);
        let seq_id = self.append_raw_event(
            EVENT_HALT_TRIGGERED,
            0,
            0,
            0,
            0,
            [0u8; 32],
            0,
            0,
            0,
            0,
            code,
        )?;
        self.mmap.flush()?;
        Ok(seq_id)
    }

    pub fn emit_candidate(
        &mut self,
        candidate: &CycleCandidate,
        block_number: u64,
        timestamp_ns: u64,
    ) -> Result<u64, AuditRingError> {
        self.check_not_halted()?;

        let mut flags = 0u32;
        let amount_in = match candidate.optimal_amount_in.to_u128() {
            Some(v) => v,
            None => {
                flags |= 1;
                u128::MAX
            }
        };
        let amount_out = match candidate.amount_out.to_u128() {
            Some(v) => v,
            None => {
                flags |= 1;
                u128::MAX
            }
        };
        let net_profit = match candidate.net_profit.to_u128() {
            Some(v) if v <= (i128::MAX as u128) => v as i128,
            _ => {
                flags |= 1;
                i128::MAX
            }
        };
        let hop_count = u16::try_from(candidate.hops.len()).unwrap_or(u16::MAX);
        let route_hash = candidate.route_hash();

        // Re-check halt right before advancing write_idx.
        self.check_not_halted()?;
        let seq_id = self.append_raw_event(
            EVENT_EXECUTION_CANDIDATE,
            hop_count,
            timestamp_ns,
            candidate.chain_id,
            block_number,
            route_hash,
            amount_in,
            amount_out,
            net_profit,
            flags,
            0,
        )?;
        self.mmap.flush()?;
        Ok(seq_id)
    }

    #[allow(clippy::too_many_arguments)]
    fn append_raw_event(
        &mut self,
        event_kind: u16,
        hop_count: u16,
        timestamp_ns: u64,
        chain_id: u64,
        block_number: u64,
        route_hash: [u8; 32],
        amount_in: u128,
        amount_out: u128,
        net_profit: i128,
        flags: u32,
        reason_code: u32,
    ) -> Result<u64, AuditRingError> {
        let idx = u64::from_le(self.header().write_idx.fetch_add(1, Ordering::AcqRel));
        let seq_id = idx.saturating_add(1);
        let slot_idx = (idx as usize) % AUDIT_RING_CAPACITY;
        let slot_offset = AUDIT_HEADER_SIZE + slot_idx * AUDIT_SLOT_SIZE;
        if idx > 0 && idx.is_multiple_of(AUDIT_RING_CAPACITY as u64) {
            self.header().generation.fetch_add(1, Ordering::AcqRel);
        }

        // SAFETY: `slot_offset` is within `AUDIT_FILE_SIZE` and 64B-aligned.
        let slot_ptr = unsafe {
            self.mmap
                .as_mut_ptr()
                .add(slot_offset)
                .cast::<AuditEventSlot>()
        };
        let seq_ptr = slot_ptr.cast::<AtomicU32>();

        // SAFETY: `seq_ptr` points to the 4B seqlock at offset 0 of the aligned slot.
        unsafe {
            let cur_even = u32::from_le((*seq_ptr).load(Ordering::Acquire)) & !1u32;
            let odd = cur_even.wrapping_add(1);
            let next_even = cur_even.wrapping_add(2);
            (*seq_ptr).store(odd.to_le(), Ordering::Release);
            fence(Ordering::Release);

            let slot = &mut *slot_ptr;
            slot.event_kind = event_kind.to_le();
            slot.hop_count = hop_count.to_le();
            slot.seq_id = seq_id.to_le();
            slot.timestamp_ns = timestamp_ns.to_le();
            slot.chain_id = chain_id.to_le();
            slot.block_number = block_number.to_le();
            slot.route_hash = route_hash;
            slot.amount_in_le = amount_in.to_le_bytes();
            slot.amount_out_le = amount_out.to_le_bytes();
            slot.net_profit_le = net_profit.to_le_bytes();
            slot.flags = flags.to_le();
            slot.reason_code = reason_code.to_le();

            fence(Ordering::Release);
            (*seq_ptr).store(next_even.to_le(), Ordering::Release);
        }

        Ok(seq_id)
    }
}

pub struct AuditRingReader {
    mmap: Mmap,
}

impl AuditRingReader {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, AuditRingError> {
        let file = File::open(path)?;
        // SAFETY: File is mapped read-only for audit consumption.
        let mmap = unsafe { Mmap::map(&file)? };
        validate_header_bytes(&mmap)?;
        Ok(Self { mmap })
    }

    #[must_use]
    pub fn header(&self) -> AuditHeaderSnapshot {
        // SAFETY: `validate_header_bytes` verified bounds and 64B alignment.
        let hdr = unsafe { &*self.mmap.as_ptr().cast::<AuditRingHeader>() };
        AuditHeaderSnapshot {
            write_idx: u64::from_le(hdr.write_idx.load(Ordering::Acquire)),
            generation: u64::from_le(hdr.generation.load(Ordering::Acquire)),
            halt_flag: u32::from_le(hdr.halt_flag.load(Ordering::Acquire)),
            capacity: u32::from_le(hdr.capacity),
            slot_size: u32::from_le(hdr.slot_size),
        }
    }

    pub fn check_not_halted(&self) -> Result<(), AuditRingError> {
        let flag = self.header().halt_flag;
        if flag != 0 {
            return Err(AuditRingError::Halted { flag });
        }
        Ok(())
    }

    pub fn read_slot(&self, slot_idx: usize) -> Result<AuditEventRecord, AuditRingError> {
        if slot_idx >= AUDIT_RING_CAPACITY {
            return Err(AuditRingError::SlotOutOfBounds(slot_idx));
        }
        let slot_offset = AUDIT_HEADER_SIZE + slot_idx * AUDIT_SLOT_SIZE;
        // SAFETY: `slot_offset` is within validated bounds and 64B aligned.
        let slot_ptr = unsafe { self.mmap.as_ptr().add(slot_offset).cast::<AuditEventSlot>() };
        let seq_ptr = slot_ptr.cast::<AtomicU32>();

        for _ in 0..1_000 {
            // SAFETY: `seq_ptr` points to the 4B seqlock at offset 0 of the aligned slot.
            let seq1 = u32::from_le(unsafe { (*seq_ptr).load(Ordering::Acquire) });
            if (seq1 & 1) != 0 {
                std::hint::spin_loop();
                continue;
            }
            if seq1 == 0 {
                return Err(AuditRingError::EmptySlot(slot_idx));
            }
            // SAFETY: `slot_ptr` is within bounds and 64B aligned; seqlock guards consistency.
            let copy = unsafe { std::ptr::read_volatile(slot_ptr) };
            fence(Ordering::Acquire);
            let seq2 = u32::from_le(unsafe { (*seq_ptr).load(Ordering::Acquire) });
            if seq1 == seq2 {
                return Ok(AuditEventRecord {
                    seqlock: seq1,
                    event_kind: u16::from_le(copy.event_kind),
                    hop_count: u16::from_le(copy.hop_count),
                    seq_id: u64::from_le(copy.seq_id),
                    timestamp_ns: u64::from_le(copy.timestamp_ns),
                    chain_id: u64::from_le(copy.chain_id),
                    block_number: u64::from_le(copy.block_number),
                    route_hash_hex: format!("0x{}", hex::encode(copy.route_hash)),
                    amount_in: u128::from_le_bytes(copy.amount_in_le).to_string(),
                    amount_out: u128::from_le_bytes(copy.amount_out_le).to_string(),
                    net_profit: i128::from_le_bytes(copy.net_profit_le).to_string(),
                    flags: u32::from_le(copy.flags),
                    reason_code: u32::from_le(copy.reason_code),
                });
            }
            std::hint::spin_loop();
        }
        Err(AuditRingError::SlotContended(slot_idx))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::graph_arb::{CycleHop, PoolKind};
    use crate::u256_math::U256;

    fn temp_ring_path(tag: &str) -> std::path::PathBuf {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!("oasis-audit-ring-{tag}-{nanos}.bin"))
    }

    fn sample_candidate() -> CycleCandidate {
        let weth = format!("0x{}", "11".repeat(20));
        let usdc = format!("0x{}", "22".repeat(20));
        CycleCandidate {
            chain_id: 8453,
            base_token: weth.clone(),
            hops: vec![
                CycleHop {
                    pool_id: format!("0x{}", "a1".repeat(20)),
                    kind: PoolKind::V2,
                    token_in: weth.clone(),
                    token_out: usdc.clone(),
                    amount_in: U256::from_u128(1_000_000_000_000_000_000),
                    amount_out: U256::from_u128(2_500_000_000),
                },
                CycleHop {
                    pool_id: format!("0x{}", "a2".repeat(20)),
                    kind: PoolKind::V3,
                    token_in: usdc,
                    token_out: weth,
                    amount_in: U256::from_u128(2_500_000_000),
                    amount_out: U256::from_u128(1_050_000_000_000_000_000),
                },
            ],
            optimal_amount_in: U256::from_u128(1_000_000_000_000_000_000),
            amount_out: U256::from_u128(1_050_000_000_000_000_000),
            gas_cost_in_base: U256::from_u128(1_000_000_000_000_000),
            l1_fee_in_base: U256::from_u128(200_000_000_000_000),
            net_profit: U256::from_u128(48_800_000_000_000_000),
        }
    }

    #[test]
    fn emits_events_and_fails_closed_when_halted() {
        let path = temp_ring_path("halt");
        let mut ring = OasisAuditRing::create_or_open(&path).unwrap();
        let cand = sample_candidate();
        let seq = ring.emit_candidate(&cand, 42_000, 999_000_000).unwrap();
        assert_eq!(seq, 1);

        let reader = AuditRingReader::open(&path).unwrap();
        let ev = reader.read_slot(0).unwrap();
        assert_eq!(ev.seq_id, 1);
        assert_eq!(ev.event_kind, EVENT_EXECUTION_CANDIDATE);
        assert_eq!(ev.chain_id, 8453);
        assert_eq!(ev.net_profit, "48800000000000000");

        ring.trigger_halt(7).unwrap();
        assert!(matches!(
            ring.check_not_halted(),
            Err(AuditRingError::Halted { flag: 7 })
        ));
        assert!(matches!(
            ring.emit_candidate(&cand, 42_001, 999_000_001),
            Err(AuditRingError::Halted { flag: 7 })
        ));
        assert!(matches!(
            reader.check_not_halted(),
            Err(AuditRingError::Halted { flag: 7 })
        ));

        std::fs::remove_file(path).unwrap();
    }
}
