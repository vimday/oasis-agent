"""Local start-configuration and RPC profile declarations only; no Docker, state, key or RPC access.

Complete configured pins are not observed runtime approval. All live prerequisites
remain unverified, even when these static release/configuration guards pass.
"""

import os
import stat
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..live.config import LiveConfig
from ..live.gas_oracle import LEGACY_PIN_NAMES, require_gas_oracle_pins
from ..live.rpc_profile import LiveRpcProfile
from ..models.common import canonical_json, sha256_hex
from .live_release import LiveRelease, live_release

DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "configs/live.base-eth-usdc.json"
RPC_PROFILE_NAME = "live.rpc.canary.json"
MAX_CONFIG_BYTES = 65_536
RuntimeRole = Literal[
    "gas_oracle_proxy", "gas_oracle_implementation", "gas_oracle_implementation_code"
]
GAS_ROLES: tuple[RuntimeRole, ...] = (
    "gas_oracle_proxy",
    "gas_oracle_implementation",
    "gas_oracle_implementation_code",
)
UnverifiedCheck = Literal[
    "runtime_image_identity",
    "original_service_state",
    "stop_controls",
    "fresh_usd_nav",
    "dual_source_runtime_simulation",
    "receipt_fund_recovery",
    "state_configuration_binding",
]
UNVERIFIED: tuple[UnverifiedCheck, ...] = (
    "runtime_image_identity",
    "original_service_state",
    "stop_controls",
    "fresh_usd_nav",
    "dual_source_runtime_simulation",
    "receipt_fund_recovery",
    "state_configuration_binding",
)
StartupBlocker = Literal[
    "release_gate_closed",
    "live_configuration_unconfigured",
    "live_configuration_missing",
    "live_configuration_invalid",
    "gas_oracle_runtime_pins_missing",
    "live_rpc_profile_missing",
    "live_rpc_profile_invalid",
]


class LiveStartupReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_: Literal["oasis.live-startup-readiness.v1"] = Field(
        default="oasis.live-startup-readiness.v1", alias="schema"
    )
    source: Literal["local_start_configuration"] = "local_start_configuration"
    execution_eligible: Literal[False] = False
    startup_prerequisites_passed: Literal[False] = False
    evidence_replayed: Literal[False] = False
    rpc_requests: Literal[0] = 0
    release_gate_passed: bool
    local_guards_passed: bool = False
    configuration_status: Literal[
        "not_checked",
        "unconfigured",
        "missing",
        "invalid",
        "legacy_runtime_policy",
        "declared_execution_policy",
    ]
    configuration_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    configured_runtime_roles: Literal[5, 8] | None = None
    missing_runtime_roles: list[RuntimeRole] = []
    blockers: list[StartupBlocker]
    unverified_checks: list[UnverifiedCheck] = Field(default_factory=lambda: list(UNVERIFIED))


def _read(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_CONFIG_BYTES:
            raise ValueError("bounded regular local configuration required")
        raw = handle.read(MAX_CONFIG_BYTES + 1)
        after = os.fstat(handle.fileno())
        if len(raw) > MAX_CONFIG_BYTES or any(
            getattr(before, field) != getattr(after, field)
            for field in ("st_size", "st_mtime_ns", "st_ctime_ns")
        ):
            raise ValueError("local configuration changed during read")
        return raw


def read_local_rpc_profile(path: Path) -> LiveRpcProfile:
    return LiveRpcProfile.from_json(_read(path))


def read_local_startup_readiness(
    path: Path | None = DEFAULT_CONFIG, *, release: LiveRelease | None = None
) -> LiveStartupReadiness:
    policy = live_release() if release is None else release
    authorized = policy.acceptance_permit is not None or (
        policy.new_actions_allowed and not policy.blockers
    )
    if not authorized:
        return LiveStartupReadiness(
            release_gate_passed=False,
            configuration_status="not_checked",
            blockers=["release_gate_closed"],
        )
    if path is None:
        return LiveStartupReadiness(
            release_gate_passed=True,
            configuration_status="unconfigured",
            blockers=["live_configuration_unconfigured"],
        )
    try:
        config = LiveConfig.from_json(_read(path))
    except FileNotFoundError:
        return LiveStartupReadiness(
            release_gate_passed=True,
            configuration_status="missing",
            blockers=["live_configuration_missing"],
        )
    except Exception:  # noqa: BLE001 - no parser contents, file paths or credentials in output
        return LiveStartupReadiness(
            release_gate_passed=True,
            configuration_status="invalid",
            blockers=["live_configuration_invalid"],
        )
    digest = sha256_hex(canonical_json(asdict(config)))
    if set(config.expected_pins) == LEGACY_PIN_NAMES:
        return LiveStartupReadiness(
            release_gate_passed=True,
            configuration_status="legacy_runtime_policy",
            configuration_hash=digest,
            configured_runtime_roles=5,
            missing_runtime_roles=list(GAS_ROLES),
            blockers=["gas_oracle_runtime_pins_missing"],
        )
    require_gas_oracle_pins(config.expected_pins)
    blockers: list[StartupBlocker] = []
    try:
        read_local_rpc_profile(path.with_name(RPC_PROFILE_NAME))
    except FileNotFoundError:
        blockers.append("live_rpc_profile_missing")
    except (OSError, ValueError, RecursionError):
        blockers.append("live_rpc_profile_invalid")
    return LiveStartupReadiness(
        release_gate_passed=True,
        local_guards_passed=not blockers,
        configuration_status="declared_execution_policy",
        configuration_hash=digest,
        configured_runtime_roles=8,
        blockers=blockers,
    )
