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
                if tick < MAX_TICK && sqrt_p >= sqrt_price_at_tick(tick + 1)? {
                    return Err(GraphArbError::Math(PoolMathError::TickPriceMismatch));
                }
                self.ticks.sort_by_key(|t| t.tick);
                validate_sorted_ticks(&self.ticks)?;
            }
        }
        Ok(self)
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

        let (amount_out, fee_paid) = match self.kind {
            PoolKind::V2 => {
                let r0 = self.reserve0.ok_or(PoolMathError::InsufficientLiquidity)?;
                let r1 = self.reserve1.ok_or(PoolMathError::InsufficientLiquidity)?;
                let (rin, rout) = if zero_for_one { (r0, r1) } else { (r1, r0) };
                let res = v2_swap_exact_in(rin, rout, self.fee_pips, amount_in)?;
                (res.amount_out, res.fee_paid)
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
                (res.amount_out, res.fee_paid)
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
                (res.amount_out, res.fee_paid)
            }
        };

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

#[derive(Clone, Debug)]
struct EvaluatedPoint {
    quote: RouteQuote,
    net_profit: SignedU512,
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
        self.pools.insert(
            (normalized.chain_id, normalized.pool_id.clone()),
            normalized,
        );
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

    /// Enumerates simple 2-hop and 3-hop cycles starting & ending at each `base_token`,
    /// optimizing `amount_in` with 48-iteration exact integer ternary search.
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
        if !(2..=4).contains(&max_hops) {
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

        // Build per-chain directed adjacency lists: token -> Vec<(pool_id, next_token)>
        let mut by_chain: BTreeMap<u64, HashMap<String, Vec<(String, String)>>> = BTreeMap::new();
        for ((chain_id, _), pool) in &self.pools {
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
            .expect("sum of two U256 values fits U512");
        let min_profit_u512 = min_net_profit.to_u512();

        let mut cycles_evaluated = 0usize;
        let mut candidates = Vec::new();

        for (chain_id, adj) in &by_chain {
            for base in &normalized_bases {
                if !adj.contains_key(base) {
                    continue;
                }
                let mut routes: Vec<Vec<RouteStep>> = Vec::new();
                let mut current_route: Vec<RouteStep> = Vec::new();
                let mut visited_tokens: Vec<String> = vec![base.clone()];
                let mut used_pools: Vec<String> = Vec::new();

                enumerate_simple_cycles(
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
                    if let Some(best) = self.optimize_route(
                        &route,
                        min_amount_in,
                        max_amount_in,
                        total_gas,
                        min_profit_u512,
                    ) {
                        let net_profit = best
                            .net_profit
                            .mag
                            .to_u256()
                            .expect("positive net_profit <= amount_out fits U256");
                        candidates.push(CycleCandidate {
                            chain_id: *chain_id,
                            base_token: base.clone(),
                            hops: best.quote.hops,
                            optimal_amount_in: best.quote.amount_in,
                            amount_out: best.quote.amount_out,
                            gas_cost_in_base,
                            l1_fee_in_base,
                            net_profit,
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

        let elapsed_ns = u64::try_from(start.elapsed().as_nanos()).unwrap_or(u64::MAX);
        Ok(CycleScanReport {
            candidates,
            cycles_evaluated,
            elapsed_ns,
        })
    }

    fn eval_route_point(
        &self,
        route: &[RouteStep],
        amount_in: U256,
        total_gas: U512,
    ) -> Result<EvaluatedPoint, GraphArbError> {
        let quote = self.quote_route(route, amount_in)?;
        let total_cost = amount_in
            .to_u512()
            .checked_add(total_gas)
            .expect("amount_in + two U256 fees fits U512");
        let net_profit = SignedU512::diff(quote.amount_out.to_u512(), total_cost);
        Ok(EvaluatedPoint { quote, net_profit })
    }

    fn optimize_route(
        &self,
        route: &[RouteStep],
        min_amount_in: U256,
        max_amount_in: U256,
        total_gas: U512,
        min_profit_u512: U512,
    ) -> Option<EvaluatedPoint> {
        let mut lo = min_amount_in;
        let mut hi = max_amount_in;
        let mut best: Option<EvaluatedPoint> = None;

        let update_best = |best_slot: &mut Option<EvaluatedPoint>, cand: EvaluatedPoint| {
            let replace = match best_slot {
                None => true,
                Some(prev) => cand.net_profit > prev.net_profit,
            };
            if replace {
                *best_slot = Some(cand);
            }
        };

        if let Ok(lo_pt) = self.eval_route_point(route, lo, total_gas) {
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
                self.eval_route_point(route, m1, total_gas),
                self.eval_route_point(route, m2, total_gas),
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
                    if let Ok(pt) = self.eval_route_point(route, cur, total_gas) {
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
                    if let Ok(pt) = self.eval_route_point(route, probe, total_gas) {
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
}

#[allow(clippy::too_many_arguments)]
fn enumerate_simple_cycles(
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

        enumerate_simple_cycles(
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
}
