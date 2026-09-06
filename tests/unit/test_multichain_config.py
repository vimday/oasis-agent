from pathlib import Path

import pytest

from oasis_agent.config import load_config
from oasis_agent.errors import ConfigurationError
from oasis_agent.models.chain import ChainConfig


@pytest.mark.parametrize("chain_id", [1, 8453, 56, 4663, 84532, 11155111, 97, 999999])
def test_network_identity_is_explicit_and_chain_neutral(tmp_path: Path, chain_id: int):
    config = load_config(
        tmp_path / "absent.toml",
        {
            "OASIS_CHAIN_ID": str(chain_id),
            "OASIS_RPC_URL": "https://rpc.example",
            "OASIS_POOL_MANAGER": "0x" + "11" * 20,
        },
    )
    assert config.chain.chain_id == chain_id
    assert config.execution.mode.value == "observe"
    assert config.execution.allow_broadcast is False


def test_missing_chain_is_not_silently_robinhood(tmp_path: Path):
    with pytest.raises(ConfigurationError, match="chain_id is required"):
        load_config(
            tmp_path / "absent.toml",
            {
                "OASIS_RPC_URL": "https://rpc.example",
                "OASIS_POOL_MANAGER": "0x" + "11" * 20,
            },
        )


@pytest.mark.parametrize("chain_id", [True, 1.0, 0, -1])
def test_chain_config_rejects_ambiguous_identity(chain_id):
    with pytest.raises(ValueError):
        ChainConfig(chain_id, "https://rpc.example", "0x" + "11" * 20)
