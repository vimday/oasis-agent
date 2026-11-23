use std::collections::{HashMap, HashSet};
use std::ffi::CString;
use std::os::raw::c_char;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::path::Path;
use std::sync::{Arc, LazyLock, Mutex};

use anyhow::{anyhow, ensure};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::aleph_shm::{AlephShmReader, SyntheticBboUpdate, write_synthetic_shm_v12};
use crate::audit_ring::{AuditHeaderSnapshot, OasisAuditRing};
use crate::graph_arb::{
    CycleCandidate, CycleScanReport, CycleScanRequest, PoolGraph, PoolState, ScanCyclesOptions,
};
use crate::pool_math::{
    amount0_delta, amount1_delta, amounts_for_liquidity, serde_u128_str, sqrt_price_at_tick,
};
use crate::u256_math::U256;

const MAX_FFI_JSON_BYTES: usize = 64 * 1024 * 1024;
const MAX_GRAPH_SESSIONS: usize = 16;
const MAX_GRAPH_SESSION_POOLS: usize = 4_096;
const MAX_GRAPH_SCAN_ANCHORS: usize = 16;
const MAX_GRAPH_SESSION_TICKS: usize = 262_144;
const GRAPH_SESSION_ABI: u32 = 1;
const GRAPH_SESSION_CAPABILITIES: &[&str] = &[
    "graph_session_v1",
    "atomic_successor_updates",
    "batched_anchor_scan",
    "shadow_only",
    "v4_bitmap_steps",
];

static GRAPH_SESSION_BUILD_ID: LazyLock<String> = LazyLock::new(|| {
    // Source and build-mode identity. Callers can separately pin the loaded library bytes.
    let sources: &[&[u8]] = &[
        include_bytes!("../../Cargo.toml"),
        include_bytes!("../../Cargo.lock"),
        include_bytes!("../Cargo.toml"),
        include_bytes!("aleph_shm.rs"),
        include_bytes!("audit_ring.rs"),
        include_bytes!("ffi.rs"),
        include_bytes!("graph_arb.rs"),
        include_bytes!("lib.rs"),
        include_bytes!("pool_math.rs"),
        include_bytes!("u256_math.rs"),
        std::env::consts::ARCH.as_bytes(),
        std::env::consts::OS.as_bytes(),
        &[u8::from(cfg!(debug_assertions))],
    ];
    let mut hasher = Sha256::new();
    for source in sources {
        hasher.update((source.len() as u64).to_be_bytes());
        hasher.update(source);
    }
    hex::encode(hasher.finalize())
});

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(deny_unknown_fields)]
struct GraphSessionBlock {
    chain_id: u64,
    block_number: u64,
    block_hash: String,
    parent_hash: String,
}

impl GraphSessionBlock {
    fn validate(&self) -> anyhow::Result<()> {
        ensure!(self.chain_id != 0, "graph block chain ID must be positive");
        let block = parse_graph_hash(&self.block_hash)?;
        let parent = parse_graph_hash(&self.parent_hash)?;
        ensure!(
            block != [0; 32] && block != parent,
            "invalid graph block hash"
        );
        ensure!(
            (parent == [0; 32]) == (self.block_number == 0),
            "invalid graph parent hash for height"
        );
        Ok(())
    }
}

fn parse_graph_hash(text: &str) -> anyhow::Result<[u8; 32]> {
    let hex = text
        .strip_prefix("0x")
        .ok_or_else(|| anyhow!("invalid graph block hash"))?;
    ensure!(
        hex.len() == 64
            && hex
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
        "graph block hash must be 32-byte lowercase hex"
    );
    let mut hash = [0; 32];
    hex::decode_to_slice(hex, &mut hash)?;
    Ok(hash)
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct GraphSessionOpenRequest {
    block: GraphSessionBlock,
    pools: Vec<PoolState>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct GraphSessionUpdateRequest {
    session_id: u64,
    block: GraphSessionBlock,
    pools: Vec<PoolState>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct GraphSessionHandle {
    session_id: u64,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct GraphSessionCapabilitiesRequest {}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct GraphSessionScanRequest {
    session_id: u64,
    block: GraphSessionBlock,
    anchors: Vec<GraphSessionAnchor>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct GraphSessionAnchor {
    base_token: String,
    max_hops: usize,
    min_amount_in: U256,
    max_amount_in: U256,
    gas_cost_in_base: U256,
    l1_fee_in_base: U256,
    min_net_profit: U256,
    #[serde(default)]
    per_hop_gas_cost_in_base: U256,
    #[serde(default)]
    v2_hop_gas_cost_in_base: Option<U256>,
    #[serde(default)]
    cl_hop_gas_cost_in_base: Option<U256>,
    #[serde(default)]
    deduplicate_cycles: bool,
    #[serde(default)]
    changed_pools: Option<Vec<String>>,
}

impl From<GraphSessionAnchor> for CycleScanRequest {
    fn from(anchor: GraphSessionAnchor) -> Self {
        Self {
            base_tokens: vec![anchor.base_token],
            max_hops: anchor.max_hops,
            min_amount_in: anchor.min_amount_in,
            max_amount_in: anchor.max_amount_in,
            gas_cost_in_base: anchor.gas_cost_in_base,
            l1_fee_in_base: anchor.l1_fee_in_base,
            min_net_profit: anchor.min_net_profit,
            options: ScanCyclesOptions {
                per_hop_gas_cost_in_base: anchor.per_hop_gas_cost_in_base,
                v2_hop_gas_cost_in_base: anchor.v2_hop_gas_cost_in_base,
                cl_hop_gas_cost_in_base: anchor.cl_hop_gas_cost_in_base,
                deduplicate_cycles: anchor.deduplicate_cycles,
                changed_pools: anchor.changed_pools,
            },
        }
    }
}

#[derive(Serialize)]
struct GraphSessionStatus {
    session_id: u64,
    block: GraphSessionBlock,
    pool_count: usize,
    valid: bool,
}

#[derive(Serialize)]
struct GraphSessionScanResult {
    #[serde(flatten)]
    status: GraphSessionStatus,
    base_tokens: Vec<String>,
    reports: Vec<CycleScanReport>,
}

struct GraphSession {
    block: GraphSessionBlock,
    graph: PoolGraph,
    tick_count: usize,
    valid: bool,
}

impl GraphSession {
    fn open(block: GraphSessionBlock, pools: Vec<PoolState>) -> anyhow::Result<Self> {
        block.validate()?;
        validate_graph_pool_batch(&block, &pools)?;
        let tick_count = pools.iter().map(|pool| pool.ticks.len()).sum();
        ensure!(
            tick_count <= MAX_GRAPH_SESSION_TICKS,
            "graph session tick limit exceeded"
        );
        let mut graph = PoolGraph::new();
        graph.upsert_pools(pools)?;
        Ok(Self {
            block,
            graph,
            tick_count,
            valid: true,
        })
    }

    fn status(&self, session_id: u64) -> GraphSessionStatus {
        GraphSessionStatus {
            session_id,
            block: self.block.clone(),
            pool_count: self.graph.len(),
            valid: self.valid,
        }
    }

    fn validate_advance(
        &self,
        block: &GraphSessionBlock,
        pools: &[PoolState],
    ) -> anyhow::Result<usize> {
        ensure!(
            self.valid,
            "graph session invalid; replace with a full snapshot"
        );
        block.validate()?;
        ensure!(
            self.block.chain_id == block.chain_id
                && self.block.block_number.checked_add(1) == Some(block.block_number)
                && self.block.block_hash == block.parent_hash
                && self.block.block_hash != block.block_hash,
            "graph block is not the current direct successor"
        );
        validate_graph_pool_batch(block, pools)?;
        let mut tick_count = self.tick_count;
        for pool in pools {
            let existing = self
                .graph
                .get_pool(block.chain_id, &pool.pool_id)
                .ok_or_else(|| {
                    anyhow!("graph delta refers to an unknown pool; replace the snapshot")
                })?;
            ensure!(
                existing.kind == pool.kind
                    && existing.token0 == canonical_graph_id(&pool.token0)
                    && existing.token1 == canonical_graph_id(&pool.token1)
                    && existing.decimals0 == pool.decimals0
                    && existing.decimals1 == pool.decimals1,
                "graph delta changes pool topology; replace the snapshot"
            );
            tick_count = tick_count - existing.ticks.len() + pool.ticks.len();
        }
        ensure!(
            tick_count <= MAX_GRAPH_SESSION_TICKS,
            "graph session tick limit exceeded"
        );
        Ok(tick_count)
    }

    fn advance(&mut self, block: GraphSessionBlock, pools: Vec<PoolState>) -> anyhow::Result<()> {
        let result = self
            .validate_advance(&block, &pools)
            .and_then(|tick_count| {
                self.graph.upsert_pools(pools)?;
                Ok(tick_count)
            });
        match result {
            Ok(tick_count) => {
                self.block = block;
                self.tick_count = tick_count;
                Ok(())
            }
            Err(error) => {
                self.valid = false;
                Err(error)
            }
        }
    }

    fn replace(&mut self, block: GraphSessionBlock, pools: Vec<PoolState>) -> anyhow::Result<()> {
        self.valid = false;
        ensure!(
            self.block.chain_id == block.chain_id,
            "graph replacement changes chain ID"
        );
        *self = Self::open(block, pools)?;
        Ok(())
    }

    fn scan(&mut self, input: GraphSessionScanRequest) -> anyhow::Result<GraphSessionScanResult> {
        ensure!(
            self.valid,
            "graph session invalid; replace with a full snapshot"
        );
        if input.block != self.block {
            self.valid = false;
            anyhow::bail!("graph scan expectation does not match the current block");
        }
        ensure!(
            (1..=MAX_GRAPH_SCAN_ANCHORS).contains(&input.anchors.len()),
            "graph scan anchor count out of bounds"
        );
        for anchor in &input.anchors {
            if let Some(changed) = &anchor.changed_pools {
                ensure!(
                    changed.len() <= MAX_GRAPH_SESSION_POOLS,
                    "graph scan pool limit exceeded"
                );
                ensure!(
                    changed
                        .iter()
                        .all(|id| self.graph.get_pool(self.block.chain_id, id).is_some()),
                    "graph scan changed pool is unknown"
                );
            }
        }
        let base_tokens = input
            .anchors
            .iter()
            .map(|anchor| canonical_graph_id(&anchor.base_token))
            .collect();
        let requests: Vec<_> = input
            .anchors
            .into_iter()
            .map(CycleScanRequest::from)
            .collect();
        let reports = self.graph.scan_cycles_batch(&requests)?;
        Ok(GraphSessionScanResult {
            status: self.status(input.session_id),
            base_tokens,
            reports,
        })
    }
}

fn canonical_graph_id(id: &str) -> String {
    id.trim().to_ascii_lowercase()
}

fn validate_graph_pool_batch(block: &GraphSessionBlock, pools: &[PoolState]) -> anyhow::Result<()> {
    ensure!(
        pools.len() <= MAX_GRAPH_SESSION_POOLS,
        "graph session pool limit exceeded"
    );
    let mut seen = HashSet::with_capacity(pools.len());
    for pool in pools {
        ensure!(
            pool.chain_id == block.chain_id && pool.block_number == block.block_number,
            "graph pool observation does not match the bound block"
        );
        ensure!(
            seen.insert(canonical_graph_id(&pool.pool_id)),
            "duplicate graph pool identity"
        );
    }
    Ok(())
}

type SharedGraphSession = Arc<Mutex<Option<GraphSession>>>;

#[derive(Default)]
struct GraphSessions {
    last_id: u64,
    sessions: HashMap<u64, SharedGraphSession>,
}

static GRAPH_SESSIONS: LazyLock<Mutex<GraphSessions>> = LazyLock::new(Mutex::default);

fn open_graph_session(input: GraphSessionOpenRequest) -> anyhow::Result<GraphSessionStatus> {
    let session = GraphSession::open(input.block, input.pools)?;
    let mut state = GRAPH_SESSIONS
        .lock()
        .map_err(|_| anyhow!("graph registry unavailable"))?;
    ensure!(
        state.sessions.len() < MAX_GRAPH_SESSIONS,
        "graph session limit exceeded"
    );
    let session_id = state
        .last_id
        .checked_add(1)
        .ok_or_else(|| anyhow!("graph session ID exhausted"))?;
    let status = session.status(session_id);
    state.last_id = session_id;
    state
        .sessions
        .insert(session_id, Arc::new(Mutex::new(Some(session))));
    Ok(status)
}

fn with_graph_session<T>(
    id: u64,
    f: impl FnOnce(&mut GraphSession) -> anyhow::Result<T>,
) -> anyhow::Result<T> {
    let session = GRAPH_SESSIONS
        .lock()
        .map_err(|_| anyhow!("graph registry unavailable"))?
        .sessions
        .get(&id)
        .cloned()
        .ok_or_else(|| anyhow!("unknown graph session"))?;
    let mut guard = session
        .lock()
        .map_err(|_| anyhow!("graph session unavailable"))?;
    f(guard
        .as_mut()
        .ok_or_else(|| anyhow!("closed graph session"))?)
}

fn close_graph_session(id: u64) -> anyhow::Result<()> {
    let session = GRAPH_SESSIONS
        .lock()
        .map_err(|_| anyhow!("graph registry unavailable"))?
        .sessions
        .remove(&id)
        .ok_or_else(|| anyhow!("unknown graph session"))?;
    let mut guard = session
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    *guard = None;
    Ok(())
}

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
    #[serde(default)]
    pub per_hop_gas_cost_in_base: U256,
    #[serde(default)]
    pub v2_hop_gas_cost_in_base: Option<U256>,
    #[serde(default)]
    pub cl_hop_gas_cost_in_base: Option<U256>,
    #[serde(default)]
    pub deduplicate_cycles: bool,
    #[serde(default)]
    pub changed_pools: Option<Vec<String>>,
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
    GraphSessionCapabilities(GraphSessionCapabilitiesRequest),
    GraphSessionOpen(GraphSessionOpenRequest),
    GraphSessionAdvance(GraphSessionUpdateRequest),
    GraphSessionReplace(GraphSessionUpdateRequest),
    GraphSessionStatus(GraphSessionHandle),
    GraphSessionScan(GraphSessionScanRequest),
    GraphSessionClose(GraphSessionHandle),
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

    let options = ScanCyclesOptions {
        per_hop_gas_cost_in_base: input.per_hop_gas_cost_in_base,
        v2_hop_gas_cost_in_base: input.v2_hop_gas_cost_in_base,
        cl_hop_gas_cost_in_base: input.cl_hop_gas_cost_in_base,
        deduplicate_cycles: input.deduplicate_cycles,
        changed_pools: input.changed_pools,
    };

    let report = graph.scan_cycles_with_options(
        &input.base_tokens,
        input.max_hops,
        input.min_amount_in,
        input.max_amount_in,
        input.gas_cost_in_base,
        input.l1_fee_in_base,
        input.min_net_profit,
        &options,
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
        FfiRequest::GraphSessionCapabilities(_) => serialize_ok(serde_json::json!({
            "service": "oasis-core",
            "version": env!("CARGO_PKG_VERSION"),
            "abi_version": GRAPH_SESSION_ABI,
            "build_id": GRAPH_SESSION_BUILD_ID.as_str(),
            "capabilities": GRAPH_SESSION_CAPABILITIES,
            "max_sessions": MAX_GRAPH_SESSIONS,
            "max_pools": MAX_GRAPH_SESSION_POOLS,
            "max_anchors": MAX_GRAPH_SCAN_ANCHORS,
            "max_ticks": MAX_GRAPH_SESSION_TICKS,
        })),
        FfiRequest::GraphSessionOpen(input) => serialize_ok(open_graph_session(input)?),
        FfiRequest::GraphSessionAdvance(input) => {
            let session_id = input.session_id;
            let status = with_graph_session(session_id, |session| {
                session.advance(input.block, input.pools)?;
                Ok(session.status(session_id))
            })?;
            serialize_ok(status)
        }
        FfiRequest::GraphSessionReplace(input) => {
            let session_id = input.session_id;
            let status = with_graph_session(session_id, |session| {
                session.replace(input.block, input.pools)?;
                Ok(session.status(session_id))
            })?;
            serialize_ok(status)
        }
        FfiRequest::GraphSessionStatus(input) => {
            let session_id = input.session_id;
            serialize_ok(with_graph_session(session_id, |session| {
                Ok(session.status(session_id))
            })?)
        }
        FfiRequest::GraphSessionScan(input) => {
            serialize_ok(with_graph_session(input.session_id, |session| {
                session.scan(input)
            })?)
        }
        FfiRequest::GraphSessionClose(input) => {
            close_graph_session(input.session_id)?;
            serialize_ok(serde_json::json!({ "session_id": input.session_id, "closed": true }))
        }
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
    use serde_json::{Value, json};

    fn ffi(request: Value) -> Value {
        let data = serde_json::to_vec(&request).unwrap();
        let response = unsafe { oasis_core_eval_json(data.as_ptr(), data.len()) };
        serde_json::from_str(&read_and_free(response)).unwrap()
    }

    fn ok(request: Value) -> Value {
        let response = ffi(request);
        assert_eq!(response["ok"], true, "{response}");
        response["result"].clone()
    }

    fn rejected(request: Value, message: &str) {
        let response = ffi(request);
        assert_eq!(response["ok"], false, "{response}");
        assert!(
            response["error"].as_str().unwrap().contains(message),
            "{response}"
        );
    }

    fn test_block(number: u64, hash: u64, parent: u64) -> Value {
        json!({
            "chain_id": 8453, "block_number": number,
            "block_hash": format!("0x{hash:064x}"), "parent_hash": format!("0x{parent:064x}"),
        })
    }

    fn test_pool(id: &str, number: u64, reserve1: &str) -> Value {
        json!({
            "chain_id": 8453, "pool_id": id, "kind": "V2", "token0": "a", "token1": "b",
            "decimals0": 18, "decimals1": 18, "fee_pips": 3000,
            "reserve0": "1000", "reserve1": reserve1, "block_number": number,
        })
    }

    fn test_anchor(token: &str) -> Value {
        json!({
            "base_token": token, "max_hops": 2, "min_amount_in": "20", "max_amount_in": "40",
            "gas_cost_in_base": "2", "l1_fee_in_base": "1", "min_net_profit": "1",
        })
    }

    fn without_time(mut report: Value) -> Value {
        report.as_object_mut().unwrap().remove("elapsed_ns");
        report
    }

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

    #[test]
    fn graph_session_capabilities_and_ordered_batch_match_stateless_scan() {
        let info = ok(json!({"cmd": "graph_session_capabilities"}));
        assert_eq!(info["abi_version"], 1);
        assert_eq!(info["capabilities"], json!(GRAPH_SESSION_CAPABILITIES));
        assert_eq!(info["max_pools"], MAX_GRAPH_SESSION_POOLS);
        parse_graph_hash(&format!("0x{}", info["build_id"].as_str().unwrap())).unwrap();
        rejected(
            json!({"cmd": "graph_session_capabilities", "extra": true}),
            "unknown field",
        );

        let block = test_block(2, 102, 101);
        let pools = vec![test_pool("p1", 2, "1900"), test_pool("p2", 2, "1000")];
        let status = ok(json!({"cmd": "graph_session_open", "block": block, "pools": pools}));
        assert_eq!(status["block"], block);
        assert_eq!(status["pool_count"], 2);
        assert_eq!(status["valid"], true);
        let id = &status["session_id"];
        rejected(
            json!({"cmd": "graph_session_scan", "session_id": id, "block": block,
                "anchors": [test_anchor("a")], "audit_ring_path": "ignored"}),
            "unknown field",
        );
        let tokens = ["B", "missing", "a"];
        let batch = ok(
            json!({"cmd": "graph_session_scan", "session_id": id, "block": block,
            "anchors": tokens.map(test_anchor)}),
        );
        assert_eq!(batch["base_tokens"], json!(["b", "missing", "a"]));
        assert_eq!(batch["block"], block);
        assert_eq!(batch["reports"].as_array().unwrap().len(), 3);
        for (index, anchor) in tokens.iter().enumerate() {
            let mut stateless = test_anchor(anchor);
            let fields = stateless.as_object_mut().unwrap();
            fields.remove("base_token");
            fields.insert("cmd".into(), json!("scan_cycles"));
            fields.insert("pools".into(), json!(pools));
            fields.insert("base_tokens".into(), json!([anchor]));
            assert_eq!(
                without_time(batch["reports"][index].clone()),
                without_time(ok(stateless))
            );
        }
        assert_eq!(batch["reports"][1]["candidates"], json!([]));
        assert_ne!(batch["reports"][2]["candidates"], json!([]));
        assert_eq!(
            ok(json!({"cmd": "graph_session_close", "session_id": id}))["closed"],
            true
        );
        rejected(
            json!({"cmd": "graph_session_status", "session_id": id}),
            "unknown graph session",
        );
    }

    #[test]
    fn graph_session_bad_delta_keeps_graph_and_invalidates_until_replaced() {
        let block = test_block(2, 202, 201);
        let child = test_block(3, 203, 202);
        let original = test_pool("p1", 2, "1800");
        let opened = ok(json!({"cmd": "graph_session_open", "block": block,
            "pools": [original, test_pool("p2", 2, "1000")]}));
        let id = opened["session_id"].as_u64().unwrap();
        let mut bad_delta = test_pool("p2", 3, "1900");
        bad_delta["reserve1"] = Value::Null;
        rejected(
            json!({"cmd": "graph_session_advance", "session_id": id, "block": child,
                "pools": [test_pool("p1", 3, "1200"), bad_delta]}),
            "require reserve0 and reserve1",
        );
        let status = ok(json!({"cmd": "graph_session_status", "session_id": id}));
        assert_eq!(status["valid"], false);
        assert_eq!(status["block"], block);
        with_graph_session(id, |session| {
            let original: PoolState = serde_json::from_value(original).unwrap();
            assert_eq!(session.graph.get_pool(8453, "p1"), Some(&original));
            Ok(())
        })
        .unwrap();
        rejected(
            json!({"cmd": "graph_session_scan", "session_id": id, "block": block,
            "anchors": [test_anchor("a")]}),
            "graph session invalid",
        );
        let fork = test_block(15, 215, 214);
        let replacement = ok(
            json!({"cmd": "graph_session_replace", "session_id": id, "block": fork,
            "pools": [test_pool("replacement", 15, "1800")]}),
        );
        assert_eq!(replacement["valid"], true);
        assert_eq!(replacement["session_id"], id);
        assert_eq!(replacement["pool_count"], 1);
        let direct = test_block(16, 216, 215);
        let status = ok(
            json!({"cmd": "graph_session_advance", "session_id": id, "block": direct,
            "pools": []}),
        );
        assert_eq!(status["block"], direct);
        assert_eq!(status["valid"], true);
        rejected(
            json!({"cmd": "graph_session_advance", "session_id": id, "block": test_block(17, 217, 216),
            "pools": [test_pool("unknown", 17, "1800")]}),
            "unknown pool",
        );
        assert_eq!(
            ok(json!({"cmd": "graph_session_status", "session_id": id}))["valid"],
            false
        );
        ok(json!({"cmd": "graph_session_close", "session_id": id}));
    }

    #[test]
    fn session_pins_full_expected_block_and_rejects_malformed_identity() {
        let block = test_block(2, 302, 301);
        rejected(
            json!({"cmd": "graph_session_open", "block": test_block(2, 302, 302), "pools": []}),
            "invalid graph block hash",
        );
        rejected(
            json!({"cmd": "graph_session_open", "block": block, "pools": [test_pool("p1", 1, "1000")]}),
            "observation does not match",
        );
        rejected(
            json!({"cmd": "graph_session_open", "block": block,
            "pools": [test_pool("p1", 2, "1000"), test_pool("P1", 2, "1000")]}),
            "duplicate graph pool identity",
        );
        let opened = ok(json!({"cmd": "graph_session_open", "block": block, "pools": []}));
        let id = &opened["session_id"];
        rejected(
            json!({"cmd": "graph_session_scan", "session_id": id,
            "block": test_block(2, 302, 399), "anchors": [test_anchor("a")]}),
            "expectation does not match",
        );
        assert_eq!(
            ok(json!({"cmd": "graph_session_status", "session_id": id}))["valid"],
            false
        );
        ok(json!({"cmd": "graph_session_replace", "session_id": id, "block": block, "pools": []}));
        rejected(
            json!({"cmd": "graph_session_advance", "session_id": id, "block": test_block(4, 304, 302),
            "pools": []}),
            "not the current direct successor",
        );
        assert_eq!(
            ok(json!({"cmd": "graph_session_status", "session_id": id}))["valid"],
            false
        );
        ok(json!({"cmd": "graph_session_close", "session_id": id}));
    }
}
