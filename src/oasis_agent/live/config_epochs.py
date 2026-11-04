"""Exact configuration recognition for retained proof replay, never admission.

Adding the three gas-oracle pins must not relabel old cash or NAV proofs. Only
the same policy's five-pin shape and the already supported USDC 90000-second
historical variant may differ. Returned historical objects are not valid live
policies and must never be used to bind a journal, sign, start or acquire data.
"""

from dataclasses import asdict, replace

from ..evidence.rpc_validation import strict_digest
from ..models.common import canonical_json, sha256_hex
from .config import LiveConfig
from .gas_oracle import EXECUTION_PIN_NAMES, LEGACY_PIN_NAMES


def retained_configurations(target: LiveConfig) -> dict[str, LiveConfig]:
    """Recognize exact hashes while retaining each proof's original policy bytes.

    The target must itself satisfy the current live bounds. Historical USDC
    variants intentionally remain invalid under LiveConfig.validate(); this is
    the same read-only compatibility the original feed migration already used.
    No account, old runtime pin, risk, gas, pool or execution bound may differ.
    """
    target.validate()
    candidates = [target, replace(target, usdc_oracle_max_age_seconds=90_000)]
    if set(target.expected_pins) == EXECUTION_PIN_NAMES:
        legacy = replace(
            target,
            expected_pins={k: v for k, v in target.expected_pins.items() if k in LEGACY_PIN_NAMES},
        )
        candidates.extend([legacy, replace(legacy, usdc_oracle_max_age_seconds=90_000)])
    result: dict[str, LiveConfig] = {}
    for config in candidates:
        # Preserve the target object's identity for callers that distinguish
        # current-policy recovery from a historical checkpoint review.
        result.setdefault(sha256_hex(canonical_json(asdict(config))), config)
    return result


def retained_config(target: LiveConfig, binding: str) -> LiveConfig:
    """Resolve one original digest; never return a guessed or relaxed fallback."""
    strict_digest(binding, "retained configuration hash")
    configurations = retained_configurations(target)
    if binding not in configurations:
        raise ValueError("unsupported retained configuration epoch")
    return configurations[binding]
