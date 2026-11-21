"""Profit-strategy daemon and fail-closed Unix socket bridge to the isolated authority.

Connects the keyless `profit-strategy` service to `profit_authority.sock` via
`ProfitAuthoritySocketClient`, translates authority operational/ledger state and
`CapitalRiskConfig` into synchronized `AccountCapitalSnapshot` + `HeadroomAllocation`,
and gates dual-simulated intent submission and settlement reconciliation.

This module never imports private keys, `eth_account`, signing callbacks, or `.env`
secrets. In `shadow` mode, it only reads `status()` + computes headroom and emits
`execution_eligible=False` and `release_permitted=False`.
"""

import json
import math
import os
import socket
import stat
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from eth_abi.abi import decode, encode
from eth_hash.auto import keccak

from ..errors import RpcError
from ..evidence.rpc_validation import (
    rpc_quantity,
    strict_json_object,
    strict_object,
    strict_string,
)
from ..models.common import canonical_json, normalize_address, normalize_hash32, sha256_hex
from ..reporting.live_release import require_live_release
from ..risk.capital_policy import (
    USD_MICROS_SCALE,
    AccountCapitalSnapshot,
    CapitalRiskConfig,
    HeadroomAllocation,
    compute_deployable_headroom,
    load_capital_risk_config,
)
from ..risk.revert_budget import RevertBudgetStatus
from ..sniping.venues import USDC, WETH
from .atomic_authority import (
    AtomicExecutionPolicy,
    DualSimulationProof,
    EnvelopeProposal,
    admit_atomic_or_intent_proposal,
    verify_signed_bytes_match_admission,
)
from .atomic_authority_socket import (
    MAX_PROFIT_FRAME,
    PROFIT_AUTHORITY_SCHEMA,
    AtomicOrIntentProposal,
    ProfitAuthoritySocketClient,
    envelope_fields_from_proposal,
    load_atomic_execution_policy,
    read_frame,
    strict_json_loads,
    validate_socket_endpoint,
)
from .canary_readiness import verify_profit_engine_readiness_report
from .dual_rpc_probe import (
    DEFAULT_BLOCK_LAG,
    DEFAULT_PRIMARY_PROVIDER,
    DEFAULT_PRIMARY_URL,
    DEFAULT_SECONDARY_PROVIDER,
    DEFAULT_SECONDARY_URL,
    DEFAULT_TIMEOUT_S,
    MAX_RESPONSE_BYTES,
    DualRpcQuorumError,
    _default_https_transport,
    _single_rpc_call,
    _validate_provider_label,
    _validate_rpc_endpoint,
)
from .gas_oracle import ORACLE
from .intent import SubmittedTransaction

STRATEGY_DAEMON_SCHEMA: Final[str] = "oasis.profit-strategy-daemon.v1"
STRATEGY_STATE_SCHEMA: Final[str] = "oasis.profit-strategy-state.v1"
StrategyMode = Literal["shadow", "dry_run", "live"]
VALID_MODES: Final[frozenset[str]] = frozenset({"shadow", "dry_run", "live"})

UnixAtomicAuthorityClient = ProfitAuthoritySocketClient


class AuthoritySocketProtocol(Protocol):
    """Minimal protocol satisfied by `ProfitAuthoritySocketClient` and test doubles."""

    def status(self) -> dict[str, Any]: ...

    def sign_admitted_proposal(
        self,
        proposal: AtomicOrIntentProposal,
        envelope_fields: list[object],
    ) -> bytes: ...

    def record_settlement(self, reconciliation_input: dict[str, Any]) -> dict[str, Any]: ...


class _AccountBoundStatusClient:
    """Read-only status client for keyless shadow mode when only `account` is configured."""

    def __init__(self, endpoint: Path, account: str, timeout: float = 10.0) -> None:
        self.endpoint = endpoint
        self.account = normalize_address(account)
        self.timeout = float(timeout)

    def status(self) -> dict[str, Any]:
        path = validate_socket_endpoint(self.endpoint)
        request = canonical_json(
            {
                "schema": PROFIT_AUTHORITY_SCHEMA,
                "account": self.account,
                "operation": "status",
            }
        )
        if len(request) >= MAX_PROFIT_FRAME:
            raise ValueError("status request exceeds frame bound")
        deadline = time.monotonic() + self.timeout
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(max(0.001, deadline - time.monotonic()))
            conn.connect(str(path))
            conn.settimeout(max(0.001, deadline - time.monotonic()))
            conn.sendall(request + b"\n")
            reply = read_frame(
                conn,
                deadline=deadline,
                idle_timeout=self.timeout,
                max_bytes=MAX_PROFIT_FRAME,
            )
        decoded = strict_json_loads(reply)
        if (
            not isinstance(decoded, dict)
            or canonical_json(decoded) != reply
            or decoded.get("schema") != PROFIT_AUTHORITY_SCHEMA
        ):
            raise ValueError("authority status reply is malformed")
        if decoded.get("refused") is True:
            raise ValueError(f"authority status refused: {decoded.get('reason_code', 'refused')}")
        if set(decoded) != {"schema", "status"} or not isinstance(decoded["status"], dict):
            raise ValueError("authority status payload malformed")
        return decoded["status"]

    def sign_admitted_proposal(
        self,
        proposal: AtomicOrIntentProposal,
        envelope_fields: list[object],
    ) -> bytes:
        del proposal, envelope_fields
        raise PermissionError("read-only shadow status client cannot request signatures")

    def record_settlement(self, reconciliation_input: dict[str, Any]) -> dict[str, Any]:
        del reconciliation_input
        raise PermissionError("read-only shadow status client cannot submit settlements")


def _compute_policy_digest(policy: AtomicExecutionPolicy) -> str:
    payload = {
        "chain_id": policy.chain_id,
        "operator_account": policy.operator_account,
        "executor_address": policy.executor_address,
        "executor_code_hash": policy.executor_code_hash,
        "intent_vault_address": policy.intent_vault_address,
        "intent_vault_code_hash": policy.intent_vault_code_hash,
        "allowed_pools": sorted(policy.allowed_pools),
        "allowed_routers": sorted(policy.allowed_routers),
        "allowed_reactors": sorted(policy.allowed_reactors),
        "allowed_profit_tokens": sorted(policy.allowed_profit_tokens),
        "max_capital_usd_micros": policy.max_capital_usd_micros,
        "max_daily_loss_usd_micros": policy.max_daily_loss_usd_micros,
        "max_cumulative_loss_usd_micros": policy.max_cumulative_loss_usd_micros,
        "max_gas_limit": policy.max_gas_limit,
        "max_fee_per_gas_wei": policy.max_fee_per_gas_wei,
        "max_priority_fee_wei": policy.max_priority_fee_wei,
        "max_deadline_horizon_s": policy.max_deadline_horizon_s,
        "release_gate_open": policy.release_gate_open,
        "rehearsal_only": policy.rehearsal_only,
    }
    return sha256_hex(canonical_json(payload))


def _require_plain_int(value: object, name: str, *, low: int | None = None) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be a plain int")
    if low is not None and value < low:
        raise ValueError(f"{name} must be >= {low}")
    return value


def _require_plain_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be a plain bool")
    return value


def _usd_field_to_micros(
    raw: Mapping[str, Any],
    *,
    micros_key: str,
    usd_key: str,
    default: int = 0,
) -> int:
    if micros_key in raw and raw[micros_key] is not None:
        return _require_plain_int(raw[micros_key], micros_key, low=0)
    if usd_key in raw and raw[usd_key] is not None:
        val = raw[usd_key]
        if type(val) is bool or not isinstance(val, (int, float, Decimal)):
            raise TypeError(f"{usd_key} must be a numeric USD value")
        try:
            dec = val if isinstance(val, Decimal) else Decimal(str(val))
        except InvalidOperation as exc:
            raise ValueError(f"{usd_key} is not a valid decimal") from exc
        if not dec.is_finite() or dec < 0:
            raise ValueError(f"{usd_key} must be finite and non-negative")
        return int((dec * Decimal(USD_MICROS_SCALE)).to_integral_value())
    return default


def _validate_private_state_dir(directory: Path) -> Path:
    if not isinstance(directory, Path):
        raise TypeError("state_dir must be a Path")
    if (
        not directory.is_absolute()
        or directory.is_symlink()
        or directory != directory.resolve()
        or not directory.is_dir()
    ):
        raise ValueError("strategy state_dir must be an existing absolute non-symlink directory")
    info = directory.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("strategy state_dir must be owned and mode 0700")
    return directory


def atomic_write_json_file(path: Path, payload: Mapping[str, Any], *, mode: int = 0o600) -> None:
    """Atomically write canonical JSON to a non-symlink file with strict permissions."""
    if not isinstance(path, Path):
        raise TypeError("output path must be a Path")
    if path.is_symlink():
        raise ValueError(f"refusing to overwrite symlink output path: {path}")
    if path.parent.is_symlink():
        raise ValueError(f"refusing symlink output parent directory: {path.parent}")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_json(dict(payload)) + b"\n"
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=".profit_strategy_",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            os.fchmod(handle.fileno(), mode)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        if tmp_path is not None:
            with suppress(OSError):
                tmp_path.unlink()
        raise


@dataclass(frozen=True, slots=True)
class ProfitStrategyDaemonConfig:
    """Configuration for the keyless profit-strategy daemon and authority bridge."""

    authority_socket: Path
    mode: StrategyMode = "shadow"
    risk_config_path: Path | None = None
    risk_config: CapitalRiskConfig | None = None
    policy_config_path: Path | None = None
    policy: AtomicExecutionPolicy | None = None
    account: str | None = None
    state_dir: Path | None = None
    poll_interval_ms: int = 1000
    max_ticks: int | None = None
    socket_timeout_s: float = 10.0
    cash_available_usd_micros: int | None = None
    rehearsal_loopback_anvil: bool = False
    readiness_report_path: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.authority_socket, Path):
            raise TypeError("authority_socket must be a Path")
        if self.mode not in VALID_MODES:
            raise ValueError(f"unsupported strategy mode: {self.mode!r}")
        if self.risk_config_path is None and self.risk_config is None:
            raise ValueError("risk_config or risk_config_path is required")
        if self.risk_config_path is not None and not isinstance(self.risk_config_path, Path):
            raise TypeError("risk_config_path must be a Path")
        if self.risk_config is not None and not isinstance(self.risk_config, CapitalRiskConfig):
            raise TypeError("risk_config must be a CapitalRiskConfig")
        if self.policy_config_path is not None and not isinstance(self.policy_config_path, Path):
            raise TypeError("policy_config_path must be a Path")
        if self.policy is not None and not isinstance(self.policy, AtomicExecutionPolicy):
            raise TypeError("policy must be an AtomicExecutionPolicy")
        if self.account is not None:
            normalized = normalize_address(self.account)
            if self.policy is not None and normalized != self.policy.operator_account:
                raise ValueError("account must match policy.operator_account")
            object.__setattr__(self, "account", normalized)
        if self.state_dir is not None and not isinstance(self.state_dir, Path):
            raise TypeError("state_dir must be a Path")
        _require_plain_int(self.poll_interval_ms, "poll_interval_ms", low=10)
        if self.poll_interval_ms > 3_600_000:
            raise ValueError("poll_interval_ms must be <= 3600000")
        if self.max_ticks is not None:
            _require_plain_int(self.max_ticks, "max_ticks", low=1)
        if (
            type(self.socket_timeout_s) not in (int, float)
            or not 0 < float(self.socket_timeout_s) <= 240.0
        ):
            raise ValueError("socket_timeout_s must be in (0, 240]")
        if self.cash_available_usd_micros is not None:
            _require_plain_int(
                self.cash_available_usd_micros,
                "cash_available_usd_micros",
                low=0,
            )
        _require_plain_bool(self.rehearsal_loopback_anvil, "rehearsal_loopback_anvil")
        if self.readiness_report_path is not None and not isinstance(
            self.readiness_report_path, Path
        ):
            raise TypeError("readiness_report_path must be a Path")

    def resolve_risk_config(self) -> CapitalRiskConfig:
        """Load or return the configured `CapitalRiskConfig`, verifying consistency."""
        loaded_from_path = (
            load_capital_risk_config(self.risk_config_path)
            if self.risk_config_path is not None
            else None
        )
        if loaded_from_path is not None and self.risk_config is not None:
            if loaded_from_path.config_sha256 != self.risk_config.config_sha256:
                raise ValueError("risk_config disagrees with risk_config_path")
            return self.risk_config
        if self.risk_config is not None:
            return self.risk_config
        if loaded_from_path is None:
            raise ValueError("risk_config or risk_config_path is required")
        return loaded_from_path

    def resolve_policy(self) -> AtomicExecutionPolicy | None:
        """Load or return the configured `AtomicExecutionPolicy` when present."""
        loaded_from_path = (
            load_atomic_execution_policy(self.policy_config_path).policy
            if self.policy_config_path is not None
            else None
        )
        if loaded_from_path is not None and self.policy is not None:
            if _compute_policy_digest(loaded_from_path) != _compute_policy_digest(self.policy):
                raise ValueError("policy disagrees with policy_config_path")
            return self.policy
        return self.policy if self.policy is not None else loaded_from_path


@dataclass(frozen=True, slots=True)
class AuthorityLedgerView:
    """Normalized read-only view of the authority daemon status frame."""

    reachable: bool
    account: str | None
    chain_id: int | None
    policy_digest: str | None
    stop_active: bool
    halted: bool
    halt_reasons: tuple[str, ...]
    revert_budget_status: str
    pending_count: int
    pending_tx_hashes: tuple[str, ...]
    signing_enabled: bool
    signing_unresolved: bool
    signatures_used: int
    max_signatures: int | None
    last_nonce: int | None
    expired: bool
    daily_loss_usd_micros: int
    cumulative_loss_usd_micros: int
    cash_available_usd_micros: int | None
    locked_inventory_usd_micros: int
    pending_commitments_usd_micros: int
    unrealized_drawdown_usd_micros: int
    settled_tx_hashes: tuple[str, ...] = ()
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize authority ledger view cleanly without sensitive data."""
        return {
            "reachable": self.reachable,
            "account": self.account,
            "chain_id": self.chain_id,
            "policy_digest": self.policy_digest,
            "stop_active": self.stop_active,
            "halted": self.halted,
            "halt_reasons": list(self.halt_reasons),
            "revert_budget_status": self.revert_budget_status,
            "pending_count": self.pending_count,
            "pending_tx_hashes": list(self.pending_tx_hashes),
            "settled_tx_hashes": list(self.settled_tx_hashes),
            "signing_enabled": self.signing_enabled,
            "signing_unresolved": self.signing_unresolved,
            "signatures_used": self.signatures_used,
            "max_signatures": self.max_signatures,
            "last_nonce": self.last_nonce,
            "expired": self.expired,
            "daily_loss_usd_micros": self.daily_loss_usd_micros,
            "cumulative_loss_usd_micros": self.cumulative_loss_usd_micros,
            "cash_available_usd_micros": self.cash_available_usd_micros,
            "locked_inventory_usd_micros": self.locked_inventory_usd_micros,
            "pending_commitments_usd_micros": self.pending_commitments_usd_micros,
            "unrealized_drawdown_usd_micros": self.unrealized_drawdown_usd_micros,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class StrategyCandidateIntent:
    """Candidate envelope or prebuilt proposal emitted for a single strategy tick."""

    envelope: EnvelopeProposal | None = None
    proposal: AtomicOrIntentProposal | None = None
    simulate_fn: Callable[[EnvelopeProposal], DualSimulationProof] | None = None
    notional_usd_micros: int | None = None
    cash_available_usd_micros: int | None = None

    def __post_init__(self) -> None:
        if self.proposal is None and self.envelope is None:
            raise ValueError("candidate requires either proposal or envelope")
        if self.proposal is not None and (
            self.envelope is not None or self.simulate_fn is not None
        ):
            raise ValueError("pass either proposal or envelope + simulate_fn, not both")
        if self.proposal is not None and not isinstance(self.proposal, AtomicOrIntentProposal):
            raise TypeError("proposal must be an AtomicOrIntentProposal")
        if self.envelope is not None and not isinstance(self.envelope, EnvelopeProposal):
            raise TypeError("envelope must be an EnvelopeProposal")
        if self.notional_usd_micros is not None:
            _require_plain_int(self.notional_usd_micros, "notional_usd_micros", low=1)
        if self.cash_available_usd_micros is not None:
            _require_plain_int(self.cash_available_usd_micros, "cash_available_usd_micros", low=0)


@dataclass(frozen=True, slots=True)
class BridgeSyncSnapshot:
    """Synchronized authority status + capital headroom snapshot for one tick."""

    mode: StrategyMode
    authority_socket: str
    risk_config_sha256: str
    policy_digest: str | None
    authority_reachable: bool
    authority_status: AuthorityLedgerView
    capital_snapshot: AccountCapitalSnapshot
    headroom: HeadroomAllocation
    deployable_notional_usd_micros: int
    can_deploy: bool
    blocked_reason: str | None
    blocked_reasons: tuple[str, ...]
    halted: bool
    stop_active: bool
    pending_count: int
    execution_eligible: bool
    release_permitted: bool
    tick: int
    schema: str = STRATEGY_DAEMON_SCHEMA
    last_submission: dict[str, Any] | None = None
    last_reconciliation: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize bridge status snapshot into deterministic JSON form."""
        payload: dict[str, Any] = {
            "schema": self.schema,
            "mode": self.mode,
            "tick": self.tick,
            "authority_socket": self.authority_socket,
            "risk_config_sha256": self.risk_config_sha256,
            "policy_digest": self.policy_digest,
            "authority_reachable": self.authority_reachable,
            "authority_status": self.authority_status.to_dict(),
            "capital_snapshot": asdict(self.capital_snapshot),
            "headroom": self.headroom.to_dict(),
            "deployable_notional_usd_micros": self.deployable_notional_usd_micros,
            "can_deploy": self.can_deploy,
            "blocked_reason": self.blocked_reason,
            "blocked_reasons": list(self.blocked_reasons),
            "halted": self.halted,
            "stop_active": self.stop_active,
            "pending_count": self.pending_count,
            "execution_eligible": self.execution_eligible,
            "release_permitted": self.release_permitted,
        }
        if self.last_submission is not None:
            payload["last_submission"] = dict(self.last_submission)
        if self.last_reconciliation is not None:
            payload["last_reconciliation"] = dict(self.last_reconciliation)
        return payload


@dataclass(frozen=True, slots=True)
class IntentSubmissionResult:
    """Result of dual-simulated intent gating, authority signing, and settlement."""

    status: str
    mode: StrategyMode
    admitted: bool
    signed: bool
    broadcast: bool
    reconciled: bool
    halted: bool
    blocked_reason: str | None
    reasons: tuple[str, ...] = ()
    proposal_digest: str | None = None
    tx_hash: str | None = None
    settlement: dict[str, Any] | None = None
    headroom: HeadroomAllocation | None = None
    execution_eligible: bool = False
    release_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize submission outcome cleanly without private keys or raw bytes."""
        return {
            "status": self.status,
            "mode": self.mode,
            "admitted": self.admitted,
            "signed": self.signed,
            "broadcast": self.broadcast,
            "reconciled": self.reconciled,
            "halted": self.halted,
            "blocked_reason": self.blocked_reason,
            "reasons": list(self.reasons),
            "proposal_digest": self.proposal_digest,
            "tx_hash": self.tx_hash,
            "settlement": self.settlement,
            "headroom": self.headroom.to_dict() if self.headroom is not None else None,
            "execution_eligible": self.execution_eligible,
            "release_permitted": self.release_permitted,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationPassResult:
    """Outcome of `reconcile_pending(...)` for `profit-strategy-recovery`."""

    status: str
    mode: StrategyMode
    authority_reachable: bool
    halted: bool
    stop_active: bool
    pending_count: int
    settled_hashes: tuple[str, ...]
    unresolved_hashes: tuple[str, ...]
    settlements: tuple[dict[str, Any], ...]
    blocked_reason: str | None
    headroom: HeadroomAllocation | None
    schema: str = STRATEGY_DAEMON_SCHEMA
    execution_eligible: Literal[False] = False
    release_permitted: Literal[False] = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize recovery reconciliation outcome deterministically."""
        return {
            "schema": self.schema,
            "status": self.status,
            "mode": self.mode,
            "authority_reachable": self.authority_reachable,
            "halted": self.halted,
            "stop_active": self.stop_active,
            "pending_count": self.pending_count,
            "settled_hashes": list(self.settled_hashes),
            "unresolved_hashes": list(self.unresolved_hashes),
            "settlements": list(self.settlements),
            "blocked_reason": self.blocked_reason,
            "headroom": self.headroom.to_dict() if self.headroom is not None else None,
            "execution_eligible": self.execution_eligible,
            "release_permitted": self.release_permitted,
        }


@dataclass(slots=True)
class _StrategyLocalState:
    halted: bool
    halt_reasons: list[str]
    pending_txs: dict[str, dict[str, Any]]
    daily_loss_usd_micros: int
    cumulative_loss_usd_micros: int
    settled_history: list[dict[str, Any]]


def _parse_authority_status(
    raw: object,
    *,
    expected_account: str | None,
    expected_policy_digest: str | None,
) -> AuthorityLedgerView:
    if not isinstance(raw, Mapping):
        raise ValueError("authority status payload must be a mapping")
    if "schema" in raw and raw["schema"] != PROFIT_AUTHORITY_SCHEMA:
        raise ValueError("authority status schema mismatch")

    account_val: str | None = None
    if "account" in raw and raw["account"] is not None:
        if not isinstance(raw["account"], str) or not raw["account"]:
            raise ValueError("authority status account invalid")
        account_val = normalize_address(raw["account"])
        if expected_account is not None and account_val != expected_account:
            raise ValueError("authority status account mismatch")

    chain_id = (
        _require_plain_int(raw["chain_id"], "chain_id", low=1)
        if "chain_id" in raw and raw["chain_id"] is not None
        else None
    )

    policy_digest: str | None = None
    if "policy_digest" in raw and raw["policy_digest"] is not None:
        if not isinstance(raw["policy_digest"], str) or not raw["policy_digest"]:
            raise ValueError("authority status policy_digest invalid")
        policy_digest = raw["policy_digest"]
        if expected_policy_digest is not None and policy_digest != expected_policy_digest:
            raise ValueError("authority status policy_digest mismatch")

    stop_present = (
        _require_plain_bool(raw["stop_present"], "stop_present") if "stop_present" in raw else False
    )
    stop_active_flag = (
        _require_plain_bool(raw["stop_active"], "stop_active") if "stop_active" in raw else False
    )
    stop_active = stop_present or stop_active_flag
    halted = _require_plain_bool(raw["halted"], "halted") if "halted" in raw else False

    raw_reasons = raw.get("halt_reasons", [])
    if not isinstance(raw_reasons, list) or any(not isinstance(r, str) for r in raw_reasons):
        raise ValueError("authority status halt_reasons malformed")

    rb_status = raw.get("revert_budget_status", RevertBudgetStatus.ACTIVE.value)
    if not isinstance(rb_status, str) or rb_status not in (
        RevertBudgetStatus.ACTIVE.value,
        RevertBudgetStatus.HALT.value,
    ):
        raise ValueError("authority status revert_budget_status invalid")

    raw_pending = raw.get("pending_tx_hashes", [])
    if not isinstance(raw_pending, list) or any(not isinstance(tx, str) for tx in raw_pending):
        raise ValueError("authority status pending_tx_hashes malformed")
    pending_hashes = tuple(normalize_hash32(tx, "pending_tx_hash") for tx in raw_pending)

    raw_settled = raw.get("settled_tx_hashes", [])
    if not isinstance(raw_settled, list) or any(not isinstance(tx, str) for tx in raw_settled):
        raise ValueError("authority status settled_tx_hashes malformed")
    settled_hashes = tuple(normalize_hash32(tx, "settled_tx_hash") for tx in raw_settled)

    signing_unresolved = (
        _require_plain_bool(raw["signing_unresolved"], "signing_unresolved")
        if "signing_unresolved" in raw
        else False
    )
    explicit_pending = (
        _require_plain_int(raw["pending_count"], "pending_count", low=0)
        if "pending_count" in raw and raw["pending_count"] is not None
        else 0
    )
    pending_count = max(len(pending_hashes), explicit_pending) + (1 if signing_unresolved else 0)

    signing_enabled = (
        _require_plain_bool(raw["signing_enabled"], "signing_enabled")
        if "signing_enabled" in raw
        else False
    )
    signatures_used = (
        _require_plain_int(raw["signatures_used"], "signatures_used", low=0)
        if "signatures_used" in raw and raw["signatures_used"] is not None
        else 0
    )
    max_signatures = (
        _require_plain_int(raw["max_signatures"], "max_signatures", low=1)
        if "max_signatures" in raw and raw["max_signatures"] is not None
        else None
    )
    last_nonce = (
        _require_plain_int(raw["last_nonce"], "last_nonce", low=0)
        if raw.get("last_nonce") is not None
        else None
    )
    expired = _require_plain_bool(raw["expired"], "expired") if "expired" in raw else False

    daily_loss_micros = _usd_field_to_micros(
        raw, micros_key="daily_loss_usd_micros", usd_key="daily_loss_usd", default=0
    )
    cumulative_loss_micros = _usd_field_to_micros(
        raw, micros_key="cumulative_loss_usd_micros", usd_key="cumulative_loss_usd", default=0
    )
    cash_micros = (
        _usd_field_to_micros(
            raw, micros_key="cash_available_usd_micros", usd_key="cash_available_usd", default=0
        )
        if ("cash_available_usd_micros" in raw or "cash_available_usd" in raw)
        and (
            raw.get("cash_available_usd_micros") is not None
            or raw.get("cash_available_usd") is not None
        )
        else None
    )
    locked_micros = _usd_field_to_micros(
        raw,
        micros_key="locked_inventory_usd_micros",
        usd_key="locked_inventory_usd",
        default=0,
    )
    pending_commitments_micros = _usd_field_to_micros(
        raw,
        micros_key="pending_commitments_usd_micros",
        usd_key="pending_commitments_usd",
        default=0,
    )
    unrealized_drawdown_micros = _usd_field_to_micros(
        raw,
        micros_key="unrealized_drawdown_usd_micros",
        usd_key="unrealized_drawdown_usd",
        default=0,
    )

    return AuthorityLedgerView(
        reachable=True,
        account=account_val,
        chain_id=chain_id,
        policy_digest=policy_digest or expected_policy_digest,
        stop_active=stop_active,
        halted=halted,
        halt_reasons=tuple(raw_reasons),
        revert_budget_status=rb_status,
        pending_count=pending_count,
        pending_tx_hashes=pending_hashes,
        signing_enabled=signing_enabled,
        signing_unresolved=signing_unresolved,
        signatures_used=signatures_used,
        max_signatures=max_signatures,
        last_nonce=last_nonce,
        expired=expired,
        daily_loss_usd_micros=daily_loss_micros,
        cumulative_loss_usd_micros=cumulative_loss_micros,
        cash_available_usd_micros=cash_micros,
        locked_inventory_usd_micros=locked_micros,
        pending_commitments_usd_micros=pending_commitments_micros,
        unrealized_drawdown_usd_micros=unrealized_drawdown_micros,
        settled_tx_hashes=settled_hashes,
        error=None,
    )


_GET_L1_FEE_UPPER_BOUND_SELECTOR: Final[bytes] = keccak(b"getL1FeeUpperBound(uint256)")[:4]
_MAX_RPC_TIP_SKEW_BLOCKS: Final[int] = 32
_TOKEN_USDC: Final[str] = normalize_address(USDC)
_TOKEN_WETH: Final[str] = normalize_address(WETH)


def _validate_simulator_args(
    *,
    primary_rpc_url: str,
    primary_source: str,
    secondary_rpc_url: str,
    secondary_source: str,
    block_lag: int,
    pinned_block_number: int | None,
    eth_usd_micros: int | None,
    timeout_s: float,
    validate_endpoints: bool,
) -> tuple[str, str, float]:
    primary_label = _validate_provider_label(primary_source, "primary_source")
    secondary_label = _validate_provider_label(secondary_source, "secondary_source")
    if primary_label.lower() == secondary_label.lower():
        raise DualRpcQuorumError("primary_source and secondary_source must be distinct")
    _require_plain_int(block_lag, "block_lag", low=0)
    if pinned_block_number is not None:
        _require_plain_int(pinned_block_number, "pinned_block_number", low=1)
    if eth_usd_micros is not None:
        _require_plain_int(eth_usd_micros, "eth_usd_micros", low=1)
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(float(timeout_s))
        or float(timeout_s) <= 0
    ):
        raise DualRpcQuorumError("timeout_s must be a positive finite number")
    if validate_endpoints:
        p_netloc, p_host, p_port = _validate_rpc_endpoint(primary_rpc_url, role="primary")
        s_netloc, s_host, s_port = _validate_rpc_endpoint(secondary_rpc_url, role="secondary")
        if p_netloc == s_netloc or (p_host, p_port) == (s_host, s_port):
            raise DualRpcQuorumError(
                "primary_rpc_url and secondary_rpc_url must have distinct netlocs"
            )
    else:
        for role, url in (
            ("primary_rpc_url", primary_rpc_url),
            ("secondary_rpc_url", secondary_rpc_url),
        ):
            if not isinstance(url, str) or not url.strip():
                raise DualRpcQuorumError(f"{role} must be a non-empty string")
        if primary_rpc_url.strip() == secondary_rpc_url.strip():
            raise DualRpcQuorumError("primary_rpc_url and secondary_rpc_url must be distinct")
    return primary_label, secondary_label, float(timeout_s)


def _rpc_call_or_revert(
    send: Callable[[str, bytes, float], bytes],
    url: str,
    *,
    role: str,
    method: str,
    params: list[object],
    timeout_s: float,
) -> tuple[object | None, bool]:
    req_bytes = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        separators=(",", ":"),
    ).encode("utf-8")
    resp_bytes = send(url, req_bytes, timeout_s)
    if not isinstance(resp_bytes, bytes) or len(resp_bytes) > MAX_RESPONSE_BYTES:
        raise RpcError(f"{role} {method} response must be bounded bytes")
    try:
        envelope = strict_json_object(resp_bytes)
    except ValueError as exc:
        raise DualRpcQuorumError(f"{role} {method} returned invalid JSON: {exc}") from exc
    if (
        envelope.get("jsonrpc") != "2.0"
        or type(envelope.get("id")) is not int
        or envelope["id"] != 1
    ):
        raise DualRpcQuorumError(f"{role} {method} JSON-RPC version or ID mismatch")
    if ("result" in envelope) == ("error" in envelope):
        raise DualRpcQuorumError(f"{role} {method} must contain exactly one result or error")
    if "error" in envelope:
        return None, True
    return envelope["result"], False


def _query_pinned_block_header(
    send: Callable[[str, bytes, float], bytes],
    url: str,
    *,
    role: str,
    target_block: int,
    timeout_s: float,
) -> tuple[str, str, str]:
    raw_header = _single_rpc_call(
        send,
        url,
        role=role,
        method="eth_getBlockByNumber",
        params=[hex(target_block), False],
        timeout_s=timeout_s,
    )
    try:
        header = strict_object(raw_header, f"{role} block header")
        header_num = rpc_quantity(header.get("number"))
        if header_num != target_block:
            raise DualRpcQuorumError(
                f"{role} returned block {header_num}, expected pinned block {target_block}"
            )
        block_hash = normalize_hash32(strict_string(header.get("hash"), "hash"), "hash")
        if int(block_hash, 16) == 0:
            raise DualRpcQuorumError(f"{role} returned zero block hash")
        parent_hash = normalize_hash32(
            strict_string(header.get("parentHash"), "parentHash"), "parentHash"
        )
        state_root = normalize_hash32(
            strict_string(header.get("stateRoot"), "stateRoot"), "stateRoot"
        )
    except ValueError as exc:
        if isinstance(exc, DualRpcQuorumError):
            raise
        raise DualRpcQuorumError(f"{role} block header invalid: {exc}") from exc
    return block_hash, parent_hash, state_root


def _decode_runtime_code_bytes(
    send: Callable[[str, bytes, float], bytes],
    url: str,
    *,
    role: str,
    target: str,
    pin: dict[str, object],
    timeout_s: float,
) -> bytes:
    raw_code = _single_rpc_call(
        send,
        url,
        role=role,
        method="eth_getCode",
        params=[target, pin],
        timeout_s=timeout_s,
    )
    try:
        raw_hex = strict_string(raw_code, f"{role}.eth_getCode")
    except ValueError as exc:
        raise DualRpcQuorumError(str(exc)) from exc
    if raw_hex == "0x" or not raw_hex.startswith("0x") or len(raw_hex) < 4 or len(raw_hex) % 2:
        raise DualRpcQuorumError(f"{role} returned empty or malformed target bytecode")
    try:
        code_bytes = bytes.fromhex(raw_hex[2:])
    except ValueError as exc:
        raise DualRpcQuorumError(f"{role} returned non-hex target bytecode") from exc
    if not code_bytes:
        raise DualRpcQuorumError(f"{role} returned empty target bytecode")
    return code_bytes


def _simulate_single_provider_call(
    send: Callable[[str, bytes, float], bytes],
    url: str,
    *,
    role: str,
    tx_obj: dict[str, str],
    pin: dict[str, object],
    gas_limit: int,
    timeout_s: float,
) -> tuple[bool, bool, int, int]:
    raw_call, call_reverted = _rpc_call_or_revert(
        send,
        url,
        role=role,
        method="eth_call",
        params=[tx_obj, pin],
        timeout_s=timeout_s,
    )
    if call_reverted:
        return True, False, 0, 0

    try:
        raw_call_hex = strict_string(raw_call, f"{role}.eth_call")
    except ValueError as exc:
        raise DualRpcQuorumError(str(exc)) from exc
    if not raw_call_hex.startswith("0x") or len(raw_call_hex) != 130:
        raise DualRpcQuorumError(f"{role}.eth_call must return 64-byte (bool, uint256)")
    try:
        call_ret_bytes = bytes.fromhex(raw_call_hex[2:])
    except ValueError as exc:
        raise DualRpcQuorumError(f"{role}.eth_call returned non-hex bytes") from exc
    if int.from_bytes(call_ret_bytes[:32], "big") not in (0, 1):
        raise DualRpcQuorumError(f"{role}.eth_call returned non-canonical bool word")
    executed_raw, net_profit_raw = decode(["bool", "uint256"], call_ret_bytes)
    executed = bool(executed_raw)
    net_profit = int(net_profit_raw)

    raw_est_gas, gas_reverted = _rpc_call_or_revert(
        send,
        url,
        role=role,
        method="eth_estimateGas",
        params=[tx_obj, pin],
        timeout_s=timeout_s,
    )
    if gas_reverted:
        return True, False, 0, 0
    try:
        gas_used = rpc_quantity(raw_est_gas)
    except ValueError as exc:
        raise DualRpcQuorumError(f"{role} invalid eth_estimateGas response: {exc}") from exc
    if gas_used <= 0 or gas_used > gas_limit:
        return True, False, 0, 0
    if not executed:
        return False, True, gas_used, 0
    return False, False, gas_used, net_profit


def _query_l1_fee_upper_bound(
    send: Callable[[str, bytes, float], bytes],
    url: str,
    *,
    role: str,
    oracle_calldata: str,
    pin: dict[str, object],
    timeout_s: float,
) -> int:
    raw_l1 = _single_rpc_call(
        send,
        url,
        role=role,
        method="eth_call",
        params=[{"to": normalize_address(ORACLE), "data": oracle_calldata}, pin],
        timeout_s=timeout_s,
    )
    try:
        raw_l1_hex = strict_string(raw_l1, f"{role}.getL1FeeUpperBound")
        if len(raw_l1_hex) != 66 or not raw_l1_hex.startswith("0x"):
            raise ValueError("expected 32-byte uint256 hex output")
        norm_l1 = normalize_hash32(raw_l1_hex, f"{role}.getL1FeeUpperBound")
    except ValueError as exc:
        raise DualRpcQuorumError(f"{role}.getL1FeeUpperBound invalid output: {exc}") from exc
    return int(norm_l1, 16)


def simulate_atomic_or_intent_envelope_dual(
    envelope: EnvelopeProposal,
    *,
    primary_rpc_url: str = DEFAULT_PRIMARY_URL,
    primary_source: str = DEFAULT_PRIMARY_PROVIDER,
    secondary_rpc_url: str = DEFAULT_SECONDARY_URL,
    secondary_source: str = DEFAULT_SECONDARY_PROVIDER,
    block_lag: int = DEFAULT_BLOCK_LAG,
    pinned_block_number: int | None = None,
    eth_usd_micros: int | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    transport: Callable[[str, bytes, float], bytes] | None = None,
) -> DualSimulationProof:
    """Dual-simulate an atomic executor or intent vault envelope at a pinned canonical block."""
    if not isinstance(envelope, EnvelopeProposal):
        raise TypeError("envelope must be an EnvelopeProposal")
    primary_label, secondary_label, timeout_val = _validate_simulator_args(
        primary_rpc_url=primary_rpc_url,
        primary_source=primary_source,
        secondary_rpc_url=secondary_rpc_url,
        secondary_source=secondary_source,
        block_lag=block_lag,
        pinned_block_number=pinned_block_number,
        eth_usd_micros=eth_usd_micros,
        timeout_s=timeout_s,
        validate_endpoints=transport is None,
    )
    if envelope.profit_token == _TOKEN_USDC:
        if eth_usd_micros is None or eth_usd_micros <= 0:
            raise ValueError("eth_usd_micros must be positive when profit_token is USDC")
    elif envelope.profit_token != _TOKEN_WETH:
        raise ValueError("unsupported profit_token for dual-RPC profit valuation")

    send = _default_https_transport if transport is None else transport
    providers = (("primary", primary_rpc_url), ("secondary", secondary_rpc_url))

    # 1. Verify eth_chainId matches envelope.chain_id on both providers.
    for role, url in providers:
        raw_chain = _single_rpc_call(
            send, url, role=role, method="eth_chainId", params=[], timeout_s=timeout_val
        )
        try:
            chain_id = rpc_quantity(raw_chain)
        except ValueError as exc:
            raise DualRpcQuorumError(f"{role} invalid eth_chainId response: {exc}") from exc
        if chain_id != envelope.chain_id:
            raise DualRpcQuorumError(
                f"{role} eth_chainId mismatch: expected {envelope.chain_id}, got {chain_id}"
            )

    # 2. Resolve pinned canonical block height.
    if pinned_block_number is not None:
        target_block = pinned_block_number
    else:
        tips: list[int] = []
        for role, url in providers:
            raw_tip = _single_rpc_call(
                send, url, role=role, method="eth_blockNumber", params=[], timeout_s=timeout_val
            )
            try:
                tips.append(rpc_quantity(raw_tip))
            except ValueError as exc:
                raise DualRpcQuorumError(f"{role} invalid eth_blockNumber response: {exc}") from exc
        if abs(tips[0] - tips[1]) > _MAX_RPC_TIP_SKEW_BLOCKS:
            raise DualRpcQuorumError(
                f"provider tip skew exceeds {_MAX_RPC_TIP_SKEW_BLOCKS} blocks: "
                f"{tips[0]} vs {tips[1]}"
            )
        target_block = min(tips) - block_lag
        if target_block <= 0:
            raise DualRpcQuorumError("resolved pinned_block_number must be positive")

    # 3. Verify identical pinned block header across both providers.
    primary_header = _query_pinned_block_header(
        send, primary_rpc_url, role="primary", target_block=target_block, timeout_s=timeout_val
    )
    secondary_header = _query_pinned_block_header(
        send, secondary_rpc_url, role="secondary", target_block=target_block, timeout_s=timeout_val
    )
    if primary_header != secondary_header:
        raise DualRpcQuorumError(f"dual RPC quorum mismatch on block {target_block} header fields")
    block_hash, _parent_hash, _state_root = primary_header
    pin: dict[str, object] = {"blockHash": block_hash, "requireCanonical": True}

    # 4. Verify identical non-empty target bytecode and pin hash across both providers.
    primary_code = _decode_runtime_code_bytes(
        send,
        primary_rpc_url,
        role="primary",
        target=envelope.target,
        pin=pin,
        timeout_s=timeout_val,
    )
    secondary_code = _decode_runtime_code_bytes(
        send,
        secondary_rpc_url,
        role="secondary",
        target=envelope.target,
        pin=pin,
        timeout_s=timeout_val,
    )
    if primary_code != secondary_code:
        raise DualRpcQuorumError("dual RPC quorum mismatch on target contract bytecode")
    observed_keccak = "0x" + keccak(primary_code).hex()
    observed_sha256 = "0x" + sha256_hex(primary_code)
    if envelope.target_observed_code_hash not in (observed_keccak, observed_sha256):
        raise DualRpcQuorumError("target_observed_code_hash disagrees with pinned bytecode")

    # 5. Simulate exact EIP-1559 envelope call & estimateGas on both providers.
    tx_obj: dict[str, str] = {
        "from": envelope.account,
        "to": envelope.target,
        "data": "0x" + envelope.calldata.hex(),
        "value": hex(envelope.value_wei),
        "gas": hex(envelope.gas_limit),
        "maxFeePerGas": hex(envelope.max_fee_per_gas_wei),
        "maxPriorityFeePerGas": hex(envelope.max_priority_fee_wei),
        "nonce": hex(envelope.nonce),
    }
    p_rev, p_abort, p_gas, p_profit = _simulate_single_provider_call(
        send,
        primary_rpc_url,
        role="primary",
        tx_obj=tx_obj,
        pin=pin,
        gas_limit=envelope.gas_limit,
        timeout_s=timeout_val,
    )
    s_rev, s_abort, s_gas, s_profit = _simulate_single_provider_call(
        send,
        secondary_rpc_url,
        role="secondary",
        tx_obj=tx_obj,
        pin=pin,
        gas_limit=envelope.gas_limit,
        timeout_s=timeout_val,
    )

    # 6. Query OP GasPriceOracle getL1FeeUpperBound(uint256) at the pinned block.
    tx_size = len(envelope.calldata) + 256
    encoded_size = encode(["uint256"], [tx_size])
    oracle_calldata = "0x" + (_GET_L1_FEE_UPPER_BOUND_SELECTOR + encoded_size).hex()
    p_l1 = _query_l1_fee_upper_bound(
        send,
        primary_rpc_url,
        role="primary",
        oracle_calldata=oracle_calldata,
        pin=pin,
        timeout_s=timeout_val,
    )
    s_l1 = _query_l1_fee_upper_bound(
        send,
        secondary_rpc_url,
        role="secondary",
        oracle_calldata=oracle_calldata,
        pin=pin,
        timeout_s=timeout_val,
    )
    if p_l1 != s_l1:
        raise DualRpcQuorumError("dual RPC mismatch on GasPriceOracle.getL1FeeUpperBound")

    l2_gas_fee_wei = max(p_gas, s_gas) * envelope.max_fee_per_gas_wei
    agreed_profit_raw = max(0, min(p_profit, s_profit))
    if envelope.profit_token == _TOKEN_WETH:
        simulated_profit_value_wei = agreed_profit_raw
    else:
        if eth_usd_micros is None or eth_usd_micros <= 0:
            raise ValueError("eth_usd_micros must be positive when profit_token is USDC")
        simulated_profit_value_wei = (agreed_profit_raw * 10**18) // eth_usd_micros

    return DualSimulationProof(
        block_number=target_block,
        block_hash=block_hash,
        primary_source=primary_label,
        secondary_source=secondary_label,
        primary_reverted=p_rev,
        secondary_reverted=s_rev,
        primary_early_aborted=p_abort,
        secondary_early_aborted=s_abort,
        primary_gas_used=p_gas,
        secondary_gas_used=s_gas,
        primary_profit_delta_raw=p_profit,
        secondary_profit_delta_raw=s_profit,
        l2_gas_fee_wei=l2_gas_fee_wei,
        l1_data_fee_wei=p_l1,
        simulated_profit_value_wei=simulated_profit_value_wei,
    )


@dataclass(frozen=True, slots=True)
class DualRpcEnvelopeSimulator:
    """Configurable two-provider JSON-RPC pre-submission simulator for `ProfitStrategyBridge`."""

    primary_rpc_url: str = DEFAULT_PRIMARY_URL
    primary_source: str = DEFAULT_PRIMARY_PROVIDER
    secondary_rpc_url: str = DEFAULT_SECONDARY_URL
    secondary_source: str = DEFAULT_SECONDARY_PROVIDER
    block_lag: int = DEFAULT_BLOCK_LAG
    pinned_block_number: int | None = None
    eth_usd_micros: int | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    transport: Callable[[str, bytes, float], bytes] | None = None

    def __post_init__(self) -> None:
        _validate_simulator_args(
            primary_rpc_url=self.primary_rpc_url,
            primary_source=self.primary_source,
            secondary_rpc_url=self.secondary_rpc_url,
            secondary_source=self.secondary_source,
            block_lag=self.block_lag,
            pinned_block_number=self.pinned_block_number,
            eth_usd_micros=self.eth_usd_micros,
            timeout_s=self.timeout_s,
            validate_endpoints=self.transport is None,
        )

    def __call__(self, envelope: EnvelopeProposal) -> DualSimulationProof:
        return simulate_atomic_or_intent_envelope_dual(
            envelope,
            primary_rpc_url=self.primary_rpc_url,
            primary_source=self.primary_source,
            secondary_rpc_url=self.secondary_rpc_url,
            secondary_source=self.secondary_source,
            block_lag=self.block_lag,
            pinned_block_number=self.pinned_block_number,
            eth_usd_micros=self.eth_usd_micros,
            timeout_s=self.timeout_s,
            transport=self.transport,
        )


class ProfitStrategyBridge:
    """Bridge connecting `profit-strategy` to `profit_authority.sock` fail-closed."""

    def __init__(
        self,
        config: ProfitStrategyDaemonConfig,
        *,
        client: AuthoritySocketProtocol | None = None,
        now_fn: Callable[[], int] | None = None,
        simulate_fn: Callable[[EnvelopeProposal], DualSimulationProof] | None = None,
    ) -> None:
        if not isinstance(config, ProfitStrategyDaemonConfig):
            raise TypeError("config must be a ProfitStrategyDaemonConfig")
        self.config = config
        self.risk_config = config.resolve_risk_config()
        self.policy = config.resolve_policy()
        self.policy_digest = (
            _compute_policy_digest(self.policy) if self.policy is not None else None
        )
        self.account = (
            self.policy.operator_account
            if self.policy is not None
            else (normalize_address(config.account) if config.account is not None else None)
        )
        self._now_fn: Callable[[], int] = now_fn if now_fn is not None else lambda: int(time.time())
        self._simulate_fn = simulate_fn
        self._state_dir = (
            _validate_private_state_dir(config.state_dir) if config.state_dir is not None else None
        )
        self._state_path = (
            self._state_dir / "profit_strategy_state.json" if self._state_dir is not None else None
        )
        self._local_state = self._load_or_init_local_state()
        self._client = self._resolve_client(client)

    def _resolve_client(
        self,
        injected: AuthoritySocketProtocol | None,
    ) -> AuthoritySocketProtocol | None:
        if injected is not None:
            return injected
        if self.policy is not None:
            account = self.account or self.policy.operator_account
            return ProfitAuthoritySocketClient(
                self.config.authority_socket,
                account,
                self.policy,
                timeout=self.config.socket_timeout_s,
            )
        if self.account is not None and self.config.mode == "shadow":
            return _AccountBoundStatusClient(
                self.config.authority_socket,
                self.account,
                timeout=self.config.socket_timeout_s,
            )
        return None

    @property
    def client(self) -> AuthoritySocketProtocol | None:
        """Return the active authority socket client if configured."""
        return self._client

    @property
    def halted(self) -> bool:
        """Whether local strategy state has latched halt."""
        return self._local_state.halted

    def _load_or_init_local_state(self) -> _StrategyLocalState:
        if self._state_path is None:
            return _StrategyLocalState(
                halted=False,
                halt_reasons=[],
                pending_txs={},
                daily_loss_usd_micros=0,
                cumulative_loss_usd_micros=0,
                settled_history=[],
            )
        if self._state_path.is_symlink():
            raise ValueError("strategy state file cannot be a symlink")
        if not self._state_path.exists():
            initial = _StrategyLocalState(
                halted=False,
                halt_reasons=[],
                pending_txs={},
                daily_loss_usd_micros=0,
                cumulative_loss_usd_micros=0,
                settled_history=[],
            )
            self._persist_local_state(initial)
            return initial
        info = self._state_path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("strategy state file must be owned and mode 0600")
        decoded = strict_json_loads(self._state_path.read_bytes())
        if not isinstance(decoded, dict) or decoded.get("schema") != STRATEGY_STATE_SCHEMA:
            raise ValueError("strategy state schema mismatch")
        halt_reasons = decoded.get("halt_reasons", [])
        if not isinstance(halt_reasons, list) or any(not isinstance(r, str) for r in halt_reasons):
            raise ValueError("strategy state halt_reasons malformed")
        pending_raw = decoded.get("pending_txs", {})
        if not isinstance(pending_raw, dict):
            raise ValueError("strategy state pending_txs malformed")
        pending_txs: dict[str, dict[str, Any]] = {}
        for tx_hash, entry in pending_raw.items():
            norm_hash = normalize_hash32(str(tx_hash), "pending_tx_hash")
            if not isinstance(entry, dict):
                raise ValueError("strategy pending_tx entry malformed")
            pending_txs[norm_hash] = dict(entry)
        settled_raw = decoded.get("settled_history", [])
        if not isinstance(settled_raw, list):
            raise ValueError("strategy state settled_history malformed")
        return _StrategyLocalState(
            halted=_require_plain_bool(decoded.get("halted", False), "halted"),
            halt_reasons=list(halt_reasons),
            pending_txs=pending_txs,
            daily_loss_usd_micros=_require_plain_int(
                decoded.get("daily_loss_usd_micros", 0),
                "daily_loss_usd_micros",
                low=0,
            ),
            cumulative_loss_usd_micros=_require_plain_int(
                decoded.get("cumulative_loss_usd_micros", 0),
                "cumulative_loss_usd_micros",
                low=0,
            ),
            settled_history=[dict(item) for item in settled_raw if isinstance(item, dict)],
        )

    def _persist_local_state(self, state: _StrategyLocalState | None = None) -> None:
        if self._state_path is None:
            return
        target = self._local_state if state is None else state
        atomic_write_json_file(
            self._state_path,
            {
                "schema": STRATEGY_STATE_SCHEMA,
                "halted": target.halted,
                "halt_reasons": list(target.halt_reasons),
                "pending_txs": target.pending_txs,
                "daily_loss_usd_micros": target.daily_loss_usd_micros,
                "cumulative_loss_usd_micros": target.cumulative_loss_usd_micros,
                "settled_history": target.settled_history,
            },
            mode=0o600,
        )

    def _latch_local_halt(self, reason: str) -> None:
        self._local_state.halted = True
        if reason not in self._local_state.halt_reasons:
            self._local_state.halt_reasons.append(reason)
        self._persist_local_state()

    def _local_pending_hashes(self) -> list[str]:
        return sorted(
            tx_h
            for tx_h, entry in self._local_state.pending_txs.items()
            if entry.get("settled") is not True
        )

    def _poll_authority_ledger(self) -> AuthorityLedgerView:
        if self._client is None:
            socket_detail = "authority_policy_or_account_unconfigured"
            try:
                validate_socket_endpoint(self.config.authority_socket)
            except (OSError, ValueError, TypeError) as exc:
                socket_detail = f"authority_socket_unavailable:{type(exc).__name__}"
            return AuthorityLedgerView(
                reachable=False,
                account=self.account,
                chain_id=self.policy.chain_id if self.policy is not None else None,
                policy_digest=self.policy_digest,
                stop_active=False,
                halted=True,
                halt_reasons=(socket_detail,),
                revert_budget_status=RevertBudgetStatus.HALT.value,
                pending_count=len(self._local_pending_hashes()),
                pending_tx_hashes=tuple(self._local_pending_hashes()),
                signing_enabled=False,
                signing_unresolved=False,
                signatures_used=0,
                max_signatures=None,
                last_nonce=None,
                expired=False,
                daily_loss_usd_micros=self._local_state.daily_loss_usd_micros,
                cumulative_loss_usd_micros=self._local_state.cumulative_loss_usd_micros,
                cash_available_usd_micros=None,
                locked_inventory_usd_micros=0,
                pending_commitments_usd_micros=0,
                unrealized_drawdown_usd_micros=0,
                error=socket_detail,
            )

        try:
            raw_status = self._client.status()
            return _parse_authority_status(
                raw_status,
                expected_account=self.account,
                expected_policy_digest=self.policy_digest,
            )
        except TimeoutError as exc:
            err_label = f"authority_status_timeout:{exc}"
        except (OSError, ValueError, TypeError, PermissionError) as exc:
            err_label = f"authority_status_error:{exc}"

        return AuthorityLedgerView(
            reachable=False,
            account=self.account,
            chain_id=self.policy.chain_id if self.policy is not None else None,
            policy_digest=self.policy_digest,
            stop_active=False,
            halted=True,
            halt_reasons=(err_label,),
            revert_budget_status=RevertBudgetStatus.HALT.value,
            pending_count=len(self._local_pending_hashes()),
            pending_tx_hashes=tuple(self._local_pending_hashes()),
            signing_enabled=False,
            signing_unresolved=False,
            signatures_used=0,
            max_signatures=None,
            last_nonce=None,
            expired=False,
            daily_loss_usd_micros=self._local_state.daily_loss_usd_micros,
            cumulative_loss_usd_micros=self._local_state.cumulative_loss_usd_micros,
            cash_available_usd_micros=None,
            locked_inventory_usd_micros=0,
            pending_commitments_usd_micros=0,
            unrealized_drawdown_usd_micros=0,
            error=err_label,
        )

    def _release_is_open(self) -> bool:
        if self.config.mode != "live":
            return False
        if self.policy is None or not self.policy.release_gate_open:
            return False
        if self.config.rehearsal_loopback_anvil:
            return False
        if self.config.readiness_report_path is None:
            return False
        allowed, _reason = verify_profit_engine_readiness_report(self.config.readiness_report_path)
        return allowed

    def sync_status(
        self,
        *,
        tick: int = 1,
        cash_available_usd_micros: int | None = None,
        locked_inventory_usd_micros: int | None = None,
        pending_commitments_usd_micros: int | None = None,
        unrealized_drawdown_usd_micros: int | None = None,
    ) -> BridgeSyncSnapshot:
        """Query authority status and translate ledger + risk config into synchronized headroom."""
        _require_plain_int(tick, "tick", low=1)
        if cash_available_usd_micros is not None:
            _require_plain_int(cash_available_usd_micros, "cash_available_usd_micros", low=0)
        if locked_inventory_usd_micros is not None:
            _require_plain_int(locked_inventory_usd_micros, "locked_inventory_usd_micros", low=0)
        if pending_commitments_usd_micros is not None:
            _require_plain_int(
                pending_commitments_usd_micros, "pending_commitments_usd_micros", low=0
            )
        if unrealized_drawdown_usd_micros is not None:
            _require_plain_int(
                unrealized_drawdown_usd_micros, "unrealized_drawdown_usd_micros", low=0
            )

        ledger = self._poll_authority_ledger()
        local_pending = self._local_pending_hashes()
        merged_pending = sorted(set(ledger.pending_tx_hashes) | set(local_pending))
        total_pending = max(
            ledger.pending_count,
            len(merged_pending) + (1 if ledger.signing_unresolved else 0),
        )
        effective_daily_loss = max(
            ledger.daily_loss_usd_micros,
            self._local_state.daily_loss_usd_micros,
        )
        effective_cum_loss = max(
            ledger.cumulative_loss_usd_micros,
            self._local_state.cumulative_loss_usd_micros,
        )

        resolved_cash = (
            cash_available_usd_micros
            if cash_available_usd_micros is not None
            else (
                self.config.cash_available_usd_micros
                if self.config.cash_available_usd_micros is not None
                else (
                    ledger.cash_available_usd_micros
                    if ledger.cash_available_usd_micros is not None
                    else self.risk_config.total_capital_usd_micros
                )
            )
        )
        resolved_locked = (
            locked_inventory_usd_micros
            if locked_inventory_usd_micros is not None
            else ledger.locked_inventory_usd_micros
        )
        resolved_pending_commitments = (
            pending_commitments_usd_micros
            if pending_commitments_usd_micros is not None
            else ledger.pending_commitments_usd_micros
        )
        resolved_drawdown = (
            unrealized_drawdown_usd_micros
            if unrealized_drawdown_usd_micros is not None
            else ledger.unrealized_drawdown_usd_micros
        )

        effective_halted = bool(
            not ledger.reachable
            or ledger.stop_active
            or ledger.halted
            or self._local_state.halted
            or ledger.expired
            or ledger.revert_budget_status != RevertBudgetStatus.ACTIVE.value
        )

        capital_snapshot = AccountCapitalSnapshot(
            cash_available_usd_micros=resolved_cash if ledger.reachable else 0,
            locked_inventory_usd_micros=resolved_locked,
            pending_commitments_usd_micros=resolved_pending_commitments,
            realized_daily_loss_usd_micros=effective_daily_loss,
            unrealized_drawdown_usd_micros=resolved_drawdown,
            cumulative_loss_usd_micros=effective_cum_loss,
            unresolved_residuals=total_pending,
            halted=effective_halted,
        )
        headroom = compute_deployable_headroom(self.risk_config, capital_snapshot)

        reasons: list[str] = []
        if not ledger.reachable:
            if ledger.error is not None and "timeout" in ledger.error:
                reasons.append("authority_status_timeout")
            elif ledger.error is not None and "unconfigured" in ledger.error:
                reasons.append("authority_account_unconfigured")
            else:
                reasons.append("authority_status_unavailable")
        if ledger.stop_active:
            reasons.append("stop_active")
        if ledger.halted or self._local_state.halted:
            reasons.append("authority_halted")
        if ledger.expired:
            reasons.append("authority_expired")
        if ledger.revert_budget_status != RevertBudgetStatus.ACTIVE.value:
            reasons.append("revert_budget_halted")
        if total_pending > 0:
            reasons.append("unresolved_residuals")
        if (
            self.config.mode != "shadow"
            and ledger.reachable
            and ledger.max_signatures is not None
            and ledger.signatures_used >= ledger.max_signatures
        ):
            reasons.append("signature_budget_exhausted")
        if self.config.mode == "live" and ledger.reachable and not ledger.signing_enabled:
            reasons.append("authority_signing_disabled")
        if headroom.block_reason is not None and headroom.block_reason not in reasons:
            reasons.append(headroom.block_reason)

        unique_reasons = tuple(dict.fromkeys(reasons))
        primary_block_reason = unique_reasons[0] if unique_reasons else None
        deployable_notional = (
            headroom.max_clip_usd_micros
            if primary_block_reason is None and headroom.can_deploy
            else 0
        )
        if primary_block_reason is None and deployable_notional <= 0:
            primary_block_reason = "insufficient_deployable_headroom"
            unique_reasons = (primary_block_reason,)

        can_deploy = primary_block_reason is None and deployable_notional > 0
        release_permitted = self._release_is_open()
        execution_eligible = bool(
            self.config.mode == "live"
            and can_deploy
            and release_permitted
            and ledger.signing_enabled
            and not self.config.rehearsal_loopback_anvil
        )

        return BridgeSyncSnapshot(
            mode=self.config.mode,
            authority_socket=str(self.config.authority_socket),
            risk_config_sha256=self.risk_config.config_sha256,
            policy_digest=ledger.policy_digest,
            authority_reachable=ledger.reachable,
            authority_status=ledger,
            capital_snapshot=capital_snapshot,
            headroom=headroom,
            deployable_notional_usd_micros=deployable_notional,
            can_deploy=can_deploy,
            blocked_reason=primary_block_reason,
            blocked_reasons=unique_reasons,
            halted=effective_halted,
            stop_active=ledger.stop_active,
            pending_count=total_pending,
            execution_eligible=execution_eligible,
            release_permitted=release_permitted,
            tick=tick,
        )

    def _build_proposal(
        self,
        *,
        proposal: AtomicOrIntentProposal | None,
        envelope: EnvelopeProposal | None,
        simulate_fn: Callable[[EnvelopeProposal], DualSimulationProof] | None,
        bridge_status: BridgeSyncSnapshot,
        now_s: int,
    ) -> AtomicOrIntentProposal:
        if proposal is not None and (envelope is not None or simulate_fn is not None):
            raise ValueError("pass either proposal or envelope + simulate_fn, not both")
        if proposal is not None:
            if not isinstance(proposal, AtomicOrIntentProposal):
                raise TypeError("proposal must be an AtomicOrIntentProposal")
            return AtomicOrIntentProposal(
                envelope=proposal.envelope,
                simulation=proposal.simulation,
                now_s=proposal.now_s,
                daily_loss_usd_micros=max(
                    proposal.daily_loss_usd_micros,
                    bridge_status.capital_snapshot.realized_daily_loss_usd_micros,
                ),
                cumulative_loss_usd_micros=max(
                    proposal.cumulative_loss_usd_micros,
                    bridge_status.capital_snapshot.cumulative_loss_usd_micros,
                ),
                revert_budget_status=RevertBudgetStatus(
                    bridge_status.authority_status.revert_budget_status
                ),
                has_unresolved_pending=(
                    proposal.has_unresolved_pending or bridge_status.pending_count > 0
                ),
                rehearsal_loopback_anvil=proposal.rehearsal_loopback_anvil,
            )
        if envelope is None or simulate_fn is None:
            raise ValueError("either proposal or (envelope and simulate_fn) is required")
        if not isinstance(envelope, EnvelopeProposal):
            raise TypeError("envelope must be an EnvelopeProposal")
        simulation = simulate_fn(envelope)
        if not isinstance(simulation, DualSimulationProof):
            raise TypeError("simulate_fn must return a DualSimulationProof")
        return AtomicOrIntentProposal(
            envelope=envelope,
            simulation=simulation,
            now_s=now_s,
            daily_loss_usd_micros=bridge_status.capital_snapshot.realized_daily_loss_usd_micros,
            cumulative_loss_usd_micros=bridge_status.capital_snapshot.cumulative_loss_usd_micros,
            revert_budget_status=RevertBudgetStatus(
                bridge_status.authority_status.revert_budget_status
            ),
            has_unresolved_pending=bridge_status.pending_count > 0,
            rehearsal_loopback_anvil=self.config.rehearsal_loopback_anvil,
        )

    def submit_dual_simulated_intent(
        self,
        *,
        proposal: AtomicOrIntentProposal | None = None,
        envelope: EnvelopeProposal | None = None,
        simulate_fn: Callable[[EnvelopeProposal], DualSimulationProof] | None = None,
        notional_usd_micros: int | None = None,
        broadcast_fn: Callable[[bytes, SubmittedTransaction], str] | None = None,
        receipt_fn: Callable[[str], dict[str, Any] | None] | None = None,
        now_s: int | None = None,
        cash_available_usd_micros: int | None = None,
    ) -> IntentSubmissionResult:
        """Gate, dual-simulate, authorize, broadcast, and reconcile one intent fail-closed."""
        if notional_usd_micros is not None:
            _require_plain_int(notional_usd_micros, "notional_usd_micros", low=1)
        resolved_now = (
            self._now_fn() if now_s is None else _require_plain_int(now_s, "now_s", low=0)
        )

        bridge_status = self.sync_status(
            tick=1,
            cash_available_usd_micros=cash_available_usd_micros,
        )

        # Shadow mode NEVER calls sign_admitted_proposal or broadcast_fn.
        if self.config.mode == "shadow":
            return IntentSubmissionResult(
                status="shadow_blocked",
                mode=self.config.mode,
                admitted=False,
                signed=False,
                broadcast=False,
                reconciled=False,
                halted=bridge_status.halted,
                blocked_reason=bridge_status.blocked_reason or "shadow_mode_no_execution",
                reasons=(bridge_status.blocked_reason or "shadow_mode_no_execution",),
                headroom=bridge_status.headroom,
                execution_eligible=False,
                release_permitted=False,
            )

        if not bridge_status.can_deploy or bridge_status.blocked_reason is not None:
            return IntentSubmissionResult(
                status="blocked",
                mode=self.config.mode,
                admitted=False,
                signed=False,
                broadcast=False,
                reconciled=False,
                halted=bridge_status.halted,
                blocked_reason=bridge_status.blocked_reason,
                reasons=bridge_status.blocked_reasons,
                headroom=bridge_status.headroom,
                execution_eligible=False,
                release_permitted=bridge_status.release_permitted,
            )

        if notional_usd_micros is not None:
            if notional_usd_micros < bridge_status.headroom.min_clip_usd_micros:
                return IntentSubmissionResult(
                    status="blocked",
                    mode=self.config.mode,
                    admitted=False,
                    signed=False,
                    broadcast=False,
                    reconciled=False,
                    halted=bridge_status.halted,
                    blocked_reason="below_min_clip_usd_micros",
                    reasons=("below_min_clip_usd_micros",),
                    headroom=bridge_status.headroom,
                    execution_eligible=False,
                    release_permitted=bridge_status.release_permitted,
                )
            if notional_usd_micros > bridge_status.deployable_notional_usd_micros:
                return IntentSubmissionResult(
                    status="blocked",
                    mode=self.config.mode,
                    admitted=False,
                    signed=False,
                    broadcast=False,
                    reconciled=False,
                    halted=bridge_status.halted,
                    blocked_reason="exceeds_deployable_headroom",
                    reasons=("exceeds_deployable_headroom",),
                    headroom=bridge_status.headroom,
                    execution_eligible=False,
                    release_permitted=bridge_status.release_permitted,
                )

        if self.policy is None or self._client is None:
            return IntentSubmissionResult(
                status="blocked",
                mode=self.config.mode,
                admitted=False,
                signed=False,
                broadcast=False,
                reconciled=False,
                halted=True,
                blocked_reason="authority_policy_required",
                reasons=("authority_policy_required",),
                headroom=bridge_status.headroom,
                execution_eligible=False,
                release_permitted=False,
            )

        effective_simulate_fn = (
            simulate_fn if (simulate_fn is not None or proposal is not None) else self._simulate_fn
        )

        try:
            active_proposal = self._build_proposal(
                proposal=proposal,
                envelope=envelope,
                simulate_fn=effective_simulate_fn,
                bridge_status=bridge_status,
                now_s=resolved_now,
            )
            admission = admit_atomic_or_intent_proposal(
                policy=self.policy,
                proposal=active_proposal.envelope,
                simulation=active_proposal.simulation,
                now_s=active_proposal.now_s,
                daily_loss_usd_micros=active_proposal.daily_loss_usd_micros,
                cumulative_loss_usd_micros=active_proposal.cumulative_loss_usd_micros,
                revert_budget_status=active_proposal.revert_budget_status,
                has_unresolved_pending=active_proposal.has_unresolved_pending,
                rehearsal_loopback_anvil=active_proposal.rehearsal_loopback_anvil,
            )
        except (OSError, ValueError, TypeError, RuntimeError, RpcError) as exc:
            return IntentSubmissionResult(
                status="simulation_or_admission_error",
                mode=self.config.mode,
                admitted=False,
                signed=False,
                broadcast=False,
                reconciled=False,
                halted=bridge_status.halted,
                blocked_reason=f"simulation_failed:{exc}",
                reasons=(f"simulation_failed:{exc}",),
                headroom=bridge_status.headroom,
                execution_eligible=False,
                release_permitted=bridge_status.release_permitted,
            )

        if not admission.admitted:
            return IntentSubmissionResult(
                status="admission_denied",
                mode=self.config.mode,
                admitted=False,
                signed=False,
                broadcast=False,
                reconciled=False,
                halted=bridge_status.halted,
                blocked_reason=admission.reasons[0],
                reasons=admission.reasons,
                proposal_digest=admission.proposal_digest,
                headroom=bridge_status.headroom,
                execution_eligible=False,
                release_permitted=bridge_status.release_permitted,
            )

        # Dry-run verifies authority status, headroom, dual simulation, and client admission
        # without consuming a signing nonce or broadcasting.
        if self.config.mode == "dry_run":
            return IntentSubmissionResult(
                status="dry_run_admitted",
                mode=self.config.mode,
                admitted=True,
                signed=False,
                broadcast=False,
                reconciled=False,
                halted=False,
                blocked_reason="dry_run_mode_no_broadcast",
                reasons=("dry_run_mode_no_broadcast",),
                proposal_digest=admission.proposal_digest,
                headroom=bridge_status.headroom,
                execution_eligible=False,
                release_permitted=False,
            )

        if not active_proposal.rehearsal_loopback_anvil:
            try:
                require_live_release()
            except ValueError as exc:
                return IntentSubmissionResult(
                    status="blocked",
                    mode=self.config.mode,
                    admitted=False,
                    signed=False,
                    broadcast=False,
                    reconciled=False,
                    halted=True,
                    blocked_reason=f"release_closed:{exc}",
                    reasons=(f"release_closed:{exc}",),
                    proposal_digest=admission.proposal_digest,
                    headroom=bridge_status.headroom,
                    execution_eligible=False,
                    release_permitted=False,
                )

        if broadcast_fn is None:
            return IntentSubmissionResult(
                status="blocked",
                mode=self.config.mode,
                admitted=True,
                signed=False,
                broadcast=False,
                reconciled=False,
                halted=True,
                blocked_reason="missing_broadcast_adapter",
                reasons=("missing_broadcast_adapter",),
                proposal_digest=admission.proposal_digest,
                headroom=bridge_status.headroom,
                execution_eligible=False,
                release_permitted=bridge_status.release_permitted,
            )

        fields = envelope_fields_from_proposal(active_proposal)
        try:
            raw_signed = self._client.sign_admitted_proposal(active_proposal, fields)
            submitted = verify_signed_bytes_match_admission(raw_signed, decision=admission)
        except (OSError, ValueError, TypeError, TimeoutError, PermissionError) as exc:
            reason = f"authority_sign_refused_or_ambiguous:{exc}"
            self._latch_local_halt(reason)
            return IntentSubmissionResult(
                status="sign_refused_or_ambiguous",
                mode=self.config.mode,
                admitted=True,
                signed=False,
                broadcast=False,
                reconciled=False,
                halted=True,
                blocked_reason=reason,
                reasons=(reason,),
                proposal_digest=admission.proposal_digest,
                headroom=bridge_status.headroom,
                execution_eligible=False,
                release_permitted=bridge_status.release_permitted,
            )

        tx_hash = submitted.transaction_hash
        self._local_state.pending_txs[tx_hash] = {
            "tx_hash": tx_hash,
            "nonce": submitted.nonce,
            "proposal_digest": admission.proposal_digest,
            "submitted_at_s": resolved_now,
            "settled": False,
            "ambiguous": False,
        }
        self._persist_local_state()

        try:
            observed_hash = broadcast_fn(raw_signed, submitted)
            normalized_obs = normalize_hash32(str(observed_hash), "broadcast_tx_hash")
            if normalized_obs != tx_hash:
                raise ValueError("broadcast tx_hash differs from signed commitment")
        except (OSError, ValueError, TypeError, RuntimeError, TimeoutError) as exc:
            self._local_state.pending_txs[tx_hash]["ambiguous"] = True
            self._latch_local_halt(f"ambiguous_broadcast_stop:{exc}")
            return IntentSubmissionResult(
                status="ambiguous_halted",
                mode=self.config.mode,
                admitted=True,
                signed=True,
                broadcast=False,
                reconciled=False,
                halted=True,
                blocked_reason="ambiguous_broadcast_stop",
                reasons=("ambiguous_broadcast_stop",),
                proposal_digest=admission.proposal_digest,
                tx_hash=tx_hash,
                headroom=bridge_status.headroom,
                execution_eligible=False,
                release_permitted=bridge_status.release_permitted,
            )

        if receipt_fn is None:
            return IntentSubmissionResult(
                status="pending_reconciliation",
                mode=self.config.mode,
                admitted=True,
                signed=True,
                broadcast=True,
                reconciled=False,
                halted=False,
                blocked_reason="pending_receipt_reconciliation",
                reasons=("pending_receipt_reconciliation",),
                proposal_digest=admission.proposal_digest,
                tx_hash=tx_hash,
                headroom=bridge_status.headroom,
                execution_eligible=bridge_status.execution_eligible,
                release_permitted=bridge_status.release_permitted,
            )

        return self._reconcile_single_tx(
            tx_hash=tx_hash,
            proposal_digest=admission.proposal_digest,
            receipt_fn=receipt_fn,
            headroom=bridge_status.headroom,
            execution_eligible=bridge_status.execution_eligible,
            release_permitted=bridge_status.release_permitted,
        )

    def _reconcile_single_tx(
        self,
        *,
        tx_hash: str,
        proposal_digest: str | None,
        receipt_fn: Callable[[str], dict[str, Any] | None],
        headroom: HeadroomAllocation | None,
        execution_eligible: bool,
        release_permitted: bool,
    ) -> IntentSubmissionResult:
        if self._client is None:
            self._latch_local_halt("authority_client_unavailable")
            return IntentSubmissionResult(
                status="pending_unresolved",
                mode=self.config.mode,
                admitted=True,
                signed=True,
                broadcast=True,
                reconciled=False,
                halted=True,
                blocked_reason="authority_client_unavailable",
                reasons=("authority_client_unavailable",),
                proposal_digest=proposal_digest,
                tx_hash=tx_hash,
                headroom=headroom,
                execution_eligible=False,
                release_permitted=False,
            )
        try:
            reconciliation_input = receipt_fn(tx_hash)
            if reconciliation_input is None:
                raise ValueError("dual-source receipt unavailable")
            settlement = self._client.record_settlement(reconciliation_input)
        except (OSError, ValueError, TypeError, RuntimeError, TimeoutError, PermissionError) as exc:
            reason = f"receipt_reconciliation_unresolved:{exc}"
            self._latch_local_halt(reason)
            return IntentSubmissionResult(
                status="pending_unresolved",
                mode=self.config.mode,
                admitted=True,
                signed=True,
                broadcast=True,
                reconciled=False,
                halted=True,
                blocked_reason=reason,
                reasons=(reason,),
                proposal_digest=proposal_digest,
                tx_hash=tx_hash,
                headroom=headroom,
                execution_eligible=False,
                release_permitted=False,
            )

        reconciled = bool(settlement.get("reconciled") is True)
        authority_halted = bool(settlement.get("halted") is True)
        entry = self._local_state.pending_txs.get(tx_hash)
        if entry is not None:
            entry["settled"] = True
            entry["reconciled"] = reconciled
        self._local_state.settled_history.append(
            {
                "tx_hash": tx_hash,
                "reconciled": reconciled,
                "halted": authority_halted,
                "settlement_digest": settlement.get("settlement_digest"),
            }
        )
        if not reconciled or authority_halted:
            self._latch_local_halt("settlement_halt_latched")
        else:
            self._persist_local_state()

        raw_reasons = settlement.get("reasons", ())
        reasons = tuple(str(r) for r in raw_reasons) if isinstance(raw_reasons, list) else ()
        blocked = "settlement_halt_latched" if (not reconciled or authority_halted) else None
        return IntentSubmissionResult(
            status="settled" if (reconciled and not authority_halted) else "settled_halted",
            mode=self.config.mode,
            admitted=True,
            signed=True,
            broadcast=True,
            reconciled=reconciled,
            halted=authority_halted or not reconciled,
            blocked_reason=blocked,
            reasons=reasons,
            proposal_digest=proposal_digest,
            tx_hash=tx_hash,
            settlement=settlement,
            headroom=headroom,
            execution_eligible=execution_eligible
            if (reconciled and not authority_halted)
            else False,
            release_permitted=release_permitted if (reconciled and not authority_halted) else False,
        )

    def reconcile_pending(
        self,
        *,
        receipt_fn: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> ReconciliationPassResult:
        """Reconcile any pending/unresolved authority or local transactions fail-closed."""
        bridge_status = self.sync_status(tick=1)
        if not bridge_status.authority_reachable:
            return ReconciliationPassResult(
                status="authority_unavailable",
                mode=self.config.mode,
                authority_reachable=False,
                halted=True,
                stop_active=bridge_status.stop_active,
                pending_count=bridge_status.pending_count,
                settled_hashes=(),
                unresolved_hashes=tuple(self._local_pending_hashes()),
                settlements=(),
                blocked_reason=bridge_status.blocked_reason,
                headroom=bridge_status.headroom,
            )

        authority_pending = set(bridge_status.authority_status.pending_tx_hashes)
        authority_settled = set(bridge_status.authority_status.settled_tx_hashes)
        local_pending = set(self._local_pending_hashes())
        # Only the authority's own settlement journal may clear a local pending transaction.
        # Absence from the pending list is not proof of settlement; it may indicate state loss.
        for tx_hash in list(local_pending):
            if tx_hash in authority_settled:
                entry = self._local_state.pending_txs.get(tx_hash)
                if entry is not None and entry.get("ambiguous") is not True:
                    entry["settled"] = True
        self._persist_local_state()

        pending_hashes = sorted(authority_pending | set(self._local_pending_hashes()))
        settled_hashes: list[str] = []
        unresolved_hashes: list[str] = []
        settlements: list[dict[str, Any]] = []
        halted = bridge_status.halted

        for tx_hash in pending_hashes:
            if receipt_fn is None or self._client is None:
                unresolved_hashes.append(tx_hash)
                halted = True
                continue
            outcome = self._reconcile_single_tx(
                tx_hash=tx_hash,
                proposal_digest=None,
                receipt_fn=receipt_fn,
                headroom=bridge_status.headroom,
                execution_eligible=False,
                release_permitted=False,
            )
            if outcome.settlement is not None:
                settlements.append(outcome.settlement)
                settled_hashes.append(tx_hash)
            else:
                unresolved_hashes.append(tx_hash)
            if outcome.halted:
                halted = True

        if bridge_status.authority_status.signing_unresolved:
            halted = True
            self._latch_local_halt("authority_signing_unresolved")

        unresolved_count = len(unresolved_hashes) + (
            1 if bridge_status.authority_status.signing_unresolved else 0
        )
        blocked_reason = bridge_status.blocked_reason
        if unresolved_count > 0 and blocked_reason is None:
            blocked_reason = "unresolved_residuals"
        elif halted and blocked_reason is None:
            blocked_reason = "authority_halted"

        status_label = "clean" if (unresolved_count == 0 and not halted) else "unresolved_or_halted"
        return ReconciliationPassResult(
            status=status_label,
            mode=self.config.mode,
            authority_reachable=True,
            halted=halted,
            stop_active=bridge_status.stop_active,
            pending_count=unresolved_count,
            settled_hashes=tuple(settled_hashes),
            unresolved_hashes=tuple(unresolved_hashes),
            settlements=tuple(settlements),
            blocked_reason=blocked_reason,
            headroom=bridge_status.headroom,
        )

    @staticmethod
    def _with_tick_outcomes(
        snapshot: BridgeSyncSnapshot,
        *,
        last_submission: dict[str, Any] | None,
        last_reconciliation: dict[str, Any] | None,
    ) -> BridgeSyncSnapshot:
        if last_submission is None and last_reconciliation is None:
            return snapshot
        return BridgeSyncSnapshot(
            mode=snapshot.mode,
            authority_socket=snapshot.authority_socket,
            risk_config_sha256=snapshot.risk_config_sha256,
            policy_digest=snapshot.policy_digest,
            authority_reachable=snapshot.authority_reachable,
            authority_status=snapshot.authority_status,
            capital_snapshot=snapshot.capital_snapshot,
            headroom=snapshot.headroom,
            deployable_notional_usd_micros=snapshot.deployable_notional_usd_micros,
            can_deploy=snapshot.can_deploy,
            blocked_reason=snapshot.blocked_reason,
            blocked_reasons=snapshot.blocked_reasons,
            halted=snapshot.halted,
            stop_active=snapshot.stop_active,
            pending_count=snapshot.pending_count,
            execution_eligible=snapshot.execution_eligible,
            release_permitted=snapshot.release_permitted,
            tick=snapshot.tick,
            schema=snapshot.schema,
            last_submission=last_submission,
            last_reconciliation=last_reconciliation,
        )

    def run_tick(
        self,
        *,
        tick: int = 1,
        candidate_fn: Callable[[BridgeSyncSnapshot], StrategyCandidateIntent | None] | None = None,
        simulate_fn: Callable[[EnvelopeProposal], DualSimulationProof] | None = None,
        broadcast_fn: Callable[[bytes, SubmittedTransaction], str] | None = None,
        receipt_fn: Callable[[str], dict[str, Any] | None] | None = None,
        now_s: int | None = None,
        cash_available_usd_micros: int | None = None,
        locked_inventory_usd_micros: int | None = None,
        pending_commitments_usd_micros: int | None = None,
        unrealized_drawdown_usd_micros: int | None = None,
    ) -> BridgeSyncSnapshot:
        """Execute one synchronized tick: reconcile pending, evaluate candidate, and submit."""
        snapshot = self.sync_status(
            tick=tick,
            cash_available_usd_micros=cash_available_usd_micros,
            locked_inventory_usd_micros=locked_inventory_usd_micros,
            pending_commitments_usd_micros=pending_commitments_usd_micros,
            unrealized_drawdown_usd_micros=unrealized_drawdown_usd_micros,
        )
        last_reconciliation: dict[str, Any] | None = None
        last_submission: dict[str, Any] | None = None

        if snapshot.pending_count > 0 and receipt_fn is not None:
            reconciliation = self.reconcile_pending(receipt_fn=receipt_fn)
            last_reconciliation = reconciliation.to_dict()
            snapshot = self.sync_status(
                tick=tick,
                cash_available_usd_micros=cash_available_usd_micros,
                locked_inventory_usd_micros=locked_inventory_usd_micros,
                pending_commitments_usd_micros=pending_commitments_usd_micros,
                unrealized_drawdown_usd_micros=unrealized_drawdown_usd_micros,
            )

        if candidate_fn is not None and (snapshot.can_deploy or self.config.mode == "shadow"):
            candidate = candidate_fn(snapshot)
            if candidate is not None:
                if not isinstance(candidate, StrategyCandidateIntent):
                    raise TypeError("candidate_fn must return a StrategyCandidateIntent or None")
                eff_cash = (
                    candidate.cash_available_usd_micros
                    if candidate.cash_available_usd_micros is not None
                    else cash_available_usd_micros
                )
                eff_simulate = (
                    None
                    if candidate.proposal is not None
                    else (
                        candidate.simulate_fn if candidate.simulate_fn is not None else simulate_fn
                    )
                )
                submission = self.submit_dual_simulated_intent(
                    proposal=candidate.proposal,
                    envelope=candidate.envelope,
                    simulate_fn=eff_simulate,
                    notional_usd_micros=candidate.notional_usd_micros,
                    broadcast_fn=broadcast_fn,
                    receipt_fn=receipt_fn,
                    now_s=now_s,
                    cash_available_usd_micros=eff_cash,
                )
                last_submission = submission.to_dict()
                snapshot = self.sync_status(
                    tick=tick,
                    cash_available_usd_micros=eff_cash,
                    locked_inventory_usd_micros=locked_inventory_usd_micros,
                    pending_commitments_usd_micros=pending_commitments_usd_micros,
                    unrealized_drawdown_usd_micros=unrealized_drawdown_usd_micros,
                )

        return self._with_tick_outcomes(
            snapshot,
            last_submission=last_submission,
            last_reconciliation=last_reconciliation,
        )

    def run_loop(
        self,
        *,
        out_json: Path | None = None,
        stop_requested: Callable[[], bool] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        candidate_fn: Callable[[BridgeSyncSnapshot], StrategyCandidateIntent | None] | None = None,
        simulate_fn: Callable[[EnvelopeProposal], DualSimulationProof] | None = None,
        broadcast_fn: Callable[[bytes, SubmittedTransaction], str] | None = None,
        receipt_fn: Callable[[str], dict[str, Any] | None] | None = None,
        now_s: int | None = None,
    ) -> list[BridgeSyncSnapshot]:
        """Run bounded or continuous strategy ticks, persisting deterministic JSON."""
        sleeper = time.sleep if sleep_fn is None else sleep_fn
        snapshots: list[BridgeSyncSnapshot] = []
        tick = 0
        while True:
            if stop_requested is not None and stop_requested():
                break
            tick += 1
            snapshot = self.run_tick(
                tick=tick,
                candidate_fn=candidate_fn,
                simulate_fn=simulate_fn,
                broadcast_fn=broadcast_fn,
                receipt_fn=receipt_fn,
                now_s=now_s,
            )
            snapshots.append(snapshot)
            if out_json is not None:
                atomic_write_json_file(out_json, snapshot.to_dict(), mode=0o600)
            if self.config.max_ticks is not None and tick >= self.config.max_ticks:
                break
            if stop_requested is not None and stop_requested():
                break
            sleeper(self.config.poll_interval_ms / 1000.0)
        return snapshots


__all__ = [
    "STRATEGY_DAEMON_SCHEMA",
    "STRATEGY_STATE_SCHEMA",
    "VALID_MODES",
    "AuthorityLedgerView",
    "AuthoritySocketProtocol",
    "BridgeSyncSnapshot",
    "DualRpcEnvelopeSimulator",
    "IntentSubmissionResult",
    "ProfitStrategyBridge",
    "ProfitStrategyDaemonConfig",
    "ReconciliationPassResult",
    "StrategyCandidateIntent",
    "StrategyMode",
    "UnixAtomicAuthorityClient",
    "atomic_write_json_file",
    "simulate_atomic_or_intent_envelope_dual",
]
