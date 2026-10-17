"""Read-only operational precheck for the bounded Base canary.

Passing these checks neither authorizes execution nor proves economic acceptance.
The caller supplies read-only RPCs; no signer, state save or broadcast is used.
"""

from dataclasses import asdict
from pathlib import Path

from ..reporting.live_release import live_release
from .chain import ChainError, Rpc
from .config import LiveConfig
from .intent import pending_plan_hash
from .receipts import RetainedReceiptCostReader
from .runner import LiveLoop
from .state import StateStore


def check_live(
    config: LiveConfig,
    store: StateStore,
    primary: Rpc,
    secondary: Rpc,
    *,
    stop_file: Path,
    receipt_costs: RetainedReceiptCostReader | None = None,
) -> dict[str, object]:
    """Observe the current account and report redacted operational blockers."""
    config.validate()
    loop = LiveLoop.recovery(config, primary, store, "live")
    loop.secondary = secondary
    loop.receipt_costs = receipt_costs
    state = loop.state
    blockers: list[str] = []
    if stop_file.exists():
        blockers.append("stop_file_present")
    if state.halted:
        blockers.append("state_halted")
    if state.signing_attempt is not None:
        blockers.append("signing_outcome_unresolved")
    if state.legacy_gas_cost_incomplete:
        blockers.append("historical_native_cost_incomplete")
    if state.pending:
        blockers.append("pending_requires_reconciliation")
        p = state.pending
        if p.transaction is None:
            blockers.append("pending_signed_commitment_missing")
        elif p.plan_hash != pending_plan_hash(
            asdict(config),
            p.kind,
            p.intent,
            p.transaction,
            asdict(state.position) if state.position else None,
            tuple(asdict(r) for r in p.wallet_evidence),
        ):
            blockers.append("pending_plan_changed")
    report: dict[str, object] = {
        "release": live_release().model_dump(),
        "schema": "oasis.live-operational-precheck.v1",
        "chain_id": config.chain_id,
        "account": config.account,
        "pool_id": config.pool_id,
        "execution_eligible": False,
        "signer_contacted": False,
        "profitability": "unresolved",
        "limitations": [
            "snapshot_does_not_authorize_a_transaction",
            "requires_complete_retained_owner_cash_for_every_action",
            "requires_separately_provisioned_transaction_authority",
            "no_sustained_live_profitability_result",
        ],
    }
    try:
        loop._check_settlements()
        loop._check_nav_checkpoint()
        market = loop.observe()
        report["observation"] = {
            "block": market.block,
            "block_hash": market.block_hash,
            "timestamp": market.timestamp,
            "native_units": market.eth,
            "usdc_units": market.usdc,
            "position_liquidity": market.position_liquidity,
            "nav_usd": loop.nav_usd(market),
            "pool_oracle_deviation_bps": float(market.deviation_bps),
            "nav_micros": market.valuation.nav_micros if market.valuation else None,
            "nav_evidence": [asdict(r) for r in market.nav_evidence],
        }
        if market.eth < config.gas_reserve_wei:
            blockers.append("native_gas_reserve_insufficient")
        if market.deviation_bps > config.max_oracle_deviation_bps:
            blockers.append("pool_oracle_deviation")
        if state.position and market.position_liquidity != state.position.liquidity:
            blockers.append("position_liquidity_changed")
        if state.last_units is not None and market.units != state.last_units and not state.pending:
            blockers.append("wallet_units_changed_since_checkpoint")
        if not loop._pins_ok(market):
            blockers.append("runtime_pins_changed")
        if (state.exact_risk is not None and state.exact_risk.basis is not None) or (
            state.initial_nav_usd is not None and state.checkpoint_nav_usd is not None
        ):
            if market.valuation is None:
                raise ValueError("exact NAV missing")
            daily_micros, cumulative_micros = state.integer_risk().losses(
                market.valuation.nav_micros
            )
            daily, cumulative = daily_micros / 1_000_000, cumulative_micros / 1_000_000
            report["loss_usd"] = {"daily": daily, "cumulative": cumulative}
            if (
                daily_micros >= config.max_daily_loss_usd * 1_000_000
                or cumulative_micros >= config.max_cumulative_loss_usd * 1_000_000
            ):
                blockers.append("loss_budget_exhausted")
        nonces = tuple(
            (rpc.nonce(config.account, "latest"), rpc.nonce(config.account, "pending"))
            for rpc in (primary, secondary)
        )
        if len({n for pair in nonces for n in pair}) != 1:
            blockers.append("account_nonce_unresolved")
    except ChainError as exc:
        blockers.append("chain_observation_unresolved")
        if (
            str(exc).startswith("account NAV refused: ")
            or str(exc) == "retained NAV expired during acquisition"
        ):
            report["observation_failure"] = str(exc)
    except Exception:  # noqa: BLE001 - RPC URLs/errors may contain credentials
        blockers.append("chain_observation_unresolved")
    report["operational_checks_passed"] = not blockers
    report["blockers"] = blockers
    return report
