use std::cmp::Ordering;
use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Deserializer, Serialize, Serializer, de};
use thiserror::Error;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum MathError {
    #[error("empty integer string")]
    EmptyInput,
    #[error("invalid character in integer string")]
    InvalidDigit,
    #[error("integer overflow")]
    Overflow,
    #[error("missing 0x prefix for hex integer")]
    MissingHexPrefix,
}

/// Exact little-endian unsigned 256-bit integer (`limbs[0]` is least significant).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub struct U256(pub [u64; 4]);

/// Exact little-endian unsigned 512-bit integer (`limbs[0]` is least significant).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub struct U512(pub [u64; 8]);

impl U256 {
    pub const ZERO: Self = Self([0, 0, 0, 0]);
    pub const ONE: Self = Self([1, 0, 0, 0]);
    pub const MAX: Self = Self([u64::MAX; 4]);
    pub const Q96: Self = Self([0, 1u64 << 32, 0, 0]);
    pub const Q128: Self = Self([0, 0, 1, 0]);

    #[must_use]
    pub const fn from_u64(v: u64) -> Self {
        Self([v, 0, 0, 0])
    }

    #[must_use]
    pub const fn from_u128(v: u128) -> Self {
        Self([v as u64, (v >> 64) as u64, 0, 0])
    }

    #[must_use]
    pub const fn to_u64(self) -> Option<u64> {
        if self.0[1] == 0 && self.0[2] == 0 && self.0[3] == 0 {
            Some(self.0[0])
        } else {
            None
        }
    }

    #[must_use]
    pub const fn to_u128(self) -> Option<u128> {
        if self.0[2] == 0 && self.0[3] == 0 {
            Some((self.0[0] as u128) | ((self.0[1] as u128) << 64))
        } else {
            None
        }
    }

    #[must_use]
    pub const fn is_zero(self) -> bool {
        self.0[0] == 0 && self.0[1] == 0 && self.0[2] == 0 && self.0[3] == 0
    }

    #[must_use]
    pub const fn bits(self) -> u32 {
        let mut i = 4;
        while i > 0 {
            i -= 1;
            let limb = self.0[i];
            if limb != 0 {
                return (i as u32) * 64 + (64 - limb.leading_zeros());
            }
        }
        0
    }

    #[must_use]
    pub const fn fits_bits(self, max_bits: u32) -> bool {
        self.bits() <= max_bits
    }

    #[must_use]
    pub const fn bit(self, index: u32) -> bool {
        if index >= 256 {
            return false;
        }
        let limb = (index / 64) as usize;
        let offset = index % 64;
        ((self.0[limb] >> offset) & 1) != 0
    }

    #[must_use]
    pub const fn to_u512(self) -> U512 {
        U512([self.0[0], self.0[1], self.0[2], self.0[3], 0, 0, 0, 0])
    }

    #[must_use]
    pub const fn checked_add(self, rhs: Self) -> Option<Self> {
        let mut out = [0u64; 4];
        let mut carry = 0u128;
        let mut i = 0;
        while i < 4 {
            let sum = (self.0[i] as u128) + (rhs.0[i] as u128) + carry;
            out[i] = sum as u64;
            carry = sum >> 64;
            i += 1;
        }
        if carry != 0 { None } else { Some(Self(out)) }
    }

    #[must_use]
    pub const fn checked_sub(self, rhs: Self) -> Option<Self> {
        let mut out = [0u64; 4];
        let mut borrow = 0u128;
        let mut i = 0;
        while i < 4 {
            let lhs_limb = self.0[i] as u128;
            let rhs_limb = (rhs.0[i] as u128) + borrow;
            if lhs_limb >= rhs_limb {
                out[i] = (lhs_limb - rhs_limb) as u64;
                borrow = 0;
            } else {
                out[i] = ((1u128 << 64) + lhs_limb - rhs_limb) as u64;
                borrow = 1;
            }
            i += 1;
        }
        if borrow != 0 { None } else { Some(Self(out)) }
    }

    /// Checked left shift: fails (`None`) if `shift >= 256` or if any non-zero bit overflows 256 bits.
    #[must_use]
    pub const fn checked_shl(self, shift: u32) -> Option<Self> {
        if shift >= 256 {
            return None;
        }
        if shift == 0 {
            return Some(self);
        }
        if self.bits() + shift > 256 {
            return None;
        }
        let word_shift = (shift / 64) as usize;
        let bit_shift = shift % 64;
        let mut out = [0u64; 4];
        let mut i = word_shift;
        while i < 4 {
            let src = i - word_shift;
            let mut val = self.0[src] << bit_shift;
            if bit_shift > 0 && src > 0 {
                val |= self.0[src - 1] >> (64 - bit_shift);
            }
            out[i] = val;
            i += 1;
        }
        Some(Self(out))
    }

    /// Checked logical right shift: fails (`None`) if `shift >= 256`.
    #[must_use]
    pub const fn checked_shr(self, shift: u32) -> Option<Self> {
        if shift >= 256 {
            return None;
        }
        if shift == 0 {
            return Some(self);
        }
        let word_shift = (shift / 64) as usize;
        let bit_shift = shift % 64;
        let mut out = [0u64; 4];
        let mut i = 0;
        while i + word_shift < 4 {
            let src = i + word_shift;
            let mut val = self.0[src] >> bit_shift;
            if bit_shift > 0 && src + 1 < 4 {
                val |= self.0[src + 1] << (64 - bit_shift);
            }
            out[i] = val;
            i += 1;
        }
        Some(Self(out))
    }

    #[must_use]
    pub fn checked_mul(self, rhs: Self) -> Option<Self> {
        mul_u256_to_u512(self, rhs).to_u256()
    }

    #[must_use]
    pub fn checked_div(self, rhs: Self) -> Option<Self> {
        div_rem_u256(self, rhs).map(|(q, _)| q)
    }

    #[must_use]
    pub fn checked_rem(self, rhs: Self) -> Option<Self> {
        div_rem_u256(self, rhs).map(|(_, r)| r)
    }

    #[must_use]
    pub fn ceil_div(self, rhs: Self) -> Option<Self> {
        let (q, r) = div_rem_u256(self, rhs)?;
        if r.is_zero() {
            Some(q)
        } else {
            q.checked_add(Self::ONE)
        }
    }

    /// Exact integer floor(sqrt(self)).
    #[must_use]
    pub fn isqrt(self) -> Self {
        if self.is_zero() {
            return Self::ZERO;
        }
        let mut bit_idx = ((self.bits() - 1) / 2) * 2;
        let mut n = self;
        let mut res = Self::ZERO;
        loop {
            let mut bit_val = Self::ZERO;
            bit_val.0[(bit_idx / 64) as usize] = 1u64 << (bit_idx % 64);
            let sum = res
                .checked_add(bit_val)
                .expect("isqrt intermediate never overflows U256");
            if n >= sum {
                n = n
                    .checked_sub(sum)
                    .expect("n >= sum verified before subtraction");
                res = res
                    .checked_shr(1)
                    .expect("shift 1 < 256")
                    .checked_add(bit_val)
                    .expect("isqrt result never overflows U256");
            } else {
                res = res.checked_shr(1).expect("shift 1 < 256");
            }
            if bit_idx == 0 {
                break;
            }
            bit_idx -= 2;
        }
        res
    }

    #[must_use]
    pub fn to_le_bytes(self) -> [u8; 32] {
        let mut out = [0u8; 32];
        for (i, limb) in self.0.iter().enumerate() {
            out[i * 8..(i + 1) * 8].copy_from_slice(&limb.to_le_bytes());
        }
        out
    }

    #[must_use]
    pub fn from_le_bytes(bytes: [u8; 32]) -> Self {
        let mut limbs = [0u64; 4];
        for (i, limb) in limbs.iter_mut().enumerate() {
            let mut chunk = [0u8; 8];
            chunk.copy_from_slice(&bytes[i * 8..(i + 1) * 8]);
            *limb = u64::from_le_bytes(chunk);
        }
        Self(limbs)
    }

    #[must_use]
    pub fn to_be_bytes(self) -> [u8; 32] {
        let mut out = [0u8; 32];
        for (i, limb) in self.0.iter().enumerate() {
            let dst = (3 - i) * 8;
            out[dst..dst + 8].copy_from_slice(&limb.to_be_bytes());
        }
        out
    }

    #[must_use]
    pub fn from_be_bytes(bytes: [u8; 32]) -> Self {
        let mut limbs = [0u64; 4];
        for (i, limb) in limbs.iter_mut().enumerate() {
            let src = (3 - i) * 8;
            let mut chunk = [0u8; 8];
            chunk.copy_from_slice(&bytes[src..src + 8]);
            *limb = u64::from_be_bytes(chunk);
        }
        Self(limbs)
    }

    pub fn from_dec_str(s: &str) -> Result<Self, MathError> {
        if s.is_empty() {
            return Err(MathError::EmptyInput);
        }
        let mut acc = Self::ZERO;
        let ten = Self::from_u64(10);
        for &byte in s.as_bytes() {
            if !byte.is_ascii_digit() {
                return Err(MathError::InvalidDigit);
            }
            let digit = Self::from_u64(u64::from(byte - b'0'));
            acc = acc
                .checked_mul(ten)
                .and_then(|v| v.checked_add(digit))
                .ok_or(MathError::Overflow)?;
        }
        Ok(acc)
    }

    pub fn from_hex_str(s: &str) -> Result<Self, MathError> {
        let hex_part = s
            .strip_prefix("0x")
            .or_else(|| s.strip_prefix("0X"))
            .ok_or(MathError::MissingHexPrefix)?;
        if hex_part.is_empty() {
            return Err(MathError::EmptyInput);
        }
        let mut acc = Self::ZERO;
        for &byte in hex_part.as_bytes() {
            let nibble = match byte {
                b'0'..=b'9' => u64::from(byte - b'0'),
                b'a'..=b'f' => u64::from(byte - b'a' + 10),
                b'A'..=b'F' => u64::from(byte - b'A' + 10),
                _ => return Err(MathError::InvalidDigit),
            };
            acc = acc
                .checked_shl(4)
                .and_then(|v| v.checked_add(Self::from_u64(nibble)))
                .ok_or(MathError::Overflow)?;
        }
        Ok(acc)
    }

    pub fn from_str_auto(s: &str) -> Result<Self, MathError> {
        if s.starts_with("0x") || s.starts_with("0X") {
            Self::from_hex_str(s)
        } else {
            Self::from_dec_str(s)
        }
    }

    #[must_use]
    pub fn to_dec_string(self) -> String {
        if self.is_zero() {
            return String::from("0");
        }
        let mut cur = self;
        let mut chunks: Vec<u64> = Vec::new();
        let base = Self::from_u64(1_000_000_000_000_000_000);
        while !cur.is_zero() {
            let (q, r) = div_rem_u256(cur, base).expect("base is non-zero");
            chunks.push(r.0[0]);
            cur = q;
        }
        let mut out = String::new();
        if let Some(&top) = chunks.last() {
            out.push_str(&top.to_string());
        }
        for &chunk in chunks.iter().rev().skip(1) {
            use fmt::Write as _;
            let _ = write!(out, "{chunk:018}");
        }
        out
    }

    #[must_use]
    pub fn to_hex_string(self) -> String {
        if self.is_zero() {
            return String::from("0x0");
        }
        let mut out = String::from("0x");
        let mut started = false;
        for &limb in self.0.iter().rev() {
            if !started {
                if limb != 0 {
                    use fmt::Write as _;
                    let _ = write!(out, "{limb:x}");
                    started = true;
                }
            } else {
                use fmt::Write as _;
                let _ = write!(out, "{limb:016x}");
            }
        }
        out
    }
}

impl Ord for U256 {
    fn cmp(&self, other: &Self) -> Ordering {
        for i in (0..4).rev() {
            match self.0[i].cmp(&other.0[i]) {
                Ordering::Equal => {}
                ord => return ord,
            }
        }
        Ordering::Equal
    }
}

impl PartialOrd for U256 {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl fmt::Display for U256 {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.to_dec_string())
    }
}

impl FromStr for U256 {
    type Err = MathError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        Self::from_str_auto(s)
    }
}

impl Serialize for U256 {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.to_dec_string())
    }
}

impl<'de> Deserialize<'de> for U256 {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct U256Visitor;
        impl de::Visitor<'_> for U256Visitor {
            type Value = U256;

            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("a decimal or 0x-prefixed hex integer string")
            }

            fn visit_str<E: de::Error>(self, v: &str) -> Result<Self::Value, E> {
                U256::from_str_auto(v).map_err(de::Error::custom)
            }
        }
        deserializer.deserialize_str(U256Visitor)
    }
}

impl U512 {
    pub const ZERO: Self = Self([0; 8]);
    pub const ONE: Self = Self([1, 0, 0, 0, 0, 0, 0, 0]);
    pub const MAX: Self = Self([u64::MAX; 8]);

    #[must_use]
    pub const fn from_u64(v: u64) -> Self {
        Self([v, 0, 0, 0, 0, 0, 0, 0])
    }

    #[must_use]
    pub const fn from_u128(v: u128) -> Self {
        Self([v as u64, (v >> 64) as u64, 0, 0, 0, 0, 0, 0])
    }

    #[must_use]
    pub const fn is_zero(self) -> bool {
        let mut i = 0;
        while i < 8 {
            if self.0[i] != 0 {
                return false;
            }
            i += 1;
        }
        true
    }

    #[must_use]
    pub const fn to_u256(self) -> Option<U256> {
        if self.0[4] == 0 && self.0[5] == 0 && self.0[6] == 0 && self.0[7] == 0 {
            Some(U256([self.0[0], self.0[1], self.0[2], self.0[3]]))
        } else {
            None
        }
    }

    #[must_use]
    pub const fn bits(self) -> u32 {
        let mut i = 8;
        while i > 0 {
            i -= 1;
            let limb = self.0[i];
            if limb != 0 {
                return (i as u32) * 64 + (64 - limb.leading_zeros());
            }
        }
        0
    }

    #[must_use]
    pub const fn bit(self, index: u32) -> bool {
        if index >= 512 {
            return false;
        }
        let limb = (index / 64) as usize;
        let offset = index % 64;
        ((self.0[limb] >> offset) & 1) != 0
    }

    pub fn set_bit(&mut self, index: u32) {
        if index < 512 {
            let limb = (index / 64) as usize;
            let offset = index % 64;
            self.0[limb] |= 1u64 << offset;
        }
    }

    #[must_use]
    pub const fn checked_add(self, rhs: Self) -> Option<Self> {
        let mut out = [0u64; 8];
        let mut carry = 0u128;
        let mut i = 0;
        while i < 8 {
            let sum = (self.0[i] as u128) + (rhs.0[i] as u128) + carry;
            out[i] = sum as u64;
            carry = sum >> 64;
            i += 1;
        }
        if carry != 0 { None } else { Some(Self(out)) }
    }

    #[must_use]
    pub const fn checked_sub(self, rhs: Self) -> Option<Self> {
        let mut out = [0u64; 8];
        let mut borrow = 0u128;
        let mut i = 0;
        while i < 8 {
            let lhs_limb = self.0[i] as u128;
            let rhs_limb = (rhs.0[i] as u128) + borrow;
            if lhs_limb >= rhs_limb {
                out[i] = (lhs_limb - rhs_limb) as u64;
                borrow = 0;
            } else {
                out[i] = ((1u128 << 64) + lhs_limb - rhs_limb) as u64;
                borrow = 1;
            }
            i += 1;
        }
        if borrow != 0 { None } else { Some(Self(out)) }
    }

    #[must_use]
    const fn wrapping_sub(self, rhs: Self) -> Self {
        let mut out = [0u64; 8];
        let mut borrow = 0u128;
        let mut i = 0;
        while i < 8 {
            let lhs_limb = self.0[i] as u128;
            let rhs_limb = (rhs.0[i] as u128) + borrow;
            if lhs_limb >= rhs_limb {
                out[i] = (lhs_limb - rhs_limb) as u64;
                borrow = 0;
            } else {
                out[i] = ((1u128 << 64) + lhs_limb - rhs_limb) as u64;
                borrow = 1;
            }
            i += 1;
        }
        Self(out)
    }

    /// Checked left shift: fails (`None`) if `shift >= 512` or if any non-zero bit overflows 512 bits.
    #[must_use]
    pub const fn checked_shl(self, shift: u32) -> Option<Self> {
        if shift >= 512 {
            return None;
        }
        if shift == 0 {
            return Some(self);
        }
        if self.bits() + shift > 512 {
            return None;
        }
        let word_shift = (shift / 64) as usize;
        let bit_shift = shift % 64;
        let mut out = [0u64; 8];
        let mut i = word_shift;
        while i < 8 {
            let src = i - word_shift;
            let mut val = self.0[src] << bit_shift;
            if bit_shift > 0 && src > 0 {
                val |= self.0[src - 1] >> (64 - bit_shift);
            }
            out[i] = val;
            i += 1;
        }
        Some(Self(out))
    }

    /// Checked logical right shift: fails (`None`) if `shift >= 512`.
    #[must_use]
    pub const fn checked_shr(self, shift: u32) -> Option<Self> {
        if shift >= 512 {
            return None;
        }
        if shift == 0 {
            return Some(self);
        }
        let word_shift = (shift / 64) as usize;
        let bit_shift = shift % 64;
        let mut out = [0u64; 8];
        let mut i = 0;
        while i + word_shift < 8 {
            let src = i + word_shift;
            let mut val = self.0[src] >> bit_shift;
            if bit_shift > 0 && src + 1 < 8 {
                val |= self.0[src + 1] << (64 - bit_shift);
            }
            out[i] = val;
            i += 1;
        }
        Some(Self(out))
    }

    fn shr1_in_place(&mut self) {
        let mut carry = 0u64;
        for limb in self.0.iter_mut().rev() {
            let next_carry = *limb << 63;
            *limb = (*limb >> 1) | carry;
            carry = next_carry;
        }
    }
}

impl Ord for U512 {
    fn cmp(&self, other: &Self) -> Ordering {
        for i in (0..8).rev() {
            match self.0[i].cmp(&other.0[i]) {
                Ordering::Equal => {}
                ord => return ord,
            }
        }
        Ordering::Equal
    }
}

impl PartialOrd for U512 {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

/// Exact widening 256x256 -> 512-bit multiplication.
#[must_use]
pub const fn mul_u256_to_u512(a: U256, b: U256) -> U512 {
    let mut out = [0u64; 8];
    let mut i = 0;
    while i < 4 {
        let mut carry = 0u128;
        let mut j = 0;
        while j < 4 {
            let prod = (a.0[i] as u128) * (b.0[j] as u128) + (out[i + j] as u128) + carry;
            out[i + j] = prod as u64;
            carry = prod >> 64;
            j += 1;
        }
        out[i + 4] = carry as u64;
        i += 1;
    }
    U512(out)
}

/// Exact 512-bit Euclidean division returning `(quotient, remainder)`. Returns `None` on division by zero.
#[must_use]
pub fn div_rem_u512(numerator: U512, divisor: U512) -> Option<(U512, U512)> {
    if divisor.is_zero() {
        return None;
    }
    if numerator < divisor {
        return Some((U512::ZERO, numerator));
    }
    // Fast path for values fitting in u128.
    if numerator.0[2..].iter().all(|&x| x == 0) && divisor.0[2..].iter().all(|&x| x == 0) {
        let n = (numerator.0[0] as u128) | ((numerator.0[1] as u128) << 64);
        let d = (divisor.0[0] as u128) | ((divisor.0[1] as u128) << 64);
        return Some((U512::from_u128(n / d), U512::from_u128(n % d)));
    }
    let n_bits = numerator.bits();
    let d_bits = divisor.bits();
    let shift = n_bits - d_bits;
    let mut q = U512::ZERO;
    let mut r = numerator;
    let mut cur_div = divisor
        .checked_shl(shift)
        .expect("d_bits + shift == n_bits <= 512");
    for bit in (0..=shift).rev() {
        if r >= cur_div {
            r = r.wrapping_sub(cur_div);
            q.set_bit(bit);
        }
        cur_div.shr1_in_place();
    }
    Some((q, r))
}

#[must_use]
pub fn div_rem_u256(numerator: U256, divisor: U256) -> Option<(U256, U256)> {
    let (q, r) = div_rem_u512(numerator.to_u512(), divisor.to_u512())?;
    Some((
        q.to_u256()?,
        r.to_u256()
            .expect("remainder < divisor <= U256::MAX always fits U256"),
    ))
}

/// Exact `floor((a * b) / denom)` with full 512-bit intermediate; returns `None` if `denom == 0` or quotient overflows `U256`.
#[must_use]
pub fn mul_div_down(a: U256, b: U256, denom: U256) -> Option<U256> {
    if denom.is_zero() {
        return None;
    }
    let product = mul_u256_to_u512(a, b);
    if denom == U256::Q96 {
        return product.checked_shr(96).and_then(|q| q.to_u256());
    }
    let (quotient, _) = div_rem_u512(product, denom.to_u512())?;
    quotient.to_u256()
}

/// Exact `ceil((a * b) / denom)` with full 512-bit intermediate; returns `None` if `denom == 0` or quotient overflows `U256`.
#[must_use]
pub fn mul_div_up(a: U256, b: U256, denom: U256) -> Option<U256> {
    if denom.is_zero() {
        return None;
    }
    let product = mul_u256_to_u512(a, b);
    if denom == U256::Q96 {
        let q = product.checked_shr(96).and_then(|v| v.to_u256())?;
        let has_rem = product.0[0] != 0 || (product.0[1] & 0xFFFF_FFFF) != 0;
        return if has_rem {
            q.checked_add(U256::ONE)
        } else {
            Some(q)
        };
    }
    let (quotient, remainder) = div_rem_u512(product, denom.to_u512())?;
    let q_u256 = quotient.to_u256()?;
    if remainder.is_zero() {
        Some(q_u256)
    } else {
        q_u256.checked_add(U256::ONE)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn add_sub_mul_checked_never_wrap() {
        assert_eq!(U256::MAX.checked_add(U256::ONE), None);
        assert_eq!(U256::ZERO.checked_sub(U256::ONE), None);
        assert_eq!(U256::MAX.checked_mul(U256::from_u64(2)), None);
        assert_eq!(U256::MAX.checked_shl(1), None);
        assert_eq!(U256::ONE.checked_shl(256), None);
        assert_eq!(U256::ONE.checked_shr(256), None);

        let a = U256::from_u128(u128::MAX);
        let prod = a.checked_mul(U256::from_u64(2)).expect("fits U256");
        assert_eq!(
            prod,
            U256::from_dec_str("680564733841876926926749214863536422910").unwrap()
        );
    }

    #[test]
    fn u512_div_rem_handles_extremes() {
        assert_eq!(div_rem_u512(U512::ONE, U512::ZERO), None);
        let (q, r) = div_rem_u512(U512::MAX, U512::MAX).unwrap();
        assert_eq!(q, U512::ONE);
        assert_eq!(r, U512::ZERO);

        let high_div = U512([10, 0, 0, 0, 0, 0, 0, 1u64 << 63]);
        let (q2, r2) = div_rem_u512(U512::MAX, high_div).unwrap();
        assert_eq!(q2, U512::ONE);
        assert_eq!(
            r2,
            U512::MAX.checked_sub(high_div).expect("MAX >= high_div")
        );

        let prod = mul_u256_to_u512(U256::MAX, U256::MAX);
        let (q3, r3) = div_rem_u512(prod, U256::MAX.to_u512()).unwrap();
        assert_eq!(q3.to_u256(), Some(U256::MAX));
        assert_eq!(r3, U512::ZERO);
    }

    #[test]
    fn mul_div_rounding_and_overflow() {
        let a = U256::from_u64(7);
        let b = U256::from_u64(5);
        let d = U256::from_u64(4);
        assert_eq!(mul_div_down(a, b, d), Some(U256::from_u64(8)));
        assert_eq!(mul_div_up(a, b, d), Some(U256::from_u64(9)));
        assert_eq!(mul_div_up(U256::MAX, U256::from_u64(2), U256::ONE), None);
        assert_eq!(mul_div_up(U256::MAX, U256::ONE, U256::ONE), Some(U256::MAX));
        assert_eq!(mul_div_up(U256::MAX, U256::MAX, U256::MAX), Some(U256::MAX));
    }

    #[test]
    fn isqrt_exact_boundaries() {
        assert_eq!(U256::ZERO.isqrt(), U256::ZERO);
        assert_eq!(U256::ONE.isqrt(), U256::ONE);
        assert_eq!(U256::from_u64(3).isqrt(), U256::ONE);
        assert_eq!(U256::from_u64(4).isqrt(), U256::from_u64(2));
        assert_eq!(U256::MAX.isqrt(), U256::from_u128(u128::MAX));

        let sq = U256::from_u128(u128::MAX)
            .checked_mul(U256::from_u128(u128::MAX))
            .unwrap();
        assert_eq!(sq.isqrt(), U256::from_u128(u128::MAX));
        assert_eq!(
            sq.checked_sub(U256::ONE).unwrap().isqrt(),
            U256::from_u128(u128::MAX - 1)
        );
    }

    #[test]
    fn decimal_and_hex_roundtrip() {
        let values = [
            U256::ZERO,
            U256::ONE,
            U256::Q96,
            U256::Q128,
            U256::MAX,
            U256::from_dec_str("1461446703485210103287273052203988822378723970342").unwrap(),
        ];
        for val in values {
            let dec = val.to_dec_string();
            let hex_s = val.to_hex_string();
            assert_eq!(U256::from_dec_str(&dec).unwrap(), val);
            assert_eq!(U256::from_hex_str(&hex_s).unwrap(), val);
            assert_eq!(U256::from_str_auto(&dec).unwrap(), val);
            assert_eq!(U256::from_str_auto(&hex_s).unwrap(), val);
        }
        assert!(U256::from_dec_str("").is_err());
        assert!(U256::from_dec_str("-1").is_err());
        assert!(
            U256::from_dec_str(
                "115792089237316195423570985008687907853269984665640564039457584007913129639936"
            )
            .is_err()
        );
        assert!(U256::from_hex_str("1234").is_err());
        assert!(U256::from_hex_str("0x").is_err());
    }
}
