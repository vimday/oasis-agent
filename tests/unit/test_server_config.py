"""Strict TOML parsing for the observation server configuration."""

from pathlib import Path

import pytest

from oasis_agent.errors import ConfigurationError
from oasis_agent.server.config import (
    ServerChainConfig,
    ServerShadowRunnerConfig,
    load_server_config,
)


def write_config(tmp_path, text: str):
    path = tmp_path / "server.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_with_empty_file(tmp_path):
    path = write_config(tmp_path, "")
    config = load_server_config(path)
    assert config.host == "127.0.0.1"
    assert config.port == 8666
    assert config.max_rows_per_page == 100
    assert config.ops_token_env == "OASIS_SERVER_OPS_TOKEN"
    assert config.ops_token_required is True
    assert config.databases.shadow is None
    assert config.shadow_runner is None
    assert config.chains == ()


def test_full_configuration_parses(tmp_path):
    shadow = tmp_path / "shadow.db"
    evidence = tmp_path / "evidence.db"
    readiness = tmp_path / "readiness.json"
    shadow.touch()
    evidence.touch()
    readiness.touch()
    path = write_config(
        tmp_path,
        f"""
host = "0.0.0.0"
port = 9000
max_rows_per_page = 200
ops_token_required = false
alert_feed_max_age_seconds = 120
alert_gate_warn_hours = 2

[databases]
shadow = "{shadow}"
evidence = "{evidence}"
ops = "data/custom_ops.db"
readiness_manifest = "{readiness}"

[shadow_runner]
command = [".venv/bin/python", "scripts/run_lp_shadow.py"]
timeout_seconds = 600
max_concurrent = 2

[[chains]]
chain_id = 46630
name = "Robinhood Testnet"
rpc_url = "https://rpc.testnet.chain.robinhood.com"
pool_manager = "0x{"ab" * 20}"
cache_seconds = 30
""",
    )
    config = load_server_config(path)
    assert config.host == "0.0.0.0"
    assert config.port == 9000
    assert config.max_rows_per_page == 200
    assert config.ops_token_required is False
    assert config.databases.shadow == shadow
    assert config.databases.ops == Path("data/custom_ops.db")
    assert config.shadow_runner == ServerShadowRunnerConfig(
        (".venv/bin/python", "scripts/run_lp_shadow.py"), 600, 2
    )
    assert config.chains == (
        ServerChainConfig(
            chain_id=46630,
            name="Robinhood Testnet",
            rpc_url="https://rpc.testnet.chain.robinhood.com",
            pool_manager="0x" + "ab" * 20,
            cache_seconds=30,
        ),
    )


def test_unknown_root_key_rejected(tmp_path):
    path = write_config(tmp_path, 'bogus = "x"\n')
    with pytest.raises(ConfigurationError, match="unknown server configuration key"):
        load_server_config(path)


def test_secrets_rejected_in_file(tmp_path):
    path = write_config(tmp_path, '[databases]\ntoken = "0xdeadbeef"\n')
    with pytest.raises(ConfigurationError, match="secrets are not accepted"):
        load_server_config(path)


def test_embedded_credentials_in_rpc_url_rejected(tmp_path):
    path = write_config(
        tmp_path,
        "[[chains]]\nchain_id = 1\nname = 'x'\nrpc_url = 'https://user:pass@host.example/rpc'\n",
    )
    with pytest.raises(ConfigurationError, match="embedded credentials"):
        load_server_config(path)


def test_chain_rpc_url_resolves_from_environment(tmp_path):
    path = write_config(
        tmp_path,
        "[[chains]]\nchain_id = 4663\nname = 'Robinhood'\nrpc_url_env = 'ROBINHOOD_RPC_URL'\n",
    )
    config = load_server_config(
        path, environ={"ROBINHOOD_RPC_URL": "https://rpc.mainnet.chain.robinhood.com"}
    )
    assert config.chains[0].rpc_url == "https://rpc.mainnet.chain.robinhood.com"


def test_chain_rpc_url_env_fails_closed_when_ambiguous_or_missing(tmp_path):
    path = write_config(
        tmp_path,
        "[[chains]]\nchain_id = 4663\nname = 'Robinhood'\n"
        "rpc_url = 'https://rpc.example'\nrpc_url_env = 'ROBINHOOD_RPC_URL'\n",
    )
    with pytest.raises(ConfigurationError, match="either rpc_url or rpc_url_env"):
        load_server_config(path, environ={"ROBINHOOD_RPC_URL": "https://rpc.example"})
    path = write_config(
        tmp_path,
        "[[chains]]\nchain_id = 4663\nname = 'Robinhood'\nrpc_url_env = 'ROBINHOOD_RPC_URL'\n",
    )
    with pytest.raises(ConfigurationError, match="environment variable is missing"):
        load_server_config(path, environ={})


def test_missing_journal_path_fails_closed(tmp_path):
    path = write_config(tmp_path, '[databases]\nshadow = "does-not-exist.db"\n')
    with pytest.raises(ConfigurationError, match="does not exist"):
        load_server_config(path)


def test_duplicate_chain_ids_rejected(tmp_path):
    path = write_config(
        tmp_path,
        "[[chains]]\nchain_id = 1\nname = 'a'\n[[chains]]\nchain_id = 1\nname = 'b'\n",
    )
    with pytest.raises(ConfigurationError, match="duplicate chain_id"):
        load_server_config(path)


def test_port_bounds_enforced(tmp_path):
    path = write_config(tmp_path, "port = 70000\n")
    with pytest.raises(ConfigurationError, match=r"within 1..65535"):
        load_server_config(path)


def test_runner_command_must_be_string_list(tmp_path):
    path = write_config(tmp_path, '[shadow_runner]\ncommand = "not-a-list"\n')
    with pytest.raises(ConfigurationError, match="non-empty list of strings"):
        load_server_config(path)


def test_ops_db_falls_back_to_environment(tmp_path):
    path = write_config(tmp_path, "")
    config = load_server_config(path, environ={"OASIS_SERVER_OPS_DB": "/tmp/custom.db"})
    assert str(config.databases.ops) == "/tmp/custom.db"


def test_missing_file_rejected():
    with pytest.raises(ConfigurationError, match="does not exist"):
        load_server_config(Path("/nonexistent/server.toml"))
