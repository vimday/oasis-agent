"""Pure validation of the sole supported journal configuration transition.

The transition only tightens USDC age to 1800 seconds and keeps execution halted.
Raw proof verification belongs to feed_transition; journal replay verifies this
record and the exact permitted state delta without opening an evidence database.
"""

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from ..models.common import canonical_json, sha256_hex
from .config import LiveConfig
from .exact_risk import ExactRisk

HALT = "strict-feed configuration migrated; operator review required before execution"


def transition_snapshot(
    prior: dict[str, Any], record: dict[str, Any], *, binding: str, parent: str
) -> tuple[str, dict[str, Any]]:
    if set(record) != {"schema", "source_tip", "source_state_hash", "target_config", "review"}:
        raise ValueError("transition record fields differ")
    if (
        record["schema"] != "oasis.strict-feed-epoch.v1"
        or record["source_tip"] != parent
        or record["source_state_hash"] != sha256_hex(canonical_json(prior))
    ):
        raise ValueError("transition does not bind the prior journal state")
    target = LiveConfig.from_json(canonical_json(record["target_config"]))
    previous = replace(target, usdc_oracle_max_age_seconds=90_000)
    if (
        target.oracle_max_age_seconds != 1800
        or target.usdc_oracle_max_age_seconds != 1800
        or binding != sha256_hex(canonical_json(asdict(previous)))
    ):
        raise ValueError("only the historical USDC age tightening is supported")
    target_hash = sha256_hex(canonical_json(asdict(target)))
    review = record["review"]
    if (
        review["schema"] != "oasis.feed-transition-service-review.v1"
        or review["source_config_hash"] != binding
        or review["target_config_hash"] != target_hash
        or review["state_hash"] != record["source_state_hash"]
        or review["execution_eligible"] is not False
        or review["historical_checkpoint_verified"] is not True
        or review["fresh_nav_verified"] is not True
        or review["blockers"] != []
        or prior.get("pending") is not None
        or prior.get("signing_attempt") is not None
        or prior.get("acknowledge_flow") is not False
        or prior.get("legacy_gas_cost_incomplete") is not False
    ):
        raise ValueError("transition evidence review or prior state is unresolved")
    return target_hash, checkpoint_snapshot(
        prior, target, review["fresh_nav"], review["risk_preview"], halt_reason=HALT
    )


def checkpoint_snapshot(
    prior: dict[str, Any],
    target: LiveConfig,
    fresh: dict[str, Any],
    risk_preview: dict[str, Any],
    *,
    halt_reason: str,
) -> dict[str, Any]:
    """Apply the existing integer checkpoint delta without changing capital/history."""
    stamp, nav = fresh["block"]["timestamp"], fresh["nav_usd_micros"]
    risk = ExactRisk(**prior["exact_risk"])
    if type(stamp) is not int or stamp <= risk.last_at or risk.basis is None:
        raise ValueError("transition needs a newer complete risk checkpoint")
    old_day = risk.day
    day = datetime.fromtimestamp(stamp, UTC).astimezone(timezone(timedelta(hours=8))).date()
    daily, cumulative = risk.observe(day=day.isoformat(), at=stamp, nav=nav, checkpoint=True)
    if (
        asdict(risk) != risk_preview
        or daily >= target.max_daily_loss_usd * 1_000_000
        or cumulative >= target.max_cumulative_loss_usd * 1_000_000
    ):
        raise ValueError("transition risk preview changes accounting or exhausts limits")
    result = deepcopy(prior)
    result.update(
        exact_risk=asdict(risk),
        nav_evidence=fresh["evidence"],
        halted=prior.get("halted") or halt_reason,
        last_observation=None,
        last_light_check=None,
        initial_nav_usd=risk.basis / 1_000_000,
        external_flow_usd=risk.external / 1_000_000,
        daily_loss_usd=risk.daily_locked / 1_000_000,
        checkpoint_nav_usd=nav / 1_000_000,
        checkpoint_at=stamp,
        last_nav_usd=nav / 1_000_000,
        day=risk.day,
        actions_today=0 if old_day != risk.day else prior["actions_today"],
    )
    # Day/action rollover follows the runtime convention. Gas, position, inventory,
    # history and every other field remain unchanged; migration never clears HALT.
    return result
