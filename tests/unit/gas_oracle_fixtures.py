"""Synthetic fee-oracle pins, never production configuration or observed proof."""

from eth_hash.auto import keccak


def synthetic_gas_pins():
    digest = "0x" + keccak(bytes.fromhex("6000")).hex()
    return {
        "gas_oracle_proxy": digest,
        "gas_oracle_implementation": "0x" + "00" * 12 + "ab" * 20,
        "gas_oracle_implementation_code": digest,
    }
