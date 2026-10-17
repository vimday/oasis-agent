"""Canonical bounded direct-canary calls shared by preparation and replay."""

from typing import Any

from .config import LiveConfig
from .state import Position
from .v4lp import burn_call, erc20_approve_call, mint_call, permit2_approve_call, swap_call


def action_call(
    config: LiveConfig,
    timestamp: int,
    kind: str,
    intent: dict[str, Any],
    position: Position | None,
) -> tuple[str, bytes, int]:
    keys = {
        "approve_usdc": {"amount"},
        "approve_permit2": {"amount", "spender", "expiration"},
        "swap": {"zero_for_one", "amount_in", "min_out"},
        "mint": {"lower", "upper", "liquidity", "amount0_max", "amount1_max"},
        "burn": {"amount0_min", "amount1_min"},
    }
    if (
        kind not in keys
        or set(intent) != keys[kind]
        or type(timestamp) is not int
        or timestamp <= 0
    ):
        raise ValueError("invalid action schema or timestamp")
    for name, value in intent.items():
        if name in {"spender", "zero_for_one"}:
            continue
        if type(value) is not int:
            raise ValueError("action terms require exact integers")
        if name in {"lower", "upper"}:
            if not -887272 <= value <= 887272 or value % config.pool.tick_spacing:
                raise ValueError("action tick outside canonical aligned range")
        elif not 0 <= value < 2**128:
            raise ValueError("action amount outside bounded uint128")
    deadline = timestamp + 300
    if kind.startswith("approve"):
        if not 0 < intent["amount"] <= config.approval_usdc:
            raise ValueError("approval exceeds configured amount")
        if kind == "approve_usdc":
            return config.pool.currency1, erc20_approve_call(config.permit2, intent["amount"]), 0
        if intent["spender"] not in (config.position_manager, config.universal_router) or (
            intent["expiration"] != timestamp + config.approval_days * 86400
        ):
            raise ValueError("approval spender/expiry differs from policy")
        return (
            config.permit2,
            permit2_approve_call(
                config.pool.currency1, intent["spender"], intent["amount"], intent["expiration"]
            ),
            0,
        )
    if kind == "swap":
        if position is not None or type(intent["zero_for_one"]) is not bool:
            raise ValueError("swap requires no position and an explicit direction")
        if not intent["amount_in"] or not intent["min_out"]:
            raise ValueError("swap requires positive input and minimum output")
        return (
            config.universal_router,
            swap_call(
                config.pool,
                intent["zero_for_one"],
                intent["amount_in"],
                intent["min_out"],
                deadline,
            ),
            (intent["amount_in"] if intent["zero_for_one"] else 0),
        )
    if kind == "mint":
        if position is not None or intent["lower"] >= intent["upper"] or not intent["liquidity"]:
            raise ValueError("invalid mint position")
        if not intent["amount0_max"] or not intent["amount1_max"]:
            raise ValueError("mint requires bounded positive inputs")
        return (
            config.position_manager,
            mint_call(
                config.pool,
                intent["lower"],
                intent["upper"],
                intent["liquidity"],
                intent["amount0_max"],
                intent["amount1_max"],
                config.account,
                deadline,
            ),
            intent["amount0_max"],
        )
    if (
        position is None
        or position.token_id <= 0
        or not (intent["amount0_min"] or intent["amount1_min"])
    ):
        raise ValueError("burn requires an existing position and minimum output")
    return (
        config.position_manager,
        burn_call(
            config.pool,
            position.token_id,
            intent["amount0_min"],
            intent["amount1_min"],
            config.account,
            deadline,
        ),
        0,
    )
