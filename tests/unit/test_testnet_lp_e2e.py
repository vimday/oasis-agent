"""The isolated exerciser cannot sign a mainnet or value-transfer request."""

import pytest

from scripts.testnet_lp_e2e import TESTNET, read_test_key, retained_budget, validate_transaction


def transaction(**changes):
    return {
        "chainId": TESTNET,
        "value": 0,
        "gas": 100_000,
        "gasPrice": 10_000_000,
        "nonce": 3,
        **changes,
    }


@pytest.mark.parametrize("chain", [1, 4663, 8453, 0])
def test_mainnet_and_unknown_chains_are_rejected(chain):
    with pytest.raises(ValueError, match="network"):
        validate_transaction(transaction(chainId=chain), chain, 10**15)


@pytest.mark.parametrize(
    "changes", [{"value": 1}, {"gas": 12_000_001}, {"gasPrice": 5_000_000_001}, {"nonce": True}]
)
def test_value_transfers_and_unbounded_gas_are_rejected(changes):
    with pytest.raises(ValueError):
        validate_transaction(transaction(**changes), TESTNET, 10**15)


def test_total_budget_is_reserved_before_signing():
    assert validate_transaction(transaction(), TESTNET, 10**12) == 10**12
    with pytest.raises(ValueError, match="budget"):
        validate_transaction(transaction(), TESTNET, 10**12 - 1)


def test_dotenv_is_not_executed_or_interpolated(tmp_path):
    path = tmp_path / "input"
    path.write_text("PRIVATE_KEY=$(touch /tmp/should-not-exist)\n")
    with pytest.raises(ValueError, match="format"):
        read_test_key(path)


def test_interrupted_budget_append_cannot_reset_spending():
    assert retained_budget("", 100) == 0
    assert retained_budget('{"reserved_wei":40}\n{"reserved_wei":60}\n', 100) == 60
    for raw in (
        '{"reserved_wei":40}\n{',
        '{"reserved_wei":40}\n{"reserved_wei":20}\n',
        '{"reserved_wei":true}\n',
    ):
        with pytest.raises(ValueError):
            retained_budget(raw, 100)
