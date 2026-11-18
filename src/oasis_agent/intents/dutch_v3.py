"""UniswapX V3DutchOrder nonlinear block-decay resolution for Base (chain ID 8453)."""

from dataclasses import dataclass
from typing import Final

from ..errors import ExecutionBlockedError
from ..models.common import ZERO_ADDRESS, normalize_address

BASE_CHAIN_ID: Final[int] = 8453
BPS: Final[int] = 10_000

# Deprecated Base OP-stack PriorityOrderReactor; Base L2 UniswapX orders use V3DutchOrderReactor.
DEPRECATED_PRIORITY_REACTOR_BASE: Final[str] = "0x000000001ec5656dcdb24d90dfa42742738de729"
DEPRECATED_REACTORS_BY_CHAIN: Final[dict[int, frozenset[str]]] = {
    BASE_CHAIN_ID: frozenset({DEPRECATED_PRIORITY_REACTOR_BASE}),
}


def _require_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    return value


def _require_nonneg_int(value: object, name: str) -> int:
    parsed = _require_int(value, name)
    if parsed < 0:
        raise ValueError(f"{name} must be nonnegative")
    return parsed


def _require_pos_int(value: object, name: str) -> int:
    parsed = _require_int(value, name)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _require_nonzero_address(value: str, name: str) -> str:
    normalized = normalize_address(value)
    if int(normalized, 16) == 0:
        raise ValueError(f"{name} must be nonzero")
    return normalized


def _trunc_div(numerator: int, denominator: int) -> int:
    """Integer division truncating toward zero, matching Solidity signed int256 `/`."""
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    if numerator >= 0:
        return numerator // denominator
    return -((-numerator) // denominator)


def validate_dutch_v3_reactor(reactor_address: str, *, chain_id: int) -> str:
    """Normalize and reject zero or deprecated PriorityOrderReactor addresses."""
    if type(chain_id) is not int or chain_id != BASE_CHAIN_ID:
        raise ValueError(f"V3DutchOrderReactor requires Base chain_id {BASE_CHAIN_ID}")
    normalized = _require_nonzero_address(reactor_address, "reactor_address")
    if normalized in DEPRECATED_REACTORS_BY_CHAIN.get(chain_id, frozenset()):
        raise ValueError(
            f"deprecated PriorityOrderReactor address {normalized} is rejected on chain {chain_id}"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class DutchV3Curve:
    """Piecewise linear block-offset delta curve matching NonlinearDutchDecayLib."""

    relative_blocks: tuple[int, ...]
    relative_amounts: tuple[int, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.relative_blocks, tuple) or not isinstance(
            self.relative_amounts, tuple
        ):
            raise TypeError("curve points must be tuples")
        if len(self.relative_blocks) != len(self.relative_amounts):
            raise ValueError("relative_blocks and relative_amounts must have equal length")
        prev_block = 0
        for block_offset in self.relative_blocks:
            _require_pos_int(block_offset, "relative_block")
            if block_offset <= prev_block:
                raise ValueError("relative_blocks must be strictly increasing")
            prev_block = block_offset
        for amount_delta in self.relative_amounts:
            _require_int(amount_delta, "relative_amount")
        if self.relative_amounts:
            if all(val >= 0 for val in self.relative_amounts):
                for idx in range(1, len(self.relative_amounts)):
                    if self.relative_amounts[idx] < self.relative_amounts[idx - 1]:
                        raise ValueError("output relative_amounts must be nondecreasing")
            elif all(val <= 0 for val in self.relative_amounts):
                for idx in range(1, len(self.relative_amounts)):
                    if self.relative_amounts[idx] > self.relative_amounts[idx - 1]:
                        raise ValueError("input relative_amounts must be nonincreasing")
            else:
                raise ValueError("curve relative_amounts must not mix positive and negative signs")


@dataclass(frozen=True, slots=True)
class DutchV3Output:
    """Single output leg of a UniswapX V3DutchOrder."""

    token: str
    start_amount: int
    end_amount: int
    recipient: str
    curve: DutchV3Curve

    def __post_init__(self) -> None:
        object.__setattr__(self, "token", _require_nonzero_address(self.token, "output.token"))
        object.__setattr__(
            self, "recipient", _require_nonzero_address(self.recipient, "output.recipient")
        )
        _require_pos_int(self.start_amount, "output.start_amount")
        _require_nonneg_int(self.end_amount, "output.end_amount")
        if self.end_amount > self.start_amount:
            raise ValueError("output.end_amount cannot exceed output.start_amount")
        if not isinstance(self.curve, DutchV3Curve):
            raise TypeError("output.curve must be a DutchV3Curve")
        if any(delta < 0 for delta in self.curve.relative_amounts):
            raise ValueError("output curve relative_amounts must be nonnegative")


@dataclass(frozen=True, slots=True)
class DutchV3Order:
    """Canonical Base V3DutchOrder model for exact block-level resolution."""

    order_hash: str
    chain_id: int
    reactor_address: str
    swapper: str
    nonce: int
    decay_start_block: int
    decay_end_block: int
    deadline_block: int
    exclusivity_end_block: int
    exclusive_filler: str
    exclusivity_override_bps: int
    input_token: str
    input_start_amount: int
    input_end_amount: int
    outputs: tuple[DutchV3Output, ...]
    input_curve: DutchV3Curve | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.order_hash, str) or not self.order_hash.strip():
            raise ValueError("order_hash must be a nonempty string")
        object.__setattr__(
            self,
            "reactor_address",
            validate_dutch_v3_reactor(self.reactor_address, chain_id=self.chain_id),
        )
        object.__setattr__(self, "swapper", _require_nonzero_address(self.swapper, "swapper"))
        nonce = _require_nonneg_int(self.nonce, "nonce")
        if nonce >= 1 << 256:
            raise ValueError("nonce exceeds uint256")
        _require_pos_int(self.decay_start_block, "decay_start_block")
        _require_pos_int(self.decay_end_block, "decay_end_block")
        _require_pos_int(self.deadline_block, "deadline_block")
        _require_nonneg_int(self.exclusivity_end_block, "exclusivity_end_block")
        if self.decay_end_block < self.decay_start_block:
            raise ValueError("decay_end_block cannot precede decay_start_block")
        if self.deadline_block < self.decay_end_block:
            raise ValueError("deadline_block cannot precede decay_end_block")

        object.__setattr__(self, "exclusive_filler", normalize_address(self.exclusive_filler))
        bps = _require_nonneg_int(self.exclusivity_override_bps, "exclusivity_override_bps")
        if bps > BPS:
            raise ValueError("exclusivity_override_bps cannot exceed 10000")

        object.__setattr__(
            self, "input_token", _require_nonzero_address(self.input_token, "input_token")
        )
        _require_pos_int(self.input_start_amount, "input_start_amount")
        _require_pos_int(self.input_end_amount, "input_end_amount")
        if self.input_end_amount < self.input_start_amount:
            raise ValueError("input_end_amount cannot be below input_start_amount")

        decay_span = self.decay_end_block - self.decay_start_block
        if self.input_curve is not None:
            if not isinstance(self.input_curve, DutchV3Curve):
                raise TypeError("input_curve must be a DutchV3Curve")
            if any(delta > 0 for delta in self.input_curve.relative_amounts):
                raise ValueError("input_curve relative_amounts must be nonpositive")
            if (
                self.input_curve.relative_blocks
                and self.input_curve.relative_blocks[-1] > decay_span
            ):
                raise ValueError("input_curve extends past decay_end_block")

        if not isinstance(self.outputs, tuple) or not self.outputs:
            raise ValueError("outputs must be a nonempty tuple of DutchV3Output")
        for output in self.outputs:
            if not isinstance(output, DutchV3Output):
                raise TypeError("each output must be a DutchV3Output")
            if output.curve.relative_blocks and output.curve.relative_blocks[-1] > decay_span:
                raise ValueError("output curve extends past decay_end_block")


@dataclass(frozen=True, slots=True)
class ResolvedDutchOutput:
    token: str
    amount: int
    recipient: str


@dataclass(frozen=True, slots=True)
class ResolvedDutchAmounts:
    """Exact resolved input and output amounts for a V3DutchOrder at fill_block."""

    order_hash: str
    fill_block: int
    filler_address: str
    input_token: str
    input_amount: int
    outputs: tuple[ResolvedDutchOutput, ...]
    exclusivity_override_applied: bool
    exclusivity_override_bps_used: int

    @property
    def output_amounts(self) -> tuple[int, ...]:
        return tuple(item.amount for item in self.outputs)


def _interpolate_curve_delta(curve: DutchV3Curve, block_delta: int) -> int:
    if block_delta <= 0 or not curve.relative_blocks:
        return 0
    prev_block = 0
    prev_amount = 0
    for rel_block, rel_amount in zip(curve.relative_blocks, curve.relative_amounts, strict=True):
        if block_delta <= rel_block:
            span = rel_block - prev_block
            elapsed = block_delta - prev_block
            return prev_amount + _trunc_div((rel_amount - prev_amount) * elapsed, span)
        prev_block = rel_block
        prev_amount = rel_amount
    return prev_amount


def _resolve_output_amount(output: DutchV3Output, block_delta: int) -> int:
    if block_delta <= 0 or not output.curve.relative_blocks:
        return output.start_amount
    rel_delta = _interpolate_curve_delta(output.curve, block_delta)
    raw = output.start_amount - rel_delta
    return max(output.end_amount, min(output.start_amount, raw))


def _resolve_input_amount(order: DutchV3Order, fill_block: int) -> int:
    if fill_block <= order.decay_start_block:
        return order.input_start_amount
    if order.input_curve is not None:
        if not order.input_curve.relative_blocks:
            return order.input_start_amount
        rel_delta = _interpolate_curve_delta(
            order.input_curve, fill_block - order.decay_start_block
        )
        raw = order.input_start_amount - rel_delta
        return min(order.input_end_amount, max(order.input_start_amount, raw))

    if order.input_end_amount == order.input_start_amount:
        return order.input_start_amount
    span = order.decay_end_block - order.decay_start_block
    if span == 0 or fill_block >= order.decay_end_block:
        return order.input_end_amount
    elapsed = fill_block - order.decay_start_block
    raw = order.input_start_amount + _trunc_div(
        (order.input_end_amount - order.input_start_amount) * elapsed, span
    )
    return min(order.input_end_amount, max(order.input_start_amount, raw))


def resolve_dutch_v3_amounts(
    order: DutchV3Order, fill_block: int, filler_address: str
) -> ResolvedDutchAmounts:
    """Resolve exact V3DutchOrder input/output amounts and exclusivity override at fill_block."""
    if not isinstance(order, DutchV3Order):
        raise TypeError("order must be a DutchV3Order")
    validate_dutch_v3_reactor(order.reactor_address, chain_id=order.chain_id)
    _require_pos_int(fill_block, "fill_block")
    filler = _require_nonzero_address(filler_address, "filler_address")

    if fill_block > order.deadline_block:
        raise ExecutionBlockedError("dutch_v3_order_expired")

    block_delta = max(0, fill_block - order.decay_start_block)
    input_amount = _resolve_input_amount(order, fill_block)
    base_outputs = [_resolve_output_amount(out, block_delta) for out in order.outputs]

    in_exclusive_window = (
        order.exclusive_filler != ZERO_ADDRESS and fill_block < order.exclusivity_end_block
    )
    is_non_exclusive_filler = filler != order.exclusive_filler

    override_applied = False
    override_bps_used = 0
    if in_exclusive_window and is_non_exclusive_filler:
        if order.exclusivity_override_bps == 0:
            raise ExecutionBlockedError("exclusive_window_strict_no_override")
        override_applied = True
        override_bps_used = order.exclusivity_override_bps
        factor = BPS + order.exclusivity_override_bps
        base_outputs = [(amt * factor + BPS - 1) // BPS for amt in base_outputs]

    resolved_outputs = tuple(
        ResolvedDutchOutput(token=out.token, amount=amt, recipient=out.recipient)
        for out, amt in zip(order.outputs, base_outputs, strict=True)
    )
    return ResolvedDutchAmounts(
        order_hash=order.order_hash,
        fill_block=fill_block,
        filler_address=filler,
        input_token=order.input_token,
        input_amount=input_amount,
        outputs=resolved_outputs,
        exclusivity_override_applied=override_applied,
        exclusivity_override_bps_used=override_bps_used,
    )


__all__ = [
    "BASE_CHAIN_ID",
    "BPS",
    "DEPRECATED_PRIORITY_REACTOR_BASE",
    "DEPRECATED_REACTORS_BY_CHAIN",
    "DutchV3Curve",
    "DutchV3Order",
    "DutchV3Output",
    "ResolvedDutchAmounts",
    "ResolvedDutchOutput",
    "resolve_dutch_v3_amounts",
    "validate_dutch_v3_reactor",
]
