"""Pure ABI encoding and typed intent specs for OasisAtomicExecutor cycles.

This module has no RPC, key, signing, or broadcast surface. It binds cycle intents
to explicit chain, executor address, executor runtime code hash, strict cycle path
continuity, and exact Solidity ABI calldata plus SHA-256 commitment.
"""

from dataclasses import asdict, dataclass

from eth_abi.abi import encode
from eth_hash.auto import keccak

from ..models.common import (
    ZERO_ADDRESS,
    canonical_json,
    normalize_address,
    normalize_hash32,
    sha256_hex,
)

UINT64_MAX = (1 << 64) - 1
UINT160_MAX = (1 << 160) - 1
UINT256_MAX = (1 << 256) - 1
BPS_DENOMINATOR = 10_000

EXECUTE_CYCLE_TYPE = (
    "(uint64,uint64,address,uint256,uint256,"
    "(address,uint160,uint160),"
    "(address,address,address,bool,uint160,uint24)[])"
)
EXECUTE_CYCLE_SIGNATURE = f"executeCycle({EXECUTE_CYCLE_TYPE})"
EXECUTE_CYCLE_SELECTOR: bytes = keccak(EXECUTE_CYCLE_SIGNATURE.encode())[:4]


def _require_int_range(value: object, name: str, *, low: int, high: int) -> int:
    if type(value) is not int or value < low or value > high:
        raise ValueError(f"{name} must be an integer in [{low}, {high}]")
    return value


def _require_nonzero_address(value: str, name: str) -> str:
    normalized = normalize_address(value)
    if normalized == ZERO_ADDRESS:
        raise ValueError(f"{name} must be nonzero")
    return normalized


@dataclass(frozen=True, slots=True)
class PrecheckGuardSpec:
    """Optional Uniswap V3 / Slipstream slot0 sqrtPriceX96 band precheck."""

    pool: str
    min_sqrt_price_x96: int = 0
    max_sqrt_price_x96: int = 0

    def __post_init__(self) -> None:
        pool = normalize_address(self.pool)
        object.__setattr__(self, "pool", pool)
        min_price = _require_int_range(
            self.min_sqrt_price_x96, "min_sqrt_price_x96", low=0, high=UINT160_MAX
        )
        max_price = _require_int_range(
            self.max_sqrt_price_x96, "max_sqrt_price_x96", low=0, high=UINT160_MAX
        )
        if min_price > max_price:
            raise ValueError("min_sqrt_price_x96 cannot exceed max_sqrt_price_x96")
        if pool == ZERO_ADDRESS and (min_price != 0 or max_price != 0):
            raise ValueError("disabled guard (zero pool) must have zero price bounds")
        if pool != ZERO_ADDRESS and max_price == 0:
            raise ValueError("active guard requires positive max_sqrt_price_x96")


@dataclass(frozen=True, slots=True)
class SwapLegSpec:
    """Single bounded AMM swap leg inside an atomic cycle."""

    pool: str
    token_in: str
    token_out: str
    zero_for_one: bool
    sqrt_price_limit_x96: int = 0
    v2_fee_bps: int = 0

    def __post_init__(self) -> None:
        pool = _require_nonzero_address(self.pool, "pool")
        token_in = _require_nonzero_address(self.token_in, "token_in")
        token_out = _require_nonzero_address(self.token_out, "token_out")
        if token_in == token_out:
            raise ValueError("swap leg token_in and token_out must differ")
        if type(self.zero_for_one) is not bool:
            raise TypeError("zero_for_one must be a boolean")
        _require_int_range(
            self.sqrt_price_limit_x96, "sqrt_price_limit_x96", low=0, high=UINT160_MAX
        )
        _require_int_range(self.v2_fee_bps, "v2_fee_bps", low=0, high=BPS_DENOMINATOR - 1)
        object.__setattr__(self, "pool", pool)
        object.__setattr__(self, "token_in", token_in)
        object.__setattr__(self, "token_out", token_out)


@dataclass(frozen=True, slots=True)
class AtomicCycleIntent:
    """Validated, code-hash-pinned atomic multi-hop arbitrage intent."""

    chain_id: int
    executor_address: str
    executor_code_hash: str
    nonce: int
    deadline: int
    profit_token: str
    initial_amount: int
    min_net_profit: int
    guard: PrecheckGuardSpec
    legs: tuple[SwapLegSpec, ...]

    def __post_init__(self) -> None:
        _require_int_range(self.chain_id, "chain_id", low=1, high=UINT256_MAX)
        executor_address = _require_nonzero_address(self.executor_address, "executor_address")
        executor_code_hash = normalize_hash32(self.executor_code_hash, "executor_code_hash")
        if int(executor_code_hash, 16) == 0:
            raise ValueError("executor_code_hash must be nonzero")
        _require_int_range(self.nonce, "nonce", low=1, high=UINT64_MAX)
        _require_int_range(self.deadline, "deadline", low=1, high=UINT64_MAX)
        profit_token = _require_nonzero_address(self.profit_token, "profit_token")
        _require_int_range(self.initial_amount, "initial_amount", low=1, high=UINT256_MAX)
        _require_int_range(self.min_net_profit, "min_net_profit", low=0, high=UINT256_MAX)
        if not isinstance(self.guard, PrecheckGuardSpec):
            raise TypeError("guard must be a PrecheckGuardSpec")

        legs = tuple(self.legs)
        if not 2 <= len(legs) <= 3:
            raise ValueError("atomic cycle requires 2 or 3 swap legs")
        if any(not isinstance(leg, SwapLegSpec) for leg in legs):
            raise TypeError("every leg must be a SwapLegSpec")
        if legs[0].token_in != profit_token or legs[-1].token_out != profit_token:
            raise ValueError("cycle legs must begin and end with profit_token")
        for idx in range(len(legs) - 1):
            if legs[idx].token_out != legs[idx + 1].token_in:
                raise ValueError("cycle legs must form a contiguous token path")

        object.__setattr__(self, "executor_address", executor_address)
        object.__setattr__(self, "executor_code_hash", executor_code_hash)
        object.__setattr__(self, "profit_token", profit_token)
        object.__setattr__(self, "legs", legs)

    @property
    def intent_hash(self) -> str:
        """Deterministic SHA-256 commitment over chain, pinned runtime, and cycle params."""
        return sha256_hex(canonical_json(asdict(self)))


def encode_execute_cycle_calldata(intent: AtomicCycleIntent) -> tuple[bytes, str]:
    """Encode `OasisAtomicExecutor.executeCycle` calldata and return `(calldata, sha256)`."""
    if not isinstance(intent, AtomicCycleIntent):
        raise TypeError("intent must be an AtomicCycleIntent")

    guard_tuple = (
        intent.guard.pool,
        intent.guard.min_sqrt_price_x96,
        intent.guard.max_sqrt_price_x96,
    )
    legs_tuples = [
        (
            leg.pool,
            leg.token_in,
            leg.token_out,
            leg.zero_for_one,
            leg.sqrt_price_limit_x96,
            leg.v2_fee_bps,
        )
        for leg in intent.legs
    ]
    cycle_params = (
        intent.nonce,
        intent.deadline,
        intent.profit_token,
        intent.initial_amount,
        intent.min_net_profit,
        guard_tuple,
        legs_tuples,
    )
    raw_calldata = EXECUTE_CYCLE_SELECTOR + encode([EXECUTE_CYCLE_TYPE], [cycle_params])
    return raw_calldata, sha256_hex(raw_calldata)


__all__ = [
    "BPS_DENOMINATOR",
    "EXECUTE_CYCLE_SELECTOR",
    "EXECUTE_CYCLE_SIGNATURE",
    "EXECUTE_CYCLE_TYPE",
    "AtomicCycleIntent",
    "PrecheckGuardSpec",
    "SwapLegSpec",
    "encode_execute_cycle_calldata",
]
