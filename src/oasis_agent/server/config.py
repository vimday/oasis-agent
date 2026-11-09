"""Strict TOML configuration for the observation server.

The parser mirrors the repository's ``config.py`` discipline: unknown keys,
secrets, placeholder endpoints and malformed types are rejected instead of
silently ignored. Operation tokens are never accepted in the file; they are
referenced by environment variable name only, and the variable is read at
request time so it can be rotated without a restart.
"""

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class ServerDatabaseConfig:
    shadow: Path | None = None
    launch: Path | None = None
    evidence: Path | None = None
    execution: Path | None = None
    ops: Path = Path("data/server_ops.db")
    readiness_manifest: Path | None = None
    live_state: Path | None = None
    live_pair_inspection: Path | None = None
    live_bounded_run: Path | None = None
    spot_run: Path | None = None
    # One `capture_spot_cost` bundle (binding.json, evidence.db, optional
    # settlement.json) checked against `spot_run`'s journal; read-only.
    spot_proof: Path | None = None


@dataclass(frozen=True, slots=True)
class ServerShadowRunnerConfig:
    command: tuple[str, ...]
    timeout_seconds: int
    max_concurrent: int


@dataclass(frozen=True, slots=True)
class ServerChainConfig:
    chain_id: int
    name: str
    rpc_url: str = ""
    pool_manager: str = ""
    cache_seconds: int = 15


@dataclass(frozen=True, slots=True)
class ServerCampaignInput:
    name: str
    input: Path
    evidence: Path


@dataclass(frozen=True, slots=True)
class ServerConfig:
    host: str
    port: int
    max_rows_per_page: int
    ops_token_env: str
    ops_token_required: bool
    alert_feed_max_age_seconds: int
    alert_gate_warn_hours: int
    databases: ServerDatabaseConfig
    shadow_runner: ServerShadowRunnerConfig | None
    chains: tuple[ServerChainConfig, ...]
    campaign_inputs: tuple[ServerCampaignInput, ...] = ()
    campaign_rpc_envs: tuple[str, ...] = ("BASE_RPC_PRIMARY", "BASE_RPC_SECONDARY")


def _secret_keys(value: object, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if str(key).lower() in {"private_key", "mnemonic", "secret", "rpc_token", "token"}:
                found.append(path)
            found.extend(_secret_keys(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_secret_keys(child, f"{prefix}[{index}]"))
    return found


def _strict_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{name} must be a TOML boolean")
    return value


def _strict_int(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be within {minimum}..{maximum}")
    return value


def _strict_path(value: object, name: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{name} must be a non-empty path string")
    return Path(value)


def _reject_unknown_keys(
    values: Mapping[str, object], allowed: frozenset[str], section: str
) -> None:
    unknown = sorted(str(key) for key in values if key not in allowed)
    if unknown:
        raise ConfigurationError(f"unknown {section} configuration key(s): {', '.join(unknown)}")


def _reject_credentialed_url(url: str, name: str) -> None:
    # Reject userinfo ("https://user:pass@host") so RPC endpoints cannot smuggle
    # credentials through the configuration file.
    if "//" in url and "@" in url.split("//", 1)[1].split("/", 1)[0]:
        raise ConfigurationError(f"{name} must not contain embedded credentials")


def _validate_existing_path(path: Path | None, name: str) -> None:
    if path is not None and not path.exists():
        raise ConfigurationError(f"{name} does not exist: {path}")


_ROOT_KEYS = frozenset(
    {
        "host",
        "port",
        "max_rows_per_page",
        "ops_token_env",
        "ops_token_required",
        "alert_feed_max_age_seconds",
        "alert_gate_warn_hours",
        "databases",
        "shadow_runner",
        "chains",
        "campaign_inputs",
        "campaign_rpc_envs",
    }
)
_DATABASE_KEYS = frozenset(
    {
        "shadow",
        "launch",
        "evidence",
        "execution",
        "ops",
        "readiness_manifest",
        "live_state",
        "live_pair_inspection",
        "live_bounded_run",
        "spot_run",
        "spot_proof",
    }
)
_RUNNER_KEYS = frozenset({"command", "timeout_seconds", "max_concurrent"})
_CHAIN_KEYS = frozenset(
    {"chain_id", "name", "rpc_url", "rpc_url_env", "pool_manager", "cache_seconds"}
)


def load_server_config(path: Path | None, environ: Mapping[str, str] | None = None) -> ServerConfig:
    env = os.environ if environ is None else environ
    raw: dict[str, Any] = {}
    if path is not None:
        if not path.exists():
            raise ConfigurationError(f"server configuration does not exist: {path}")
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    _reject_unknown_keys(raw, _ROOT_KEYS, "server")
    secrets = _secret_keys(raw)
    if secrets:
        raise ConfigurationError(f"secrets are not accepted in configuration: {', '.join(secrets)}")

    host = raw.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host.strip():
        raise ConfigurationError("host must be a non-empty string")
    port = _strict_int(raw.get("port", 8666), "port", minimum=1, maximum=65535)
    max_rows = _strict_int(
        raw.get("max_rows_per_page", 100), "max_rows_per_page", minimum=1, maximum=1_000
    )
    ops_token_env = raw.get("ops_token_env", "OASIS_SERVER_OPS_TOKEN")
    if not isinstance(ops_token_env, str) or not ops_token_env.strip():
        raise ConfigurationError("ops_token_env must be a non-empty environment variable name")
    ops_token_required = _strict_bool(raw.get("ops_token_required", True), "ops_token_required")
    feed_age = _strict_int(
        raw.get("alert_feed_max_age_seconds", 3600),
        "alert_feed_max_age_seconds",
        minimum=60,
        maximum=7 * 24 * 3600,
    )
    gate_warn = _strict_int(
        raw.get("alert_gate_warn_hours", 24),
        "alert_gate_warn_hours",
        minimum=1,
        maximum=24 * 30,
    )

    databases_raw = raw.get("databases", {})
    if not isinstance(databases_raw, dict):
        raise ConfigurationError("[server.databases] must be a table")
    _reject_unknown_keys(databases_raw, _DATABASE_KEYS, "databases")
    databases = ServerDatabaseConfig(
        shadow=_strict_path(databases_raw.get("shadow"), "databases.shadow"),
        launch=_strict_path(databases_raw.get("launch"), "databases.launch"),
        evidence=_strict_path(databases_raw.get("evidence"), "databases.evidence"),
        execution=_strict_path(databases_raw.get("execution"), "databases.execution"),
        ops=Path(
            str(databases_raw.get("ops") or env.get("OASIS_SERVER_OPS_DB") or "data/server_ops.db")
        ),
        readiness_manifest=_strict_path(
            databases_raw.get("readiness_manifest"), "databases.readiness_manifest"
        ),
        live_state=_strict_path(databases_raw.get("live_state"), "databases.live_state"),
        live_pair_inspection=_strict_path(
            databases_raw.get("live_pair_inspection"), "databases.live_pair_inspection"
        ),
        live_bounded_run=_strict_path(
            databases_raw.get("live_bounded_run"), "databases.live_bounded_run"
        ),
        spot_run=_strict_path(databases_raw.get("spot_run"), "databases.spot_run"),
        spot_proof=_strict_path(databases_raw.get("spot_proof"), "databases.spot_proof"),
    )
    if databases.spot_proof is not None and databases.spot_run is None:
        # A proof bundle is checked against one journal; without it there is nothing to bind.
        raise ConfigurationError("databases.spot_proof requires databases.spot_run")
    for name, value in (
        ("databases.shadow", databases.shadow),
        ("databases.launch", databases.launch),
        ("databases.evidence", databases.evidence),
        ("databases.execution", databases.execution),
        ("databases.readiness_manifest", databases.readiness_manifest),
    ):
        _validate_existing_path(value, name)

    runner: ServerShadowRunnerConfig | None = None
    runner_raw = raw.get("shadow_runner")
    if runner_raw is not None:
        if not isinstance(runner_raw, dict):
            raise ConfigurationError("[server.shadow_runner] must be a table")
        _reject_unknown_keys(runner_raw, _RUNNER_KEYS, "shadow_runner")
        command_raw = runner_raw.get("command")
        if (
            not isinstance(command_raw, list)
            or not command_raw
            or not all(isinstance(item, str) and item.strip() for item in command_raw)
        ):
            raise ConfigurationError("shadow_runner.command must be a non-empty list of strings")
        command = tuple(str(item) for item in command_raw)
        runner = ServerShadowRunnerConfig(
            command=command,
            timeout_seconds=_strict_int(
                runner_raw.get("timeout_seconds", 900),
                "shadow_runner.timeout_seconds",
                minimum=10,
                maximum=24 * 3600,
            ),
            max_concurrent=_strict_int(
                runner_raw.get("max_concurrent", 1),
                "shadow_runner.max_concurrent",
                minimum=1,
                maximum=4,
            ),
        )

    chains_raw = raw.get("chains", [])
    if not isinstance(chains_raw, list):
        raise ConfigurationError("chains must be a list of tables")
    chains: list[ServerChainConfig] = []
    for index, item in enumerate(chains_raw):
        if not isinstance(item, dict):
            raise ConfigurationError(f"chains[{index}] must be a table")
        _reject_unknown_keys(item, _CHAIN_KEYS, f"chains[{index}]")
        chain_id = _strict_int(
            item.get("chain_id"), f"chains[{index}].chain_id", minimum=1, maximum=(1 << 256) - 1
        )
        chain_name = item.get("name")
        if not isinstance(chain_name, str) or not chain_name.strip():
            raise ConfigurationError(f"chains[{index}].name must be a non-empty string")
        rpc_url_value = item.get("rpc_url")
        rpc_url_env = item.get("rpc_url_env")
        if rpc_url_value is not None and rpc_url_env is not None:
            raise ConfigurationError(
                f"chains[{index}] must use either rpc_url or rpc_url_env, not both"
            )
        if rpc_url_env is not None:
            if not isinstance(rpc_url_env, str) or not re.fullmatch(
                r"[A-Z][A-Z0-9_]{0,127}", rpc_url_env
            ):
                raise ConfigurationError(
                    f"chains[{index}].rpc_url_env must name an environment variable"
                )
            rpc_url = env.get(rpc_url_env, "")
            if not rpc_url:
                raise ConfigurationError(f"RPC URL environment variable is missing: {rpc_url_env}")
        else:
            rpc_url = "" if rpc_url_value is None else rpc_url_value
        if not isinstance(rpc_url, str):
            raise ConfigurationError(f"chains[{index}].rpc_url must be a string")
        if rpc_url:
            _reject_credentialed_url(rpc_url, f"chains[{index}].rpc_url")
        pool_manager = item.get("pool_manager", "")
        if not isinstance(pool_manager, str):
            raise ConfigurationError(f"chains[{index}].pool_manager must be a string")
        chains.append(
            ServerChainConfig(
                chain_id=chain_id,
                name=chain_name,
                rpc_url=rpc_url,
                pool_manager=pool_manager,
                cache_seconds=_strict_int(
                    item.get("cache_seconds", 15),
                    f"chains[{index}].cache_seconds",
                    minimum=1,
                    maximum=3600,
                ),
            )
        )
    if len({chain.chain_id for chain in chains}) != len(chains):
        raise ConfigurationError("duplicate chain_id in chains")

    inputs_raw = raw.get("campaign_inputs", [])
    if not isinstance(inputs_raw, list):
        raise ConfigurationError("campaign_inputs must be a list")
    inputs: list[ServerCampaignInput] = []
    for item in inputs_raw:
        if not isinstance(item, dict) or set(item) != {"name", "input", "evidence"}:
            raise ConfigurationError("campaign input requires name, input and evidence")
        name = item["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
            raise ConfigurationError("invalid campaign input name")
        paths = []
        for field in ("input", "evidence"):
            path_value = _strict_path(item[field], field)
            if path_value is None or not path_value.is_file():
                raise ConfigurationError("campaign input files must exist")
            paths.append(path_value.resolve())
        inputs.append(ServerCampaignInput(name, paths[0], paths[1]))
    if len({item.name for item in inputs}) != len(inputs):
        raise ConfigurationError("duplicate campaign input name")
    rpc_envs = raw.get("campaign_rpc_envs", ["BASE_RPC_PRIMARY", "BASE_RPC_SECONDARY"])
    if (
        not isinstance(rpc_envs, list)
        or any(
            not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", name)
            for name in rpc_envs
        )
        or len(set(rpc_envs)) != len(rpc_envs)
    ):
        raise ConfigurationError("unique campaign RPC environment names required")

    return ServerConfig(
        host=host,
        port=port,
        max_rows_per_page=max_rows,
        ops_token_env=ops_token_env,
        ops_token_required=ops_token_required,
        alert_feed_max_age_seconds=feed_age,
        alert_gate_warn_hours=gate_warn,
        databases=databases,
        shadow_runner=runner,
        chains=tuple(chains),
        campaign_inputs=tuple(inputs),
        campaign_rpc_envs=tuple(rpc_envs),
    )
