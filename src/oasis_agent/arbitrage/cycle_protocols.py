"""Exact-input quote adapters and strict event ABI shared by the case decoder."""

from dataclasses import dataclass
from typing import Any

from eth_abi.abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_hash.auto import keccak

from ..evidence.rpc_validation import strict_string
from ..models.common import ZERO_ADDRESS
from ..models.v4 import PoolKey
from ..uniswap_v4.liquidity_math import MAX_SQRT_PRICE, MIN_SQRT_PRICE
from ..uniswap_v4.pool_id import pool_id_from_key
from ..uniswap_v4.quotes import (
    QuoteDirection,
    QuoteKind,
    QuotePoolKey,
    SinglePoolQuoteRequest,
    decode_single_pool_quote,
    encode_single_pool_quote,
)
from .cycle_evidence import Reads, Unavailable
from .cycle_plan import Asset, Pool

UINT256 = 1 << 256
INT128 = 1 << 127


def topic(signature: str) -> str:
    return "0x" + keccak(signature.encode()).hex()


def calldata(signature: str, types: tuple[str, ...] = (), args: tuple[object, ...] = ()) -> str:
    return topic(signature)[:10] + encode(types, args).hex()


def data_bytes(value: object) -> bytes:
    data = strict_string(value, "ABI data")
    if not data.startswith("0x") or any(c not in "0123456789abcdefABCDEF" for c in data[2:]):
        raise ValueError("invalid hex ABI data")
    return bytes.fromhex(data[2:])


def abi(types: tuple[str, ...], raw: bytes) -> tuple[Any, ...]:
    try:
        values = decode(types, raw, strict=True)
        if encode(types, values) != raw:
            raise ValueError("noncanonical ABI data")
        return values
    except DecodingError as exc:
        raise ValueError("invalid ABI data") from exc


def call(reads: Reads, target: str, data: str) -> tuple[bytes, tuple[str, ...]]:
    raw, refs = reads.pinned("eth_call", {"to": target, "data": data})
    return data_bytes(raw), refs


def getter(
    reads: Reads,
    target: str,
    signature: str,
    result_type: str,
) -> tuple[Any, tuple[str, ...]]:
    raw, refs = call(reads, target, calldata(signature))
    return abi((result_type,), raw)[0], refs


def verify_code(reads: Reads, target: str, expected: str | None) -> tuple[str, ...]:
    if expected is None:
        raise Unavailable("reviewed_runtime_hash_missing")
    raw, refs = reads.pinned("eth_getCode", target)
    code = data_bytes(raw)
    if not code or "0x" + keccak(code).hex() != expected:
        raise Unavailable("runtime_hash_mismatch")
    return refs


def verify_asset(reads: Reads, asset: Asset) -> tuple[str, ...]:
    if asset.address is None:
        return ()
    code_refs = verify_code(reads, asset.address, asset.runtime_hash)
    decimals, refs = getter(reads, asset.address, "decimals()", "uint8")
    if decimals != asset.decimals:
        raise Unavailable("token_decimals_mismatch")
    return code_refs + refs


def v4_key(pool: Pool) -> QuotePoolKey:
    if pool.fee is None or pool.tick_spacing is None or pool.hooks is None:
        raise Unavailable("v4_pool_key_missing")
    key = QuotePoolKey(pool.token0, pool.token1, pool.fee, pool.tick_spacing, pool.hooks)
    if pool_id_from_key(
        PoolKey(pool.token0, pool.token1, pool.fee, pool.tick_spacing, pool.hooks)
    ) != (pool.pool_id):
        raise Unavailable("v4_pool_id_mismatch")
    return key


def verify_pool(reads: Reads, pool: Pool) -> tuple[str, ...]:
    refs = verify_code(reads, pool.address, pool.runtime_hash)
    if pool.protocol == "v4":
        v4_key(pool)
        return refs
    if pool.factory is None:
        raise Unavailable("pool_factory_missing")
    refs += verify_code(reads, pool.factory, pool.factory_hash)
    checks: tuple[tuple[str, str, object], ...] = (
        ("token0()", "address", pool.token0),
        ("token1()", "address", pool.token1),
        ("factory()", "address", pool.factory),
    )
    if pool.protocol == "solidly_volatile":
        checks += (("stable()", "bool", False),)
    else:
        if pool.fee is None or pool.fee >= 1_000_000:
            raise Unavailable("v3_fee_missing_or_invalid")
        checks += (("fee()", "uint24", pool.fee),)
    for signature, result_type, expected in checks:
        actual, check_refs = getter(reads, pool.address, signature, result_type)
        if actual != expected:
            raise Unavailable("pool_metadata_mismatch")
        refs += check_refs
    if pool.protocol == "v3":
        raw, check_refs = call(
            reads,
            pool.factory,
            calldata(
                "getPool(address,address,uint24)",
                ("address", "address", "uint24"),
                (pool.token0, pool.token1, pool.fee),
            ),
        )
        if abi(("address",), raw)[0] != pool.address:
            raise Unavailable("v3_factory_pool_mismatch")
        refs += check_refs
    return refs


def verify_quoter(reads: Reads, pool: Pool) -> tuple[str, ...]:
    if pool.protocol == "solidly_volatile":
        return ()
    if pool.quoter is None:
        raise Unavailable("quoter_missing")
    refs = verify_code(reads, pool.quoter, pool.quoter_hash)
    expected: str | None
    if pool.protocol == "v4":
        key = v4_key(pool)
        if key.hooks != ZERO_ADDRESS or key.fee >= 1_000_000:
            raise Unavailable("unsupported_v4_hook_or_dynamic_fee")
        actual, target_refs = getter(reads, pool.quoter, "poolManager()", "address")
        expected = pool.address
    else:
        actual, target_refs = getter(reads, pool.quoter, "factory()", "address")
        expected = pool.factory
    if actual != expected:
        raise Unavailable("quoter_deployment_mismatch")
    return refs + target_refs


@dataclass(frozen=True, slots=True)
class Reserves:
    reserve0: int
    reserve1: int
    last_update: int
    evidence: tuple[str, ...]


def reserves(reads: Reads, pool: Pool) -> Reserves:
    raw, refs = call(reads, pool.address, calldata("getReserves()"))
    reserve0, reserve1, last_update = abi(("uint256", "uint256", "uint256"), raw)
    assert reads.block is not None and reads.block.timestamp is not None
    if not reserve0 or not reserve1 or last_update > reads.block.timestamp:
        raise Unavailable("invalid_solidly_reserves")
    return Reserves(reserve0, reserve1, last_update, refs)


def quote_request(
    pool: Pool,
    token_in: str,
    amount: int,
    state: Reserves | None,
) -> tuple[str, str]:
    if token_in not in (pool.token0, pool.token1) or not 0 < amount < 1 << 255:
        raise Unavailable("invalid_quote_input")
    if pool.protocol == "solidly_volatile":
        if state is None:
            raise Unavailable("solidly_reserves_unavailable")
        target = pool.address
        data = calldata("getAmountOut(uint256,address)", ("uint256", "address"), (amount, token_in))
    elif pool.protocol == "v3":
        assert pool.quoter is not None
        target = pool.quoter
        token_out = pool.token1 if token_in == pool.token0 else pool.token0
        types = ("(address,address,uint256,uint24,uint160)",)
        data = calldata(
            "quoteExactInputSingle((address,address,uint256,uint24,uint160))",
            types,
            ((token_in, token_out, amount, pool.fee, 0),),
        )
    else:
        if amount >= INT128:
            raise Unavailable("v4_amount_outside_signed_range")
        assert pool.quoter is not None
        target = pool.quoter
        direction = (
            QuoteDirection.ZERO_FOR_ONE if token_in == pool.token0 else QuoteDirection.ONE_FOR_ZERO
        )
        data = encode_single_pool_quote(
            v4_key(pool), SinglePoolQuoteRequest(QuoteKind.EXACT_INPUT, direction, amount)
        )
    return target, data


def quote_result(
    pool: Pool, token_in: str, raw: bytes, state: Reserves | None
) -> tuple[int, int | None]:
    if pool.protocol == "v4":
        result = decode_single_pool_quote("0x" + raw.hex())
        output, gas = result.quoted_amount, result.gas_estimate
        ceiling = INT128
    elif pool.protocol == "v3":
        output, sqrt_price, _, gas = abi(("uint256", "uint160", "uint32", "uint256"), raw)
        if not MIN_SQRT_PRICE + 1 < sqrt_price < MAX_SQRT_PRICE - 1:
            raise Unavailable("v3_partial_fill_possible")
        ceiling = 1 << 255
    else:
        assert state is not None
        output = abi(("uint256",), raw)[0]
        gas = None
        ceiling = state.reserve1 if token_in == pool.token0 else state.reserve0
    if not 0 < output < ceiling:
        raise Unavailable("quote_output_outside_bounds")
    return output, gas


def quote(
    reads: Reads,
    pool: Pool,
    token_in: str,
    amount: int,
    state: Reserves | None,
) -> tuple[int, tuple[str, ...]]:
    target, data = quote_request(pool, token_in, amount, state)
    raw, refs = call(reads, target, data)
    output, _ = quote_result(pool, token_in, raw, state)
    return output, refs
