"""Explicit Base fee-oracle proxy/implementation approval, with no I/O or keys."""

import re
from collections.abc import Mapping

from eth_hash.auto import keccak

from ..models.common import normalize_hash32

ORACLE = "0x420000000000000000000000000000000000000f"
# OP Proxy reads Constants.PROXY_IMPLEMENTATION_ADDRESS (EIP-1967).
IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
LEGACY_PIN_NAMES = frozenset(
    {"state_view", "position_manager", "universal_router", "permit2", "usdc_implementation"}
)
GAS_PIN_NAMES = frozenset(
    {"gas_oracle_proxy", "gas_oracle_implementation", "gas_oracle_implementation_code"}
)
EXECUTION_PIN_NAMES = LEGACY_PIN_NAMES | GAS_PIN_NAMES


def implementation_address(word: object) -> str:
    if (
        not isinstance(word, str)
        or re.fullmatch(r"0x0{24}[0-9a-f]{40}", word) is None
        or int(word, 16) == 0
    ):
        raise ValueError("gas oracle implementation slot must name one canonical nonzero address")
    return "0x" + word[-40:]


def require_gas_oracle_pins(pins: Mapping[str, str]) -> str:
    """Legacy configurations remain replayable, but cannot approve new signatures."""
    if set(pins) != EXECUTION_PIN_NAMES:
        raise ValueError("gas oracle runtime pins unresolved")
    implementation = implementation_address(pins["gas_oracle_implementation"])
    for name in ("gas_oracle_proxy", "gas_oracle_implementation_code"):
        value = pins[name]
        if (
            normalize_hash32(value) != value
            or value == "0x" + "00" * 32
            or value == "0x" + keccak(b"").hex()
        ):
            raise ValueError("gas oracle runtime pins must approve nonempty code")
    return implementation


def runtime_hash(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"0x(?:[0-9a-fA-F]{2})+", value) is None:
        raise ValueError("gas oracle runtime must be nonempty byte data")
    return "0x" + keccak(bytes.fromhex(value[2:])).hex()


def observed_gas_oracle_pins(proxy: object, word: object, implementation: object) -> dict[str, str]:
    implementation_address(word)
    assert isinstance(word, str)
    return {
        "gas_oracle_proxy": runtime_hash(proxy),
        "gas_oracle_implementation": word,
        "gas_oracle_implementation_code": runtime_hash(implementation),
    }


def validate_gas_oracle_proof(
    pins: Mapping[str, str], proxy: object, word: object, implementation: object
) -> dict[str, str]:
    require_gas_oracle_pins(pins)
    observed = observed_gas_oracle_pins(proxy, word, implementation)
    if any(pins[name] != value for name, value in observed.items()):
        raise ValueError("gas oracle runtime differs from approved pins")
    return observed
