"""Declared bookkeeping allocation, distinct from observed transaction payment.

The actual payer remains on the transaction receipt. Allocation cannot establish
beneficial ownership, reimbursements, or which NFT physically paid shared gas.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AllocatedCost:
    position_id: str
    weight: int
    native_units: int


def allocate_cost(
    total_native: int, weights: tuple[tuple[str, int], ...]
) -> tuple[AllocatedCost, ...]:
    """Largest remainder, exact conservation; ties by stable position identifier."""
    if type(total_native) is not int or not 0 <= total_native < 1 << 256:
        raise ValueError("transaction cost must fit uint256")
    if not weights or len(weights) > 1000:
        raise ValueError("bounded complete allocation scope required")
    if any(
        not isinstance(key, str) or not key.strip() or type(w) is not int or not 0 < w < 1 << 256
        for key, w in weights
    ):
        raise ValueError("nonempty position identifiers and positive uint256 weights required")
    if len({key for key, _ in weights}) != len(weights):
        raise ValueError("duplicate allocation position")
    ordered = tuple(sorted(weights))
    denominator = sum(w for _, w in ordered)
    base = {key: total_native * w // denominator for key, w in ordered}
    spare = total_native - sum(base.values())
    ranked = sorted(ordered, key=lambda x: (-(total_native * x[1] % denominator), x[0]))
    for key, _ in ranked[:spare]:
        base[key] += 1
    return tuple(AllocatedCost(key, w, base[key]) for key, w in ordered)
