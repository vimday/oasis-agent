"""Offline evidence review for the historical 90000s -> 1800s feed transition.

Produces a reviewable risk preview only. No state writer, RPC acquisition,
configuration rebinding, halt clearance or execution permit is exposed.
"""

from copy import deepcopy
from dataclasses import asdict, replace
from typing import Any, NoReturn

from ..evidence.store import EvidenceStore
from ..models.common import EvidenceRef, canonical_json, sha256_hex
from .config import LiveConfig
from .receipts import RetainedReceiptCostReader
from .runner import LiveLoop
from .state import LoopState, Position, day_of, validate_state
from .valuation import nav_fresh_until, replay_nav, replay_nav_quorum


def historical_config(target: LiveConfig, binding: str) -> LiveConfig:
    """Recognize exactly the legacy USDC age difference, for historical replay only."""
    target.validate()
    if target.oracle_max_age_seconds != 1800 or target.usdc_oracle_max_age_seconds != 1800:
        raise ValueError("transition requires the owner's two 1800-second ceilings")
    previous = replace(target, usdc_oracle_max_age_seconds=90_000)
    if sha256_hex(canonical_json(asdict(previous))) != binding:
        raise ValueError("unrecognized prior configuration; only USDC age may differ")
    return previous


def _no_acquisition(*_: object) -> NoReturn:
    raise ValueError("offline transition review cannot acquire data")


def _identity(p: Position | None) -> tuple[int, int, int, int] | None:
    # opened_at is service-local, not part of the on-chain position identity.
    return (p.token_id, p.lower, p.upper, p.liquidity) if p else None


def review_service(
    target: LiveConfig,
    state: LoopState,
    binding: str,
    evidence: EvidenceStore,
    *,
    sources: tuple[str, str],
    fresh: tuple[EvidenceRef, ...] = (),
    now: int,
) -> dict[str, Any]:
    """The caller must obtain state and binding from one verified journal snapshot.

    Old risk is replayed under its recorded policy. Only new proofs are evaluated
    under the current policy. Missing fresh proofs never produce a risk preview.
    """
    if type(now) is not int or now < 0:
        raise ValueError("explicit integer review clock required")
    previous = historical_config(target, binding)
    validate_state(state)
    if state.mode != "live":
        raise ValueError("live state required")
    blockers: list[str] = []
    report: dict[str, Any] = {
        "schema": "oasis.feed-transition-service-review.v1",
        "execution_eligible": False,
        "state_hash": sha256_hex(canonical_json(asdict(state))),
        "source_config_hash": binding,
        "target_config_hash": sha256_hex(canonical_json(asdict(target))),
        "historical_checkpoint_verified": False,
        "fresh_nav_verified": False,
        "fresh_nav": None,
        "risk_preview": None,
        "halt_preserved": bool(state.halted),
        "blockers": blockers,
    }
    for field in ("pending", "signing_attempt"):
        if getattr(state, field) is not None:
            report["blockers"].append(field + "_unresolved")
    if state.legacy_gas_cost_incomplete:
        report["blockers"].append("historical_native_cost_incomplete")
    if state.acknowledge_flow:
        report["blockers"].append("external_flow_acknowledgement_pending")
    risk = state.exact_risk
    if risk is None or risk.basis is None or risk.last_nav is None or state.last_units is None:
        report["blockers"].append("complete_integer_risk_baseline_required")
    if report["blockers"]:
        return report
    assert risk is not None
    # Reuse the production historical-proof verifier, with no acquisition capability.
    loop = object.__new__(LiveLoop)
    loop.mode, loop.c, loop.state = "live", previous, deepcopy(state)
    loop.receipt_costs = RetainedReceiptCostReader(
        evidence,
        chain_id=previous.chain_id,
        account=previous.account,
        native_decimals=previous.decimals0,
        sources=sources,
        sessions=_no_acquisition,
    )
    try:
        loop._check_nav_checkpoint()
        if state.nav_evidence:
            prior = replay_nav_quorum(evidence, state.nav_evidence, previous, sources=sources)
            prior_position = replay_nav(evidence, state.nav_evidence[0], previous).position
            if (
                _identity(prior_position) != _identity(state.position)
                or [
                    prior.wallet.native,
                    prior.wallet.token,
                    prior_position.liquidity if prior_position else 0,
                ]
                != state.last_units
            ):
                raise ValueError("historical holdings differ from checkpoint")
    except Exception:  # noqa: BLE001 - fixed redacted refusal, retain source evidence
        report["blockers"].append("historical_checkpoint_unresolved")
        return report
    report["historical_checkpoint_verified"] = True
    if not fresh:
        report["blockers"].append("fresh_strict_nav_required")
        return report
    try:
        value = replay_nav_quorum(evidence, fresh, target, sources=sources)
        deadline = nav_fresh_until(evidence, fresh, target, sources=sources)
        block = value.wallet.header.ref
        if block.timestamp is None or not risk.last_at < block.timestamp <= now <= deadline:
            raise ValueError("new current NAV required")
        position = replay_nav(evidence, fresh[0], target).position

        if _identity(position) != _identity(state.position):
            raise ValueError("position changed")
        if [
            value.wallet.native,
            value.wallet.token,
            position.liquidity if position else 0,
        ] != state.last_units:
            raise ValueError("unexplained account movement")
        # Future application must recheck all settlement anchors against both providers.
        if any(
            a["number"] > block.number
            or (a["number"] == block.number and a["hash"] != block.block_hash)
            for a in state.settled_blocks
        ):
            raise ValueError("NAV predates or contradicts settlement")
        preview = deepcopy(risk)
        daily, cumulative = preview.observe(
            day=day_of(block.timestamp), at=block.timestamp, nav=value.nav_micros, checkpoint=True
        )
        report["fresh_nav_verified"] = True
        report["fresh_nav"] = {
            "block": asdict(block),
            "nav_usd_micros": value.nav_micros,
            "valid_until": deadline,
            "evidence": [asdict(ref) for ref in fresh],
        }
        report["risk_preview"] = asdict(preview)
        if (
            daily >= target.max_daily_loss_usd * 1_000_000
            or cumulative >= target.max_cumulative_loss_usd * 1_000_000
        ):
            report["blockers"].append("loss_budget_exhausted")
    except Exception:  # noqa: BLE001 - no raw provider or evidence diagnostics
        report["blockers"].append("fresh_nav_or_continuity_unresolved")
    return report
