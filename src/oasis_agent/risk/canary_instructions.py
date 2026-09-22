"""Strict owner-approved USD limits, kept separate from execution authorization."""

import re
from dataclasses import dataclass

from ..evidence.rpc_validation import strict_json_object
from ..models.common import canonical_json, sha256_hex

_USD = re.compile(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,6})?\Z")


def usd_micros(value: object) -> int:
    """Exact decimal strings only; no float, exponent, implicit rounding or stablecoin peg."""
    if not isinstance(value, str) or _USD.fullmatch(value) is None:
        raise ValueError("USD amount must be a plain decimal string with at most six decimals")
    whole, _, fraction = value.partition(".")
    return int(whole) * 1_000_000 + int(fraction.ljust(6, "0"))


@dataclass(frozen=True, slots=True)
class CanaryOwnerInstructions:
    chain_id: int
    wallet_dedicated_to_project: bool
    maximum_total_capital_usd_micros: int | None
    maximum_daily_loss_usd_micros: int
    maximum_cumulative_loss_usd_micros: int

    def __post_init__(self) -> None:
        if type(self.chain_id) is not int or not 0 < self.chain_id < 2**256:
            raise ValueError("explicit EVM chain required")
        if type(self.wallet_dedicated_to_project) is not bool:
            raise ValueError("wallet purpose must be explicitly boolean")
        for value in (
            self.maximum_daily_loss_usd_micros,
            self.maximum_cumulative_loss_usd_micros,
        ):
            if type(value) is not int or value <= 0:
                raise ValueError("positive integer USD-micro loss limits required")
        capital = self.maximum_total_capital_usd_micros
        if capital is not None and (type(capital) is not int or capital <= 0):
            raise ValueError("total capital must be positive integer USD micros or missing")
        if self.maximum_daily_loss_usd_micros > self.maximum_cumulative_loss_usd_micros:
            raise ValueError("daily loss cannot exceed cumulative loss limit")

    @property
    def blockers(self) -> tuple[str, ...]:
        reasons = []
        if not self.wallet_dedicated_to_project:
            reasons.append("dedicated_wallet_not_confirmed")
        if self.maximum_total_capital_usd_micros is None:
            reasons.append("total_capital_missing")
        return tuple(reasons)

    @property
    def payload(self) -> bytes:
        return canonical_json(
            {
                "schema": "oasis.canary-owner-limits.v1",
                "chain_id": self.chain_id,
                "wallet_dedicated_to_project": self.wallet_dedicated_to_project,
                "valuation_currency": "USD",
                "valuation_scale": 1_000_000,
                "maximum_total_capital": self.maximum_total_capital_usd_micros,
                "maximum_daily_loss": self.maximum_daily_loss_usd_micros,
                "maximum_cumulative_loss": self.maximum_cumulative_loss_usd_micros,
                "configuration_blockers": self.blockers,
                "execution_eligible": False,
            }
        )

    @property
    def digest(self) -> str:
        return sha256_hex(self.payload)


def parse_canary_instructions(payload: bytes) -> CanaryOwnerInstructions:
    data = strict_json_object(payload)
    if set(data) != {
        "schema",
        "chain_id",
        "wallet_dedicated_to_project",
        "valuation_currency",
        "maximum_total_capital_usd",
        "maximum_daily_loss_usd",
        "maximum_cumulative_loss_usd",
        "execution_enabled",
        "status",
    }:
        raise ValueError("owner instructions have missing or unknown fields")
    if (
        data["schema"] != "oasis.canary-owner-instructions.v1"
        or data["valuation_currency"] != "USD"
    ):
        raise ValueError("unsupported owner instruction schema or currency")
    if data["execution_enabled"] is not False:
        raise ValueError("owner instructions cannot enable execution")
    if data["status"] not in {
        "pending_total_capital_and_production_validation",
        "pending_production_validation",
    }:
        raise ValueError("owner instructions cannot claim production validation")
    capital = data["maximum_total_capital_usd"]
    if capital is None and data["status"] != "pending_total_capital_and_production_validation":
        raise ValueError("missing capital must remain explicitly pending")
    chain_id, dedicated = data["chain_id"], data["wallet_dedicated_to_project"]
    if type(chain_id) is not int or type(dedicated) is not bool:
        raise ValueError("strict chain and wallet-purpose types required")
    return CanaryOwnerInstructions(
        chain_id,
        dedicated,
        None if capital is None else usd_micros(capital),
        usd_micros(data["maximum_daily_loss_usd"]),
        usd_micros(data["maximum_cumulative_loss_usd"]),
    )


def capital_limit_breaches(
    limits: CanaryOwnerInstructions,
    *,
    committed_capital_usd_micros: int,
    daily_loss_usd_micros: int,
    cumulative_loss_usd_micros: int,
) -> tuple[str, ...]:
    """Pure threshold check on already reconciled USD inputs, never a trade permit.

    Caller must independently establish account identity, prices, complete costs,
    cash-flow adjustment and the day's boundary. Missing inputs cannot become zero.
    A caller must persist a resulting HALT; this function neither resets nor stores it.
    """
    values = (committed_capital_usd_micros, daily_loss_usd_micros, cumulative_loss_usd_micros)
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("reconciled nonnegative integer USD micros required")
    reasons = list(limits.blockers)
    cap = limits.maximum_total_capital_usd_micros
    if cap is not None and committed_capital_usd_micros > cap:
        reasons.append("total_capital_exceeded")
    if daily_loss_usd_micros >= limits.maximum_daily_loss_usd_micros:
        reasons.append("daily_loss_limit_reached")
    if cumulative_loss_usd_micros >= limits.maximum_cumulative_loss_usd_micros:
        reasons.append("cumulative_loss_limit_reached")
    return tuple(reasons)
