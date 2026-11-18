use std::ffi::CString;
use std::os::raw::c_char;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::path::Path;

use serde::{Deserialize, Serialize};

use crate::aleph_shm::{AlephShmReader, SyntheticBboUpdate, write_synthetic_shm_v12};
use crate::audit_ring::{AuditHeaderSnapshot, OasisAuditRing};
use crate::graph_arb::{CycleCandidate, CycleScanReport, PoolGraph, PoolState};
use crate::pool_math::{
    amount0_delta, amount1_delta, amounts_for_liquidity, serde_u128_str, sqrt_price_at_tick,
};
use crate::u256_math::U256;

const MAX_FFI_JSON_BYTES: usize = 64 * 1024 * 1024;

#[derive(Debug, Clone, Deserialize)]
pub struct ScanCyclesInput {
    pub pools: Vec<PoolState>,
    pub base_tokens: Vec<String>,
    pub max_hops: usize,
    pub min_amount_in: U256,
    pub max_amount_in: U256,
    pub gas_cost_in_base: U256,
    pub l1_fee_in_base: U256,
    pub min_net_profit: U256,
    #[serde(default)]
    pub audit_ring_path: Option<String>,
    #[serde(default)]
    pub block_number: Option<u64>,
    #[serde(default)]
    pub timestamp_ns: Option<u64>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct SyntheticBboUpdateDto {
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

#[derive(Debug, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
enum FfiRequest {
    Ping,
    SqrtPriceAtTick {
        tick: i32,
    },
    AmountDeltas {
        sqrt_price_a_x96: U256,
        sqrt_price_b_x96: U256,
        #[serde(with = "serde_u128_str")]
        liquidity: u128,
        round_up: bool,
    },
    AmountsForLiquidity {
        sqrt_price_x96: U256,
        sqrt_price_lower_x96: U256,
        sqrt_price_upper_x96: U256,
        #[serde(with = "serde_u128_str")]
        liquidity: u128,
        round_up: bool,
    },
    QuotePool {
        pool: PoolState,
        token_in: String,
        amount_in: U256,
    },
    ScanCycles(ScanCyclesInput),
    ReadAlephBbo {
        path: String,
        num_symbols: usize,
        symbol_id: u16,
        exchange_id: u8,
    },
    WriteSyntheticAlephShm {
        path: String,
        num_symbols: usize,
        updates: Vec<SyntheticBboUpdateDto>,
    },
    AuditRingInit {
        path: String,
    },
    AuditRingEmit {
        path: String,
        candidate: CycleCandidate,
        block_number: u64,
        timestamp_ns: u64,
    },
}

#[derive(Serialize)]
struct FfiSuccess<T: Serialize> {
    ok: bool,
    result: T,
}

#[derive(Serialize)]
struct FfiError {
    ok: bool,
    error: String,
}

#[derive(Serialize)]
struct PingResult {
    service: &'static str,
    version: &'static str,
}

#[derive(Serialize)]
struct SqrtPriceResult {
    sqrt_price_x96: U256,
}

#[derive(Serialize)]
struct AmountDeltasResult {
    amount0: U256,
    amount1: U256,
}

#[derive(Serialize)]
struct AlephBboResult {
    seqlock: u32,
    msg_type: u8,
    exchange_id: u8,
    symbol_id: u16,
    timestamp_ns: u64,
    bid_price: f64,
    bid_size: f64,
    ask_price: f64,
    ask_size: f64,
}

#[derive(Serialize)]
struct AuditEmitResult {
    seq_id: u64,
}

pub fn execute_scan_cycles(input: ScanCyclesInput) -> anyhow::Result<CycleScanReport> {
    let mut ring = match input.audit_ring_path.as_deref() {
        Some(path) => {
            let r = OasisAuditRing::create_or_open(Path::new(path))?;
            r.check_not_halted()?;
            Some(r)
        }
        None => None,
    };

    let mut graph = PoolGraph::new();
    for pool in input.pools {
        graph.upsert_pool(pool)?;
    }

    let report = graph.scan_cycles(
        &input.base_tokens,
        input.max_hops,
        input.min_amount_in,
        input.max_amount_in,
        input.gas_cost_in_base,
        input.l1_fee_in_base,
        input.min_net_profit,
    )?;

    if let Some(ref mut audit_ring) = ring {
        let block_num = input.block_number.unwrap_or(0);
        let ts_ns = input.timestamp_ns.unwrap_or(0);
        for candidate in &report.candidates {
            audit_ring.emit_candidate(candidate, block_num, ts_ns)?;
        }
    }

    Ok(report)
}

fn eval_json_inner(raw: &str) -> anyhow::Result<String> {
    let req: FfiRequest = serde_json::from_str(raw)?;
    match req {
        FfiRequest::Ping => serialize_ok(PingResult {
            service: "oasis-core",
            version: env!("CARGO_PKG_VERSION"),
        }),
        FfiRequest::SqrtPriceAtTick { tick } => {
            let sqrt_price_x96 = sqrt_price_at_tick(tick)?;
            serialize_ok(SqrtPriceResult { sqrt_price_x96 })
        }
        FfiRequest::AmountDeltas {
            sqrt_price_a_x96,
            sqrt_price_b_x96,
            liquidity,
            round_up,
        } => {
            let amount0 = amount0_delta(sqrt_price_a_x96, sqrt_price_b_x96, liquidity, round_up)?;
            let amount1 = amount1_delta(sqrt_price_a_x96, sqrt_price_b_x96, liquidity, round_up)?;
            serialize_ok(AmountDeltasResult { amount0, amount1 })
        }
        FfiRequest::AmountsForLiquidity {
            sqrt_price_x96,
            sqrt_price_lower_x96,
            sqrt_price_upper_x96,
            liquidity,
            round_up,
        } => {
            let (amount0, amount1) = amounts_for_liquidity(
                sqrt_price_x96,
                sqrt_price_lower_x96,
                sqrt_price_upper_x96,
                liquidity,
                round_up,
            )?;
            serialize_ok(AmountDeltasResult { amount0, amount1 })
        }
        FfiRequest::QuotePool {
            pool,
            token_in,
            amount_in,
        } => {
            let chain_id = pool.chain_id;
            let pool_id = pool.pool_id.clone();
            let mut graph = PoolGraph::new();
            graph.upsert_pool(pool)?;
            let out = graph.quote_pool(chain_id, &pool_id, &token_in, amount_in)?;
            serialize_ok(out)
        }
        FfiRequest::ScanCycles(input) => {
            let report = execute_scan_cycles(input)?;
            serialize_ok(report)
        }
        FfiRequest::ReadAlephBbo {
            path,
            num_symbols,
            symbol_id,
            exchange_id,
        } => {
            let mut reader = AlephShmReader::open(Path::new(&path), num_symbols)?;
            let msg = reader.read_slot(symbol_id, exchange_id)?;
            serialize_ok(AlephBboResult {
                seqlock: msg.seqlock,
                msg_type: msg.msg_type,
                exchange_id: msg.exchange_id,
                symbol_id: msg.symbol_id,
                timestamp_ns: msg.timestamp_ns,
                bid_price: msg.bid_price,
                bid_size: msg.bid_size,
                ask_price: msg.ask_price,
                ask_size: msg.ask_size,
            })
        }
        FfiRequest::WriteSyntheticAlephShm {
            path,
            num_symbols,
            updates,
        } => {
            let mapped: Vec<SyntheticBboUpdate> = updates
                .into_iter()
                .map(|u| SyntheticBboUpdate {
                    symbol_id: u.symbol_id,
                    exchange_id: u.exchange_id,
                    msg_type: u.msg_type,
                    seqlock: u.seqlock,
                    timestamp_ns: u.timestamp_ns,
                    bid_price: u.bid_price,
                    bid_size: u.bid_size,
                    ask_price: u.ask_price,
                    ask_size: u.ask_size,
                    version: u.version,
                })
                .collect();
            write_synthetic_shm_v12(Path::new(&path), num_symbols, &mapped)?;
            serialize_ok(serde_json::json!({ "written": mapped.len() }))
        }
        FfiRequest::AuditRingInit { path } => {
            let ring = OasisAuditRing::create_or_open(Path::new(&path))?;
            let snapshot: AuditHeaderSnapshot = ring.header_snapshot();
            serialize_ok(snapshot)
        }
        FfiRequest::AuditRingEmit {
            path,
            candidate,
            block_number,
            timestamp_ns,
        } => {
            let mut ring = OasisAuditRing::create_or_open(Path::new(&path))?;
            let seq_id = ring.emit_candidate(&candidate, block_number, timestamp_ns)?;
            serialize_ok(AuditEmitResult { seq_id })
        }
    }
}

fn serialize_ok<T: Serialize>(result: T) -> anyhow::Result<String> {
    Ok(serde_json::to_string(&FfiSuccess { ok: true, result })?)
}

fn serialize_err(msg: &str) -> String {
    serde_json::to_string(&FfiError {
        ok: false,
        error: msg.to_string(),
    })
    .unwrap_or_else(|_| String::from(r#"{"ok":false,"error":"serialization_failed"}"#))
}

fn into_c_string_ptr(s: String) -> *mut c_char {
    match CString::new(s) {
        Ok(cstr) => cstr.into_raw(),
        Err(_) => CString::new(r#"{"ok":false,"error":"interior_nul_in_response"}"#)
            .expect("static JSON contains no NUL")
            .into_raw(),
    }
}

/// Evaluate a JSON command and return a heap-allocated NUL-terminated JSON string.
/// Callers must free the returned pointer with `oasis_core_free_str`.
///
/// # Safety
/// If `len > 0`, `ptr` must point to `len` valid, readable bytes for the duration of this call.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn oasis_core_eval_json(ptr: *const u8, len: usize) -> *mut c_char {
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        if ptr.is_null() || len == 0 || len > MAX_FFI_JSON_BYTES {
            return serialize_err("invalid_input: null, empty, or oversized buffer");
        }
        // SAFETY: Caller guarantees `ptr` points to `len` valid readable bytes.
        let bytes = unsafe { std::slice::from_raw_parts(ptr, len) };
        match std::str::from_utf8(bytes) {
            Ok(text) => match eval_json_inner(text) {
                Ok(json_out) => json_out,
                Err(err) => serialize_err(&err.to_string()),
            },
            Err(err) => serialize_err(&format!("invalid_utf8: {err}")),
        }
    }));

    match outcome {
        Ok(json_out) => into_c_string_ptr(json_out),
        Err(_) => into_c_string_ptr(String::from(r#"{"ok":false,"error":"internal_panic"}"#)),
    }
}

/// Free a string previously returned by `oasis_core_eval_json`.
///
/// # Safety
/// `ptr` must either be null or a pointer previously returned by `oasis_core_eval_json`
/// that has not already been freed.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn oasis_core_free_str(ptr: *mut c_char) {
    if ptr.is_null() {
        return;
    }
    let _ = catch_unwind(AssertUnwindSafe(|| {
        // SAFETY: `ptr` was allocated via `CString::into_raw` by `oasis_core_eval_json`.
        unsafe {
            drop(CString::from_raw(ptr));
        }
    }));
}

/// Read back a C string pointer (test helper for Rust tests).
#[cfg(test)]
fn read_and_free(ptr: *mut c_char) -> String {
    assert!(!ptr.is_null());
    let text = unsafe { std::ffi::CStr::from_ptr(ptr) }
        .to_str()
        .expect("valid UTF-8")
        .to_string();
    unsafe { oasis_core_free_str(ptr) };
    text
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ffi_ping_and_null_safety() {
        let null_res = unsafe { oasis_core_eval_json(std::ptr::null(), 0) };
        let null_txt = read_and_free(null_res);
        assert!(null_txt.contains(r#""ok":false"#));

        let req = br#"{"cmd":"ping"}"#;
        let ok_res = unsafe { oasis_core_eval_json(req.as_ptr(), req.len()) };
        let ok_txt = read_and_free(ok_res);
        assert!(ok_txt.contains(r#""ok":true"#));
        assert!(ok_txt.contains(r#""service":"oasis-core""#));
    }
}
