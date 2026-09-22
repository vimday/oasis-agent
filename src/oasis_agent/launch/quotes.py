"""Complete two-provider quote matrix checks at one canonical snapshot.

The quote grid is keyed by native spend against Target.buy_tranches_wei
(cumulative native amounts); USD valuations are snapshot-bound integers used
only for FDV and impact checks, never a fixed denomination.
"""

from .entry_price import below_fdv
from .models import Observation, TargetPolicy


def matrix_reasons(observations: tuple[Observation, ...], target: TargetPolicy) -> list[str]:
    reasons: list[str] = []
    if len(observations) != 2 or len({o.source for o in observations}) != 2:
        return ["two_distinct_sources_required"]
    left, right = observations
    if left.snapshot != right.snapshot:
        reasons.append("snapshot_disagreement")
    if (
        left.target_ca,
        left.runtime_hash,
        left.token_decimals,
        left.control_digest,
        left.pool,
        left.token0,
        left.token1,
    ) != (
        right.target_ca,
        right.runtime_hash,
        right.token_decimals,
        right.control_digest,
        right.pool,
        right.token0,
        right.token1,
    ):
        reasons.append("identity_disagreement")
    if target.buy_tranches_wei is None:
        return [*reasons, "tranche_schedule_unfrozen"]
    expected = tuple(sorted(target.buy_tranches_wei))
    matrices = [tuple(sorted(o.quotes, key=lambda q: q.native_wei)) for o in observations]
    if any(tuple(q.native_wei for q in m) != expected for m in matrices):
        return [*reasons, "quote_grid_incomplete_or_duplicate"]
    if matrices[0] != matrices[1]:
        reasons.append("quote_disagreement")
    for observation, matrix in zip(observations, matrices, strict=True):
        if any(
            not below_fdv(
                spend_usd_micros=q.spend_usd_micros,
                received_base_units=q.received_base_units,
                token_decimals=observation.token_decimals,
                max_fdv_usd_micros=target.max_fdv_usd_micros,
                reference_supply=target.reference_supply,
            )
            for q in matrix
        ):
            reasons.append("entry_fdv_at_or_above_cap")
        if target.max_impact_bps is None:
            reasons.append("impact_threshold_unfrozen")
        else:
            small = matrix[0]
            # P(size)/P(small) <= 1 + impact, with no float division.
            if any(
                quote.spend_usd_micros * small.received_base_units * 10_000
                > small.spend_usd_micros
                * quote.received_base_units
                * (10_000 + target.max_impact_bps)
                for quote in matrix[1:]
            ):
                reasons.append("impact_limit_exceeded")
    return sorted(set(reasons))
