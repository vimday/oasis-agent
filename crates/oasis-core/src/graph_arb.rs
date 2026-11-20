use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::time::Instant;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;

use crate::pool_math::{
    FEE_PIPS_DENOM, MAX_SQRT_PRICE, MAX_TICK, MIN_SQRT_PRICE, MIN_TICK, PoolMathError,
    TickLiquidity, aerodrome_stable_swap_exact_in, cl_swap_exact_in_prevalidated, serde_u128_str,
    sqrt_price_at_tick, v2_swap_exact_in, validate_sorted_ticks,
};
use crate::u256_math::{U256, U512, div_rem_u256};

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum GraphArbError {
    #[error("pool validation failed: {0}")]
    InvalidPool(String),
    #[error("pool not found for chain_id={chain_id} pool_id={pool_id}")]
    PoolNotFound { chain_id: u64, pool_id: String },
    #[error(
        "conflicting pool snapshot for chain_id={chain_id} pool_id={pool_id} at block {block_number}"
    )]
    ConflictingPoolSnapshot {
        chain_id: u64,
        pool_id: String,
        block_number: u64,
    },
    #[error("token {token} does not belong to pool {pool_id}")]
    TokenNotInPool { pool_id: String, token: String },
    #[error("route cannot be empty")]
    EmptyRoute,
    #[error("route hop token mismatch: expected {expected}, got {actual}")]
    RouteTokenMismatch { expected: String, actual: String },
    #[error("base_tokens cannot be empty")]
    EmptyBaseTokens,
    #[error("max_hops must be between 2 and 4, got {0}")]
    InvalidMaxHops(usize),
    #[error("invalid search bounds: min_amount_in must be > 0 and <= max_amount_in")]
    InvalidAmountBounds,
    #[error("pool math error: {0}")]
    Math(#[from] PoolMathError),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum PoolKind {
    V2,
    AerodromeStable,
    V3,
    Slipstream,
    V4,
}

pub mod serde_opt_u128_str {
    use super::*;
    use serde::{Deserializer, Serializer, de};

    pub fn serialize<S: Serializer>(val: &Option<u128>, serializer: S) -> Result<S::Ok, S::Error> {
        match val {
            Some(v) => serde_u128_str::serialize(v, serializer),
            None => serializer.serialize_none(),
        }
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<Option<u128>, D::Error> {
        struct OptVisitor;
        impl<'de> de::Visitor<'de> for OptVisitor {
            type Value = Option<u128>;

            fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                formatter.write_str("null or an unsigned integer string")
            }

            fn visit_none<E: de::Error>(self) -> Result<Self::Value, E> {
                Ok(None)
            }

            fn visit_unit<E: de::Error>(self) -> Result<Self::Value, E> {
                Ok(None)
            }

            fn visit_some<D2: Deserializer<'de>>(
                self,
                deserializer: D2,
            ) -> Result<Self::Value, D2::Error> {
                serde_u128_str::deserialize(deserializer).map(Some)
            }
        }
        deserializer.deserialize_option(OptVisitor)
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PoolState {
    pub chain_id: u64,
    pub pool_id: String,
    pub kind: PoolKind,
    pub token0: String,
    pub token1: String,
    pub decimals0: u8,
    pub decimals1: u8,
    pub fee_pips: u32,
    #[serde(default)]
    pub reserve0: Option<U256>,
    #[serde(default)]
    pub reserve1: Option<U256>,
    #[serde(default)]
    pub sqrt_price_x96: Option<U256>,
    #[serde(default)]
    pub tick: Option<i32>,
    #[serde(default, with = "serde_opt_u128_str")]
    pub liquidity: Option<u128>,
    #[serde(default)]
    pub ticks: Vec<TickLiquidity>,
    #[serde(default)]
    pub block_number: u64,
    #[serde(default)]
    pub timestamp_ms: u64,
}

impl PoolState {
    fn normalized_and_validated(mut self) -> Result<Self, GraphArbError> {
        if self.chain_id == 0 {
            return Err(GraphArbError::InvalidPool("chain_id must be > 0".into()));
        }
        self.pool_id = normalize_id(&self.pool_id);
        self.token0 = normalize_id(&self.token0);
        self.token1 = normalize_id(&self.token1);
        if self.pool_id.is_empty() || self.token0.is_empty() || self.token1.is_empty() {
            return Err(GraphArbError::InvalidPool(
                "pool_id, token0, and token1 must be non-empty".into(),
            ));
        }
        if self.token0 == self.token1 {
            return Err(GraphArbError::InvalidPool(
                "token0 and token1 must be distinct".into(),
            ));
        }
        if self.decimals0 > 36 || self.decimals1 > 36 {
            return Err(GraphArbError::InvalidPool(
                "token decimals must be <= 36".into(),
            ));
        }
        if self.fee_pips >= FEE_PIPS_DENOM {
            return Err(GraphArbError::InvalidPool(
                "fee_pips must be < 1_000_000".into(),
            ));
        }
        match self.kind {
            PoolKind::V2 | PoolKind::AerodromeStable => {
                if self.reserve0.is_none() || self.reserve1.is_none() {
                    return Err(GraphArbError::InvalidPool(
                        "V2/AerodromeStable pools require reserve0 and reserve1".into(),
                    ));
                }
            }
            PoolKind::V3 | PoolKind::Slipstream | PoolKind::V4 => {
                let sqrt_p = self.sqrt_price_x96.ok_or_else(|| {
                    GraphArbError::InvalidPool("CL pool requires sqrt_price_x96".into())
                })?;
                let tick = self
                    .tick
                    .ok_or_else(|| GraphArbError::InvalidPool("CL pool requires tick".into()))?;
                if self.liquidity.is_none() {
                    return Err(GraphArbError::InvalidPool(
                        "CL pool requires liquidity".into(),
                    ));
                }
                if !(MIN_TICK..=MAX_TICK).contains(&tick) {
                    return Err(GraphArbError::Math(PoolMathError::TickOutOfBounds(tick)));
                }
                if sqrt_p < MIN_SQRT_PRICE || sqrt_p > MAX_SQRT_PRICE {
                    return Err(GraphArbError::Math(PoolMathError::SqrtPriceOutOfBounds));
                }
                if sqrt_p < sqrt_price_at_tick(tick)? {
                    return Err(GraphArbError::Math(PoolMathError::TickPriceMismatch));
                }
                if tick < MAX_TICK && sqrt_p > sqrt_price_at_tick(tick + 1)? {
                    return Err(GraphArbError::Math(PoolMathError::TickPriceMismatch));
                }
                self.ticks.sort_by_key(|t| t.tick);
                validate_sorted_ticks(&self.ticks)?;
            }
        }
        Ok(self)
    }

    fn quote_exact_in_direction(
        &self,
        zero_for_one: bool,
        amount_in: U256,
    ) -> Result<(U256, U256), GraphArbError> {
        match self.kind {
            PoolKind::V2 => {
                let r0 = self.reserve0.ok_or(PoolMathError::InsufficientLiquidity)?;
                let r1 = self.reserve1.ok_or(PoolMathError::InsufficientLiquidity)?;
                let (rin, rout) = if zero_for_one { (r0, r1) } else { (r1, r0) };
                let res = v2_swap_exact_in(rin, rout, self.fee_pips, amount_in)?;
                Ok((res.amount_out, res.fee_paid))
            }
            PoolKind::AerodromeStable => {
                let r0 = self.reserve0.ok_or(PoolMathError::InsufficientLiquidity)?;
                let r1 = self.reserve1.ok_or(PoolMathError::InsufficientLiquidity)?;
                let (rin, rout, dec_in, dec_out) = if zero_for_one {
                    (r0, r1, self.decimals0, self.decimals1)
                } else {
                    (r1, r0, self.decimals1, self.decimals0)
                };
                let res = aerodrome_stable_swap_exact_in(
                    rin,
                    rout,
                    dec_in,
                    dec_out,
                    self.fee_pips,
                    amount_in,
                )?;
                Ok((res.amount_out, res.fee_paid))
            }
            PoolKind::V3 | PoolKind::Slipstream | PoolKind::V4 => {
                let sqrt_p = self
                    .sqrt_price_x96
                    .ok_or(PoolMathError::InsufficientLiquidity)?;
                let tick = self.tick.ok_or(PoolMathError::InsufficientLiquidity)?;
                let liq = self.liquidity.ok_or(PoolMathError::InsufficientLiquidity)?;
                let res = cl_swap_exact_in_prevalidated(
                    sqrt_p,
                    tick,
                    liq,
                    &self.ticks,
                    self.fee_pips,
                    zero_for_one,
                    amount_in,
                )?;
                Ok((res.amount_out, res.fee_paid))
            }
        }
    }

    pub fn quote_exact_in(
        &self,
        token_in: &str,
        amount_in: U256,
    ) -> Result<PoolQuoteOutput, GraphArbError> {
        let token_in_norm = normalize_id(token_in);
        let (zero_for_one, token_out) = if token_in_norm == self.token0 {
            (true, self.token1.clone())
        } else if token_in_norm == self.token1 {
            (false, self.token0.clone())
        } else {
            return Err(GraphArbError::TokenNotInPool {
                pool_id: self.pool_id.clone(),
                token: token_in.to_string(),
            });
        };

        let (amount_out, fee_paid) = self.quote_exact_in_direction(zero_for_one, amount_in)?;

        Ok(PoolQuoteOutput {
            pool_id: self.pool_id.clone(),
            kind: self.kind,
            token_in: token_in_norm,
            token_out,
            amount_in,
            amount_out,
            fee_paid,
        })
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PoolQuoteOutput {
    pub pool_id: String,
    pub kind: PoolKind,
    pub token_in: String,
    pub token_out: String,
    pub amount_in: U256,
    pub amount_out: U256,
    pub fee_paid: U256,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct CycleHop {
    pub pool_id: String,
    pub kind: PoolKind,
    pub token_in: String,
    pub token_out: String,
    pub amount_in: U256,
    pub amount_out: U256,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct CycleCandidate {
    pub chain_id: u64,
    pub base_token: String,
    pub hops: Vec<CycleHop>,
    pub optimal_amount_in: U256,
    pub amount_out: U256,
    pub gas_cost_in_base: U256,
    pub l1_fee_in_base: U256,
    pub net_profit: U256,
}

impl CycleCandidate {
    #[must_use]
    pub fn route_hash(&self) -> [u8; 32] {
        let mut hasher = Sha256::new();
        hasher.update(self.chain_id.to_le_bytes());
        hasher.update(self.base_token.as_bytes());
        for hop in &self.hops {
            hasher.update(b"|");
            hasher.update(hop.pool_id.as_bytes());
            hasher.update(b":");
            hasher.update(hop.token_in.as_bytes());
            hasher.update(b"->");
            hasher.update(hop.token_out.as_bytes());
        }
        hasher.finalize().into()
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct CycleScanReport {
    pub candidates: Vec<CycleCandidate>,
    pub cycles_evaluated: usize,
    pub elapsed_ns: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RouteStep {
    pub chain_id: u64,
    pub pool_id: String,
    pub token_in: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RouteQuote {
    pub hops: Vec<CycleHop>,
    pub amount_in: U256,
    pub amount_out: U256,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct SignedU512 {
    negative: bool,
    mag: U512,
}

impl SignedU512 {
    fn diff(positive: U512, subtract: U512) -> Self {
        if positive >= subtract {
            Self {
                negative: false,
                mag: positive
                    .checked_sub(subtract)
                    .expect("positive >= subtract"),
            }
        } else {
            Self {
                negative: true,
                mag: subtract.checked_sub(positive).expect("subtract > positive"),
            }
        }
    }
}

impl Ord for SignedU512 {
    fn cmp(&self, other: &Self) -> Ordering {
        match (self.negative, other.negative) {
            (false, true) => Ordering::Greater,
            (true, false) => Ordering::Less,
            (false, false) => self.mag.cmp(&other.mag),
            (true, true) => other.mag.cmp(&self.mag),
        }
    }
}

impl PartialOrd for SignedU512 {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

const MAX_SUPPORTED_HOPS: usize = 4;
const SPOT_UPPER_SAFETY_MULT: f64 = 1.000_001;

#[allow(clippy::cast_precision_loss)]
fn u256_to_f64_lossy(v: U256) -> f64 {
    const TWO_POW_64: f64 = 18_446_744_073_709_551_616.0;
    const TWO_POW_128: f64 = TWO_POW_64 * TWO_POW_64;
    const TWO_POW_192: f64 = TWO_POW_128 * TWO_POW_64;
    (v.0[0] as f64)
        + (v.0[1] as f64) * TWO_POW_64
        + (v.0[2] as f64) * TWO_POW_128
        + (v.0[3] as f64) * TWO_POW_192
}

fn edge_max_rate_upper(pool: &PoolState, zero_for_one: bool) -> f64 {
    let fee_mult = f64::from(FEE_PIPS_DENOM - pool.fee_pips) / f64::from(FEE_PIPS_DENOM);
    match pool.kind {
        PoolKind::AerodromeStable => f64::INFINITY,
        PoolKind::V2 => {
            let (Some(r0), Some(r1)) = (pool.reserve0, pool.reserve1) else {
                return f64::INFINITY;
            };
            let (rin, rout) = if zero_for_one { (r0, r1) } else { (r1, r0) };
            if rin.is_zero() || rout.is_zero() {
                return f64::INFINITY;
            }
            let rin_f64 = u256_to_f64_lossy(rin);
            let rout_f64 = u256_to_f64_lossy(rout);
            if !rin_f64.is_finite() || rin_f64 <= 0.0 || !rout_f64.is_finite() || rout_f64 <= 0.0 {
                return f64::INFINITY;
            }
            let val = (rout_f64 / rin_f64) * fee_mult * SPOT_UPPER_SAFETY_MULT;
            if !val.is_finite() || val <= 0.0 {
                f64::INFINITY
            } else {
                val
            }
        }
        PoolKind::V3 | PoolKind::Slipstream | PoolKind::V4 => {
            let Some(sqrt_p) = pool.sqrt_price_x96 else {
                return f64::INFINITY;
            };
            if sqrt_p.is_zero() {
                return f64::INFINITY;
            }
            let sp = u256_to_f64_lossy(sqrt_p) / 2.0_f64.powi(96);
            if !sp.is_finite() || sp <= 0.0 {
                return f64::INFINITY;
            }
            let sp2 = sp * sp;
            if !sp2.is_finite() || sp2 <= 0.0 {
                return f64::INFINITY;
            }
            let raw_rate = if zero_for_one { sp2 } else { 1.0 / sp2 };
            let val = raw_rate * fee_mult * SPOT_UPPER_SAFETY_MULT;
            if !val.is_finite() || val <= 0.0 {
                f64::INFINITY
            } else {
                val
            }
        }
    }
}

#[derive(Clone, Copy, Debug)]
struct IndexedEdge {
    to_token: usize,
    pool_idx: usize,
    zero_for_one: bool,
    max_rate_upper: f64,
}

#[derive(Clone, Copy, Debug, Default)]
struct IndexedStep {
    pool_idx: usize,
    zero_for_one: bool,
}

#[derive(Clone, Copy, Debug)]
struct EvaluatedIndexedPoint {
    amount_in: U256,
    amount_out: U256,
    net_profit: SignedU512,
}

struct IndexedGraph<'a> {
    chain_id: u64,
    pools: Vec<&'a PoolState>,
    tokens: Vec<&'a str>,
    token_ids: HashMap<&'a str, usize>,
    adj: Vec<Vec<IndexedEdge>>,
}

impl<'a> IndexedGraph<'a> {
    fn from_chain_pools(chain_id: u64, chain_pools: Vec<&'a PoolState>) -> Self {
        let mut tokens = Vec::with_capacity(chain_pools.len() * 2);
        let mut token_ids = HashMap::with_capacity(chain_pools.len() * 2);
        let mut adj = Vec::with_capacity(chain_pools.len() * 2);

        for (pool_idx, pool) in chain_pools.iter().enumerate() {
            let t0 = intern_token(&pool.token0, &mut tokens, &mut token_ids, &mut adj);
            let t1 = intern_token(&pool.token1, &mut tokens, &mut token_ids, &mut adj);
            adj[t0].push(IndexedEdge {
                to_token: t1,
                pool_idx,
                zero_for_one: true,
                max_rate_upper: edge_max_rate_upper(pool, true),
            });
            adj[t1].push(IndexedEdge {
                to_token: t0,
                pool_idx,
                zero_for_one: false,
                max_rate_upper: edge_max_rate_upper(pool, false),
            });
        }

        Self {
            chain_id,
            pools: chain_pools,
            tokens,
            token_ids,
            adj,
        }
    }

    fn token_id(&self, normalized_token: &str) -> Option<usize> {
        self.token_ids.get(normalized_token).copied()
    }

    fn materialize_candidate(
        &self,
        base_id: usize,
        steps: &[IndexedStep],
        best: EvaluatedIndexedPoint,
        gas_cost_in_base: U256,
        l1_fee_in_base: U256,
    ) -> Result<CycleCandidate, GraphArbError> {
        let mut hops = Vec::with_capacity(steps.len());
        let mut cur_amount = best.amount_in;
        for step in steps {
            let pool = self.pools[step.pool_idx];
            let (amount_out, _) = pool.quote_exact_in_direction(step.zero_for_one, cur_amount)?;
            let (token_in, token_out) = if step.zero_for_one {
                (pool.token0.clone(), pool.token1.clone())
            } else {
                (pool.token1.clone(), pool.token0.clone())
            };
            hops.push(CycleHop {
                pool_id: pool.pool_id.clone(),
                kind: pool.kind,
                token_in,
                token_out,
                amount_in: cur_amount,
                amount_out,
            });
            cur_amount = amount_out;
        }
        let net_profit = best
            .net_profit
            .mag
            .to_u256()
            .expect("positive net_profit <= amount_out fits U256");
        Ok(CycleCandidate {
            chain_id: self.chain_id,
            base_token: self.tokens[base_id].to_string(),
            hops,
            optimal_amount_in: best.amount_in,
            amount_out: best.amount_out,
            gas_cost_in_base,
            l1_fee_in_base,
            net_profit,
        })
    }
}

fn intern_token<'a>(
    token: &'a str,
    tokens: &mut Vec<&'a str>,
    token_ids: &mut HashMap<&'a str, usize>,
    adj: &mut Vec<Vec<IndexedEdge>>,
) -> usize {
    if let Some(&id) = token_ids.get(token) {
        return id;
    }
    let id = tokens.len();
    tokens.push(token);
    token_ids.insert(token, id);
    adj.push(Vec::new());
    id
}

#[derive(Default, Debug)]
pub struct PoolGraph {
    pools: BTreeMap<(u64, String), PoolState>,
}

impl PoolGraph {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    pub fn upsert_pool(&mut self, pool: PoolState) -> Result<(), GraphArbError> {
        let normalized = pool.normalized_and_validated()?;
        let key = (normalized.chain_id, normalized.pool_id.clone());
        if let Some(existing) = self.pools.get(&key) {
            if existing.block_number == normalized.block_number && existing != &normalized {
                return Err(GraphArbError::ConflictingPoolSnapshot {
                    chain_id: normalized.chain_id,
                    pool_id: normalized.pool_id,
                    block_number: normalized.block_number,
                });
            }
            if normalized.block_number < existing.block_number {
                return Err(GraphArbError::InvalidPool(format!(
                    "stale pool snapshot for chain_id={} pool_id={}: block {} < {}",
                    normalized.chain_id,
                    normalized.pool_id,
                    normalized.block_number,
                    existing.block_number
                )));
            }
        }
        self.pools.insert(key, normalized);
        Ok(())
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.pools.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.pools.is_empty()
    }

    pub fn get_pool(&self, chain_id: u64, pool_id: &str) -> Option<&PoolState> {
        self.pools.get(&(chain_id, normalize_id(pool_id)))
    }

    pub fn quote_pool(
        &self,
        chain_id: u64,
        pool_id: &str,
        token_in: &str,
        amount_in: U256,
    ) -> Result<PoolQuoteOutput, GraphArbError> {
        let pool_key = normalize_id(pool_id);
        let pool =
            self.pools
                .get(&(chain_id, pool_key.clone()))
                .ok_or(GraphArbError::PoolNotFound {
                    chain_id,
                    pool_id: pool_key,
                })?;
        pool.quote_exact_in(token_in, amount_in)
    }

    pub fn quote_route(
        &self,
        route: &[RouteStep],
        amount_in: U256,
    ) -> Result<RouteQuote, GraphArbError> {
        if route.is_empty() {
            return Err(GraphArbError::EmptyRoute);
        }
        let mut cur_amount = amount_in;
        let mut expected_token = normalize_id(&route[0].token_in);
        let mut hops = Vec::with_capacity(route.len());

        for step in route {
            let step_token = normalize_id(&step.token_in);
            if step_token != expected_token {
                return Err(GraphArbError::RouteTokenMismatch {
                    expected: expected_token,
                    actual: step_token,
                });
            }
            let out = self.quote_pool(step.chain_id, &step.pool_id, &step_token, cur_amount)?;
            expected_token = out.token_out.clone();
            cur_amount = out.amount_out;
            hops.push(CycleHop {
                pool_id: out.pool_id,
                kind: out.kind,
                token_in: out.token_in,
                token_out: out.token_out,
                amount_in: out.amount_in,
                amount_out: out.amount_out,
            });
        }

        Ok(RouteQuote {
            hops,
            amount_in,
            amount_out: cur_amount,
        })
    }

    /// Enumerates simple 2-hop to 4-hop cycles starting & ending at each `base_token`,
    /// optimizing `amount_in` with 48-iteration exact integer ternary search over an
    /// indexed zero-allocation graph representation.
    #[allow(clippy::too_many_arguments)]
    pub fn scan_cycles(
        &self,
        base_tokens: &[String],
        max_hops: usize,
        min_amount_in: U256,
        max_amount_in: U256,
        gas_cost_in_base: U256,
        l1_fee_in_base: U256,
        min_net_profit: U256,
    ) -> Result<CycleScanReport, GraphArbError> {
        let start = Instant::now();
        if base_tokens.is_empty() {
            return Err(GraphArbError::EmptyBaseTokens);
        }
        if !(2..=MAX_SUPPORTED_HOPS).contains(&max_hops) {
            return Err(GraphArbError::InvalidMaxHops(max_hops));
        }
        if min_amount_in.is_zero() || min_amount_in > max_amount_in {
            return Err(GraphArbError::InvalidAmountBounds);
        }

        let normalized_bases: BTreeSet<String> =
            base_tokens.iter().map(|t| normalize_id(t)).collect();
        if normalized_bases.iter().any(String::is_empty) {
            return Err(GraphArbError::EmptyBaseTokens);
        }

        let total_gas = gas_cost_in_base
            .to_u512()
            .checked_add(l1_fee_in_base.to_u512())
            .expect("sum of two U256 values fits U512");
        let min_profit_u512 = min_net_profit.to_u512();

        let mut cycles_evaluated = 0usize;
        let mut candidates = Vec::new();

        // `self.pools` is a `BTreeMap<(chain_id, pool_id), PoolState>`, so chains appear in
        // contiguous ascending `(chain_id, pool_id)` order.
        let mut cur_chain: Option<u64> = None;
        let mut chain_pools: Vec<&PoolState> = Vec::new();

        for ((chain_id, _), pool) in &self.pools {
            match cur_chain {
                Some(active_chain) if active_chain != *chain_id => {
                    let indexed = IndexedGraph::from_chain_pools(
                        active_chain,
                        std::mem::take(&mut chain_pools),
                    );
                    scan_indexed_chain(
                        &indexed,
                        &normalized_bases,
                        max_hops,
                        min_amount_in,
                        max_amount_in,
                        gas_cost_in_base,
                        l1_fee_in_base,
                        total_gas,
                        min_profit_u512,
                        &mut cycles_evaluated,
                        &mut candidates,
                    )?;
                    cur_chain = Some(*chain_id);
                }
                None => cur_chain = Some(*chain_id),
                Some(_) => {}
            }
            chain_pools.push(pool);
        }
        if let Some(active_chain) = cur_chain {
            let indexed = IndexedGraph::from_chain_pools(active_chain, chain_pools);
            scan_indexed_chain(
                &indexed,
                &normalized_bases,
                max_hops,
                min_amount_in,
                max_amount_in,
                gas_cost_in_base,
                l1_fee_in_base,
                total_gas,
                min_profit_u512,
                &mut cycles_evaluated,
                &mut candidates,
            )?;
        }

        candidates.sort_by(|a, b| {
            b.net_profit
                .cmp(&a.net_profit)
                .then_with(|| a.chain_id.cmp(&b.chain_id))
                .then_with(|| a.base_token.cmp(&b.base_token))
                .then_with(|| a.hops.len().cmp(&b.hops.len()))
        });

        let elapsed_ns = u64::try_from(start.elapsed().as_nanos()).unwrap_or(u64::MAX);
        Ok(CycleScanReport {
            candidates,
            cycles_evaluated,
            elapsed_ns,
        })
    }
}

#[allow(clippy::too_many_arguments)]
fn scan_indexed_chain(
    graph: &IndexedGraph<'_>,
    normalized_bases: &BTreeSet<String>,
    max_hops: usize,
    min_amount_in: U256,
    max_amount_in: U256,
    gas_cost_in_base: U256,
    l1_fee_in_base: U256,
    total_gas: U512,
    min_profit_u512: U512,
    cycles_evaluated: &mut usize,
    candidates: &mut Vec<CycleCandidate>,
) -> Result<(), GraphArbError> {
    for base in normalized_bases {
        let Some(base_id) = graph.token_id(base) else {
            continue;
        };
        let mut visited_tokens = [0usize; MAX_SUPPORTED_HOPS];
        visited_tokens[0] = base_id;
        let mut used_pools = [0usize; MAX_SUPPORTED_HOPS];
        let mut current_steps = [IndexedStep::default(); MAX_SUPPORTED_HOPS];

        enumerate_indexed_cycles(
            graph,
            base_id,
            base_id,
            max_hops,
            0,
            1,
            1.0_f64,
            &mut visited_tokens,
            &mut used_pools,
            &mut current_steps,
            min_amount_in,
            max_amount_in,
            gas_cost_in_base,
            l1_fee_in_base,
            total_gas,
            min_profit_u512,
            cycles_evaluated,
            candidates,
        )?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn enumerate_indexed_cycles(
    graph: &IndexedGraph<'_>,
    base_id: usize,
    current_token: usize,
    max_hops: usize,
    depth: usize,
    visited_len: usize,
    running_upper_rate: f64,
    visited_tokens: &mut [usize; MAX_SUPPORTED_HOPS],
    used_pools: &mut [usize; MAX_SUPPORTED_HOPS],
    current_steps: &mut [IndexedStep; MAX_SUPPORTED_HOPS],
    min_amount_in: U256,
    max_amount_in: U256,
    gas_cost_in_base: U256,
    l1_fee_in_base: U256,
    total_gas: U512,
    min_profit_u512: U512,
    cycles_evaluated: &mut usize,
    candidates: &mut Vec<CycleCandidate>,
) -> Result<(), GraphArbError> {
    let edges = &graph.adj[current_token];
    let next_depth = depth + 1;
    for &edge in edges {
        if used_pools[..depth].contains(&edge.pool_idx) {
            continue;
        }
        if edge.to_token == base_id {
            if next_depth >= 2 {
                current_steps[depth] = IndexedStep {
                    pool_idx: edge.pool_idx,
                    zero_for_one: edge.zero_for_one,
                };
                *cycles_evaluated += 1;
                let cycle_upper = running_upper_rate * edge.max_rate_upper;
                if cycle_upper > 0.0 && cycle_upper.is_finite() && cycle_upper <= 1.0 {
                    continue;
                }
                let cycle_steps = &current_steps[..next_depth];
                if let Some(best) = ternary_search_indexed(
                    graph,
                    cycle_steps,
                    min_amount_in,
                    max_amount_in,
                    total_gas,
                    min_profit_u512,
                ) {
                    candidates.push(graph.materialize_candidate(
                        base_id,
                        cycle_steps,
                        best,
                        gas_cost_in_base,
                        l1_fee_in_base,
                    )?);
                }
            }
            continue;
        }
        if next_depth >= max_hops || visited_tokens[..visited_len].contains(&edge.to_token) {
            continue;
        }
        visited_tokens[visited_len] = edge.to_token;
        used_pools[depth] = edge.pool_idx;
        current_steps[depth] = IndexedStep {
            pool_idx: edge.pool_idx,
            zero_for_one: edge.zero_for_one,
        };

        enumerate_indexed_cycles(
            graph,
            base_id,
            edge.to_token,
            max_hops,
            next_depth,
            visited_len + 1,
            running_upper_rate * edge.max_rate_upper,
            visited_tokens,
            used_pools,
            current_steps,
            min_amount_in,
            max_amount_in,
            gas_cost_in_base,
            l1_fee_in_base,
            total_gas,
            min_profit_u512,
            cycles_evaluated,
            candidates,
        )?;
    }
    Ok(())
}

fn evaluate_indexed_cycle_in(
    graph: &IndexedGraph<'_>,
    steps: &[IndexedStep],
    amount_in: U256,
    total_gas: U512,
) -> Result<EvaluatedIndexedPoint, GraphArbError> {
    let mut cur_amount = amount_in;
    for step in steps {
        let pool = graph.pools[step.pool_idx];
        let (amount_out, _) = pool.quote_exact_in_direction(step.zero_for_one, cur_amount)?;
        cur_amount = amount_out;
    }
    let total_cost = amount_in
        .to_u512()
        .checked_add(total_gas)
        .expect("amount_in + two U256 fees fits U512");
    let net_profit = SignedU512::diff(cur_amount.to_u512(), total_cost);
    Ok(EvaluatedIndexedPoint {
        amount_in,
        amount_out: cur_amount,
        net_profit,
    })
}

fn ternary_search_indexed(
    graph: &IndexedGraph<'_>,
    steps: &[IndexedStep],
    min_amount_in: U256,
    max_amount_in: U256,
    total_gas: U512,
    min_profit_u512: U512,
) -> Option<EvaluatedIndexedPoint> {
    let mut lo = min_amount_in;
    let mut hi = max_amount_in;
    let mut best: Option<EvaluatedIndexedPoint> = None;

    let update_best = |best_slot: &mut Option<EvaluatedIndexedPoint>,
                       cand: EvaluatedIndexedPoint| {
        let replace = match best_slot {
            None => true,
            Some(prev) => cand.net_profit > prev.net_profit,
        };
        if replace {
            *best_slot = Some(cand);
        }
    };

    if let Ok(lo_pt) = evaluate_indexed_cycle_in(graph, steps, lo, total_gas) {
        update_best(&mut best, lo_pt);
    }

    let three = U256::from_u64(3);
    let two = U256::from_u64(2);

    for _ in 0..48 {
        let Some(span) = hi.checked_sub(lo) else {
            break;
        };
        if span <= two {
            break;
        }
        let (third, _) = div_rem_u256(span, three).expect("divisor 3 is non-zero");
        let m1 = lo.checked_add(third)?;
        let m2 = hi.checked_sub(third)?;

        match (
            evaluate_indexed_cycle_in(graph, steps, m1, total_gas),
            evaluate_indexed_cycle_in(graph, steps, m2, total_gas),
        ) {
            (Ok(p1), Ok(p2)) => {
                let cmp = p1.net_profit.cmp(&p2.net_profit);
                update_best(&mut best, p1);
                update_best(&mut best, p2);
                match cmp {
                    Ordering::Less => lo = m1,
                    Ordering::Greater => hi = m2,
                    Ordering::Equal => {
                        lo = m1;
                        hi = m2;
                    }
                }
            }
            (Ok(p1), Err(_)) => {
                update_best(&mut best, p1);
                hi = m2.checked_sub(U256::ONE).unwrap_or(m1);
            }
            (Err(_), Ok(p2)) => {
                update_best(&mut best, p2);
                lo = m1.checked_add(U256::ONE).unwrap_or(m2);
            }
            (Err(_), Err(_)) => {
                if m1 <= lo {
                    break;
                }
                hi = m1.checked_sub(U256::ONE).unwrap_or(lo);
            }
        }
    }

    if let Some(span) = hi.checked_sub(lo) {
        if span <= U256::from_u64(4) {
            let mut cur = lo;
            while cur <= hi {
                if let Ok(pt) = evaluate_indexed_cycle_in(graph, steps, cur, total_gas) {
                    update_best(&mut best, pt);
                }
                let Some(next) = cur.checked_add(U256::ONE) else {
                    break;
                };
                cur = next;
            }
        } else {
            let (half, _) = div_rem_u256(span, two).expect("divisor 2 is non-zero");
            let mid = lo.checked_add(half).unwrap_or(lo);
            for probe in [lo, mid, hi] {
                if let Ok(pt) = evaluate_indexed_cycle_in(graph, steps, probe, total_gas) {
                    update_best(&mut best, pt);
                }
            }
        }
    }

    let candidate = best?;
    if candidate.net_profit.negative || candidate.net_profit.mag < min_profit_u512 {
        return None;
    }
    Some(candidate)
}

#[must_use]
fn normalize_id(raw: &str) -> String {
    raw.trim().to_ascii_lowercase()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn token(byte: &str) -> String {
        format!("0x{}", byte.repeat(20))
    }

    #[test]
    fn finds_profitable_cycle_and_rejects_negative_ev() {
        let mut graph = PoolGraph::new();
        let weth = token("11");
        let usdc = token("22");
        let aero = token("33");

        // Pool 1: V2 WETH/USDC cheap WETH (1 WETH = 2000 USDC)
        graph
            .upsert_pool(PoolState {
                chain_id: 8453,
                pool_id: token("a1"),
                kind: PoolKind::V2,
                token0: weth.clone(),
                token1: usdc.clone(),
                decimals0: 18,
                decimals1: 6,
                fee_pips: 3000,
                reserve0: Some(U256::from_u128(100_000_000_000_000_000_000)),
                reserve1: Some(U256::from_u128(200_000_000_000)),
                sqrt_price_x96: None,
                tick: None,
                liquidity: None,
                ticks: vec![],
                block_number: 100,
                timestamp_ms: 1_000_000,
            })
            .unwrap();

        // Pool 2: Slipstream USDC/AERO (1 USDC = 2 AERO, sqrtPriceX96 at tick where price=2e12)
        // Here decimals0=6, decimals1=18, tick 276324 -> ~1e12 raw ratio => 1 USDC = 1 AERO
        // Let's use tick 283255 => ~2e12 raw ratio => 1 USDC = 2 AERO
        let tick2 = 283_255;
        graph
            .upsert_pool(PoolState {
                chain_id: 8453,
                pool_id: token("a2"),
                kind: PoolKind::Slipstream,
                token0: usdc.clone(),
                token1: aero.clone(),
                decimals0: 6,
                decimals1: 18,
                fee_pips: 500,
                reserve0: None,
                reserve1: None,
                sqrt_price_x96: Some(sqrt_price_at_tick(tick2).unwrap()),
                tick: Some(tick2),
                liquidity: Some(1_000_000_000_000_000_000_000),
                ticks: vec![],
                block_number: 100,
                timestamp_ms: 1_000_000,
            })
            .unwrap();

        // Pool 3: V3 WETH/AERO mispriced (1 WETH = 3600 AERO -> selling 4000 AERO returns > 1.1 WETH!)
        // Wait: decimals0=18 (WETH), decimals1=18 (AERO), tick 81890 => 1.0001^81890 ~= 3600
        let tick3 = 81_890;
        graph
            .upsert_pool(PoolState {
                chain_id: 8453,
                pool_id: token("a3"),
                kind: PoolKind::V3,
                token0: weth.clone(),
                token1: aero.clone(),
                decimals0: 18,
                decimals1: 18,
                fee_pips: 500,
                reserve0: None,
                reserve1: None,
                sqrt_price_x96: Some(sqrt_price_at_tick(tick3).unwrap()),
                tick: Some(tick3),
                liquidity: Some(1_000_000_000_000_000_000_000),
                ticks: vec![],
                block_number: 100,
                timestamp_ms: 1_000_000,
            })
            .unwrap();

        let report = graph
            .scan_cycles(
                std::slice::from_ref(&weth),
                3,
                U256::from_u128(10_000_000_000_000_000), // 0.01 WETH
                U256::from_u128(10_000_000_000_000_000_000), // 10 WETH
                U256::from_u128(500_000_000_000_000),    // 0.0005 WETH L2 gas
                U256::from_u128(100_000_000_000_000),    // 0.0001 WETH L1 fee
                U256::from_u128(1_000_000_000_000_000),  // 0.001 WETH min profit
            )
            .unwrap();

        assert_eq!(report.cycles_evaluated, 2); // forward and reverse 3-hop
        assert_eq!(report.candidates.len(), 1);
        let best = &report.candidates[0];
        assert!(best.net_profit >= U256::from_u128(1_000_000_000_000_000));
        assert_eq!(best.hops.len(), 3);

        // Huge gas fee rejects all cycles
        let blocked = graph
            .scan_cycles(
                &[weth],
                3,
                U256::from_u128(10_000_000_000_000_000),
                U256::from_u128(10_000_000_000_000_000_000),
                U256::from_u128(5_000_000_000_000_000_000),
                U256::ZERO,
                U256::ONE,
            )
            .unwrap();
        assert!(blocked.candidates.is_empty());
    }

    #[derive(Clone, Debug)]
    struct ReferenceEvaluatedPoint {
        quote: RouteQuote,
        net_profit: SignedU512,
    }

    #[allow(clippy::too_many_arguments)]
    fn reference_enumerate_cycles(
        chain_id: u64,
        base: &str,
        current_token: &str,
        max_hops: usize,
        adj: &HashMap<String, Vec<(String, String)>>,
        visited_tokens: &mut Vec<String>,
        used_pools: &mut Vec<String>,
        current_route: &mut Vec<RouteStep>,
        out_routes: &mut Vec<Vec<RouteStep>>,
    ) {
        let Some(edges) = adj.get(current_token) else {
            return;
        };
        let depth = current_route.len() + 1;
        for (pool_id, next_token) in edges {
            if used_pools.contains(pool_id) {
                continue;
            }
            if next_token == base {
                if depth >= 2 {
                    let mut completed = current_route.clone();
                    completed.push(RouteStep {
                        chain_id,
                        pool_id: pool_id.clone(),
                        token_in: current_token.to_string(),
                    });
                    out_routes.push(completed);
                }
                continue;
            }
            if depth >= max_hops || visited_tokens.contains(next_token) {
                continue;
            }
            visited_tokens.push(next_token.clone());
            used_pools.push(pool_id.clone());
            current_route.push(RouteStep {
                chain_id,
                pool_id: pool_id.clone(),
                token_in: current_token.to_string(),
            });

            reference_enumerate_cycles(
                chain_id,
                base,
                next_token,
                max_hops,
                adj,
                visited_tokens,
                used_pools,
                current_route,
                out_routes,
            );

            current_route.pop();
            used_pools.pop();
            visited_tokens.pop();
        }
    }

    fn reference_eval_point(
        graph: &PoolGraph,
        route: &[RouteStep],
        amount_in: U256,
        total_gas: U512,
    ) -> Result<ReferenceEvaluatedPoint, GraphArbError> {
        let quote = graph.quote_route(route, amount_in)?;
        let total_cost = amount_in
            .to_u512()
            .checked_add(total_gas)
            .expect("amount_in + two U256 fees fits U512");
        let net_profit = SignedU512::diff(quote.amount_out.to_u512(), total_cost);
        Ok(ReferenceEvaluatedPoint { quote, net_profit })
    }

    fn reference_optimize_route(
        graph: &PoolGraph,
        route: &[RouteStep],
        min_amount_in: U256,
        max_amount_in: U256,
        total_gas: U512,
        min_profit_u512: U512,
    ) -> Option<ReferenceEvaluatedPoint> {
        let mut lo = min_amount_in;
        let mut hi = max_amount_in;
        let mut best: Option<ReferenceEvaluatedPoint> = None;

        let update_best = |best_slot: &mut Option<ReferenceEvaluatedPoint>,
                           cand: ReferenceEvaluatedPoint| {
            let replace = match best_slot {
                None => true,
                Some(prev) => cand.net_profit > prev.net_profit,
            };
            if replace {
                *best_slot = Some(cand);
            }
        };

        if let Ok(lo_pt) = reference_eval_point(graph, route, lo, total_gas) {
            update_best(&mut best, lo_pt);
        }

        let three = U256::from_u64(3);
        let two = U256::from_u64(2);

        for _ in 0..48 {
            let Some(span) = hi.checked_sub(lo) else {
                break;
            };
            if span <= two {
                break;
            }
            let (third, _) = div_rem_u256(span, three).expect("divisor 3 is non-zero");
            let m1 = lo.checked_add(third)?;
            let m2 = hi.checked_sub(third)?;

            match (
                reference_eval_point(graph, route, m1, total_gas),
                reference_eval_point(graph, route, m2, total_gas),
            ) {
                (Ok(p1), Ok(p2)) => {
                    let cmp = p1.net_profit.cmp(&p2.net_profit);
                    update_best(&mut best, p1);
                    update_best(&mut best, p2);
                    match cmp {
                        Ordering::Less => lo = m1,
                        Ordering::Greater => hi = m2,
                        Ordering::Equal => {
                            lo = m1;
                            hi = m2;
                        }
                    }
                }
                (Ok(p1), Err(_)) => {
                    update_best(&mut best, p1);
                    hi = m2.checked_sub(U256::ONE).unwrap_or(m1);
                }
                (Err(_), Ok(p2)) => {
                    update_best(&mut best, p2);
                    lo = m1.checked_add(U256::ONE).unwrap_or(m2);
                }
                (Err(_), Err(_)) => {
                    if m1 <= lo {
                        break;
                    }
                    hi = m1.checked_sub(U256::ONE).unwrap_or(lo);
                }
            }
        }

        if let Some(span) = hi.checked_sub(lo) {
            if span <= U256::from_u64(4) {
                let mut cur = lo;
                while cur <= hi {
                    if let Ok(pt) = reference_eval_point(graph, route, cur, total_gas) {
                        update_best(&mut best, pt);
                    }
                    let Some(next) = cur.checked_add(U256::ONE) else {
                        break;
                    };
                    cur = next;
                }
            } else {
                let (half, _) = div_rem_u256(span, two).expect("divisor 2 is non-zero");
                let mid = lo.checked_add(half).unwrap_or(lo);
                for probe in [lo, mid, hi] {
                    if let Ok(pt) = reference_eval_point(graph, route, probe, total_gas) {
                        update_best(&mut best, pt);
                    }
                }
            }
        }

        let candidate = best?;
        if candidate.net_profit.negative || candidate.net_profit.mag < min_profit_u512 {
            return None;
        }
        Some(candidate)
    }

    #[allow(clippy::too_many_arguments)]
    fn reference_scan_cycles(
        graph: &PoolGraph,
        base_tokens: &[String],
        max_hops: usize,
        min_amount_in: U256,
        max_amount_in: U256,
        gas_cost_in_base: U256,
        l1_fee_in_base: U256,
        min_net_profit: U256,
    ) -> CycleScanReport {
        let normalized_bases: BTreeSet<String> =
            base_tokens.iter().map(|t| normalize_id(t)).collect();
        let mut by_chain: BTreeMap<u64, HashMap<String, Vec<(String, String)>>> = BTreeMap::new();
        for ((chain_id, _), pool) in &graph.pools {
            let adj = by_chain.entry(*chain_id).or_default();
            adj.entry(pool.token0.clone())
                .or_default()
                .push((pool.pool_id.clone(), pool.token1.clone()));
            adj.entry(pool.token1.clone())
                .or_default()
                .push((pool.pool_id.clone(), pool.token0.clone()));
        }

        let total_gas = gas_cost_in_base
            .to_u512()
            .checked_add(l1_fee_in_base.to_u512())
            .expect("sum fits U512");
        let min_profit_u512 = min_net_profit.to_u512();
        let mut cycles_evaluated = 0usize;
        let mut candidates = Vec::new();

        for (chain_id, adj) in &by_chain {
            for base in &normalized_bases {
                if !adj.contains_key(base) {
                    continue;
                }
                let mut routes = Vec::new();
                let mut current_route = Vec::new();
                let mut visited_tokens = vec![base.clone()];
                let mut used_pools = Vec::new();
                reference_enumerate_cycles(
                    *chain_id,
                    base,
                    base,
                    max_hops,
                    adj,
                    &mut visited_tokens,
                    &mut used_pools,
                    &mut current_route,
                    &mut routes,
                );
                for route in routes {
                    cycles_evaluated += 1;
                    if let Some(best) = reference_optimize_route(
                        graph,
                        &route,
                        min_amount_in,
                        max_amount_in,
                        total_gas,
                        min_profit_u512,
                    ) {
                        candidates.push(CycleCandidate {
                            chain_id: *chain_id,
                            base_token: base.clone(),
                            hops: best.quote.hops,
                            optimal_amount_in: best.quote.amount_in,
                            amount_out: best.quote.amount_out,
                            gas_cost_in_base,
                            l1_fee_in_base,
                            net_profit: best.net_profit.mag.to_u256().unwrap(),
                        });
                    }
                }
            }
        }
        candidates.sort_by(|a, b| {
            b.net_profit
                .cmp(&a.net_profit)
                .then_with(|| a.chain_id.cmp(&b.chain_id))
                .then_with(|| a.base_token.cmp(&b.base_token))
                .then_with(|| a.hops.len().cmp(&b.hops.len()))
        });
        CycleScanReport {
            candidates,
            cycles_evaluated,
            elapsed_ns: 0,
        }
    }

    fn build_six_pool_multidex_graph() -> (PoolGraph, String, String) {
        let mut graph = PoolGraph::new();
        let weth = token("11");
        let usdc = token("22");
        let usdt = token("33");
        let aero = token("44");

        // Pool 1: V3 WETH/USDC (~3000 USDC/WETH)
        let tick_v3 = -196_258;
        graph
            .upsert_pool(PoolState {
                chain_id: 8453,
                pool_id: token("a1"),
                kind: PoolKind::V3,
                token0: weth.clone(),
                token1: usdc.clone(),
                decimals0: 18,
                decimals1: 6,
                fee_pips: 500,
                reserve0: None,
                reserve1: None,
                sqrt_price_x96: Some(sqrt_price_at_tick(tick_v3).unwrap()),
                tick: Some(tick_v3),
                liquidity: Some(20_000_000_000_000_000),
                ticks: vec![
                    TickLiquidity {
                        tick: -196_800,
                        liquidity_net: 10_000_000_000_000_000,
                    },
                    TickLiquidity {
                        tick: -195_800,
                        liquidity_net: -10_000_000_000_000_000,
                    },
                ],
                block_number: 21_000_000,
                timestamp_ms: 1_760_000_000_000,
            })
            .unwrap();

        // Pool 2: AerodromeStable USDC/USDT (slightly imbalanced)
        graph
            .upsert_pool(PoolState {
                chain_id: 8453,
                pool_id: token("a2"),
                kind: PoolKind::AerodromeStable,
                token0: usdc.clone(),
                token1: usdt.clone(),
                decimals0: 6,
                decimals1: 6,
                fee_pips: 100,
                reserve0: Some(U256::from_u64(5_000_000_000_000)),
                reserve1: Some(U256::from_u64(5_050_000_000_000)),
                sqrt_price_x96: None,
                tick: None,
                liquidity: None,
                ticks: vec![],
                block_number: 21_000_000,
                timestamp_ms: 1_760_000_000_000,
            })
            .unwrap();

        // Pool 3: Slipstream WETH/USDC (~3150 USDC/WETH)
        let tick_ss = -195_770;
        graph
            .upsert_pool(PoolState {
                chain_id: 8453,
                pool_id: token("a3"),
                kind: PoolKind::Slipstream,
                token0: weth.clone(),
                token1: usdc.clone(),
                decimals0: 18,
                decimals1: 6,
                fee_pips: 500,
                reserve0: None,
                reserve1: None,
                sqrt_price_x96: Some(sqrt_price_at_tick(tick_ss).unwrap()),
                tick: Some(tick_ss),
                liquidity: Some(20_000_000_000_000_000),
                ticks: vec![
                    TickLiquidity {
                        tick: -196_500,
                        liquidity_net: 5_000_000_000_000_000,
                    },
                    TickLiquidity {
                        tick: -195_200,
                        liquidity_net: -5_000_000_000_000_000,
                    },
                ],
                block_number: 21_000_000,
                timestamp_ms: 1_760_000_000_000,
            })
            .unwrap();

        // Pool 4: V2 WETH/USDT (3250 USDT/WETH)
        graph
            .upsert_pool(PoolState {
                chain_id: 8453,
                pool_id: token("a4"),
                kind: PoolKind::V2,
                token0: weth.clone(),
                token1: usdt.clone(),
                decimals0: 18,
                decimals1: 6,
                fee_pips: 3000,
                reserve0: Some(U256::from_u128(100_000_000_000_000_000_000)),
                reserve1: Some(U256::from_u128(325_000_000_000)),
                sqrt_price_x96: None,
                tick: None,
                liquidity: None,
                ticks: vec![],
                block_number: 21_000_000,
                timestamp_ms: 1_760_000_000_000,
            })
            .unwrap();

        // Pool 5: V4 USDC/AERO (~2 AERO per USDC)
        let tick_v4 = 283_255;
        graph
            .upsert_pool(PoolState {
                chain_id: 8453,
                pool_id: format!("0x{}", "a5".repeat(32)),
                kind: PoolKind::V4,
                token0: usdc.clone(),
                token1: aero.clone(),
                decimals0: 6,
                decimals1: 18,
                fee_pips: 500,
                reserve0: None,
                reserve1: None,
                sqrt_price_x96: Some(sqrt_price_at_tick(tick_v4).unwrap()),
                tick: Some(tick_v4),
                liquidity: Some(1_000_000_000_000_000_000_000),
                ticks: vec![],
                block_number: 21_000_000,
                timestamp_ms: 1_760_000_000_000,
            })
            .unwrap();

        // Pool 6: V2 WETH/AERO (1 WETH = 5800 AERO)
        graph
            .upsert_pool(PoolState {
                chain_id: 8453,
                pool_id: token("a6"),
                kind: PoolKind::V2,
                token0: weth.clone(),
                token1: aero,
                decimals0: 18,
                decimals1: 18,
                fee_pips: 3000,
                reserve0: Some(U256::from_u128(50_000_000_000_000_000_000)),
                reserve1: Some(U256::from_u128(290_000_000_000_000_000_000_000)),
                sqrt_price_x96: None,
                tick: None,
                liquidity: None,
                ticks: vec![],
                block_number: 21_000_000,
                timestamp_ms: 1_760_000_000_000,
            })
            .unwrap();

        (graph, weth, usdt)
    }

    #[test]
    fn indexed_hot_loop_matches_reference_bit_for_bit_and_meets_latency_budget() {
        let (graph, weth, usdt) = build_six_pool_multidex_graph();
        let min_in = U256::from_u128(10_000_000_000_000_000);
        let max_in = U256::from_u128(5_000_000_000_000_000_000);
        let gas_l2 = U256::from_u128(200_000_000_000_000);
        let gas_l1 = U256::from_u128(50_000_000_000_000);
        let min_profit = U256::from_u128(100_000_000_000_000);

        for max_hops in [2usize, 3, 4] {
            let bases = [weth.to_ascii_uppercase(), usdt.clone()];
            let actual = graph
                .scan_cycles(&bases, max_hops, min_in, max_in, gas_l2, gas_l1, min_profit)
                .unwrap();
            let expected = reference_scan_cycles(
                &graph, &bases, max_hops, min_in, max_in, gas_l2, gas_l1, min_profit,
            );

            assert_eq!(actual.cycles_evaluated, expected.cycles_evaluated);
            assert_eq!(actual.candidates, expected.candidates);
            assert!(!actual.candidates.is_empty());
            for (act, exp) in actual.candidates.iter().zip(expected.candidates.iter()) {
                assert_eq!(act.route_hash(), exp.route_hash());
            }
        }

        // Measure scan latency across a 6-pool multi-DEX graph (V2, V3, Slipstream, V4)
        let mut perf_graph = PoolGraph::new();
        let p_weth = token("11");
        let p_usdc = token("22");
        let p_usdt = token("33");
        for (idx, r1, fee) in [
            ("b1", 295_000u128, 3000u32),
            ("b2", 315_000u128, 500u32),
            ("b6", 320_000u128, 3000u32),
        ] {
            perf_graph
                .upsert_pool(PoolState {
                    chain_id: 8453,
                    pool_id: token(idx),
                    kind: PoolKind::V2,
                    token0: p_weth.clone(),
                    token1: p_usdc.clone(),
                    decimals0: 18,
                    decimals1: 6,
                    fee_pips: fee,
                    reserve0: Some(U256::from_u128(100_000_000_000_000_000_000)),
                    reserve1: Some(U256::from_u128(r1 * 1_000_000)),
                    sqrt_price_x96: None,
                    tick: None,
                    liquidity: None,
                    ticks: vec![],
                    block_number: 21_000_000,
                    timestamp_ms: 1_760_000_000_000,
                })
                .unwrap();
        }
        for (idx, kind, tok0, tok1) in [
            ("b3", PoolKind::V3, p_usdc.clone(), p_usdt.clone()),
            ("b4", PoolKind::Slipstream, p_usdc.clone(), p_usdt.clone()),
            ("b5", PoolKind::V4, p_usdc.clone(), p_usdt.clone()),
        ] {
            let pid = if kind == PoolKind::V4 {
                format!("0x{}", idx.repeat(32))
            } else {
                token(idx)
            };
            perf_graph
                .upsert_pool(PoolState {
                    chain_id: 8453,
                    pool_id: pid,
                    kind,
                    token0: tok0,
                    token1: tok1,
                    decimals0: 6,
                    decimals1: 6,
                    fee_pips: 500,
                    reserve0: None,
                    reserve1: None,
                    sqrt_price_x96: Some(sqrt_price_at_tick(0).unwrap()),
                    tick: Some(0),
                    liquidity: Some(20_000_000_000_000_000),
                    ticks: vec![],
                    block_number: 21_000_000,
                    timestamp_ms: 1_760_000_000_000,
                })
                .unwrap();
        }

        let iterations = 20u32;
        let t0 = Instant::now();
        let mut last_cycles = 0usize;
        let mut last_candidates = 0usize;
        for _ in 0..iterations {
            let report = perf_graph
                .scan_cycles(
                    std::slice::from_ref(&p_weth),
                    2,
                    min_in,
                    max_in,
                    gas_l2,
                    gas_l1,
                    min_profit,
                )
                .unwrap();
            last_cycles = report.cycles_evaluated;
            last_candidates = report.candidates.len();
        }
        let avg_us = t0.elapsed().as_micros() / u128::from(iterations);
        eprintln!(
            "6-pool multi-DEX 2-hop ({last_cycles} cycles, {last_candidates} candidates): indexed={avg_us} us/scan"
        );
        assert_eq!(last_cycles, 6);
        assert!(last_candidates >= 1);
        let budget_us: u128 = if cfg!(debug_assertions) {
            10_000
        } else {
            2_000
        };
        assert!(
            avg_us < budget_us,
            "indexed scan_cycles took {avg_us} us/scan (budget < {budget_us} us)"
        );
    }

    #[test]
    fn prefilter_matches_unpruned_reference_with_one_wei_truncation() {
        assert_eq!(u256_to_f64_lossy(U256::ZERO), 0.0);
        assert_eq!(u256_to_f64_lossy(U256::ONE), 1.0);
        assert_eq!(u256_to_f64_lossy(U256::Q96), 2.0_f64.powi(96));
        assert!(u256_to_f64_lossy(U256::MAX).is_finite());

        let (mut graph, weth, usdt) = build_six_pool_multidex_graph();
        let usdc = token("22");
        let aero = token("44");
        let cbbtc = token("55");

        // Add a V2 WETH/USDC pool where 1 wei WETH truncates to 0 USDC,
        // yet 1 WETH swaps to ~2991 USDC and completes profitable multi-hop cycles.
        let trunc_pool_id = token("c1");
        graph
            .upsert_pool(PoolState {
                chain_id: 8453,
                pool_id: trunc_pool_id.clone(),
                kind: PoolKind::V2,
                token0: weth.clone(),
                token1: usdc.clone(),
                decimals0: 18,
                decimals1: 6,
                fee_pips: 3000,
                reserve0: Some(U256::from_u128(100_000_000_000_000_000_000)),
                reserve1: Some(U256::from_u128(300_000_000_000)),
                sqrt_price_x96: None,
                tick: None,
                liquidity: None,
                ticks: vec![],
                block_number: 21_000_000,
                timestamp_ms: 1_760_000_000_000,
            })
            .unwrap();

        // Confirm hop 1 literally truncates to 0 at min_amount_in = 1 wei.
        let one_wei_quote = graph
            .quote_pool(8453, &trunc_pool_id, &weth, U256::ONE)
            .unwrap();
        assert_eq!(one_wei_quote.amount_out, U256::ZERO);

        // Add aligned & slightly negative-spread cbBTC/WETH/USDC/AERO pools across
        // Slipstream and V3 to exercise pruning without false negatives.
        // 1 WETH = 0.05 cbBTC (8 dec vs 18 dec -> raw ratio 5e-12 -> tick ~ -260_234).
        graph
            .upsert_pool(PoolState {
                chain_id: 8453,
                pool_id: token("c2"),
                kind: PoolKind::V3,
                token0: weth.clone(),
                token1: cbbtc.clone(),
                decimals0: 18,
                decimals1: 8,
                fee_pips: 500,
                reserve0: None,
                reserve1: None,
                sqrt_price_x96: Some(sqrt_price_at_tick(-260_234).unwrap()),
                tick: Some(-260_234),
                liquidity: Some(1_000_000_000_000_000_000),
                ticks: vec![],
                block_number: 21_000_000,
                timestamp_ms: 1_760_000_000_000,
            })
            .unwrap();
        // 1 cbBTC = 60_000 USDC (8 dec vs 6 dec -> raw ratio 600 -> tick ~ 63_972).
        graph
            .upsert_pool(PoolState {
                chain_id: 8453,
                pool_id: token("c3"),
                kind: PoolKind::Slipstream,
                token0: cbbtc.clone(),
                token1: usdc.clone(),
                decimals0: 8,
                decimals1: 6,
                fee_pips: 500,
                reserve0: None,
                reserve1: None,
                sqrt_price_x96: Some(sqrt_price_at_tick(63_972).unwrap()),
                tick: Some(63_972),
                liquidity: Some(1_000_000_000_000_000),
                ticks: vec![],
                block_number: 21_000_000,
                timestamp_ms: 1_760_000_000_000,
            })
            .unwrap();
        // 0-fee exact break-even V2 pool pair to verify safety margin around rate=1.0.
        graph
            .upsert_pool(PoolState {
                chain_id: 8453,
                pool_id: token("c4"),
                kind: PoolKind::V2,
                token0: weth.clone(),
                token1: aero.clone(),
                decimals0: 18,
                decimals1: 18,
                fee_pips: 0,
                reserve0: Some(U256::from_u128(50_000_000_000_000_000_000)),
                reserve1: Some(U256::from_u128(300_000_000_000_000_000_000_000)),
                sqrt_price_x96: None,
                tick: None,
                liquidity: None,
                ticks: vec![],
                block_number: 21_000_000,
                timestamp_ms: 1_760_000_000_000,
            })
            .unwrap();

        let min_in = U256::ONE;
        let max_in = U256::from_u128(5_000_000_000_000_000_000);
        let bases = [weth.clone(), usdt];

        for max_hops in [2usize, 3, 4] {
            for (gas_l2, gas_l1, min_profit) in [
                (U256::ZERO, U256::ZERO, U256::ZERO),
                (
                    U256::from_u128(200_000_000_000_000),
                    U256::from_u128(50_000_000_000_000),
                    U256::from_u128(100_000_000_000_000),
                ),
            ] {
                let actual = graph
                    .scan_cycles(&bases, max_hops, min_in, max_in, gas_l2, gas_l1, min_profit)
                    .unwrap();
                let expected = reference_scan_cycles(
                    &graph, &bases, max_hops, min_in, max_in, gas_l2, gas_l1, min_profit,
                );
                assert_eq!(actual.cycles_evaluated, expected.cycles_evaluated);
                assert_eq!(actual.candidates, expected.candidates);
                assert!(!actual.candidates.is_empty());
            }
        }

        // Verify the 1-wei-truncating pool participates in a winning candidate.
        let report_1wei = graph
            .scan_cycles(
                std::slice::from_ref(&weth),
                3,
                min_in,
                max_in,
                U256::ZERO,
                U256::ZERO,
                U256::ONE,
            )
            .unwrap();
        assert!(
            report_1wei
                .candidates
                .iter()
                .any(|c| c.hops[0].pool_id == trunc_pool_id
                    && c.optimal_amount_in > U256::from_u64(1_000_000_000_000))
        );
    }

    #[test]
    fn prefilter_30_plus_pool_multidex_benchmark() {
        let mut graph = PoolGraph::new();
        let tokens: Vec<String> = (1u8..=6).map(|i| token(&format!("{i:02x}"))).collect();

        // Build 34 pools across all 15 pairs of 6 tokens (V2, V3, Slipstream, V4), aligned at
        // 1:1 raw ratio with 30 bps V2 / 15 bps CL fees, plus one +25 bps dislocated Slipstream
        // pool on (0, 1) and one tight 5 bps V3 pool on (0, 1) so 2-hop arb exists while
        // multi-hop aligned cycles (>=1000) are pruned by the spot upper-bound prefilter.
        let mut pool_counter = 1u16;
        let mut next_pool_id = |is_v4: bool| -> String {
            let id = pool_counter;
            pool_counter += 1;
            if is_v4 {
                format!("0x{id:064x}")
            } else {
                format!("0x{id:040x}")
            }
        };

        for i in 0..tokens.len() {
            for j in (i + 1)..tokens.len() {
                // Pool A: V2 aligned at 1:1 with 30 bps fee
                graph
                    .upsert_pool(PoolState {
                        chain_id: 8453,
                        pool_id: next_pool_id(false),
                        kind: PoolKind::V2,
                        token0: tokens[i].clone(),
                        token1: tokens[j].clone(),
                        decimals0: 18,
                        decimals1: 18,
                        fee_pips: 3000,
                        reserve0: Some(U256::from_u128(1_000_000_000_000_000_000_000)),
                        reserve1: Some(U256::from_u128(1_000_000_000_000_000_000_000)),
                        sqrt_price_x96: None,
                        tick: None,
                        liquidity: None,
                        ticks: vec![],
                        block_number: 22_000_000,
                        timestamp_ms: 1_794_000_000_000,
                    })
                    .unwrap();

                // Pool B: alternate across V3, Slipstream, and V4 (aligned at tick=0, 15 bps fee)
                let kind = match (i + j) % 3 {
                    0 => PoolKind::V3,
                    1 => PoolKind::Slipstream,
                    _ => PoolKind::V4,
                };
                graph
                    .upsert_pool(PoolState {
                        chain_id: 8453,
                        pool_id: next_pool_id(kind == PoolKind::V4),
                        kind,
                        token0: tokens[i].clone(),
                        token1: tokens[j].clone(),
                        decimals0: 18,
                        decimals1: 18,
                        fee_pips: 1500,
                        reserve0: None,
                        reserve1: None,
                        sqrt_price_x96: Some(sqrt_price_at_tick(0).unwrap()),
                        tick: Some(0),
                        liquidity: Some(500_000_000_000_000_000_000),
                        ticks: vec![],
                        block_number: 22_000_000,
                        timestamp_ms: 1_794_000_000_000,
                    })
                    .unwrap();
            }
        }

        // Add 4 extra pools (total 34 pools): one dislocated Slipstream (0, 1) at +28 ticks
        // (1 bps fee) paired with a 5 bps V3 (0, 1) at tick=0, plus two aligned Slipstream pools.
        for &(a, b, tick, fee) in &[
            (0usize, 1usize, 28i32, 100u32),
            (0, 1, 0, 500),
            (2, 3, 0, 500),
            (3, 4, 0, 500),
        ] {
            graph
                .upsert_pool(PoolState {
                    chain_id: 8453,
                    pool_id: next_pool_id(false),
                    kind: PoolKind::Slipstream,
                    token0: tokens[a].clone(),
                    token1: tokens[b].clone(),
                    decimals0: 18,
                    decimals1: 18,
                    fee_pips: fee,
                    reserve0: None,
                    reserve1: None,
                    sqrt_price_x96: Some(sqrt_price_at_tick(tick).unwrap()),
                    tick: Some(tick),
                    liquidity: Some(5_000_000_000_000_000_000_000),
                    ticks: vec![],
                    block_number: 22_000_000,
                    timestamp_ms: 1_794_000_000_000,
                })
                .unwrap();
        }
        assert!(graph.len() >= 30);

        let min_in = U256::from_u128(10_000_000_000_000_000);
        let max_in = U256::from_u128(10_000_000_000_000_000_000);
        let gas_l2 = U256::from_u128(200_000_000_000_000);
        let gas_l1 = U256::from_u128(50_000_000_000_000);
        let min_profit = U256::from_u128(100_000_000_000_000);

        let iterations = 5u32;
        let t0 = Instant::now();
        let mut last_cycles = 0usize;
        let mut last_candidates = 0usize;
        for _ in 0..iterations {
            let report = graph
                .scan_cycles(
                    std::slice::from_ref(&tokens[0]),
                    4,
                    min_in,
                    max_in,
                    gas_l2,
                    gas_l1,
                    min_profit,
                )
                .unwrap();
            last_cycles = report.cycles_evaluated;
            last_candidates = report.candidates.len();
        }
        let avg_us = t0.elapsed().as_micros() / u128::from(iterations);
        eprintln!(
            "34-pool 6-token 4-hop ({last_cycles} cycles, {last_candidates} candidates): prefiltered={avg_us} us/scan"
        );
        assert!(
            last_cycles >= 1000,
            "expected >= 1000 cycles, got {last_cycles}"
        );
        assert!(last_candidates >= 1);
        let budget_us: u128 = if cfg!(debug_assertions) {
            250_000
        } else {
            50_000
        };
        assert!(
            avg_us < budget_us,
            "34-pool scan_cycles took {avg_us} us/scan (budget < {budget_us} us)"
        );
    }
}
