"""Bounded full-envelope Unix socket authority for atomic cycle and intent vault execution."""

import json
import math
import os
import re
import socket
import stat
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

from ..execution.lp_signer_daemon import prepare_endpoint, sign_digest
from ..models.common import (
    canonical_json,
    normalize_address,
    normalize_hash32,
    normalize_hex,
    sha256_hex,
)
from ..risk.revert_budget import (
    AttemptOutcome,
    ReceiptLog,
    RevertBudgetStatus,
    RollingRevertBudgetPolicy,
    RollingRevertBudgetTracker,
)
from .atomic_authority import (
    AdmissionDecision,
    AtomicExecutionPolicy,
    DualReceiptObservation,
    DualSimulationProof,
    EnvelopeProposal,
    TargetKind,
    admit_atomic_or_intent_proposal,
    reconcile_atomic_or_intent_receipt,
    verify_signed_bytes_match_admission,
)
from .authority_socket import fields_json
from .chain import sign_transaction

PROFIT_AUTHORITY_SCHEMA: Final[str] = "oasis.profit-engine-authority.v1"
PROFIT_AUTHORITY_STATE_SCHEMA: Final[str] = "oasis.profit-authority-state.v1"
PROFIT_AUTHORITY_INFRA_SCHEMA: Final[str] = "oasis-profit-authority-infra.v1"
MAX_PROFIT_FRAME: Final[int] = 65_536

_DEFAULT_REVERT_POLICY: Final[RollingRevertBudgetPolicy] = RollingRevertBudgetPolicy(
    max_hourly_gas_burn_usd_micros=5_000_000,
    max_deep_reverts_per_hour=2,
    max_early_abort_gas=65_000,
    window_seconds=3_600,
)
_FORBIDDEN_PROVIDER_MARKERS: Final[tuple[str, ...]] = (
    "127.0.0.1",
    "localhost",
    "::1",
    "unused.invalid",
    "example.invalid",
    "placeholder",
    "@",
)
_RAW_HEX_KEY_RE: Final[re.Pattern[str]] = re.compile(r"0x[0-9a-fA-F]{64}")
_POLICY_CONFIG_KEYS: Final[frozenset[str]] = frozenset(
    {
        "chain_id",
        "operator_account",
        "executor_address",
        "executor_code_hash",
        "intent_vault_address",
        "intent_vault_code_hash",
        "allowed_pools",
        "allowed_routers",
        "allowed_reactors",
        "allowed_profit_tokens",
        "max_capital_usd_micros",
        "max_daily_loss_usd_micros",
        "max_cumulative_loss_usd_micros",
        "max_gas_limit",
        "max_fee_per_gas_wei",
        "max_priority_fee_wei",
        "max_deadline_horizon_s",
        "release_gate_open",
        "revert_budget",
        "isolated_key_volume",
        "primary_rpc_provider",
        "secondary_rpc_provider",
        "secondary_rpc_verified",
    }
)
_REVERT_POLICY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "max_hourly_gas_burn_usd_micros",
        "max_deep_reverts_per_hour",
        "max_early_abort_gas",
        "window_seconds",
    }
)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant not permitted: {value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite float not permitted")
    return parsed


def strict_json_loads(raw: bytes) -> object:
    """Decode UTF-8 JSON bytes while rejecting duplicate keys and non-finite constants."""
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
        parse_float=_finite_float,
    )


def _require_int(value: object, name: str, *, low: int | None = None) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be a plain int")
    if low is not None and value < low:
        raise ValueError(f"{name} must be >= {low}")
    return value


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be a plain bool")
    return value


def _require_str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("profit authority exchange deadline expired")
    return remaining


def read_frame(
    connection: socket.socket,
    *,
    deadline: float | None = None,
    idle_timeout: float = 5.0,
    max_bytes: int = MAX_PROFIT_FRAME,
) -> bytes:
    """Read exactly one newline-delimited JSON frame within `max_bytes` and `deadline`."""
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_PROFIT_FRAME:
        raise ValueError("max_bytes out of bounds")
    result = bytearray()
    while len(result) <= max_bytes:
        if deadline is not None:
            connection.settimeout(min(idle_timeout, _remaining(deadline)))
        chunk = connection.recv(min(4096, max_bytes + 1 - len(result)))
        if deadline is not None:
            _remaining(deadline)
        if not chunk:
            raise ValueError("incomplete profit authority frame")
        result.extend(chunk)
        if b"\n" in result:
            if result[-1:] != b"\n" or result.count(b"\n") != 1:
                raise ValueError("one frame per connection required")
            return bytes(result[:-1])
    raise ValueError("profit authority frame exceeds bound")


def validate_socket_endpoint(endpoint: Path) -> Path:
    """Verify endpoint is an absolute non-symlink 0600 socket in a 0700 owned directory."""
    if not isinstance(endpoint, Path):
        raise TypeError("profit authority endpoint must be a Path")
    if not endpoint.is_absolute() or endpoint.is_symlink() or endpoint != endpoint.resolve():
        raise ValueError("profit authority endpoint cannot traverse a symlink")
    if len(os.fsencode(endpoint)) > 100:
        raise ValueError("profit authority socket path exceeds Unix socket length bound")
    parent = endpoint.parent.stat()
    info = endpoint.lstat()
    uid = os.getuid()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != uid
        or stat.S_IMODE(parent.st_mode) != 0o700
        or not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != uid
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ValueError("profit authority socket must be private and owned")
    return endpoint


def _validate_private_dir(directory: Path) -> Path:
    if not isinstance(directory, Path):
        raise TypeError("state_dir must be a Path")
    if (
        not directory.is_absolute()
        or directory.is_symlink()
        or directory != directory.resolve()
        or not directory.is_dir()
    ):
        raise ValueError("profit authority state_dir must be an existing non-symlink directory")
    info = directory.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("profit authority state_dir must be owned and mode 0700")
    return directory


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    if path.is_symlink():
        raise ValueError("refusing to overwrite symlink state file")
    encoded = canonical_json(payload) + b"\n"
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=".profit_auth_",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
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


@dataclass(frozen=True, slots=True, kw_only=True)
class AtomicOrIntentProposal:
    """Bundled envelope proposal, dual-provider simulation proof, and clock/risk context."""

    envelope: EnvelopeProposal
    simulation: DualSimulationProof
    now_s: int
    daily_loss_usd_micros: int = 0
    cumulative_loss_usd_micros: int = 0
    revert_budget_status: RevertBudgetStatus = RevertBudgetStatus.ACTIVE
    has_unresolved_pending: bool = False
    rehearsal_loopback_anvil: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.envelope, EnvelopeProposal):
            raise TypeError("envelope must be an EnvelopeProposal")
        if not isinstance(self.simulation, DualSimulationProof):
            raise TypeError("simulation must be a DualSimulationProof")
        _require_int(self.now_s, "now_s", low=0)
        _require_int(self.daily_loss_usd_micros, "daily_loss_usd_micros", low=0)
        _require_int(self.cumulative_loss_usd_micros, "cumulative_loss_usd_micros", low=0)
        if not isinstance(self.revert_budget_status, RevertBudgetStatus):
            raise TypeError("revert_budget_status must be a RevertBudgetStatus")
        _require_bool(self.has_unresolved_pending, "has_unresolved_pending")
        _require_bool(self.rehearsal_loopback_anvil, "rehearsal_loopback_anvil")

    @property
    def nonce(self) -> int:
        return self.envelope.nonce

    @property
    def account(self) -> str:
        return self.envelope.account


def envelope_fields_from_proposal(
    proposal: AtomicOrIntentProposal | EnvelopeProposal,
) -> list[object]:
    """Build canonical 9-element EIP-1559 unsigned transaction fields from a proposal."""
    env = proposal.envelope if isinstance(proposal, AtomicOrIntentProposal) else proposal
    if not isinstance(env, EnvelopeProposal):
        raise TypeError("EnvelopeProposal or AtomicOrIntentProposal required")
    return [
        env.chain_id,
        env.nonce,
        env.max_priority_fee_wei,
        env.max_fee_per_gas_wei,
        env.gas_limit,
        bytes.fromhex(env.target[2:]),
        env.value_wei,
        env.calldata,
        [],
    ]


def _validate_envelope_fields_match(fields: object, env: EnvelopeProposal) -> list[object]:
    if not isinstance(fields, list) or len(fields) != 9:
        raise ValueError("unsigned EIP-1559 envelope fields required")
    if fields[8] != []:
        raise ValueError("envelope access list must be empty")
    expected_target = bytes.fromhex(env.target[2:])
    if (
        type(fields[0]) is not int
        or fields[0] != env.chain_id
        or type(fields[1]) is not int
        or fields[1] != env.nonce
        or type(fields[2]) is not int
        or fields[2] != env.max_priority_fee_wei
        or type(fields[3]) is not int
        or fields[3] != env.max_fee_per_gas_wei
        or type(fields[4]) is not int
        or fields[4] != env.gas_limit
        or not isinstance(fields[5], bytes)
        or fields[5] != expected_target
        or type(fields[6]) is not int
        or fields[6] != env.value_wei
        or not isinstance(fields[7], bytes)
        or fields[7] != env.calldata
    ):
        raise ValueError("envelope fields differ from proposal")
    return list(fields)


def _decode_canonical_hex_bytes(value: object, name: str) -> bytes:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError(f"{name} must be canonical 0x-prefixed hex")
    normalized = normalize_hex(value)
    if normalized != value:
        raise ValueError(f"{name} must be canonical lowercase hex")
    return bytes.fromhex(normalized[2:])


def proposal_to_dict(proposal: AtomicOrIntentProposal) -> dict[str, Any]:
    """Serialize `AtomicOrIntentProposal` cleanly with 0x-hex calldata."""
    if not isinstance(proposal, AtomicOrIntentProposal):
        raise TypeError("AtomicOrIntentProposal required")
    env = proposal.envelope
    return {
        "envelope": {
            "chain_id": env.chain_id,
            "account": env.account,
            "nonce": env.nonce,
            "target": env.target,
            "target_observed_code_hash": env.target_observed_code_hash,
            "value_wei": env.value_wei,
            "calldata": "0x" + env.calldata.hex(),
            "gas_limit": env.gas_limit,
            "max_fee_per_gas_wei": env.max_fee_per_gas_wei,
            "max_priority_fee_wei": env.max_priority_fee_wei,
            "profit_token": env.profit_token,
            "min_net_profit_raw": env.min_net_profit_raw,
            "referenced_pools": list(env.referenced_pools),
            "referenced_routers": list(env.referenced_routers),
            "referenced_reactor": env.referenced_reactor,
            "deadline_s": env.deadline_s,
        },
        "simulation": asdict(proposal.simulation),
        "now_s": proposal.now_s,
        "daily_loss_usd_micros": proposal.daily_loss_usd_micros,
        "cumulative_loss_usd_micros": proposal.cumulative_loss_usd_micros,
        "revert_budget_status": proposal.revert_budget_status.value,
        "has_unresolved_pending": proposal.has_unresolved_pending,
        "rehearsal_loopback_anvil": proposal.rehearsal_loopback_anvil,
    }


def proposal_from_dict(data: object) -> AtomicOrIntentProposal:
    """Strictly parse and validate `AtomicOrIntentProposal` from a JSON object."""
    expected_top = {
        "envelope",
        "simulation",
        "now_s",
        "daily_loss_usd_micros",
        "cumulative_loss_usd_micros",
        "revert_budget_status",
        "has_unresolved_pending",
        "rehearsal_loopback_anvil",
    }
    if not isinstance(data, dict) or set(data) != expected_top:
        raise ValueError("malformed proposal payload")

    raw_env = data["envelope"]
    expected_env = {
        "chain_id",
        "account",
        "nonce",
        "target",
        "target_observed_code_hash",
        "value_wei",
        "calldata",
        "gas_limit",
        "max_fee_per_gas_wei",
        "max_priority_fee_wei",
        "profit_token",
        "min_net_profit_raw",
        "referenced_pools",
        "referenced_routers",
        "referenced_reactor",
        "deadline_s",
    }
    if not isinstance(raw_env, dict) or set(raw_env) != expected_env:
        raise ValueError("malformed envelope payload")
    for list_key in ("referenced_pools", "referenced_routers"):
        raw_list = raw_env[list_key]
        if not isinstance(raw_list, list) or any(not isinstance(item, str) for item in raw_list):
            raise ValueError(f"{list_key} must be a list of strings")
    reactor = raw_env["referenced_reactor"]
    if reactor is not None and not isinstance(reactor, str):
        raise ValueError("referenced_reactor must be a string or null")

    envelope = EnvelopeProposal(
        chain_id=_require_int(raw_env["chain_id"], "chain_id"),
        account=_require_str(raw_env["account"], "account"),
        nonce=_require_int(raw_env["nonce"], "nonce"),
        target=_require_str(raw_env["target"], "target"),
        target_observed_code_hash=_require_str(
            raw_env["target_observed_code_hash"], "target_observed_code_hash"
        ),
        value_wei=_require_int(raw_env["value_wei"], "value_wei"),
        calldata=_decode_canonical_hex_bytes(raw_env["calldata"], "calldata"),
        gas_limit=_require_int(raw_env["gas_limit"], "gas_limit"),
        max_fee_per_gas_wei=_require_int(raw_env["max_fee_per_gas_wei"], "max_fee_per_gas_wei"),
        max_priority_fee_wei=_require_int(raw_env["max_priority_fee_wei"], "max_priority_fee_wei"),
        profit_token=_require_str(raw_env["profit_token"], "profit_token"),
        min_net_profit_raw=_require_int(raw_env["min_net_profit_raw"], "min_net_profit_raw"),
        referenced_pools=tuple(raw_env["referenced_pools"]),
        referenced_routers=tuple(raw_env["referenced_routers"]),
        referenced_reactor=reactor,
        deadline_s=_require_int(raw_env["deadline_s"], "deadline_s"),
    )

    raw_sim = data["simulation"]
    expected_sim = {
        "block_number",
        "block_hash",
        "primary_source",
        "secondary_source",
        "primary_reverted",
        "secondary_reverted",
        "primary_early_aborted",
        "secondary_early_aborted",
        "primary_gas_used",
        "secondary_gas_used",
        "primary_profit_delta_raw",
        "secondary_profit_delta_raw",
        "l2_gas_fee_wei",
        "l1_data_fee_wei",
        "simulated_profit_value_wei",
    }
    if not isinstance(raw_sim, dict) or set(raw_sim) != expected_sim:
        raise ValueError("malformed simulation payload")
    simulation = DualSimulationProof(
        block_number=_require_int(raw_sim["block_number"], "block_number"),
        block_hash=_require_str(raw_sim["block_hash"], "block_hash"),
        primary_source=_require_str(raw_sim["primary_source"], "primary_source"),
        secondary_source=_require_str(raw_sim["secondary_source"], "secondary_source"),
        primary_reverted=_require_bool(raw_sim["primary_reverted"], "primary_reverted"),
        secondary_reverted=_require_bool(raw_sim["secondary_reverted"], "secondary_reverted"),
        primary_early_aborted=_require_bool(
            raw_sim["primary_early_aborted"], "primary_early_aborted"
        ),
        secondary_early_aborted=_require_bool(
            raw_sim["secondary_early_aborted"], "secondary_early_aborted"
        ),
        primary_gas_used=_require_int(raw_sim["primary_gas_used"], "primary_gas_used"),
        secondary_gas_used=_require_int(raw_sim["secondary_gas_used"], "secondary_gas_used"),
        primary_profit_delta_raw=_require_int(
            raw_sim["primary_profit_delta_raw"], "primary_profit_delta_raw"
        ),
        secondary_profit_delta_raw=_require_int(
            raw_sim["secondary_profit_delta_raw"], "secondary_profit_delta_raw"
        ),
        l2_gas_fee_wei=_require_int(raw_sim["l2_gas_fee_wei"], "l2_gas_fee_wei"),
        l1_data_fee_wei=_require_int(raw_sim["l1_data_fee_wei"], "l1_data_fee_wei"),
        simulated_profit_value_wei=_require_int(
            raw_sim["simulated_profit_value_wei"], "simulated_profit_value_wei"
        ),
    )

    rb_raw = data["revert_budget_status"]
    if not isinstance(rb_raw, str) or rb_raw not in (
        RevertBudgetStatus.ACTIVE.value,
        RevertBudgetStatus.HALT.value,
    ):
        raise ValueError("invalid revert_budget_status")

    return AtomicOrIntentProposal(
        envelope=envelope,
        simulation=simulation,
        now_s=_require_int(data["now_s"], "now_s", low=0),
        daily_loss_usd_micros=_require_int(
            data["daily_loss_usd_micros"], "daily_loss_usd_micros", low=0
        ),
        cumulative_loss_usd_micros=_require_int(
            data["cumulative_loss_usd_micros"], "cumulative_loss_usd_micros", low=0
        ),
        revert_budget_status=RevertBudgetStatus(rb_raw),
        has_unresolved_pending=_require_bool(
            data["has_unresolved_pending"], "has_unresolved_pending"
        ),
        rehearsal_loopback_anvil=_require_bool(
            data["rehearsal_loopback_anvil"], "rehearsal_loopback_anvil"
        ),
    )


def _decode_wire_fields(raw_fields: object) -> list[object]:
    if not isinstance(raw_fields, list) or len(raw_fields) != 9:
        raise ValueError("unsigned envelope fields required")
    if raw_fields[8] != []:
        raise ValueError("envelope access list must be empty")
    for idx in (0, 1, 2, 3, 4, 6):
        _require_int(raw_fields[idx], f"fields[{idx}]", low=0)
    target = _decode_canonical_hex_bytes(raw_fields[5], "fields[5]")
    if len(target) != 20:
        raise ValueError("envelope target must be 20 bytes")
    calldata = _decode_canonical_hex_bytes(raw_fields[7], "fields[7]")
    return [*raw_fields[:5], target, raw_fields[6], calldata, []]


def _receipt_from_dict(raw: object, label: str) -> DualReceiptObservation:
    expected = {
        "source",
        "tx_hash",
        "block_number",
        "block_hash",
        "status",
        "gas_used",
        "effective_gas_price_wei",
        "l1_fee_wei",
        "logs",
    }
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    keys = set(raw)
    if keys != expected and keys != (expected - {"logs"}):
        raise ValueError(f"{label} has unexpected keys")
    raw_logs = raw.get("logs", [])
    if not isinstance(raw_logs, list):
        raise ValueError(f"{label}.logs must be a list")
    logs: list[ReceiptLog] = []
    for idx, entry in enumerate(raw_logs):
        if not isinstance(entry, dict) or not set(entry).issubset({"address", "topics", "data"}):
            raise ValueError(f"{label}.logs[{idx}] malformed")
        if "address" not in entry or "topics" not in entry:
            raise ValueError(f"{label}.logs[{idx}] missing address or topics")
        topics = entry["topics"]
        if not isinstance(topics, list) or any(not isinstance(t, str) for t in topics):
            raise ValueError(f"{label}.logs[{idx}].topics must be a string list")
        log_data = entry.get("data", "0x")
        if not isinstance(log_data, str):
            raise ValueError(f"{label}.logs[{idx}].data must be a string")
        logs.append(
            ReceiptLog(
                address=str(entry["address"]),
                topics=tuple(topics),
                data=log_data,
            )
        )
    return DualReceiptObservation(
        source=_require_str(raw["source"], f"{label}.source"),
        tx_hash=_require_str(raw["tx_hash"], f"{label}.tx_hash"),
        block_number=_require_int(raw["block_number"], f"{label}.block_number", low=0),
        block_hash=_require_str(raw["block_hash"], f"{label}.block_hash"),
        status=_require_int(raw["status"], f"{label}.status", low=0),
        gas_used=_require_int(raw["gas_used"], f"{label}.gas_used", low=0),
        effective_gas_price_wei=_require_int(
            raw["effective_gas_price_wei"], f"{label}.effective_gas_price_wei", low=0
        ),
        l1_fee_wei=_require_int(raw["l1_fee_wei"], f"{label}.l1_fee_wei", low=0),
        logs=tuple(logs),
    )


def _policy_digest(policy: AtomicExecutionPolicy) -> str:
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
    }
    return sha256_hex(canonical_json(payload))


def _decision_to_dict(decision: AdmissionDecision) -> dict[str, Any]:
    return {
        "admitted": decision.admitted,
        "target_kind": decision.target_kind,
        "calldata_sha256": decision.calldata_sha256,
        "proposal_digest": decision.proposal_digest,
        "reasons": list(decision.reasons),
        "execution_eligible": decision.execution_eligible,
        "chain_id": decision.chain_id,
        "account": decision.account,
        "nonce": decision.nonce,
        "target": decision.target,
        "value_wei": decision.value_wei,
        "gas_limit": decision.gas_limit,
        "max_fee_per_gas_wei": decision.max_fee_per_gas_wei,
        "max_priority_fee_wei": decision.max_priority_fee_wei,
        "profit_token": decision.profit_token,
        "min_net_profit_raw": decision.min_net_profit_raw,
        "simulation_block_number": decision.simulation_block_number,
        "simulation_block_hash": decision.simulation_block_hash,
    }


def _decision_from_dict(data: object) -> AdmissionDecision:
    expected = {
        "admitted",
        "target_kind",
        "calldata_sha256",
        "proposal_digest",
        "reasons",
        "execution_eligible",
        "chain_id",
        "account",
        "nonce",
        "target",
        "value_wei",
        "gas_limit",
        "max_fee_per_gas_wei",
        "max_priority_fee_wei",
        "profit_token",
        "min_net_profit_raw",
        "simulation_block_number",
        "simulation_block_hash",
    }
    if not isinstance(data, dict) or set(data) != expected:
        raise ValueError("stored admission decision malformed")
    reasons_raw = data["reasons"]
    if not isinstance(reasons_raw, list) or any(not isinstance(r, str) for r in reasons_raw):
        raise ValueError("stored admission decision reasons malformed")
    raw_kind = data["target_kind"]
    if raw_kind not in ("atomic_executor", "intent_vault", "rejected"):
        raise ValueError("stored admission decision target_kind malformed")
    target_kind: TargetKind = raw_kind
    return AdmissionDecision(
        admitted=_require_bool(data["admitted"], "admitted"),
        target_kind=target_kind,
        calldata_sha256=_require_str(data["calldata_sha256"], "calldata_sha256"),
        proposal_digest=_require_str(data["proposal_digest"], "proposal_digest"),
        reasons=tuple(reasons_raw),
        execution_eligible=_require_bool(data["execution_eligible"], "execution_eligible"),
        chain_id=_require_int(data["chain_id"], "chain_id"),
        account=_require_str(data["account"], "account"),
        nonce=_require_int(data["nonce"], "nonce"),
        target=_require_str(data["target"], "target"),
        value_wei=_require_int(data["value_wei"], "value_wei"),
        gas_limit=_require_int(data["gas_limit"], "gas_limit"),
        max_fee_per_gas_wei=_require_int(data["max_fee_per_gas_wei"], "max_fee_per_gas_wei"),
        max_priority_fee_wei=_require_int(data["max_priority_fee_wei"], "max_priority_fee_wei"),
        profit_token=_require_str(data["profit_token"], "profit_token"),
        min_net_profit_raw=_require_int(data["min_net_profit_raw"], "min_net_profit_raw"),
        simulation_block_number=_require_int(
            data["simulation_block_number"], "simulation_block_number"
        ),
        simulation_block_hash=_require_str(data["simulation_block_hash"], "simulation_block_hash"),
    )


@dataclass(frozen=True, slots=True)
class LoadedPolicyConfig:
    """Validated policy file contents without any signing material."""

    policy: AtomicExecutionPolicy
    revert_policy: RollingRevertBudgetPolicy
    isolated_key_volume: str | None = None
    primary_rpc_provider: str | None = None
    secondary_rpc_provider: str | None = None
    secondary_rpc_verified: bool = False


def load_atomic_execution_policy(path: Path) -> LoadedPolicyConfig:
    """Load `AtomicExecutionPolicy` and optional metadata fail-closed (`extra=forbid`)."""
    if not isinstance(path, Path):
        raise TypeError("policy config path must be a Path")
    if path.is_symlink() or not path.is_file():
        raise ValueError("policy config must be a regular non-symlink file")
    decoded = strict_json_loads(path.read_bytes())
    if not isinstance(decoded, dict):
        raise ValueError("policy config root must be a JSON object")
    unknown = set(decoded) - _POLICY_CONFIG_KEYS
    if unknown:
        raise ValueError(f"unknown policy config keys: {sorted(unknown)}")

    def req_addr_set(key: str) -> frozenset[str]:
        val = decoded.get(key)
        if not isinstance(val, list) or any(not isinstance(item, str) for item in val):
            raise ValueError(f"policy config {key} must be a list of address strings")
        return frozenset(val)

    kwargs: dict[str, Any] = {
        "operator_account": _require_str(decoded.get("operator_account"), "operator_account"),
        "executor_address": _require_str(decoded.get("executor_address"), "executor_address"),
        "executor_code_hash": _require_str(decoded.get("executor_code_hash"), "executor_code_hash"),
        "intent_vault_address": _require_str(
            decoded.get("intent_vault_address"), "intent_vault_address"
        ),
        "intent_vault_code_hash": _require_str(
            decoded.get("intent_vault_code_hash"), "intent_vault_code_hash"
        ),
        "allowed_pools": req_addr_set("allowed_pools"),
        "allowed_routers": req_addr_set("allowed_routers"),
        "allowed_reactors": req_addr_set("allowed_reactors"),
        "allowed_profit_tokens": req_addr_set("allowed_profit_tokens"),
    }
    for int_key in (
        "chain_id",
        "max_capital_usd_micros",
        "max_daily_loss_usd_micros",
        "max_cumulative_loss_usd_micros",
        "max_gas_limit",
        "max_fee_per_gas_wei",
        "max_priority_fee_wei",
        "max_deadline_horizon_s",
    ):
        if int_key in decoded:
            kwargs[int_key] = _require_int(decoded[int_key], int_key)
    if "release_gate_open" in decoded:
        kwargs["release_gate_open"] = _require_bool(
            decoded["release_gate_open"], "release_gate_open"
        )
    policy = AtomicExecutionPolicy(**kwargs)

    revert_policy = _DEFAULT_REVERT_POLICY
    if "revert_budget" in decoded:
        rb = decoded["revert_budget"]
        if not isinstance(rb, dict) or set(rb) - _REVERT_POLICY_KEYS:
            raise ValueError("malformed revert_budget section")
        revert_policy = RollingRevertBudgetPolicy(
            max_hourly_gas_burn_usd_micros=_require_int(
                rb.get(
                    "max_hourly_gas_burn_usd_micros",
                    _DEFAULT_REVERT_POLICY.max_hourly_gas_burn_usd_micros,
                ),
                "max_hourly_gas_burn_usd_micros",
            ),
            max_deep_reverts_per_hour=_require_int(
                rb.get(
                    "max_deep_reverts_per_hour",
                    _DEFAULT_REVERT_POLICY.max_deep_reverts_per_hour,
                ),
                "max_deep_reverts_per_hour",
            ),
            max_early_abort_gas=_require_int(
                rb.get("max_early_abort_gas", _DEFAULT_REVERT_POLICY.max_early_abort_gas),
                "max_early_abort_gas",
            ),
            window_seconds=_require_int(
                rb.get("window_seconds", _DEFAULT_REVERT_POLICY.window_seconds),
                "window_seconds",
            ),
        )

    key_vol = decoded.get("isolated_key_volume")
    if key_vol is not None:
        key_vol = _require_str(key_vol, "isolated_key_volume")
    primary_rpc = decoded.get("primary_rpc_provider")
    if primary_rpc is not None:
        primary_rpc = _require_str(primary_rpc, "primary_rpc_provider")
    secondary_rpc = decoded.get("secondary_rpc_provider")
    if secondary_rpc is not None:
        secondary_rpc = _require_str(secondary_rpc, "secondary_rpc_provider")
    secondary_ok = False
    if "secondary_rpc_verified" in decoded:
        secondary_ok = _require_bool(decoded["secondary_rpc_verified"], "secondary_rpc_verified")

    return LoadedPolicyConfig(
        policy=policy,
        revert_policy=revert_policy,
        isolated_key_volume=key_vol,
        primary_rpc_provider=primary_rpc,
        secondary_rpc_provider=secondary_rpc,
        secondary_rpc_verified=secondary_ok,
    )


@dataclass(slots=True)
class _LocalKeyProvider:
    account: str
    key: bytes

    def sign(self, digest: bytes) -> tuple[int, int, int]:
        return sign_digest(self.key, digest)


def make_local_key_signer(
    private_key: bytes, expected_account: str
) -> Callable[[dict[str, Any], list[object]], bytes]:
    """Wrap raw 32-byte key into an isolated EIP-1559 envelope signing callback."""
    account = normalize_address(expected_account)
    provider = _LocalKeyProvider(account=account, key=private_key)

    def _sign_callback(_proposal: dict[str, Any], envelope_fields: list[object]) -> bytes:
        return sign_transaction(provider, envelope_fields)

    return _sign_callback


def _is_clean_provider_label(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    lowered = value.strip().lower()
    if any(marker in lowered for marker in _FORBIDDEN_PROVIDER_MARKERS):
        return False
    return _RAW_HEX_KEY_RE.search(value) is None


def build_authority_infra_proof(
    *,
    socket_path: Path,
    account: str,
    isolated_key_volume: str,
    primary_rpc_provider: str,
    secondary_rpc_provider: str,
    secondary_rpc_verified: bool,
    authority_status: dict[str, Any],
) -> dict[str, Any]:
    """Emit `oasis-profit-authority-infra.v1` JSON only after socket & RPC checks pass."""
    validate_socket_endpoint(socket_path)
    normalized_account = normalize_address(account)

    if not isinstance(isolated_key_volume, str) or not isolated_key_volume.strip():
        raise ValueError("isolated_key_volume must be a non-empty string")
    clean_volume = isolated_key_volume.strip()
    if _RAW_HEX_KEY_RE.search(clean_volume) is not None:
        raise ValueError("isolated_key_volume cannot contain hex key material")
    if _RAW_HEX_KEY_RE.search(str(socket_path)) is not None:
        raise ValueError("authority_socket_path cannot contain hex key material")

    if not _is_clean_provider_label(primary_rpc_provider) or not _is_clean_provider_label(
        secondary_rpc_provider
    ):
        raise ValueError("primary and secondary RPC providers must be non-placeholder labels")
    clean_primary = primary_rpc_provider.strip()
    clean_secondary = secondary_rpc_provider.strip()
    if clean_primary.lower() == clean_secondary.lower():
        raise ValueError("primary and secondary RPC providers must be distinct")
    if secondary_rpc_verified is not True:
        raise ValueError("secondary_rpc_verified must be True")

    if not isinstance(authority_status, dict):
        raise TypeError("authority_status must be a dict")
    if authority_status.get("schema") != PROFIT_AUTHORITY_SCHEMA:
        raise ValueError("authority_status schema mismatch")
    status_account = authority_status.get("account")
    if (
        not isinstance(status_account, str)
        or normalize_address(status_account) != normalized_account
    ):
        raise ValueError("authority_status account mismatch")
    sig_used = authority_status.get("signatures_used")
    max_sigs = authority_status.get("max_signatures")
    last_nonce = authority_status.get("last_nonce")
    halted = authority_status.get("halted")
    stop_present = authority_status.get("stop_present")
    if (
        type(sig_used) is not int
        or sig_used < 0
        or type(max_sigs) is not int
        or max_sigs < 1
        or (last_nonce is not None and (type(last_nonce) is not int or last_nonce < 0))
        or type(halted) is not bool
        or type(stop_present) is not bool
    ):
        raise ValueError("authority_status missing valid operational fields")
    if halted or stop_present:
        raise ValueError("authority is halted or STOP is present; infra readiness refused")

    proof: dict[str, Any] = {
        "schema": PROFIT_AUTHORITY_INFRA_SCHEMA,
        "execution_eligible": False,
        "account": normalized_account,
        "isolated_signer_verified": True,
        "isolated_key_volume": clean_volume,
        "authority_socket_path": str(socket_path),
        "primary_rpc_provider": clean_primary,
        "secondary_rpc_provider": clean_secondary,
        "secondary_rpc_verified": True,
        "isolated_signer": {
            "isolated_signer_verified": True,
            "isolated_key_volume": clean_volume,
            "authority_socket_path": str(socket_path),
        },
        "dual_rpc_quorum": {
            "primary_rpc_provider": clean_primary,
            "secondary_rpc_provider": clean_secondary,
            "secondary_rpc_verified": True,
        },
        "authority_status": {
            "schema": PROFIT_AUTHORITY_SCHEMA,
            "account": normalized_account,
            "signatures_used": sig_used,
            "max_signatures": max_sigs,
            "last_nonce": last_nonce,
            "halted": False,
            "stop_present": False,
        },
    }
    proof["proof_sha256"] = sha256_hex(canonical_json(proof))
    return proof


class ProfitAuthoritySocketClient:
    """Fail-closed Unix socket client for the isolated profit-engine signing authority."""

    def __init__(
        self,
        endpoint: Path,
        account: str,
        policy: AtomicExecutionPolicy,
        timeout: float = 30.0,
    ) -> None:
        if not isinstance(policy, AtomicExecutionPolicy):
            raise TypeError("policy must be an AtomicExecutionPolicy")
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 240
        ):
            raise ValueError("authority timeout must be finite and within 240 seconds")
        normalized_account = normalize_address(account)
        if normalized_account != policy.operator_account:
            raise ValueError("client account must match policy.operator_account")
        self.endpoint = endpoint
        self.account = normalized_account
        self.policy = policy
        self.timeout = float(timeout)

    def _exchange(self, request: bytes) -> dict[str, Any]:
        path = validate_socket_endpoint(self.endpoint)
        if len(request) >= MAX_PROFIT_FRAME:
            raise ValueError("profit authority request exceeds frame bound")
        deadline = time.monotonic() + self.timeout
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(_remaining(deadline))
            conn.connect(str(path))
            conn.settimeout(_remaining(deadline))
            conn.sendall(request + b"\n")
            reply = read_frame(
                conn, deadline=deadline, idle_timeout=self.timeout, max_bytes=MAX_PROFIT_FRAME
            )
        decoded = strict_json_loads(reply)
        if (
            not isinstance(decoded, dict)
            or canonical_json(decoded) != reply
            or decoded.get("schema") != PROFIT_AUTHORITY_SCHEMA
        ):
            raise ValueError("profit authority reply is malformed")
        return decoded

    def status(self) -> dict[str, Any]:
        """Query canonical authority operational status and validate every required field."""
        request = canonical_json(
            {
                "schema": PROFIT_AUTHORITY_SCHEMA,
                "account": self.account,
                "operation": "status",
            }
        )
        reply = self._exchange(request)
        if reply.get("refused") is True:
            raise ValueError(f"authority status refused: {reply.get('reason_code', 'refused')}")
        if set(reply) != {"schema", "status"} or not isinstance(reply["status"], dict):
            raise ValueError("authority status reply malformed")
        st = reply["status"]
        required_keys = {
            "schema",
            "account",
            "signatures_used",
            "max_signatures",
            "last_nonce",
            "halted",
            "stop_present",
        }
        if not required_keys.issubset(st):
            raise ValueError("authority status missing required fields")
        if st["schema"] != PROFIT_AUTHORITY_SCHEMA:
            raise ValueError("authority status schema mismatch")
        if not isinstance(st["account"], str) or normalize_address(st["account"]) != self.account:
            raise ValueError("authority status account mismatch")
        if (
            type(st["signatures_used"]) is not int
            or st["signatures_used"] < 0
            or type(st["max_signatures"]) is not int
            or st["max_signatures"] < 1
        ):
            raise ValueError("authority status signature counters invalid")
        last_nonce = st["last_nonce"]
        if last_nonce is not None and (type(last_nonce) is not int or last_nonce < 0):
            raise ValueError("authority status last_nonce invalid")
        if type(st["halted"]) is not bool or type(st["stop_present"]) is not bool:
            raise ValueError("authority status flags invalid")
        return st

    def sign_admitted_proposal(
        self,
        proposal: AtomicOrIntentProposal,
        envelope_fields: list[object],
    ) -> bytes:
        """Run client-side admission, request signing over socket, and verify signed bytes."""
        if not isinstance(proposal, AtomicOrIntentProposal):
            raise TypeError("proposal must be an AtomicOrIntentProposal")
        if proposal.envelope.account != self.account:
            raise ValueError("proposal account differs from client account")
        validated_fields = _validate_envelope_fields_match(envelope_fields, proposal.envelope)

        admission = admit_atomic_or_intent_proposal(
            policy=self.policy,
            proposal=proposal.envelope,
            simulation=proposal.simulation,
            now_s=proposal.now_s,
            daily_loss_usd_micros=proposal.daily_loss_usd_micros,
            cumulative_loss_usd_micros=proposal.cumulative_loss_usd_micros,
            revert_budget_status=proposal.revert_budget_status,
            has_unresolved_pending=proposal.has_unresolved_pending,
            rehearsal_loopback_anvil=proposal.rehearsal_loopback_anvil,
        )
        if not admission.admitted:
            raise ValueError("proposal denied by client admission: " + ", ".join(admission.reasons))

        request = canonical_json(
            {
                "schema": PROFIT_AUTHORITY_SCHEMA,
                "account": self.account,
                "operation": "sign",
                "proposal": proposal_to_dict(proposal),
                "fields": fields_json(validated_fields),
            }
        )
        reply = self._exchange(request)
        if reply.get("refused") is True:
            raise ValueError(f"authority refused signature: {reply.get('reason_code', 'refused')}")
        if set(reply) != {"schema", "signed"} or not isinstance(reply["signed"], str):
            raise ValueError("authority sign reply malformed")

        signed_hex = reply["signed"]
        raw_bytes = _decode_canonical_hex_bytes(signed_hex, "signed")
        submitted = verify_signed_bytes_match_admission(raw_bytes, decision=admission)
        if submitted.account != self.account:
            raise ValueError("authority signed for a different sender account")
        return raw_bytes

    def record_settlement(self, reconciliation_input: dict[str, Any]) -> dict[str, Any]:
        """Submit dual receipts and balance deltas for fail-closed settlement reconciliation."""
        if not isinstance(reconciliation_input, dict):
            raise TypeError("reconciliation_input must be a dict")
        request = canonical_json(
            {
                "schema": PROFIT_AUTHORITY_SCHEMA,
                "account": self.account,
                "operation": "settle",
                "reconciliation": reconciliation_input,
            }
        )
        reply = self._exchange(request)
        if reply.get("refused") is True:
            raise ValueError(f"authority refused settlement: {reply.get('reason_code', 'refused')}")
        if set(reply) != {"schema", "settlement"} or not isinstance(reply["settlement"], dict):
            raise ValueError("authority settlement reply malformed")
        return reply["settlement"]


class _Refusal(Exception):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(slots=True)
class _PersistedAuthorityState:
    account: str
    policy_digest: str
    signatures_used: int
    last_nonce: int | None
    halted: bool
    halt_reasons: list[str]
    daily_loss_usd_micros: int
    cumulative_loss_usd_micros: int
    signing_attempt: dict[str, Any] | None
    signed_decisions: dict[str, dict[str, Any]]
    settlement_history: list[dict[str, Any]]
    revert_attempts: list[dict[str, Any]]


class ProfitSigningAuthorityServer:
    """Isolated Unix socket server holding policy, persistent state, and optional key signer."""

    def __init__(
        self,
        *,
        policy: AtomicExecutionPolicy,
        state_dir: Path,
        endpoint: Path | None = None,
        key_signer: Callable[[dict[str, Any], list[object]], bytes] | None = None,
        revert_tracker: RollingRevertBudgetTracker | None = None,
        max_signatures: int = 12,
        ttl_seconds: int = 3600,
        allow_rehearsal_loopback_anvil: bool = False,
        require_existing_state: bool = False,
        status_only: bool = False,
        now_fn: Callable[[], int] | None = None,
        monotonic_fn: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(policy, AtomicExecutionPolicy):
            raise TypeError("policy must be an AtomicExecutionPolicy")
        if type(status_only) is not bool or type(require_existing_state) is not bool:
            raise TypeError("status_only and require_existing_state must be plain bools")
        if type(allow_rehearsal_loopback_anvil) is not bool:
            raise TypeError("allow_rehearsal_loopback_anvil must be a plain bool")
        if status_only and key_signer is not None:
            raise ValueError("status-only authority cannot hold a key_signer")
        if key_signer is not None and not callable(key_signer):
            raise TypeError("key_signer must be callable or None")
        if type(max_signatures) is not int or not 1 <= max_signatures <= 1000:
            raise ValueError("max_signatures must be an integer in 1..1000")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
            raise ValueError("ttl_seconds must be an integer in 1..86400")

        self.policy = policy
        self.state_dir = _validate_private_dir(state_dir)
        self.endpoint = endpoint
        self._key_signer = key_signer
        self.max_signatures = max_signatures
        self.ttl_seconds = ttl_seconds
        self.allow_rehearsal_loopback_anvil = allow_rehearsal_loopback_anvil
        self.status_only = status_only
        self._now_fn: Callable[[], int] = now_fn if now_fn is not None else lambda: int(time.time())
        self._monotonic: Callable[[], float] = (
            monotonic_fn if monotonic_fn is not None else time.monotonic
        )
        self._deadline = self._monotonic() + ttl_seconds
        self._state_path = self.state_dir / "profit_authority_state.json"
        self._stop_path = self.state_dir / "STOP"
        self._sock: socket.socket | None = None

        base_tracker = revert_tracker or RollingRevertBudgetTracker(_DEFAULT_REVERT_POLICY)
        if not isinstance(base_tracker, RollingRevertBudgetTracker):
            raise TypeError("revert_tracker must be a RollingRevertBudgetTracker")
        self._revert_policy = base_tracker.policy
        self._tracker = base_tracker

        self._state = self._load_or_init_state(require_existing_state=require_existing_state)

    def __enter__(self) -> Self:
        self.bind()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def bind(self) -> socket.socket:
        """Bind the private 0600 Unix domain socket if not already bound."""
        if self._sock is not None:
            return self._sock
        if self.endpoint is None:
            raise ValueError("endpoint path required to bind socket")
        self._sock = prepare_endpoint(self.endpoint)
        return self._sock

    def close(self) -> None:
        """Close the listening socket and unlink this process's endpoint."""
        if self._sock is not None:
            with suppress(OSError):
                self._sock.close()
            self._sock = None
        if self.endpoint is not None:
            with suppress(OSError):
                info = self.endpoint.lstat()
                if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid():
                    self.endpoint.unlink(missing_ok=True)

    @property
    def tracker(self) -> RollingRevertBudgetTracker:
        return self._tracker

    @property
    def state_path(self) -> Path:
        return self._state_path

    def _load_or_init_state(self, *, require_existing_state: bool) -> _PersistedAuthorityState:
        expected_digest = _policy_digest(self.policy)
        if self._state_path.is_symlink():
            raise ValueError("profit authority state file cannot be a symlink")
        if self._state_path.exists():
            info = self._state_path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ValueError("profit authority state file must be owned and mode 0600")
            decoded = strict_json_loads(self._state_path.read_bytes())
            if not isinstance(decoded, dict):
                raise ValueError("profit authority state root must be an object")
            if decoded.get("schema") != PROFIT_AUTHORITY_STATE_SCHEMA:
                raise ValueError("profit authority state schema mismatch")
            if (
                not isinstance(decoded.get("account"), str)
                or normalize_address(str(decoded["account"])) != self.policy.operator_account
                or decoded.get("policy_digest") != expected_digest
            ):
                raise ValueError("profit authority state does not match bound policy")
            last_nonce = decoded.get("last_nonce")
            if last_nonce is not None:
                _require_int(last_nonce, "last_nonce", low=0)
            halt_reasons = decoded.get("halt_reasons", [])
            if not isinstance(halt_reasons, list) or any(
                not isinstance(r, str) for r in halt_reasons
            ):
                raise ValueError("profit authority halt_reasons malformed")
            signing_attempt = decoded.get("signing_attempt")
            if signing_attempt is not None and not isinstance(signing_attempt, dict):
                raise ValueError("profit authority signing_attempt malformed")
            signed_decisions = decoded.get("signed_decisions", {})
            if not isinstance(signed_decisions, dict):
                raise ValueError("profit authority signed_decisions malformed")
            for tx_hash_key, entry in signed_decisions.items():
                normalize_hash32(str(tx_hash_key), "tx_hash")
                if not isinstance(entry, dict) or not isinstance(entry.get("decision"), dict):
                    raise ValueError("profit authority stored decision entry malformed")
                _decision_from_dict(entry["decision"])
                _require_bool(entry.get("settled"), "settled")
            settlement_history = decoded.get("settlement_history", [])
            if not isinstance(settlement_history, list):
                raise ValueError("profit authority settlement_history malformed")
            revert_attempts = decoded.get("revert_attempts", [])
            if not isinstance(revert_attempts, list):
                raise ValueError("profit authority revert_attempts malformed")

            rebuilt = RollingRevertBudgetTracker(self._revert_policy)
            for attempt in revert_attempts:
                if not isinstance(attempt, dict):
                    raise ValueError("profit authority revert_attempt malformed")
                rebuilt.record(
                    timestamp_s=_require_int(attempt.get("timestamp_s"), "timestamp_s", low=0),
                    outcome=AttemptOutcome(str(attempt.get("outcome"))),
                    gas_cost_usd_micros=_require_int(
                        attempt.get("gas_cost_usd_micros"), "gas_cost_usd_micros", low=0
                    ),
                    realized_profit_usd_micros=_require_int(
                        attempt.get("realized_profit_usd_micros", 0),
                        "realized_profit_usd_micros",
                        low=0,
                    ),
                )
            self._tracker = rebuilt

            return _PersistedAuthorityState(
                account=self.policy.operator_account,
                policy_digest=expected_digest,
                signatures_used=_require_int(
                    decoded.get("signatures_used"), "signatures_used", low=0
                ),
                last_nonce=last_nonce,
                halted=_require_bool(decoded.get("halted"), "halted"),
                halt_reasons=list(halt_reasons),
                daily_loss_usd_micros=_require_int(
                    decoded.get("daily_loss_usd_micros", 0), "daily_loss_usd_micros", low=0
                ),
                cumulative_loss_usd_micros=_require_int(
                    decoded.get("cumulative_loss_usd_micros", 0),
                    "cumulative_loss_usd_micros",
                    low=0,
                ),
                signing_attempt=signing_attempt,
                signed_decisions=dict(signed_decisions),
                settlement_history=list(settlement_history),
                revert_attempts=list(revert_attempts),
            )

        if require_existing_state:
            raise FileNotFoundError("existing profit authority state file required")

        initial = _PersistedAuthorityState(
            account=self.policy.operator_account,
            policy_digest=expected_digest,
            signatures_used=0,
            last_nonce=None,
            halted=False,
            halt_reasons=[],
            daily_loss_usd_micros=0,
            cumulative_loss_usd_micros=0,
            signing_attempt=None,
            signed_decisions={},
            settlement_history=[],
            revert_attempts=[],
        )
        if not self.status_only:
            self._persist_state(initial)
        return initial

    def _persist_state(self, state: _PersistedAuthorityState | None = None) -> None:
        target = self._state if state is None else state
        payload: dict[str, Any] = {
            "schema": PROFIT_AUTHORITY_STATE_SCHEMA,
            "account": target.account,
            "policy_digest": target.policy_digest,
            "signatures_used": target.signatures_used,
            "last_nonce": target.last_nonce,
            "halted": target.halted,
            "halt_reasons": list(target.halt_reasons),
            "daily_loss_usd_micros": target.daily_loss_usd_micros,
            "cumulative_loss_usd_micros": target.cumulative_loss_usd_micros,
            "signing_attempt": target.signing_attempt,
            "signed_decisions": target.signed_decisions,
            "settlement_history": target.settlement_history,
            "revert_attempts": target.revert_attempts,
        }
        _atomic_write_json(self._state_path, payload)

    def current_status(self) -> dict[str, Any]:
        """Return safe operational status summary without private keys or raw payloads."""
        stop_present = self._stop_path.exists() or self._stop_path.is_symlink()
        pending_hashes = sorted(
            tx_h for tx_h, entry in self._state.signed_decisions.items() if not entry["settled"]
        )
        return {
            "schema": PROFIT_AUTHORITY_SCHEMA,
            "account": self.policy.operator_account,
            "chain_id": self.policy.chain_id,
            "signatures_used": self._state.signatures_used,
            "max_signatures": self.max_signatures,
            "last_nonce": self._state.last_nonce,
            "halted": self._state.halted,
            "halt_reasons": list(self._state.halt_reasons),
            "stop_present": stop_present,
            "revert_budget_status": self._tracker.status.value,
            "signing_enabled": self._key_signer is not None and not self.status_only,
            "signing_unresolved": self._state.signing_attempt is not None,
            "pending_tx_hashes": pending_hashes,
            "expired": self._monotonic() >= self._deadline,
        }

    def reconcile_settlement(self, reconciliation_input: dict[str, Any]) -> dict[str, Any]:
        """Reconcile dual receipts + cash/token deltas and latch halt on residual or deep revert."""
        if not isinstance(reconciliation_input, dict):
            raise _Refusal("invalid_request")
        allowed_keys = {
            "tx_hash",
            "proposal_digest",
            "primary_receipt",
            "secondary_receipt",
            "native_balance_before_wei",
            "native_balance_after_wei",
            "profit_token_balance_before_raw",
            "profit_token_balance_after_raw",
            "gas_cost_usd_micros",
            "realized_profit_usd_micros",
            "timestamp_s",
        }
        if not set(reconciliation_input).issubset(allowed_keys):
            raise _Refusal("invalid_request")

        try:
            primary_obs = _receipt_from_dict(
                reconciliation_input.get("primary_receipt"), "primary_receipt"
            )
            secondary_obs = _receipt_from_dict(
                reconciliation_input.get("secondary_receipt"), "secondary_receipt"
            )
            tx_hash_raw = reconciliation_input.get("tx_hash", primary_obs.tx_hash)
            tx_hash = normalize_hash32(str(tx_hash_raw), "tx_hash")
            native_before = _require_int(
                reconciliation_input.get("native_balance_before_wei"),
                "native_balance_before_wei",
                low=0,
            )
            native_after = _require_int(
                reconciliation_input.get("native_balance_after_wei"),
                "native_balance_after_wei",
                low=0,
            )
            token_before = _require_int(
                reconciliation_input.get("profit_token_balance_before_raw"),
                "profit_token_balance_before_raw",
                low=0,
            )
            token_after = _require_int(
                reconciliation_input.get("profit_token_balance_after_raw"),
                "profit_token_balance_after_raw",
                low=0,
            )
            gas_cost_usd = _require_int(
                reconciliation_input.get("gas_cost_usd_micros"), "gas_cost_usd_micros", low=0
            )
            realized_profit_usd = _require_int(
                reconciliation_input.get("realized_profit_usd_micros"),
                "realized_profit_usd_micros",
                low=0,
            )
            ts_raw = reconciliation_input.get("timestamp_s")
            timestamp_s = (
                self._now_fn() if ts_raw is None else _require_int(ts_raw, "timestamp_s", low=0)
            )
        except (TypeError, ValueError) as exc:
            raise _Refusal("invalid_request") from exc

        entry = self._state.signed_decisions.get(tx_hash)
        if entry is None:
            digest_filter = reconciliation_input.get("proposal_digest")
            if isinstance(digest_filter, str):
                for candidate_hash, candidate_entry in self._state.signed_decisions.items():
                    if candidate_entry["decision"].get("proposal_digest") == digest_filter:
                        tx_hash = candidate_hash
                        entry = candidate_entry
                        break
        if entry is None:
            raise _Refusal("unknown_pending_transaction")
        if entry["settled"]:
            raise _Refusal("already_settled")

        decision = _decision_from_dict(entry["decision"])
        reconciliation = reconcile_atomic_or_intent_receipt(
            decision=decision,
            primary_receipt=primary_obs,
            secondary_receipt=secondary_obs,
            native_balance_before_wei=native_before,
            native_balance_after_wei=native_after,
            profit_token_balance_before_raw=token_before,
            profit_token_balance_after_raw=token_after,
            gas_cost_usd_micros=gas_cost_usd,
            realized_profit_usd_micros=realized_profit_usd,
            tracker=self._tracker,
            timestamp_s=timestamp_s,
        )

        entry["settled"] = True
        recorded_profit_usd = (
            realized_profit_usd if reconciliation.outcome is AttemptOutcome.SUCCESS else 0
        )
        self._state.revert_attempts.append(
            {
                "timestamp_s": timestamp_s,
                "outcome": reconciliation.outcome.value,
                "gas_cost_usd_micros": gas_cost_usd,
                "realized_profit_usd_micros": recorded_profit_usd,
            }
        )
        net_delta_usd = recorded_profit_usd - gas_cost_usd
        if net_delta_usd < 0:
            self._state.daily_loss_usd_micros += -net_delta_usd
            self._state.cumulative_loss_usd_micros += -net_delta_usd

        should_halt = (
            not reconciliation.reconciled
            or reconciliation.halt_latch
            or reconciliation.native_residual_wei != 0
            or reconciliation.outcome
            in (AttemptOutcome.DEEP_EXECUTION_REVERT, AttemptOutcome.AMBIGUOUS_HALT)
        )
        if should_halt:
            self._state.halted = True
            for reason in reconciliation.reasons:
                if reason not in self._state.halt_reasons:
                    self._state.halt_reasons.append(reason)
            if reconciliation.outcome is AttemptOutcome.DEEP_EXECUTION_REVERT:
                deep_reason = "deep_execution_revert"
                if deep_reason not in self._state.halt_reasons:
                    self._state.halt_reasons.append(deep_reason)
            if reconciliation.native_residual_wei != 0:
                res_reason = "native_balance_residual_nonzero"
                if res_reason not in self._state.halt_reasons:
                    self._state.halt_reasons.append(res_reason)

        digest = sha256_hex(
            canonical_json(
                {
                    "tx_hash": tx_hash,
                    "proposal_digest": decision.proposal_digest,
                    "reconciled": reconciliation.reconciled,
                    "halt_latch": reconciliation.halt_latch,
                    "halted": self._state.halted,
                    "outcome": reconciliation.outcome.value,
                    "native_residual_wei": reconciliation.native_residual_wei,
                    "erc20_profit_delta_raw": reconciliation.erc20_profit_delta_raw,
                    "reasons": list(reconciliation.reasons),
                }
            )
        )
        summary = {
            "tx_hash": tx_hash,
            "reconciled": reconciliation.reconciled,
            "halt_latch": reconciliation.halt_latch,
            "halted": self._state.halted,
            "outcome": reconciliation.outcome.value,
            "native_spent_wei": reconciliation.native_spent_wei,
            "native_expected_wei": reconciliation.native_expected_wei,
            "native_residual_wei": reconciliation.native_residual_wei,
            "erc20_profit_delta_raw": reconciliation.erc20_profit_delta_raw,
            "revert_budget_status": reconciliation.revert_budget_status.value,
            "reasons": list(reconciliation.reasons),
            "settlement_digest": digest,
        }
        self._state.settlement_history.append(summary)
        self._persist_state()
        return summary

    def _handle_sign(self, parsed: dict[str, Any]) -> bytes:
        if self.status_only or self._key_signer is None:
            raise _Refusal("signing_disabled")
        if self._monotonic() >= self._deadline:
            raise _Refusal("authority_expired")
        if self._stop_path.exists() or self._stop_path.is_symlink():
            raise _Refusal("stop_file_present")
        if self._state.halted:
            raise _Refusal("authority_halted")
        if self._state.signing_attempt is not None:
            raise _Refusal("unresolved_signing_attempt")
        if self._state.signatures_used >= self.max_signatures:
            raise _Refusal("signature_budget_exhausted")
        if set(parsed) != {"schema", "account", "operation", "proposal", "fields"}:
            raise _Refusal("invalid_request")

        try:
            proposal = proposal_from_dict(parsed["proposal"])
            fields = _decode_wire_fields(parsed["fields"])
            _validate_envelope_fields_match(fields, proposal.envelope)
        except (TypeError, ValueError) as exc:
            raise _Refusal("envelope_mismatch") from exc

        if proposal.envelope.account != self.policy.operator_account:
            raise _Refusal("account_mismatch")
        nonce = fields[1]
        if type(nonce) is not int:
            raise _Refusal("envelope_mismatch")
        if self._state.last_nonce is not None and nonce <= self._state.last_nonce:
            raise _Refusal("stale_nonce")

        rehearsal = self.allow_rehearsal_loopback_anvil and proposal.rehearsal_loopback_anvil
        if proposal.rehearsal_loopback_anvil and not self.allow_rehearsal_loopback_anvil:
            raise _Refusal("rehearsal_mode_disabled")

        effective_budget = (
            RevertBudgetStatus.HALT
            if self._tracker.status is RevertBudgetStatus.HALT
            or proposal.revert_budget_status is RevertBudgetStatus.HALT
            else RevertBudgetStatus.ACTIVE
        )
        try:
            admission = admit_atomic_or_intent_proposal(
                policy=self.policy,
                proposal=proposal.envelope,
                simulation=proposal.simulation,
                now_s=self._now_fn(),
                daily_loss_usd_micros=max(
                    self._state.daily_loss_usd_micros, proposal.daily_loss_usd_micros
                ),
                cumulative_loss_usd_micros=max(
                    self._state.cumulative_loss_usd_micros, proposal.cumulative_loss_usd_micros
                ),
                revert_budget_status=effective_budget,
                has_unresolved_pending=proposal.has_unresolved_pending,
                rehearsal_loopback_anvil=rehearsal,
            )
        except (TypeError, ValueError) as exc:
            raise _Refusal("admission_error") from exc

        if not admission.admitted:
            reason_code = admission.reasons[0] if admission.reasons else "admission_denied"
            raise _Refusal(reason_code)

        self._state.signing_attempt = {
            "nonce": admission.nonce,
            "proposal_digest": admission.proposal_digest,
            "calldata_sha256": admission.calldata_sha256,
        }
        self._persist_state()

        if self._monotonic() >= self._deadline:
            raise _Refusal("authority_expired")
        if self._stop_path.exists() or self._stop_path.is_symlink():
            raise _Refusal("stop_file_present")

        try:
            raw_signed = self._key_signer(parsed["proposal"], fields)
            submitted = verify_signed_bytes_match_admission(raw_signed, decision=admission)
            if submitted.account != self.policy.operator_account:
                raise ValueError("signed transaction recovered wrong sender")
        except Exception as exc:
            raise _Refusal("signed_bytes_verification_failed") from exc

        self._state.last_nonce = admission.nonce
        self._state.signatures_used += 1
        self._state.signing_attempt = None
        self._state.signed_decisions[submitted.transaction_hash] = {
            "decision": _decision_to_dict(admission),
            "settled": False,
        }
        self._persist_state()
        return raw_signed

    def _dispatch_frame(self, frame: bytes) -> tuple[str, dict[str, Any]]:
        try:
            parsed = strict_json_loads(frame)
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise _Refusal("malformed_frame") from exc

        if (
            not isinstance(parsed, dict)
            or canonical_json(parsed) != frame
            or parsed.get("schema") != PROFIT_AUTHORITY_SCHEMA
        ):
            raise _Refusal("invalid_request")

        raw_account = parsed.get("account")
        if not isinstance(raw_account, str):
            raise _Refusal("account_mismatch")
        try:
            if normalize_address(raw_account) != self.policy.operator_account:
                raise _Refusal("account_mismatch")
        except ValueError as exc:
            raise _Refusal("account_mismatch") from exc

        operation = parsed.get("operation")
        if operation == "status":
            if set(parsed) != {"schema", "account", "operation"}:
                raise _Refusal("invalid_request")
            return "status", {"schema": PROFIT_AUTHORITY_SCHEMA, "status": self.current_status()}
        if operation == "settle":
            if self.status_only:
                raise _Refusal("signing_disabled")
            if self._monotonic() >= self._deadline:
                raise _Refusal("authority_expired")
            if set(parsed) != {"schema", "account", "operation", "reconciliation"}:
                raise _Refusal("invalid_request")
            reconciliation_obj = parsed["reconciliation"]
            if not isinstance(reconciliation_obj, dict):
                raise _Refusal("invalid_request")
            summary = self.reconcile_settlement(reconciliation_obj)
            return "settle", {"schema": PROFIT_AUTHORITY_SCHEMA, "settlement": summary}
        if operation == "sign":
            signed = self._handle_sign(parsed)
            return "sign", {"schema": PROFIT_AUTHORITY_SCHEMA, "signed": "0x" + signed.hex()}
        raise _Refusal("invalid_request")

    def serve_once(self, *, timeout: float | None = 1.0) -> str | None:
        """Accept and serve at most one socket connection within deadline; return op or None."""
        sock = self.bind()
        remaining = self._deadline - self._monotonic()
        if remaining <= 0:
            return None
        accept_timeout = remaining if timeout is None else min(timeout, remaining)
        sock.settimeout(max(0.001, accept_timeout))
        try:
            connection, _ = sock.accept()
        except TimeoutError:
            return None

        op = "refused"
        with connection:
            try:
                frame = read_frame(
                    connection,
                    deadline=min(self._deadline, self._monotonic() + 30.0),
                    idle_timeout=30.0,
                    max_bytes=MAX_PROFIT_FRAME,
                )
                if self._monotonic() >= self._deadline:
                    raise _Refusal("authority_expired")
                op, reply = self._dispatch_frame(frame)
            except _Refusal as refusal:
                reply = {
                    "schema": PROFIT_AUTHORITY_SCHEMA,
                    "refused": True,
                    "reason_code": refusal.reason_code,
                }
            except TimeoutError:
                reply = {
                    "schema": PROFIT_AUTHORITY_SCHEMA,
                    "refused": True,
                    "reason_code": "authority_expired",
                }
            except Exception:  # noqa: BLE001 - never disclose internal/key/path diagnostics
                reply = {
                    "schema": PROFIT_AUTHORITY_SCHEMA,
                    "refused": True,
                    "reason_code": "invalid_request",
                }

            encoded = canonical_json(reply)
            if len(encoded) >= MAX_PROFIT_FRAME:
                encoded = canonical_json(
                    {
                        "schema": PROFIT_AUTHORITY_SCHEMA,
                        "refused": True,
                        "reason_code": "reply_exceeds_frame_bound",
                    }
                )
            if self._monotonic() < self._deadline:
                with suppress(OSError):
                    connection.settimeout(min(5.0, max(0.001, self._deadline - self._monotonic())))
                    connection.sendall(encoded + b"\n")
        return op

    def serve(
        self,
        *,
        max_requests: int | None = None,
        max_status_requests: int | None = None,
        max_sign_requests: int | None = None,
    ) -> dict[str, int]:
        """Serve bounded requests over the Unix socket until budget or TTL expires."""
        for label, bound in (
            ("max_requests", max_requests),
            ("max_status_requests", max_status_requests),
            ("max_sign_requests", max_sign_requests),
        ):
            if bound is not None and (type(bound) is not int or not 1 <= bound <= 10_000):
                raise ValueError(f"{label} must be an integer in 1..10000")

        self.bind()
        counts = {"total": 0, "status": 0, "sign": 0, "settle": 0, "refused": 0}
        while self._monotonic() < self._deadline:
            if max_requests is not None and counts["total"] >= max_requests:
                break
            if (
                self.status_only
                and max_status_requests is not None
                and (counts["total"] >= max_status_requests)
            ):
                break
            if (
                not self.status_only
                and max_status_requests is not None
                and counts["status"] >= max_status_requests
            ):
                break
            if max_sign_requests is not None and (counts["sign"] >= max_sign_requests):
                break
            outcome = self.serve_once(timeout=0.5)
            if outcome is None:
                continue
            counts["total"] += 1
            counts[outcome] = counts.get(outcome, 0) + 1
        return counts


__all__ = [
    "MAX_PROFIT_FRAME",
    "PROFIT_AUTHORITY_INFRA_SCHEMA",
    "PROFIT_AUTHORITY_SCHEMA",
    "PROFIT_AUTHORITY_STATE_SCHEMA",
    "AtomicOrIntentProposal",
    "LoadedPolicyConfig",
    "ProfitAuthoritySocketClient",
    "ProfitSigningAuthorityServer",
    "build_authority_infra_proof",
    "envelope_fields_from_proposal",
    "load_atomic_execution_policy",
    "make_local_key_signer",
    "proposal_from_dict",
    "proposal_to_dict",
    "read_frame",
    "strict_json_loads",
    "validate_socket_endpoint",
]
