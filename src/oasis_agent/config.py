from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .models.chain import ChainConfig
from .models.common import normalize_address
from .models.execution import ExecutionMode


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    mode: ExecutionMode = ExecutionMode.OBSERVE
    max_capital: Decimal = Decimal(0)
    max_gas: Decimal = Decimal(0)
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


def _strict_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{name} must be a TOML boolean")
    return value


def _nonnegative_decimal(value: object, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ConfigurationError(f"{name} must be a decimal number") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ConfigurationError(f"{name} must be finite and nonnegative")
    return parsed


_ROOT_KEYS = frozenset({"chain", "execution", "chain_id", "rpc_url", "rpc_url_env", "pool_manager"})
_CHAIN_KEYS = frozenset({"chain_id", "rpc_url", "rpc_url_env", "pool_manager"})
_EXECUTION_KEYS = frozenset({"mode", "max_capital", "max_gas", "allow_broadcast"})


def _reject_unknown_keys(
    values: Mapping[str, object], allowed: frozenset[str], section: str
) -> None:
    unknown = sorted(str(key) for key in values if key not in allowed)
    if unknown:
        raise ConfigurationError(f"unknown {section} configuration key(s): {', '.join(unknown)}")


def _reject_placeholder_endpoint(url: str, environ: Mapping[str, str]) -> None:
    if "example.invalid" in url and environ.get("OASIS_ALLOW_FIXTURE_ENDPOINT") != "1":
        raise ConfigurationError("placeholder RPC endpoint requires explicit fixture injection")


def load_config(path: Path, environ: Mapping[str, str] | None = None) -> AppConfig:
    env = os.environ if environ is None else environ
    raw: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    _reject_unknown_keys(raw, _ROOT_KEYS, "root")
    secrets = _secret_keys(raw)
    if secrets:
        raise ConfigurationError(f"secrets are not accepted in configuration: {', '.join(secrets)}")

    nested_chain = raw.get("chain")
    if nested_chain is not None and not isinstance(nested_chain, dict):
        raise ConfigurationError("[chain] must be a table")
    flat_chain_keys = _CHAIN_KEYS.intersection(raw)
    if nested_chain is not None and flat_chain_keys:
        raise ConfigurationError("use either flat chain keys or [chain], not both")
    chain_raw = (
        nested_chain
        if isinstance(nested_chain, dict)
        else {
            key: raw[key]
            for key in ("chain_id", "rpc_url", "rpc_url_env", "pool_manager")
            if key in raw
        }
    )
    _reject_unknown_keys(chain_raw, _CHAIN_KEYS, "chain")
    rpc_url_value = chain_raw.get("rpc_url")
    rpc_url_env = chain_raw.get("rpc_url_env")
    if rpc_url_value is not None and rpc_url_env is not None:
        raise ConfigurationError("use either rpc_url or rpc_url_env, not both")
    if rpc_url_env is not None:
        if not isinstance(rpc_url_env, str) or not re.fullmatch(
            r"[A-Z][A-Z0-9_]{0,127}", rpc_url_env
        ):
            raise ConfigurationError("rpc_url_env must name an environment variable")
        rpc_url = env.get(rpc_url_env, "")
        if not rpc_url:
            raise ConfigurationError(f"RPC URL environment variable is missing: {rpc_url_env}")
    else:
        rpc_url = rpc_url_value if rpc_url_value is not None else env.get("OASIS_RPC_URL", "")
    pool_manager = chain_raw.get("pool_manager", env.get("OASIS_POOL_MANAGER", ""))
    if not isinstance(rpc_url, str):
        raise ConfigurationError("rpc_url must be a string")
    if not isinstance(pool_manager, str):
        raise ConfigurationError("pool_manager must be a string")
    chain_id = chain_raw.get("chain_id", env.get("OASIS_CHAIN_ID"))
    if not rpc_url or not pool_manager:
        raise ConfigurationError("chain rpc_url and pool_manager are required")
    _reject_placeholder_endpoint(rpc_url, env)
    if chain_id is None:
        raise ConfigurationError("chain_id is required; no implicit network default")
    try:
        chain = ChainConfig(int(str(chain_id)), rpc_url, normalize_address(pool_manager))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(str(exc)) from exc

    execution_raw = raw.get("execution", {})
    if not isinstance(execution_raw, dict):
        raise ConfigurationError("[execution] must be a table")
    _reject_unknown_keys(execution_raw, _EXECUTION_KEYS, "execution")
    try:
        mode = ExecutionMode(str(execution_raw.get("mode", "observe")))
    except ValueError as exc:
        raise ConfigurationError("invalid execution mode") from exc
    execution = ExecutionConfig(
        mode=mode,
        max_capital=_nonnegative_decimal(execution_raw.get("max_capital", "0"), "max_capital"),
        max_gas=_nonnegative_decimal(execution_raw.get("max_gas", "0"), "max_gas"),
        allow_broadcast=_strict_bool(
            execution_raw.get("allow_broadcast", False), "allow_broadcast"
        ),
    )
    if execution.mode is ExecutionMode.EXECUTE and not execution.allow_broadcast:
        raise ConfigurationError("execute mode requires allow_broadcast = true")
    return AppConfig(chain=chain, execution=execution)
