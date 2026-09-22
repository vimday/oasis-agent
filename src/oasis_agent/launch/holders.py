"""Deterministic holder diagnostics; imported balances are never raw chain proof."""

from typing import Any

from .models import Cycle, HolderTarget


def assess_holders(cycle: Cycle, target: HolderTarget) -> dict[str, Any]:
    reasons = ["holder_raw_proof_unverified", "holder_common_control_unresolved"]
    result: dict[str, Any] = {
        "schema": "oasis.holder-assessment.v1",
        "execution_eligible": False,
        "evidence_level": "imported_unverified",
        "metrics": None,
        "stand_down": True,
        "reasons": reasons,
    }
    samples = cycle.holder_observations or ()
    if len(samples) != 2 or len({sample.source for sample in samples}) != 2:
        reasons.append("holder_two_sources_required")
        return result
    invalid: list[str] = []
    if target.max_age_seconds is None or target.max_age_blocks is None:
        invalid.append("holder_freshness_limits_unfrozen")
    for sample in samples:
        matching = [item for item in cycle.observations if item.source == sample.source]
        if (
            len(matching) != 1
            or matching[0].snapshot != sample.snapshot
            or sample.snapshot.chain_id != target.chain_id
        ):
            invalid.append("holder_snapshot_mismatch")
        if (
            sample.target_ca != target.target_ca
            or sample.runtime_hash != target.runtime_hash
            or sample.token_decimals != target.token_decimals
        ):
            invalid.append("holder_identity_mismatch")
        if not sample.snapshot.timestamp <= sample.retrieved_at <= cycle.evaluated_at:
            invalid.append("holder_time_incoherent")
        if (
            target.max_age_seconds is not None
            and not 0 <= cycle.evaluated_at - sample.snapshot.timestamp <= target.max_age_seconds
        ) or (
            target.max_age_blocks is not None
            and not 0 <= cycle.current_block - sample.snapshot.number <= target.max_age_blocks
        ):
            invalid.append("holder_stale_or_future_snapshot")
        if not sample.complete:
            invalid.append("holder_coverage_incomplete")
        if (
            sum(item.balance_base_units for item in sample.balances)
            != sample.total_supply_base_units
        ):
            invalid.append("holder_supply_unreconciled")
    left, right = samples
    if (
        left.snapshot != right.snapshot
        or left.total_supply_base_units != right.total_supply_base_units
        or {item.address: item.balance_base_units for item in left.balances}
        != {item.address: item.balance_base_units for item in right.balances}
    ):
        invalid.append("holder_provider_disagreement")
    if invalid:
        reasons.extend(sorted(set(invalid)))
        return result
    balances = sorted((item.balance_base_units for item in left.balances), reverse=True)
    supply = left.total_supply_base_units
    single = balances[0]
    top10 = sum(balances[:10])
    squares = sum(value * value for value in balances)
    limits = target.holder_limits
    result["metrics"] = {
        "holder_count": len(balances),
        "single_bps": (single * 10_000 + supply - 1) // supply,
        "top10_bps": (top10 * 10_000 + supply - 1) // supply,
        "hhi_bps": (squares * 10_000 + supply * supply - 1) // (supply * supply),
    }
    result["stand_down"] = False
    if len(balances) < limits.min_holders:
        reasons.append("holder_count_below_minimum")
    for exceeds, reason in (
        (single * 10_000 > limits.max_single_bps * supply, "holder_single_limit_exceeded"),
        (top10 * 10_000 > limits.max_top10_bps * supply, "holder_top10_limit_exceeded"),
        (squares * 10_000 > limits.max_hhi_bps * supply * supply, "holder_hhi_limit_exceeded"),
    ):
        if exceeds:
            reasons.append(reason)
    return result
