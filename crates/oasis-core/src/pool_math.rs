use serde::{Deserialize, Deserializer, Serialize, Serializer, de};
use thiserror::Error;

use crate::u256_math::{U256, mul_div_down, mul_div_up, mul_u256_to_u512};

pub const MIN_TICK: i32 = -887_272;
pub const MAX_TICK: i32 = 887_272;
pub const MIN_SQRT_PRICE: U256 = U256([4_295_128_739, 0, 0, 0]);
pub const MAX_SQRT_PRICE: U256 = U256([
    0x5d95_1d52_6398_8d26,
    0xefd1_fc6a_5064_8849,
    0x0000_0000_fffd_8963,
    0,
]);
pub const FEE_PIPS_DENOM: u32 = 1_000_000;
pub const E18: U256 = U256([1_000_000_000_000_000_000, 0, 0, 0]);

const TICK_RATIOS: [u128; 20] = [
    0xFFFCB933BD6FAD37AA2D162D1A594001,
    0xFFF97272373D413259A46990580E213A,
    0xFFF2E50F5F656932EF12357CF3C7FDCC,
    0xFFE5CACA7E10E4E61C3624EAA0941CD0,
    0xFFCB9843D60F6159C9DB58835C926644,
    0xFF973B41FA98C081472E6896DFB254C0,
    0xFF2EA16466C96A3843EC78B326B52861,
    0xFE5DEE046A99A2A811C461F1969C3053,
    0xFCBE86C7900A88AEDCFFC83B479AA3A4,
    0xF987A7253AC413176F2B074CF7815E54,
    0xF3392B0822B70005940C7A398E4B70F3,
    0xE7159475A2C29B7443B29C7FA6E889D9,
    0xD097F3BDFD2022B8845AD8F792AA5825,
    0xA9F746462D870FDF8A65DC1F90E061E5,
    0x70D869A156D2A1B890BB3DF62BAF32F7,
    0x31BE135F97D08FD981231505542FCFA6,
    0x9AA508B5B7A84E1C677DE54F3E99BC9,
    0x5D6AF8DEDB81196699C329225EE604,
    0x2216E584F5FA1EA926041BEDFE98,
    0x48A170391F7DC42444E8FA2,
];

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum PoolMathError {
    #[error("tick {0} is outside canonical TickMath bounds")]
    TickOutOfBounds(i32),
    #[error("sqrt price is outside canonical TickMath bounds")]
    SqrtPriceOutOfBounds,
    #[error("sqrt price does not fit uint160")]
    SqrtPriceNotUint160,
    #[error("lower sqrt price cannot be zero")]
    ZeroSqrtPrice,
    #[error("sqrt price range must be ordered")]
    UnorderedSqrtPriceRange,
    #[error("fee_pips {0} must be < 1_000_000")]
    InvalidFeePips(u32),
    #[error("amount_in must be positive")]
    ZeroAmount,
    #[error("insufficient pool liquidity")]
    InsufficientLiquidity,
    #[error("math overflow")]
    MathOverflow,
    #[error("division by zero")]
    DivisionByZero,
    #[error("tick list must be strictly ascending")]
    UnsortedOrDuplicateTicks,
    #[error("pool tick does not match sqrt_price_x96")]
    TickPriceMismatch,
    #[error("decimals {0} exceeds supported maximum 36")]
    InvalidDecimals(u8),
    #[error("AerodromeStable Newton-Raphson solver did not converge in 255 iterations")]
    StableDidNotConverge,
}

pub mod serde_i128_str {
    use super::*;

    pub fn serialize<S: Serializer>(val: &i128, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&val.to_string())
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<i128, D::Error> {
        struct Visitor;
        impl de::Visitor<'_> for Visitor {
            type Value = i128;

            fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                formatter.write_str("a signed base-10 integer string")
            }

            fn visit_str<E: de::Error>(self, v: &str) -> Result<Self::Value, E> {
                v.parse::<i128>().map_err(de::Error::custom)
            }
        }
        deserializer.deserialize_str(Visitor)
    }
}

pub mod serde_u128_str {
    use super::*;

    pub fn serialize<S: Serializer>(val: &u128, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&val.to_string())
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<u128, D::Error> {
        struct Visitor;
        impl de::Visitor<'_> for Visitor {
            type Value = u128;

            fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                formatter.write_str("an unsigned integer string")
            }

            fn visit_str<E: de::Error>(self, v: &str) -> Result<Self::Value, E> {
                let u = U256::from_str_auto(v).map_err(de::Error::custom)?;
                u.to_u128()
                    .ok_or_else(|| de::Error::custom("value exceeds u128"))
            }
        }
        deserializer.deserialize_str(Visitor)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct TickLiquidity {
    pub tick: i32,
    #[serde(with = "serde_i128_str")]
    pub liquidity_net: i128,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SwapStepResult {
    pub sqrt_ratio_next_x96: U256,
    pub amount_in: U256,
    pub amount_out: U256,
    pub fee_amount: U256,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ClSwapResult {
    pub amount_out: U256,
    pub fee_paid: U256,
    pub sqrt_price_after_x96: U256,
    pub tick_after: i32,
    pub liquidity_after: u128,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SimpleSwapResult {
    pub amount_out: U256,
    pub fee_paid: U256,
}

/// Bit-exact port of `liquidity_math.sqrt_price_at_tick`.
pub fn sqrt_price_at_tick(tick: i32) -> Result<U256, PoolMathError> {
    if !(MIN_TICK..=MAX_TICK).contains(&tick) {
        return Err(PoolMathError::TickOutOfBounds(tick));
    }
    let abs_tick = tick.unsigned_abs();
    let mut ratio = if (abs_tick & 1) != 0 {
        U256::from_u128(TICK_RATIOS[0])
    } else {
        U256::Q128
    };
    for (bit, &multiplier) in TICK_RATIOS.iter().enumerate().skip(1) {
        if (abs_tick & (1u32 << bit)) != 0 {
            let prod = mul_u256_to_u512(ratio, U256::from_u128(multiplier));
            ratio = prod
                .checked_shr(128)
                .and_then(|v| v.to_u256())
                .ok_or(PoolMathError::MathOverflow)?;
        }
    }
    if tick > 0 {
        ratio = U256::MAX
            .checked_div(ratio)
            .ok_or(PoolMathError::DivisionByZero)?;
    }
    let round_bias = U256::from_u64((1u64 << 32) - 1);
    ratio
        .checked_add(round_bias)
        .and_then(|v| v.checked_shr(32))
        .ok_or(PoolMathError::MathOverflow)
}

/// Exact inverse of `sqrt_price_at_tick`: returns `tick` such that
/// `sqrt_price_at_tick(tick) <= sqrt_price_x96 < sqrt_price_at_tick(tick + 1)`
/// (or `MAX_TICK` when `sqrt_price_x96 == MAX_SQRT_PRICE`).
pub fn tick_at_sqrt_price(sqrt_price_x96: U256) -> Result<i32, PoolMathError> {
    if sqrt_price_x96 < MIN_SQRT_PRICE || sqrt_price_x96 > MAX_SQRT_PRICE {
        return Err(PoolMathError::SqrtPriceOutOfBounds);
    }
    if sqrt_price_x96 == MAX_SQRT_PRICE {
        return Ok(MAX_TICK);
    }
    let mut lo = MIN_TICK;
    let mut hi = MAX_TICK - 1;
    let mut ans = MIN_TICK;
    while lo <= hi {
        let mid = lo + ((hi - lo) / 2);
        let p = sqrt_price_at_tick(mid)?;
        if p <= sqrt_price_x96 {
            ans = mid;
            lo = mid + 1;
        } else {
            hi = mid - 1;
        }
    }
    Ok(ans)
}

/// Bit-exact port of `liquidity_math.amount0_delta`.
pub fn amount0_delta(
    sqrt_price_a_x96: U256,
    sqrt_price_b_x96: U256,
    liquidity: u128,
    round_up: bool,
) -> Result<U256, PoolMathError> {
    if !sqrt_price_a_x96.fits_bits(160) || !sqrt_price_b_x96.fits_bits(160) {
        return Err(PoolMathError::SqrtPriceNotUint160);
    }
    let (lower, upper) = if sqrt_price_a_x96 <= sqrt_price_b_x96 {
        (sqrt_price_a_x96, sqrt_price_b_x96)
    } else {
        (sqrt_price_b_x96, sqrt_price_a_x96)
    };
    if lower.is_zero() {
        return Err(PoolMathError::ZeroSqrtPrice);
    }
    let numerator1 = U256::from_u128(liquidity)
        .checked_shl(96)
        .ok_or(PoolMathError::MathOverflow)?;
    let numerator2 = upper
        .checked_sub(lower)
        .ok_or(PoolMathError::MathOverflow)?;
    if round_up {
        let intermediate =
            mul_div_up(numerator1, numerator2, upper).ok_or(PoolMathError::MathOverflow)?;
        intermediate
            .ceil_div(lower)
            .ok_or(PoolMathError::MathOverflow)
    } else {
        let intermediate =
            mul_div_down(numerator1, numerator2, upper).ok_or(PoolMathError::MathOverflow)?;
        intermediate
            .checked_div(lower)
            .ok_or(PoolMathError::MathOverflow)
    }
}

/// Bit-exact port of `liquidity_math.amount1_delta`.
pub fn amount1_delta(
    sqrt_price_a_x96: U256,
    sqrt_price_b_x96: U256,
    liquidity: u128,
    round_up: bool,
) -> Result<U256, PoolMathError> {
    if !sqrt_price_a_x96.fits_bits(160) || !sqrt_price_b_x96.fits_bits(160) {
        return Err(PoolMathError::SqrtPriceNotUint160);
    }
    let diff = if sqrt_price_a_x96 >= sqrt_price_b_x96 {
        sqrt_price_a_x96
            .checked_sub(sqrt_price_b_x96)
            .ok_or(PoolMathError::MathOverflow)?
    } else {
        sqrt_price_b_x96
            .checked_sub(sqrt_price_a_x96)
            .ok_or(PoolMathError::MathOverflow)?
    };
    let liq = U256::from_u128(liquidity);
    if round_up {
        mul_div_up(liq, diff, U256::Q96).ok_or(PoolMathError::MathOverflow)
    } else {
        mul_div_down(liq, diff, U256::Q96).ok_or(PoolMathError::MathOverflow)
    }
}

/// Bit-exact port of `liquidity_math.amounts_for_liquidity`.
pub fn amounts_for_liquidity(
    sqrt_price_x96: U256,
    sqrt_price_lower_x96: U256,
    sqrt_price_upper_x96: U256,
    liquidity: u128,
    round_up: bool,
) -> Result<(U256, U256), PoolMathError> {
    if !sqrt_price_x96.fits_bits(160)
        || !sqrt_price_lower_x96.fits_bits(160)
        || !sqrt_price_upper_x96.fits_bits(160)
    {
        return Err(PoolMathError::SqrtPriceNotUint160);
    }
    if sqrt_price_lower_x96 >= sqrt_price_upper_x96 {
        return Err(PoolMathError::UnorderedSqrtPriceRange);
    }
    if sqrt_price_x96 <= sqrt_price_lower_x96 {
        Ok((
            amount0_delta(
                sqrt_price_lower_x96,
                sqrt_price_upper_x96,
                liquidity,
                round_up,
            )?,
            U256::ZERO,
        ))
    } else if sqrt_price_x96 < sqrt_price_upper_x96 {
        Ok((
            amount0_delta(sqrt_price_x96, sqrt_price_upper_x96, liquidity, round_up)?,
            amount1_delta(sqrt_price_lower_x96, sqrt_price_x96, liquidity, round_up)?,
        ))
    } else {
        Ok((
            U256::ZERO,
            amount1_delta(
                sqrt_price_lower_x96,
                sqrt_price_upper_x96,
                liquidity,
                round_up,
            )?,
        ))
    }
}

fn next_sqrt_price_from_amount0_rounding_up(
    sqrt_p_x96: U256,
    liquidity: u128,
    amount: U256,
) -> Result<U256, PoolMathError> {
    if amount.is_zero() {
        return Ok(sqrt_p_x96);
    }
    let numerator1 = U256::from_u128(liquidity)
        .checked_shl(96)
        .ok_or(PoolMathError::MathOverflow)?;
    if let Some(product) = amount.checked_mul(sqrt_p_x96)
        && let Some(denominator) = numerator1.checked_add(product)
    {
        let next =
            mul_div_up(numerator1, sqrt_p_x96, denominator).ok_or(PoolMathError::MathOverflow)?;
        if !next.fits_bits(160) || next < MIN_SQRT_PRICE {
            return Err(PoolMathError::SqrtPriceOutOfBounds);
        }
        return Ok(next);
    }
    let q = numerator1
        .checked_div(sqrt_p_x96)
        .ok_or(PoolMathError::ZeroSqrtPrice)?;
    let denom = q.checked_add(amount).ok_or(PoolMathError::MathOverflow)?;
    let next = numerator1
        .ceil_div(denom)
        .ok_or(PoolMathError::MathOverflow)?;
    if !next.fits_bits(160) || next < MIN_SQRT_PRICE {
        return Err(PoolMathError::SqrtPriceOutOfBounds);
    }
    Ok(next)
}

fn next_sqrt_price_from_amount1_rounding_down(
    sqrt_p_x96: U256,
    liquidity: u128,
    amount: U256,
) -> Result<U256, PoolMathError> {
    if amount.is_zero() {
        return Ok(sqrt_p_x96);
    }
    if liquidity == 0 {
        return Err(PoolMathError::InsufficientLiquidity);
    }
    let quotient = mul_div_down(amount, U256::Q96, U256::from_u128(liquidity))
        .ok_or(PoolMathError::MathOverflow)?;
    let next = sqrt_p_x96
        .checked_add(quotient)
        .ok_or(PoolMathError::SqrtPriceOutOfBounds)?;
    if !next.fits_bits(160) || next > MAX_SQRT_PRICE {
        return Err(PoolMathError::SqrtPriceOutOfBounds);
    }
    Ok(next)
}

fn next_sqrt_price_from_input(
    sqrt_p_x96: U256,
    liquidity: u128,
    amount_in: U256,
    zero_for_one: bool,
) -> Result<U256, PoolMathError> {
    if sqrt_p_x96.is_zero() || liquidity == 0 {
        return Err(PoolMathError::InsufficientLiquidity);
    }
    if zero_for_one {
        next_sqrt_price_from_amount0_rounding_up(sqrt_p_x96, liquidity, amount_in)
    } else {
        next_sqrt_price_from_amount1_rounding_down(sqrt_p_x96, liquidity, amount_in)
    }
}

/// Exact Uniswap V3/Slipstream/V4 single-step swap (`exact_in`, ceiling fee rounding).
pub fn compute_swap_step(
    sqrt_ratio_current_x96: U256,
    sqrt_ratio_target_x96: U256,
    liquidity: u128,
    amount_remaining: U256,
    fee_pips: u32,
) -> Result<SwapStepResult, PoolMathError> {
    if fee_pips >= FEE_PIPS_DENOM {
        return Err(PoolMathError::InvalidFeePips(fee_pips));
    }
    if sqrt_ratio_current_x96 < MIN_SQRT_PRICE
        || sqrt_ratio_current_x96 > MAX_SQRT_PRICE
        || sqrt_ratio_target_x96 < MIN_SQRT_PRICE
        || sqrt_ratio_target_x96 > MAX_SQRT_PRICE
    {
        return Err(PoolMathError::SqrtPriceOutOfBounds);
    }
    let zero_for_one = sqrt_ratio_current_x96 >= sqrt_ratio_target_x96;
    let fee_denom = U256::from_u64(u64::from(FEE_PIPS_DENOM));
    let fee_compl = U256::from_u64(u64::from(FEE_PIPS_DENOM - fee_pips));
    let fee_pips_u256 = U256::from_u64(u64::from(fee_pips));

    let amount_remaining_less_fee =
        mul_div_down(amount_remaining, fee_compl, fee_denom).ok_or(PoolMathError::MathOverflow)?;
    let amount_in_to_target = if zero_for_one {
        amount0_delta(
            sqrt_ratio_target_x96,
            sqrt_ratio_current_x96,
            liquidity,
            true,
        )?
    } else {
        amount1_delta(
            sqrt_ratio_current_x96,
            sqrt_ratio_target_x96,
            liquidity,
            true,
        )?
    };

    let sqrt_ratio_next_x96 = if amount_remaining_less_fee >= amount_in_to_target {
        sqrt_ratio_target_x96
    } else {
        next_sqrt_price_from_input(
            sqrt_ratio_current_x96,
            liquidity,
            amount_remaining_less_fee,
            zero_for_one,
        )?
    };

    let max = sqrt_ratio_target_x96 == sqrt_ratio_next_x96;
    let (amount_in, amount_out) = if zero_for_one {
        let amt_in = if max {
            amount_in_to_target
        } else {
            amount0_delta(sqrt_ratio_next_x96, sqrt_ratio_current_x96, liquidity, true)?
        };
        let amt_out = amount1_delta(
            sqrt_ratio_next_x96,
            sqrt_ratio_current_x96,
            liquidity,
            false,
        )?;
        (amt_in, amt_out)
    } else {
        let amt_in = if max {
            amount_in_to_target
        } else {
            amount1_delta(sqrt_ratio_current_x96, sqrt_ratio_next_x96, liquidity, true)?
        };
        let amt_out = amount0_delta(
            sqrt_ratio_current_x96,
            sqrt_ratio_next_x96,
            liquidity,
            false,
        )?;
        (amt_in, amt_out)
    };

    let fee_amount = if !max {
        amount_remaining
            .checked_sub(amount_in)
            .ok_or(PoolMathError::MathOverflow)?
    } else {
        mul_div_up(amount_in, fee_pips_u256, fee_compl).ok_or(PoolMathError::MathOverflow)?
    };

    Ok(SwapStepResult {
        sqrt_ratio_next_x96,
        amount_in,
        amount_out,
        fee_amount,
    })
}

pub fn validate_sorted_ticks(ticks: &[TickLiquidity]) -> Result<(), PoolMathError> {
    for t in ticks {
        if !(MIN_TICK..=MAX_TICK).contains(&t.tick) {
            return Err(PoolMathError::TickOutOfBounds(t.tick));
        }
    }
    for pair in ticks.windows(2) {
        if pair[0].tick >= pair[1].tick {
            return Err(PoolMathError::UnsortedOrDuplicateTicks);
        }
    }
    Ok(())
}

fn apply_liquidity_net(
    liquidity: u128,
    liquidity_net: i128,
    zero_for_one: bool,
) -> Result<u128, PoolMathError> {
    let signed_delta = if zero_for_one {
        liquidity_net
            .checked_neg()
            .ok_or(PoolMathError::MathOverflow)?
    } else {
        liquidity_net
    };
    if signed_delta >= 0 {
        liquidity
            .checked_add(signed_delta.cast_unsigned())
            .ok_or(PoolMathError::MathOverflow)
    } else {
        liquidity
            .checked_sub(signed_delta.unsigned_abs())
            .ok_or(PoolMathError::InsufficientLiquidity)
    }
}

/// Exact-input concentrated liquidity swap across sorted initialized ticks (`V3` / `Slipstream` / `V4`).
/// Fails closed with `InsufficientLiquidity` if liquidity is exhausted before filling `amount_in`.
pub fn cl_swap_exact_in(
    sqrt_price_x96: U256,
    tick: i32,
    liquidity: u128,
    ticks: &[TickLiquidity],
    fee_pips: u32,
    zero_for_one: bool,
    amount_in: U256,
) -> Result<ClSwapResult, PoolMathError> {
    if !(MIN_TICK..=MAX_TICK).contains(&tick) {
        return Err(PoolMathError::TickOutOfBounds(tick));
    }
    if sqrt_price_x96 < MIN_SQRT_PRICE || sqrt_price_x96 > MAX_SQRT_PRICE {
        return Err(PoolMathError::SqrtPriceOutOfBounds);
    }
    let lower_tick_price = sqrt_price_at_tick(tick)?;
    if sqrt_price_x96 < lower_tick_price {
        return Err(PoolMathError::TickPriceMismatch);
    }
    if tick < MAX_TICK && sqrt_price_x96 >= sqrt_price_at_tick(tick + 1)? {
        return Err(PoolMathError::TickPriceMismatch);
    }
    validate_sorted_ticks(ticks)?;
    cl_swap_exact_in_inner(
        sqrt_price_x96,
        tick,
        liquidity,
        ticks,
        fee_pips,
        zero_for_one,
        amount_in,
        true,
    )
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn cl_swap_exact_in_prevalidated(
    sqrt_price_x96: U256,
    tick: i32,
    liquidity: u128,
    ticks: &[TickLiquidity],
    fee_pips: u32,
    zero_for_one: bool,
    amount_in: U256,
) -> Result<ClSwapResult, PoolMathError> {
    cl_swap_exact_in_inner(
        sqrt_price_x96,
        tick,
        liquidity,
        ticks,
        fee_pips,
        zero_for_one,
        amount_in,
        false,
    )
}

#[allow(clippy::too_many_arguments)]
fn cl_swap_exact_in_inner(
    sqrt_price_x96: U256,
    tick: i32,
    liquidity: u128,
    ticks: &[TickLiquidity],
    fee_pips: u32,
    zero_for_one: bool,
    amount_in: U256,
    compute_final_tick: bool,
) -> Result<ClSwapResult, PoolMathError> {
    if amount_in.is_zero() {
        return Err(PoolMathError::ZeroAmount);
    }
    if fee_pips >= FEE_PIPS_DENOM {
        return Err(PoolMathError::InvalidFeePips(fee_pips));
    }

    if zero_for_one && sqrt_price_x96 <= MIN_SQRT_PRICE {
        return Err(PoolMathError::InsufficientLiquidity);
    }
    if !zero_for_one && sqrt_price_x96 >= MAX_SQRT_PRICE {
        return Err(PoolMathError::InsufficientLiquidity);
    }

    let mut state_sqrt = sqrt_price_x96;
    let mut state_tick = tick;
    let mut state_liq = liquidity;
    let mut amount_remaining = amount_in;
    let mut amount_out_total = U256::ZERO;
    let mut fee_total = U256::ZERO;

    while !amount_remaining.is_zero() {
        let next_tick_opt = if zero_for_one {
            let count = ticks.partition_point(|t| t.tick <= state_tick);
            if count > 0 {
                Some(ticks[count - 1])
            } else {
                None
            }
        } else {
            let idx = ticks.partition_point(|t| t.tick <= state_tick);
            ticks.get(idx).copied()
        };

        let target_sqrt = match next_tick_opt {
            Some(next_tick) => sqrt_price_at_tick(next_tick.tick)?,
            None => {
                if state_liq == 0 {
                    return Err(PoolMathError::InsufficientLiquidity);
                }
                if zero_for_one {
                    MIN_SQRT_PRICE
                } else {
                    MAX_SQRT_PRICE
                }
            }
        };

        let step = compute_swap_step(
            state_sqrt,
            target_sqrt,
            state_liq,
            amount_remaining,
            fee_pips,
        )
        .map_err(|err| match err {
            PoolMathError::SqrtPriceOutOfBounds => PoolMathError::InsufficientLiquidity,
            other => other,
        })?;

        let consumed = step
            .amount_in
            .checked_add(step.fee_amount)
            .ok_or(PoolMathError::MathOverflow)?;
        amount_remaining = amount_remaining
            .checked_sub(consumed)
            .ok_or(PoolMathError::MathOverflow)?;
        amount_out_total = amount_out_total
            .checked_add(step.amount_out)
            .ok_or(PoolMathError::MathOverflow)?;
        fee_total = fee_total
            .checked_add(step.fee_amount)
            .ok_or(PoolMathError::MathOverflow)?;
        state_sqrt = step.sqrt_ratio_next_x96;

        if state_sqrt == target_sqrt {
            match next_tick_opt {
                Some(next_tick) => {
                    state_liq =
                        apply_liquidity_net(state_liq, next_tick.liquidity_net, zero_for_one)?;
                    if zero_for_one {
                        if next_tick.tick == MIN_TICK && !amount_remaining.is_zero() {
                            return Err(PoolMathError::InsufficientLiquidity);
                        }
                        state_tick = next_tick.tick - 1;
                    } else {
                        if next_tick.tick == MAX_TICK && !amount_remaining.is_zero() {
                            return Err(PoolMathError::InsufficientLiquidity);
                        }
                        state_tick = next_tick.tick;
                    }
                }
                None => {
                    if !amount_remaining.is_zero() {
                        return Err(PoolMathError::InsufficientLiquidity);
                    }
                    if compute_final_tick {
                        state_tick = tick_at_sqrt_price(state_sqrt)?;
                    }
                }
            }
        } else if compute_final_tick {
            state_tick = tick_at_sqrt_price(state_sqrt)?;
        }
    }

    Ok(ClSwapResult {
        amount_out: amount_out_total,
        fee_paid: fee_total,
        sqrt_price_after_x96: state_sqrt,
        tick_after: state_tick,
        liquidity_after: state_liq,
    })
}

/// Exact integer V2 / Aerodrome vAMM swap:
/// `amount_in * (1_000_000 - fee_pips) * reserve_out / (reserve_in * 1_000_000 + amount_in * (1_000_000 - fee_pips))`.
pub fn v2_swap_exact_in(
    reserve_in: U256,
    reserve_out: U256,
    fee_pips: u32,
    amount_in: U256,
) -> Result<SimpleSwapResult, PoolMathError> {
    if amount_in.is_zero() {
        return Err(PoolMathError::ZeroAmount);
    }
    if reserve_in.is_zero() || reserve_out.is_zero() {
        return Err(PoolMathError::InsufficientLiquidity);
    }
    if fee_pips >= FEE_PIPS_DENOM {
        return Err(PoolMathError::InvalidFeePips(fee_pips));
    }
    let fee_denom = U256::from_u64(u64::from(FEE_PIPS_DENOM));
    let fee_compl = U256::from_u64(u64::from(FEE_PIPS_DENOM - fee_pips));
    let amount_in_with_fee = amount_in
        .checked_mul(fee_compl)
        .ok_or(PoolMathError::MathOverflow)?;
    let denom = reserve_in
        .checked_mul(fee_denom)
        .and_then(|v| v.checked_add(amount_in_with_fee))
        .ok_or(PoolMathError::MathOverflow)?;
    let amount_out =
        mul_div_down(amount_in_with_fee, reserve_out, denom).ok_or(PoolMathError::MathOverflow)?;
    if amount_out >= reserve_out {
        return Err(PoolMathError::InsufficientLiquidity);
    }
    let fee_paid = mul_div_up(amount_in, U256::from_u64(u64::from(fee_pips)), fee_denom)
        .ok_or(PoolMathError::MathOverflow)?;
    Ok(SimpleSwapResult {
        amount_out,
        fee_paid,
    })
}

fn pow10_u256(decimals: u8) -> Result<U256, PoolMathError> {
    if decimals > 36 {
        return Err(PoolMathError::InvalidDecimals(decimals));
    }
    let mut acc = U256::ONE;
    let ten = U256::from_u64(10);
    for _ in 0..decimals {
        acc = acc.checked_mul(ten).ok_or(PoolMathError::MathOverflow)?;
    }
    Ok(acc)
}

fn stable_f(x0: U256, y: U256) -> Result<U256, PoolMathError> {
    let a = mul_div_down(x0, y, E18).ok_or(PoolMathError::MathOverflow)?;
    let x2 = mul_div_down(x0, x0, E18).ok_or(PoolMathError::MathOverflow)?;
    let y2 = mul_div_down(y, y, E18).ok_or(PoolMathError::MathOverflow)?;
    let b = x2.checked_add(y2).ok_or(PoolMathError::MathOverflow)?;
    mul_div_down(a, b, E18).ok_or(PoolMathError::MathOverflow)
}

fn stable_d(x0: U256, y: U256) -> Result<U256, PoolMathError> {
    let y2 = mul_div_down(y, y, E18).ok_or(PoolMathError::MathOverflow)?;
    let three_x0 = x0
        .checked_mul(U256::from_u64(3))
        .ok_or(PoolMathError::MathOverflow)?;
    let term1 = mul_div_down(three_x0, y2, E18).ok_or(PoolMathError::MathOverflow)?;
    let x2 = mul_div_down(x0, x0, E18).ok_or(PoolMathError::MathOverflow)?;
    let term2 = mul_div_down(x2, x0, E18).ok_or(PoolMathError::MathOverflow)?;
    term1.checked_add(term2).ok_or(PoolMathError::MathOverflow)
}

fn stable_get_y(x0: U256, xy: U256, mut y: U256) -> Result<U256, PoolMathError> {
    for _ in 0..255 {
        let k = stable_f(x0, y)?;
        let d = stable_d(x0, y)?;
        if d.is_zero() {
            return Err(PoolMathError::StableDidNotConverge);
        }
        if k < xy {
            let diff = xy.checked_sub(k).ok_or(PoolMathError::MathOverflow)?;
            let mut dy = mul_div_down(diff, E18, d).ok_or(PoolMathError::MathOverflow)?;
            if dy.is_zero() {
                let y_plus_1 = y
                    .checked_add(U256::ONE)
                    .ok_or(PoolMathError::MathOverflow)?;
                if stable_f(x0, y_plus_1)? >= xy {
                    return Ok(y_plus_1);
                }
                dy = U256::ONE;
            }
            y = y.checked_add(dy).ok_or(PoolMathError::MathOverflow)?;
        } else {
            let diff = k.checked_sub(xy).ok_or(PoolMathError::MathOverflow)?;
            let mut dy = mul_div_down(diff, E18, d).ok_or(PoolMathError::MathOverflow)?;
            if dy.is_zero() {
                if k == xy {
                    return Ok(y);
                }
                if y.is_zero() {
                    return Ok(U256::ZERO);
                }
                let y_minus_1 = y
                    .checked_sub(U256::ONE)
                    .ok_or(PoolMathError::MathOverflow)?;
                if stable_f(x0, y_minus_1)? < xy {
                    return Ok(y);
                }
                dy = U256::ONE;
            }
            y = y.checked_sub(dy).ok_or(PoolMathError::MathOverflow)?;
        }
    }
    Err(PoolMathError::StableDidNotConverge)
}

/// Exact integer AerodromeStable (`x^3*y + y^3*x >= k`) swap normalized to `1e18`.
/// Fails closed if Newton-Raphson does not converge within 255 iterations.
pub fn aerodrome_stable_swap_exact_in(
    reserve_in: U256,
    reserve_out: U256,
    decimals_in: u8,
    decimals_out: u8,
    fee_pips: u32,
    amount_in: U256,
) -> Result<SimpleSwapResult, PoolMathError> {
    if amount_in.is_zero() {
        return Err(PoolMathError::ZeroAmount);
    }
    if reserve_in.is_zero() || reserve_out.is_zero() {
        return Err(PoolMathError::InsufficientLiquidity);
    }
    if fee_pips >= FEE_PIPS_DENOM {
        return Err(PoolMathError::InvalidFeePips(fee_pips));
    }
    let scale_in = pow10_u256(decimals_in)?;
    let scale_out = pow10_u256(decimals_out)?;

    let rx = mul_div_down(reserve_in, E18, scale_in).ok_or(PoolMathError::MathOverflow)?;
    let ry = mul_div_down(reserve_out, E18, scale_out).ok_or(PoolMathError::MathOverflow)?;
    if rx.is_zero() || ry.is_zero() {
        return Err(PoolMathError::InsufficientLiquidity);
    }
    let xy = stable_f(rx, ry)?;
    if xy.is_zero() {
        return Err(PoolMathError::InsufficientLiquidity);
    }

    let fee_denom = U256::from_u64(u64::from(FEE_PIPS_DENOM));
    let fee_compl = U256::from_u64(u64::from(FEE_PIPS_DENOM - fee_pips));
    let amount_in_after_fee =
        mul_div_down(amount_in, fee_compl, fee_denom).ok_or(PoolMathError::MathOverflow)?;
    let fee_paid = amount_in
        .checked_sub(amount_in_after_fee)
        .ok_or(PoolMathError::MathOverflow)?;

    let dx = mul_div_down(amount_in_after_fee, E18, scale_in).ok_or(PoolMathError::MathOverflow)?;
    if dx.is_zero() {
        return Ok(SimpleSwapResult {
            amount_out: U256::ZERO,
            fee_paid,
        });
    }
    let x0 = rx.checked_add(dx).ok_or(PoolMathError::MathOverflow)?;
    let y_new = stable_get_y(x0, xy, ry)?;
    let dy_norm = ry
        .checked_sub(y_new)
        .ok_or(PoolMathError::InsufficientLiquidity)?;
    let amount_out = mul_div_down(dy_norm, scale_out, E18).ok_or(PoolMathError::MathOverflow)?;
    if amount_out >= reserve_out {
        return Err(PoolMathError::InsufficientLiquidity);
    }
    Ok(SimpleSwapResult {
        amount_out,
        fee_paid,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tick_bounds_match_canonical_constants() {
        assert_eq!(sqrt_price_at_tick(MIN_TICK).unwrap(), MIN_SQRT_PRICE);
        assert_eq!(sqrt_price_at_tick(MAX_TICK).unwrap(), MAX_SQRT_PRICE);
        assert_eq!(sqrt_price_at_tick(0).unwrap(), U256::Q96);
        assert_eq!(tick_at_sqrt_price(MIN_SQRT_PRICE).unwrap(), MIN_TICK);
        assert_eq!(tick_at_sqrt_price(MAX_SQRT_PRICE).unwrap(), MAX_TICK);
        assert_eq!(tick_at_sqrt_price(U256::Q96).unwrap(), 0);
        assert!(sqrt_price_at_tick(MIN_TICK - 1).is_err());
        assert!(sqrt_price_at_tick(MAX_TICK + 1).is_err());
    }

    #[test]
    fn v2_swap_exact_integer_matches_formula() {
        let res_in = U256::from_u128(1_000_000_000_000_000_000_000);
        let res_out = U256::from_u128(2_000_000_000_000);
        let amt_in = U256::from_u128(1_000_000_000_000_000_000);
        let r = v2_swap_exact_in(res_in, res_out, 3000, amt_in).unwrap();
        // 1e18 * 997000 * 2e12 / (1e21 * 1e6 + 1e18 * 997000) = 1992013962
        assert_eq!(r.amount_out, U256::from_u64(1_992_013_962));
        assert_eq!(
            r.fee_paid,
            U256::from_u128(3_000_000_000_000_000) // 0.3% of 1e18
        );
    }

    #[test]
    fn aerodrome_stable_preserves_invariant() {
        let r0 = U256::from_u128(1_000_000 * 1_000_000); // 1M USDC (6 dec)
        let r1 = U256::from_u128(1_000_000 * 1_000_000_000_000_000_000); // 1M DAI (18 dec)
        let amt_in = U256::from_u64(10_000 * 1_000_000); // 10k USDC
        let res = aerodrome_stable_swap_exact_in(r0, r1, 6, 18, 500, amt_in).unwrap();
        // Near 1:1 minus 5 bps fee => ~9,995 DAI
        let approx_out = res.amount_out.to_u128().unwrap() / 1_000_000_000_000_000_000;
        assert!((9_994..=9_996).contains(&approx_out));

        // Verify post-swap invariant k_after >= k_before
        let s0 = pow10_u256(6).unwrap();
        let s1 = pow10_u256(18).unwrap();
        let k_before = stable_f(
            mul_div_down(r0, E18, s0).unwrap(),
            mul_div_down(r1, E18, s1).unwrap(),
        )
        .unwrap();
        let r0_after = r0
            .checked_add(amt_in.checked_sub(res.fee_paid).unwrap())
            .unwrap();
        let r1_after = r1.checked_sub(res.amount_out).unwrap();
        let k_after = stable_f(
            mul_div_down(r0_after, E18, s0).unwrap(),
            mul_div_down(r1_after, E18, s1).unwrap(),
        )
        .unwrap();
        assert!(k_after >= k_before);
    }

    #[test]
    fn cl_swap_crosses_ticks_both_directions() {
        let liq: u128 = 1_000_000_000_000_000_000;
        let ticks = vec![
            TickLiquidity {
                tick: -120,
                liquidity_net: 500_000_000_000_000_000,
            },
            TickLiquidity {
                tick: 120,
                liquidity_net: -500_000_000_000_000_000,
            },
        ];
        // Swap enough to cross tick 120 (one_for_zero) and tick -120 (zero_for_one)
        let amt = U256::from_u128(20_000_000_000_000_000);
        let res_up = cl_swap_exact_in(U256::Q96, 0, liq, &ticks, 3000, false, amt).unwrap();
        assert!(res_up.tick_after >= 120);
        assert_eq!(res_up.liquidity_after, 500_000_000_000_000_000);

        let res_down = cl_swap_exact_in(U256::Q96, 0, liq, &ticks, 3000, true, amt).unwrap();
        assert!(res_down.tick_after < -120);
        assert_eq!(res_down.liquidity_after, 500_000_000_000_000_000);

        // Exhausting all liquidity fails closed
        let drain_ticks = vec![TickLiquidity {
            tick: 60,
            liquidity_net: -(liq as i128),
        }];
        let huge = U256::from_u128(100_000_000_000_000_000);
        assert_eq!(
            cl_swap_exact_in(U256::Q96, 0, liq, &drain_ticks, 3000, false, huge),
            Err(PoolMathError::InsufficientLiquidity)
        );
    }

    #[test]
    fn cl_swap_starting_at_initialized_tick_boundary() {
        let liq: u128 = 1_000_000_000_000_000_000;
        let ticks = vec![
            TickLiquidity {
                tick: -60,
                liquidity_net: 200_000_000_000_000_000,
            },
            TickLiquidity {
                tick: 0,
                liquidity_net: 300_000_000_000_000_000,
            },
            TickLiquidity {
                tick: 60,
                liquidity_net: -500_000_000_000_000_000,
            },
        ];
        // Starting exactly at tick=0: moving zero_for_one immediately crosses tick 0
        // (liquidity drops by 300e15 to 700e15); moving one_for_zero stays at 1000e15 until tick 60.
        let small = U256::from_u128(100_000_000_000_000);
        let down = cl_swap_exact_in(U256::Q96, 0, liq, &ticks, 500, true, small).unwrap();
        assert_eq!(down.liquidity_after, 700_000_000_000_000_000);
        assert!(down.tick_after < 0);

        let up = cl_swap_exact_in(U256::Q96, 0, liq, &ticks, 500, false, small).unwrap();
        assert_eq!(up.liquidity_after, 1_000_000_000_000_000_000);
        assert!(up.tick_after >= 0);
    }
}
