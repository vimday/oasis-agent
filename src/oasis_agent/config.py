from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .errors import ConfigurationError
from .models.chain import ChainConfig
from .models.execution import ExecutionMode
from .models.common import normalize_address


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    mode: ExecutionMode = ExecutionMode.OBSERVE
    max_capital: str = "0"
    max_gas: str = "0"
    allow_broadcast: bool = False


@dataclass(frozen=True, slots=True)
class AppConfig:
    chain: ChainConfig
    execution: ExecutionConfig


def _secret_keys(value: object, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if str(key).lower() in {"private_key", "mnemonic", "secret", "rpc_token"}:
                found.append(path)
            found.extend(_secret_keys(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_secret_keys(child, f"{prefix}[{index}]"))
    return found


def load_config(path: Path, environ: Mapping[str, str] | None = None) -> AppConfig:
    env = os.environ if environ is None else environ
    raw: dict[str, object] = {}
    if path.exists():
        with path.open("rb") as handle:
            loaded = tomllib.load(handle)
        raw = loaded
    secrets = _secret_keys(raw)
    if secrets:
        raise ConfigurationError(f"secrets are not accepted in configuration: {', '.join(secrets)}")
    chain_raw = raw.get("chain", {})
    if not isinstance(chain_raw, dict):
        raise ConfigurationError("[chain] must be a table")
    rpc_url = str(chain_raw.get("rpc_url", env.get("OASIS_RPC_URL", "")))
    chain_id = int(chain_raw.get("chain_id", env.get("OASIS_CHAIN_ID", "4663")))
    pool_manager = str(chain_raw.get("pool_manager", env.get("OASIS_POOL_MANAGER", "")))
    if not rpc_url or not pool_manager:
        raise ConfigurationError("chain rpc_url and pool_manager are required")
    try:
        chain = ChainConfig(chain_id=chain_id, rpc_url=rpc_url, pool_manager=normalize_address(pool_manager))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(str(exc)) from exc
    execution_raw = raw.get("execution", {})
    if not isinstance(execution_raw, dict):
        raise ConfigurationError("[execution] must be a table")
    try:
        mode = ExecutionMode(str(execution_raw.get("mode", "observe")))
    except ValueError as exc:
        raise ConfigurationError("invalid execution mode") from exc
    execution = ExecutionConfig(
        mode=mode,
        max_capital=str(execution_raw.get("max_capital", "0")),
        max_gas=str(execution_raw.get("max_gas", "0")),
        allow_broadcast=bool(execution_raw.get("allow_broadcast", False)),
    )
    if execution.mode is ExecutionMode.EXECUTE and not execution.allow_broadcast:
        raise ConfigurationError("execute mode requires allow_broadcast = true")
    return AppConfig(chain=chain, execution=execution)
