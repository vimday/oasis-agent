"""Shared read-only recovery checkpoint verification; no acquisition or state writes."""

from copy import deepcopy

from oasis_agent.live.config_epochs import retained_config
from oasis_agent.live.feed_transition import _no_acquisition
from oasis_agent.live.receipts import RetainedReceiptCostReader
from oasis_agent.live.runner import LiveLoop
from oasis_agent.live.startup_state import SOURCES, _position_identity
from oasis_agent.live.state import day_of, validate_state
from oasis_agent.live.valuation import nav_fresh_until, replay_nav, replay_nav_quorum
from oasis_agent.live.wallet import require_wallet_continuity


def review_checkpoint(config, state, binding, evidence, *, fresh=(), now):
    """Replay original policy bytes, including the exact prior five-pin shape."""
    previous = retained_config(config, binding)
    validate_state(state)
    risk = state.exact_risk
    if (
        state.mode != "live"
        or (previous is config and not state.halted)
        or state.pending is not None
        or state.signing_attempt is not None
        or state.legacy_gas_cost_incomplete
        or state.acknowledge_flow
        or risk is None
        or risk.basis is None
        or risk.last_nav is None
        or state.last_units is None
        or risk.day != day_of(risk.last_at)
        or not 0 < risk.checkpoint_at <= risk.last_at
    ):
        raise ValueError("complete halted integer baseline required")
    if any(
        value >= limit * 1_000_000
        for value, limit in zip(
            risk.losses(risk.last_nav),
            (config.max_daily_loss_usd, config.max_cumulative_loss_usd),
            strict=True,
        )
    ):
        raise ValueError("existing loss latch cannot be recovered by capture")
    loop = object.__new__(LiveLoop)
    loop.mode, loop.c, loop.state = "live", previous, deepcopy(state)
    loop.receipt_costs = RetainedReceiptCostReader(
        evidence,
        chain_id=previous.chain_id,
        account=previous.account,
        native_decimals=previous.decimals0,
        sources=SOURCES,
        sessions=_no_acquisition,
    )
    anchor = loop._check_nav_checkpoint()
    if anchor is None:
        raise ValueError("verified original wallet anchor required")
    if state.nav_evidence:
        prior = replay_nav_quorum(evidence, state.nav_evidence, previous, sources=SOURCES)
        prior_position = replay_nav(evidence, state.nav_evidence[0], previous).position
        if (
            _position_identity(prior_position) != _position_identity(state.position)
            or [
                prior.wallet.native,
                prior.wallet.token,
                prior_position.liquidity if prior_position else 0,
            ]
            != state.last_units
        ):
            raise ValueError("historical holdings differ from checkpoint")
    if not fresh:
        return
    value = replay_nav_quorum(evidence, fresh, config, sources=SOURCES)
    try:
        require_wallet_continuity(anchor, value.wallet, allow_same_block=True)
    except ValueError:
        raise ValueError("fresh NAV does not reconcile original inventory or nonce") from None
    position = replay_nav(evidence, fresh[0], config).position
    block = value.wallet.header.ref
    if (
        block.timestamp is None
        or not risk.last_at
        < block.timestamp
        <= now
        <= nav_fresh_until(evidence, fresh, config, sources=SOURCES)
        or _position_identity(position) != _position_identity(state.position)
        or [value.wallet.native, value.wallet.token, position.liquidity if position else 0]
        != state.last_units
        or any(
            a["number"] > block.number
            or (a["number"] == block.number and a["hash"] != block.block_hash)
            for a in state.settled_blocks
        )
    ):
        raise ValueError("fresh NAV does not reconcile original inventory or nonce")
    losses = deepcopy(risk).observe(
        day=day_of(block.timestamp), at=block.timestamp, nav=value.nav_micros, checkpoint=True
    )
    if any(
        value >= limit * 1_000_000
        for value, limit in zip(
            losses, (config.max_daily_loss_usd, config.max_cumulative_loss_usd), strict=True
        )
    ):
        raise ValueError("fresh loss budget exhausted")
