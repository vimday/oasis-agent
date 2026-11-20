"""Fail-closed 9-gate canary readiness evaluator for the Oasis profit engine.

Evaluates compiled Solidity artifacts, loopback Anvil E2E settlement results,
BaseTime predeploy verification, public-shadow sample size and cost-adjusted
LCB edge, rolling revert-budget health, dual-RPC quorum proof, isolated signer
authority readiness, and owner-signed scope approval. No input or passing
verdict grants signing or broadcast authority (`execution_eligible` is
permanently `False`).
"""

import json
import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, Literal

from ..models.common import (
    ZERO_ADDRESS,
    canonical_json,
    normalize_address,
    normalize_hash32,
    sha256_hex,
)
from ..risk.capital_policy import (
    USD_MICROS_SCALE,
    CapitalRiskConfig,
    load_capital_risk_config,
)

READINESS_SCHEMA_VERSION: Final[str] = "oasis.profit-engine-readiness.v1"
DUAL_RPC_QUORUM_SCHEMA_VERSION: Final[str] = "oasis.dual-rpc-quorum.v1"
ANVIL_E2E_SCHEMA_VERSION: Final[str] = "oasis-anvil-profit-e2e-v1"
PROFIT_AUTHORITY_INFRA_SCHEMAS: Final[tuple[str, ...]] = (
    "oasis-profit-authority-infra.v1",
    "oasis.profit-authority-infra-proof.v1",
)
SOFTWARE_SCOPE_SIGNOFF_SCHEMA_VERSION: Final[str] = "oasis.software-scope-signoff.v1"
BASE_CHAIN_ID: Final[int] = 8453
BASE_TIME_PREDEPLOY: Final[str] = "0x4200000000000000000000000000000000000030"
MAX_EARLY_ABORT_GAS: Final[int] = 65_000
DEFAULT_MIN_SHADOW_TICKS: Final[int] = 100
DEFAULT_MIN_LCB_NET_EDGE_BPS: Final[int] = 5
DEFAULT_MIN_OOS_TRADES: Final[int] = 5
MAX_APPROVED_CAPITAL_USD: Final[int] = 500
MAX_APPROVED_DAILY_LOSS_USD: Final[int] = 200
MAX_APPROVED_CUMULATIVE_LOSS_USD: Final[int] = 500
MAX_INPUT_BYTES: Final[int] = 50_000_000

DEFAULT_CONTRACTS_OUT_DIR: Final[Path] = Path(__file__).resolve().parents[3] / "contracts" / "out"

REQUIRED_ANVIL_DRILLS: Final[tuple[str, ...]] = (
    "executor_two_hop_profit_and_sweep",
    "executor_early_abort_under_65k_gas",
    "vault_erc1271_and_epoch_revocation",
    "vault_reactor_fill_and_zero_allowances",
)

GATE_NAMES: Final[tuple[str, ...]] = (
    "compiled_contracts_pinned",
    "anvil_e2e_settlement_reconciled",
    "base_time_predeploy_verified",
    "untouched_shadow_sample_size",
    "positive_cost_adjusted_lcb_edge",
    "revert_budget_healthy",
    "dual_rpc_quorum_verified",
    "isolated_signer_authority_ready",
    "owner_scope_release_manifest_present",
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
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_RAW_HEX_KEY_RE: Final[re.Pattern[str]] = re.compile(r"0x[0-9a-fA-F]{64}")


@dataclass(frozen=True, slots=True)
class GateCheckResult:
    """Result of one fail-closed readiness gate check."""

    name: str
    passed: bool
    detail: str
    required_threshold: str
    observed_value: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("gate name must be a non-empty string")
        if type(self.passed) is not bool:
            raise TypeError("gate passed must be a boolean")
        if not isinstance(self.detail, str) or not self.detail.strip():
            raise ValueError("gate detail must be a non-empty string")
        if not isinstance(self.required_threshold, str) or not self.required_threshold.strip():
            raise ValueError("gate required_threshold must be a non-empty string")
        if not isinstance(self.observed_value, str) or not self.observed_value.strip():
            raise ValueError("gate observed_value must be a non-empty string")


@dataclass(frozen=True, slots=True)
class ProfitEngineReadinessReport:
    """Immutable 9-gate profit-engine readiness verdict."""

    release_permitted: bool
    passed_count: int
    total_gates: int
    gates: tuple[GateCheckResult, ...]
    blocked_gates: tuple[str, ...]
    next_operator_actions: tuple[str, ...]
    report_sha256: str
    schema_version: str = READINESS_SCHEMA_VERSION
    execution_eligible: Literal[False] = False
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != READINESS_SCHEMA_VERSION:
            raise ValueError("unsupported readiness report schema_version")
        if self.execution_eligible is not False:
            raise ValueError("readiness report cannot claim execution_eligible=True")
        if type(self.release_permitted) is not bool:
            raise TypeError("release_permitted must be a boolean")
        if type(self.passed_count) is not int or self.passed_count < 0:
            raise ValueError("passed_count must be a non-negative integer")
        if type(self.total_gates) is not int or self.total_gates != len(GATE_NAMES):
            raise ValueError(f"total_gates must equal {len(GATE_NAMES)}")
        if not isinstance(self.gates, tuple) or len(self.gates) != self.total_gates:
            raise ValueError("gates must contain one GateCheckResult per gate")
        if any(not isinstance(gate, GateCheckResult) for gate in self.gates):
            raise TypeError("all gates must be GateCheckResult instances")
        if tuple(gate.name for gate in self.gates) != GATE_NAMES:
            raise ValueError("gates order or names do not match canonical gate list")
        expected_passed = sum(1 for gate in self.gates if gate.passed)
        if self.passed_count != expected_passed:
            raise ValueError("passed_count disagrees with gate results")
        expected_blocked = tuple(gate.name for gate in self.gates if not gate.passed)
        if self.blocked_gates != expected_blocked:
            raise ValueError("blocked_gates disagrees with gate results")
        if self.release_permitted != (self.passed_count == self.total_gates):
            raise ValueError("release_permitted disagrees with gate outcomes")
        if not isinstance(self.next_operator_actions, tuple) or any(
            not isinstance(action, str) or not action.strip()
            for action in self.next_operator_actions
        ):
            raise TypeError("next_operator_actions must be a tuple of non-empty strings")
        if _SHA256_RE.fullmatch(self.report_sha256) is None:
            raise ValueError("report_sha256 must be a lowercase 64-hex SHA-256 digest")
        if not isinstance(self.evidence, dict):
            raise TypeError("evidence must be a dictionary")

    def to_dict(self) -> dict[str, Any]:
        """Serialize report to a JSON-compatible dictionary."""
        return {
            "schema_version": self.schema_version,
            "release_permitted": self.release_permitted,
            "execution_eligible": self.execution_eligible,
            "passed_count": self.passed_count,
            "total_gates": self.total_gates,
            "gates": [asdict(gate) for gate in self.gates],
            "blocked_gates": list(self.blocked_gates),
            "next_operator_actions": list(self.next_operator_actions),
            "evidence": self.evidence,
            "report_sha256": self.report_sha256,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        """Render the readiness report as formatted JSON."""
        return json.dumps(self.to_dict(), indent=indent) + "\n"


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


def _load_json_object(path: Path | None, label: str) -> tuple[dict[str, Any] | None, str | None]:
    if path is None:
        return None, f"{label} path was not supplied"
    if path.is_symlink():
        return None, f"{label} refuses symlink path: {path}"
    if not path.is_file():
        return None, f"{label} file not found: {path}"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"{label} read error: {exc}"
    if len(raw) > MAX_INPUT_BYTES:
        return None, f"{label} exceeds maximum byte limit ({MAX_INPUT_BYTES})"
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        return None, f"{label} is malformed JSON: {exc}"
    if not isinstance(decoded, dict):
        return None, f"{label} JSON root must be an object"
    return decoded, None


def _is_nonempty_evm_hex(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) <= 4:
        return False
    digits = value[2:]
    if len(digits) % 2 != 0:
        return False
    try:
        int(digits, 16)
    except ValueError:
        return False
    return True


def _validate_contract_artifact(path: Path, expected_method: str) -> tuple[str | None, str | None]:
    data, err = _load_json_object(path, path.name)
    if data is None:
        return None, err
    bytecode = data.get("bytecode")
    deployed = data.get("deployedBytecode")
    abi = data.get("abi")
    method_ids = data.get("methodIdentifiers")
    if not isinstance(bytecode, dict) or not _is_nonempty_evm_hex(bytecode.get("object")):
        return None, f"{path.name} missing valid creation bytecode"
    if not isinstance(deployed, dict) or not _is_nonempty_evm_hex(deployed.get("object")):
        return None, f"{path.name} missing valid deployedBytecode"
    if not isinstance(abi, list) or not abi:
        return None, f"{path.name} missing compiled ABI"
    if not isinstance(method_ids, dict) or not any(
        isinstance(k, str) and k.startswith(expected_method) for k in method_ids
    ):
        return None, f"{path.name} missing expected method {expected_method}"
    deployed_hex = str(deployed["object"]).lower()
    return sha256_hex(bytes.fromhex(deployed_hex[2:])), None


def _eval_compiled_contracts_pinned(contracts_out_dir: Path) -> tuple[GateCheckResult, str | None]:
    threshold = "OasisAtomicExecutor.json & OasisIntentVault.json bytecode pinned"
    exec_path = contracts_out_dir / "OasisAtomicExecutor.sol" / "OasisAtomicExecutor.json"
    vault_path = contracts_out_dir / "OasisIntentVault.sol" / "OasisIntentVault.json"

    exec_sha, exec_err = _validate_contract_artifact(exec_path, "executeCycle(")
    if exec_sha is None:
        return (
            GateCheckResult(
                name="compiled_contracts_pinned",
                passed=False,
                detail=exec_err or "OasisAtomicExecutor artifact missing or invalid",
                required_threshold=threshold,
                observed_value="missing_or_invalid",
            ),
            (
                "Run 'forge build --root contracts' to compile and pin OasisAtomicExecutor.json "
                "and OasisIntentVault.json."
            ),
        )

    vault_sha, vault_err = _validate_contract_artifact(vault_path, "executeReactorFill(")
    if vault_sha is None:
        return (
            GateCheckResult(
                name="compiled_contracts_pinned",
                passed=False,
                detail=vault_err or "OasisIntentVault artifact missing or invalid",
                required_threshold=threshold,
                observed_value="missing_or_invalid",
            ),
            (
                "Run 'forge build --root contracts' to compile and pin OasisAtomicExecutor.json "
                "and OasisIntentVault.json."
            ),
        )

    return (
        GateCheckResult(
            name="compiled_contracts_pinned",
            passed=True,
            detail="Compiled creation and runtime bytecode verified for both executor and vault.",
            required_threshold=threshold,
            observed_value=f"executor={exec_sha[:12]},vault={vault_sha[:12]}",
        ),
        None,
    )


def _eval_anvil_e2e_settlement_reconciled(
    anvil_data: dict[str, Any] | None,
    anvil_err: str | None,
) -> tuple[GateCheckResult, str | None]:
    threshold = (
        f"all_drills_passed=True, early_abort_gas<{MAX_EARLY_ABORT_GAS}, allowance_residuals=0"
    )
    action = (
        "Run 'python -m scripts.verify_atomic_vault_anvil --out <anvil_e2e_report.json>' "
        "and pass --anvil-report."
    )
    if anvil_data is None:
        return (
            GateCheckResult(
                name="anvil_e2e_settlement_reconciled",
                passed=False,
                detail=anvil_err or "Anvil E2E report missing or corrupt",
                required_threshold=threshold,
                observed_value="missing_or_invalid",
            ),
            action,
        )

    if anvil_data.get("schema_version") != ANVIL_E2E_SCHEMA_VERSION:
        return (
            GateCheckResult(
                name="anvil_e2e_settlement_reconciled",
                passed=False,
                detail="Anvil E2E report schema_version mismatch",
                required_threshold=threshold,
                observed_value=f"schema={anvil_data.get('schema_version')!r}",
            ),
            action,
        )
    if anvil_data.get("chain_id") != BASE_CHAIN_ID:
        return (
            GateCheckResult(
                name="anvil_e2e_settlement_reconciled",
                passed=False,
                detail="Anvil E2E report chain_id must equal Base (8453)",
                required_threshold=threshold,
                observed_value=f"chain_id={anvil_data.get('chain_id')!r}",
            ),
            action,
        )
    if anvil_data.get("execution_eligible") is not False:
        return (
            GateCheckResult(
                name="anvil_e2e_settlement_reconciled",
                passed=False,
                detail="Anvil E2E report must retain execution_eligible=False",
                required_threshold=threshold,
                observed_value=f"execution_eligible={anvil_data.get('execution_eligible')!r}",
            ),
            action,
        )

    declared_sha = anvil_data.get("report_sha256")
    if declared_sha is not None:
        body = {k: v for k, v in anvil_data.items() if k != "report_sha256"}
        expected_sha = sha256_hex(canonical_json(body))
        if not isinstance(declared_sha, str) or declared_sha != expected_sha:
            return (
                GateCheckResult(
                    name="anvil_e2e_settlement_reconciled",
                    passed=False,
                    detail="Anvil E2E report_sha256 integrity check failed",
                    required_threshold=threshold,
                    observed_value="checksum_mismatch",
                ),
                action,
            )

    if anvil_data.get("all_drills_passed") is not True:
        return (
            GateCheckResult(
                name="anvil_e2e_settlement_reconciled",
                passed=False,
                detail="Anvil E2E report indicates one or more drills failed",
                required_threshold=threshold,
                observed_value=f"all_drills_passed={anvil_data.get('all_drills_passed')!r}",
            ),
            action,
        )

    drills = anvil_data.get("drills")
    if not isinstance(drills, dict) or not all(
        isinstance(drills.get(name), dict) and drills[name].get("passed") is True
        for name in REQUIRED_ANVIL_DRILLS
    ):
        return (
            GateCheckResult(
                name="anvil_e2e_settlement_reconciled",
                passed=False,
                detail="Anvil E2E report missing passing drill records for all 4 drills",
                required_threshold=threshold,
                observed_value="incomplete_drills",
            ),
            action,
        )

    early_drill = drills["executor_early_abort_under_65k_gas"]
    early_gas = early_drill.get("gas_used")
    if type(early_gas) is not int or early_gas <= 0 or early_gas >= MAX_EARLY_ABORT_GAS:
        return (
            GateCheckResult(
                name="anvil_e2e_settlement_reconciled",
                passed=False,
                detail=f"Anvil early-abort gas ({early_gas!r}) must be in (0, 65000)",
                required_threshold=threshold,
                observed_value=f"early_abort_gas={early_gas!r}",
            ),
            action,
        )

    vault_drill = drills["vault_reactor_fill_and_zero_allowances"]
    allowances = vault_drill.get("allowance_residuals")
    if (
        not isinstance(allowances, dict)
        or not allowances
        or any(type(v) is not int or v != 0 for v in allowances.values())
    ):
        return (
            GateCheckResult(
                name="anvil_e2e_settlement_reconciled",
                passed=False,
                detail="Anvil vault fill left non-zero or missing allowance residuals",
                required_threshold=threshold,
                observed_value="nonzero_allowance_residual",
            ),
            action,
        )

    return (
        GateCheckResult(
            name="anvil_e2e_settlement_reconciled",
            passed=True,
            detail=(
                "All 4 Anvil EIP-1559 drills passed with early-abort gas < 65k and "
                "zero ERC-20 allowance residuals."
            ),
            required_threshold=threshold,
            observed_value=f"drills=4/4,early_abort_gas={early_gas},allowance_residuals=0",
        ),
        None,
    )


def _eval_base_time_predeploy_verified(
    radar_data: dict[str, Any] | None,
    radar_err: str | None,
) -> tuple[GateCheckResult, str | None]:
    threshold = "status=='ok' and header_matches_predeploy=True on Base (8453)"
    action = (
        "Run 'python -m scripts.run_opportunity_radar --mode public-shadow --out <dir>' "
        "against a live Base RPC and pass --radar-report <dir>/radar_report.json."
    )
    if radar_data is None:
        return (
            GateCheckResult(
                name="base_time_predeploy_verified",
                passed=False,
                detail=radar_err or "BaseTime/radar report missing or corrupt",
                required_threshold=threshold,
                observed_value="missing_or_invalid",
            ),
            action,
        )

    if "base_time_predeploy_verification" in radar_data:
        section = radar_data.get("base_time_predeploy_verification")
        if not isinstance(section, dict):
            return (
                GateCheckResult(
                    name="base_time_predeploy_verified",
                    passed=False,
                    detail="radar_report.base_time_predeploy_verification must be an object",
                    required_threshold=threshold,
                    observed_value="malformed_section",
                ),
                action,
            )
        status = section.get("status")
        header_matches = section.get("header_matches_predeploy")
        chain_id = section.get("chain_id")
        predeploy = section.get("predeploy_address")
        block_number = section.get("block_number")
        block_hash = section.get("block_hash")
        try:
            normalized_predeploy = normalize_address(str(predeploy))
            normalize_hash32(str(block_hash), "block_hash")
        except (TypeError, ValueError):
            normalized_predeploy = ""
        ok = (
            status == "ok"
            and header_matches is True
            and chain_id == BASE_CHAIN_ID
            and normalized_predeploy == BASE_TIME_PREDEPLOY
            and type(block_number) is int
            and block_number >= 0
        )
        observed = f"status={status!r},header_match={header_matches!r},block={block_number!r}"
        if not ok:
            return (
                GateCheckResult(
                    name="base_time_predeploy_verified",
                    passed=False,
                    detail=(
                        "BaseTime predeploy verification requires live status=='ok' and "
                        "header_matches_predeploy=True on Base."
                    ),
                    required_threshold=threshold,
                    observed_value=observed,
                ),
                action,
            )
        return (
            GateCheckResult(
                name="base_time_predeploy_verified",
                passed=True,
                detail="BaseTime predeploy cross-checked cleanly against pinned Base block header.",
                required_threshold=threshold,
                observed_value=observed,
            ),
            None,
        )

    # Fallback: direct BaseTimeVerificationReceipt JSON
    chain_id = radar_data.get("chain_id")
    predeploy = radar_data.get("predeploy_address")
    header_ms = radar_data.get("header_timestamp_ms")
    predeploy_ms = radar_data.get("predeploy_timestamp_ms")
    block_number = radar_data.get("block_number")
    rpc_origin = radar_data.get("rpc_origin")
    try:
        normalized_predeploy = normalize_address(str(predeploy))
    except ValueError:
        normalized_predeploy = ""

    if (
        chain_id == BASE_CHAIN_ID
        and normalized_predeploy == BASE_TIME_PREDEPLOY
        and type(header_ms) is int
        and type(predeploy_ms) is int
        and header_ms == predeploy_ms
        and type(block_number) is int
        and block_number >= 0
        and isinstance(rpc_origin, str)
        and rpc_origin.startswith("https://")
    ):
        return (
            GateCheckResult(
                name="base_time_predeploy_verified",
                passed=True,
                detail="BaseTime standalone verification receipt matches Base block header ms.",
                required_threshold=threshold,
                observed_value=f"status='ok',header_match=True,block={block_number}",
            ),
            None,
        )

    return (
        GateCheckResult(
            name="base_time_predeploy_verified",
            passed=False,
            detail="Input file did not contain a valid live BaseTime predeploy verification.",
            required_threshold=threshold,
            observed_value="unverified",
        ),
        action,
    )


def _eval_untouched_shadow_sample_size(
    shadow_data: dict[str, Any] | None,
    shadow_err: str | None,
    *,
    min_shadow_ticks: int,
) -> tuple[GateCheckResult, str | None]:
    threshold = f"mode=='public-shadow' and ticks_completed>={min_shadow_ticks}"
    action = (
        f"Run the Base live shadow pipeline in public-shadow mode for at least "
        f"{min_shadow_ticks} untouched ticks and pass --shadow-summary."
    )
    if shadow_data is None:
        return (
            GateCheckResult(
                name="untouched_shadow_sample_size",
                passed=False,
                detail=shadow_err or "Shadow summary missing or corrupt",
                required_threshold=threshold,
                observed_value="missing_or_invalid",
            ),
            action,
        )
    if shadow_data.get("execution_eligible", False) is not False:
        return (
            GateCheckResult(
                name="untouched_shadow_sample_size",
                passed=False,
                detail="Shadow summary must keep execution_eligible=False",
                required_threshold=threshold,
                observed_value=f"execution_eligible={shadow_data.get('execution_eligible')!r}",
            ),
            action,
        )

    mode = shadow_data.get("mode")
    ticks = shadow_data.get("ticks_completed")
    observed = f"mode={mode!r},ticks_completed={ticks!r}"
    if mode != "public-shadow":
        return (
            GateCheckResult(
                name="untouched_shadow_sample_size",
                passed=False,
                detail=f"Shadow mode must be 'public-shadow' (got {mode!r}).",
                required_threshold=threshold,
                observed_value=observed,
            ),
            action,
        )
    if type(ticks) is not int or ticks < min_shadow_ticks:
        remaining = min_shadow_ticks - max(0, ticks) if type(ticks) is int else min_shadow_ticks
        return (
            GateCheckResult(
                name="untouched_shadow_sample_size",
                passed=False,
                detail=(
                    f"Shadow sample size {ticks!r} is below required {min_shadow_ticks} ticks "
                    f"({remaining} ticks remaining)."
                ),
                required_threshold=threshold,
                observed_value=observed,
            ),
            (
                f"Collect {remaining} additional untouched public-shadow ticks to reach "
                f"{min_shadow_ticks} ticks."
            ),
        )

    return (
        GateCheckResult(
            name="untouched_shadow_sample_size",
            passed=True,
            detail=f"Completed {ticks} untouched public-shadow ticks (>= {min_shadow_ticks}).",
            required_threshold=threshold,
            observed_value=observed,
        ),
        None,
    )


def _sanitize_evidence_value(value: object) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _sanitize_evidence_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_evidence_value(v) for v in value]
    return str(value)


def _validate_oos_execution_summary(
    raw_summary: object,
    *,
    source: str,
    min_oos_trades: int,
    min_lcb_net_edge_bps: int,
) -> tuple[bool, str | None, dict[str, Any]]:
    if not isinstance(raw_summary, Mapping):
        err = f"oos_execution_summary must be a mapping, got {type(raw_summary).__name__}"
        return (
            False,
            err,
            {
                "source": source,
                "validated": False,
                "min_oos_trades": min_oos_trades,
                "min_lcb_net_edge_bps": min_lcb_net_edge_bps,
                "validation_error": err,
                "raw_type": type(raw_summary).__name__,
            },
        )

    metrics = _sanitize_evidence_value(raw_summary)
    errors: list[str] = []

    if raw_summary.get("execution_eligible", False) is not False:
        errors.append("oos_execution_summary must keep execution_eligible=False")

    trades_count = raw_summary.get("oos_trades_count")
    if type(trades_count) is not int or trades_count < min_oos_trades:
        errors.append(f"oos_trades_count ({trades_count!r}) must be an integer >= {min_oos_trades}")

    pos_trades = raw_summary.get("oos_positive_trades")
    if type(pos_trades) is not int or pos_trades <= 0:
        errors.append(f"oos_positive_trades ({pos_trades!r}) must be an integer > 0")
    elif type(trades_count) is int and pos_trades > trades_count:
        errors.append(
            f"oos_positive_trades ({pos_trades}) exceeds oos_trades_count ({trades_count})"
        )

    raw_profit = raw_summary.get("oos_total_net_profit_usd")
    if raw_profit is None or isinstance(raw_profit, bool):
        errors.append(f"oos_total_net_profit_usd ({raw_profit!r}) must be > 0")
    else:
        try:
            profit_text = str(raw_profit).strip()
            profit_dec = Decimal(profit_text) if profit_text else Decimal("NaN")
        except InvalidOperation:
            profit_dec = Decimal("NaN")
        if not profit_dec.is_finite() or profit_dec <= 0:
            errors.append(f"oos_total_net_profit_usd ({raw_profit!r}) must be finite and > 0")

    oos_lcb = raw_summary.get("oos_lcb_net_edge_bps")
    if type(oos_lcb) is not int or oos_lcb < min_lcb_net_edge_bps:
        errors.append(
            f"oos_lcb_net_edge_bps ({oos_lcb!r}) must be an integer >= {min_lcb_net_edge_bps}"
        )

    passed = len(errors) == 0
    err_msg = "; ".join(errors) if errors else None
    evidence_entry: dict[str, Any] = {
        **metrics,
        "source": source,
        "validated": passed,
        "min_oos_trades": min_oos_trades,
        "min_lcb_net_edge_bps": min_lcb_net_edge_bps,
        "validation_error": err_msg,
    }
    return passed, err_msg, evidence_entry


def _eval_legacy_positive_cost_adjusted_lcb_edge(
    shadow_data: dict[str, Any] | None,
    shadow_err: str | None,
    *,
    min_lcb_net_edge_bps: int,
) -> tuple[GateCheckResult, str | None]:
    threshold = f"positive_net_ticks>0 and lcb_net_edge_bps>={min_lcb_net_edge_bps}"
    action = (
        f"Demonstrate positive cost-adjusted out-of-sample public-shadow edge "
        f"(positive_net_ticks > 0 and lcb_net_edge_bps >= {min_lcb_net_edge_bps})."
    )
    if shadow_data is None:
        return (
            GateCheckResult(
                name="positive_cost_adjusted_lcb_edge",
                passed=False,
                detail=shadow_err or "Shadow summary missing or corrupt",
                required_threshold=threshold,
                observed_value="missing_or_invalid",
            ),
            action,
        )

    mode = shadow_data.get("mode")
    pos_ticks = shadow_data.get("positive_net_ticks")
    lcb_bps = shadow_data.get("lcb_net_edge_bps")
    observed = f"mode={mode!r},positive_net_ticks={pos_ticks!r},lcb_net_edge_bps={lcb_bps!r}"
    if mode != "public-shadow":
        return (
            GateCheckResult(
                name="positive_cost_adjusted_lcb_edge",
                passed=False,
                detail=f"LCB edge requires mode=='public-shadow' (got {mode!r}).",
                required_threshold=threshold,
                observed_value=observed,
            ),
            action,
        )
    if type(pos_ticks) is not int or pos_ticks <= 0:
        return (
            GateCheckResult(
                name="positive_cost_adjusted_lcb_edge",
                passed=False,
                detail=(
                    f"Shadow summary reports zero/invalid positive_net_ticks ({pos_ticks!r}); "
                    "net cost-adjusted profitability is unproven."
                ),
                required_threshold=threshold,
                observed_value=observed,
            ),
            action,
        )
    if type(lcb_bps) is not int or lcb_bps < min_lcb_net_edge_bps:
        return (
            GateCheckResult(
                name="positive_cost_adjusted_lcb_edge",
                passed=False,
                detail=(
                    f"Shadow LCB net edge {lcb_bps!r} bps is below threshold "
                    f"{min_lcb_net_edge_bps} bps."
                ),
                required_threshold=threshold,
                observed_value=observed,
            ),
            action,
        )

    return (
        GateCheckResult(
            name="positive_cost_adjusted_lcb_edge",
            passed=True,
            detail=(
                f"Public-shadow recorded {pos_ticks} positive net ticks with "
                f"lcb_net_edge_bps={lcb_bps} (>= {min_lcb_net_edge_bps})."
            ),
            required_threshold=threshold,
            observed_value=observed,
        ),
        None,
    )


def _eval_positive_cost_adjusted_lcb_edge(
    shadow_data: dict[str, Any] | None,
    shadow_err: str | None,
    *,
    min_lcb_net_edge_bps: int,
    min_oos_trades: int = DEFAULT_MIN_OOS_TRADES,
    oos_execution_summary: object = None,
) -> tuple[GateCheckResult, str | None, dict[str, Any] | None]:
    legacy_gate, legacy_action = _eval_legacy_positive_cost_adjusted_lcb_edge(
        shadow_data,
        shadow_err,
        min_lcb_net_edge_bps=min_lcb_net_edge_bps,
    )

    effective_oos: object = None
    oos_source: str | None = None
    if oos_execution_summary is not None:
        effective_oos = oos_execution_summary
        oos_source = "explicit_arg"
    elif (
        shadow_data is not None
        and "oos_execution_summary" in shadow_data
        and shadow_data["oos_execution_summary"] is not None
    ):
        effective_oos = shadow_data["oos_execution_summary"]
        oos_source = "shadow_summary"

    if effective_oos is None or oos_source is None:
        return legacy_gate, legacy_action, None

    oos_passed, oos_err, oos_evidence = _validate_oos_execution_summary(
        effective_oos,
        source=oos_source,
        min_oos_trades=min_oos_trades,
        min_lcb_net_edge_bps=min_lcb_net_edge_bps,
    )
    threshold = (
        f"(positive_net_ticks>0 and lcb_net_edge_bps>={min_lcb_net_edge_bps}) OR "
        f"(oos_trades_count>={min_oos_trades}, oos_positive_trades>0, "
        f"oos_total_net_profit_usd>0, oos_lcb_net_edge_bps>={min_lcb_net_edge_bps})"
    )
    oos_obs = (
        f"oos_trades={oos_evidence.get('oos_trades_count')!r},"
        f"oos_pos={oos_evidence.get('oos_positive_trades')!r},"
        f"oos_profit_usd={oos_evidence.get('oos_total_net_profit_usd')!r},"
        f"oos_lcb_bps={oos_evidence.get('oos_lcb_net_edge_bps')!r}"
    )
    combined_obs = f"{legacy_gate.observed_value};{oos_obs}"

    if oos_passed and legacy_gate.passed:
        return (
            GateCheckResult(
                name="positive_cost_adjusted_lcb_edge",
                passed=True,
                detail=f"{legacy_gate.detail} OOS execution also validated ({oos_obs}).",
                required_threshold=threshold,
                observed_value=combined_obs,
            ),
            None,
            oos_evidence,
        )
    if oos_passed:
        return (
            GateCheckResult(
                name="positive_cost_adjusted_lcb_edge",
                passed=True,
                detail=(
                    f"Out-of-sample execution validated ({oos_obs}); "
                    f"legacy shadow check did not pass ({legacy_gate.detail})."
                ),
                required_threshold=threshold,
                observed_value=combined_obs,
            ),
            None,
            oos_evidence,
        )
    if legacy_gate.passed:
        return (
            GateCheckResult(
                name="positive_cost_adjusted_lcb_edge",
                passed=True,
                detail=(
                    f"{legacy_gate.detail} "
                    f"(OOS execution summary failed strict validation: {oos_err})."
                ),
                required_threshold=threshold,
                observed_value=combined_obs,
            ),
            None,
            oos_evidence,
        )

    action = (
        f"Demonstrate positive cost-adjusted out-of-sample edge via >= {min_oos_trades} "
        f"OOS trades with positive net profit and oos_lcb_net_edge_bps >= "
        f"{min_lcb_net_edge_bps}, or public-shadow lcb_net_edge_bps >= "
        f"{min_lcb_net_edge_bps}."
    )
    return (
        GateCheckResult(
            name="positive_cost_adjusted_lcb_edge",
            passed=False,
            detail=(
                f"Legacy shadow check failed ({legacy_gate.detail}) "
                f"and OOS execution check failed ({oos_err})."
            ),
            required_threshold=threshold,
            observed_value=combined_obs,
        ),
        action,
        oos_evidence,
    )


def _has_nonzero_residual(value: object) -> bool:
    if value is None:
        return False
    if type(value) is bool:
        return value
    if type(value) is int:
        return value != 0
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) > 0
    return True


def _eval_revert_budget_healthy(
    anvil_data: dict[str, Any] | None,
    radar_data: dict[str, Any] | None,
    shadow_data: dict[str, Any] | None,
) -> tuple[GateCheckResult, str | None]:
    threshold = "revert_budget_status=='active' and unresolved_residuals==0"
    action = (
        "Clear any halted revert budget or unresolved execution residuals after operator "
        "reconciliation, and supply a passing Anvil E2E report."
    )

    if shadow_data is not None:
        shadow_rb = shadow_data.get("revert_budget_status")
        if shadow_rb is not None and shadow_rb != "active":
            return (
                GateCheckResult(
                    name="revert_budget_healthy",
                    passed=False,
                    detail=(
                        f"Shadow summary revert_budget_status is {shadow_rb!r}, expected 'active'."
                    ),
                    required_threshold=threshold,
                    observed_value=f"shadow_revert_budget={shadow_rb!r}",
                ),
                action,
            )
        if shadow_data.get("revert_budget_halted") is True or (
            shadow_data.get("deep_revert_halt") is True
        ):
            return (
                GateCheckResult(
                    name="revert_budget_healthy",
                    passed=False,
                    detail="Shadow summary indicates revert budget HALT is latched.",
                    required_threshold=threshold,
                    observed_value="shadow_revert_budget='halt'",
                ),
                action,
            )
        for residual_key in (
            "unresolved_residuals",
            "unresolved_residual_count",
            "open_pending_receipts",
            "ambiguous_halt_count",
        ):
            if residual_key in shadow_data and _has_nonzero_residual(shadow_data[residual_key]):
                return (
                    GateCheckResult(
                        name="revert_budget_healthy",
                        passed=False,
                        detail=f"Shadow summary reports unresolved residual ({residual_key}).",
                        required_threshold=threshold,
                        observed_value=f"{residual_key}={shadow_data[residual_key]!r}",
                    ),
                    action,
                )

    if radar_data is not None:
        rust_sec = radar_data.get("rust_fastpath_and_atomic_executor")
        if isinstance(rust_sec, dict):
            bundle_sec = rust_sec.get("atomic_executor_bundle")
            if isinstance(bundle_sec, dict):
                radar_rb = bundle_sec.get("revert_budget_status")
                if radar_rb is not None and radar_rb != "active":
                    return (
                        GateCheckResult(
                            name="revert_budget_healthy",
                            passed=False,
                            detail=f"Radar bundle revert_budget_status is {radar_rb!r}.",
                            required_threshold=threshold,
                            observed_value=f"radar_revert_budget={radar_rb!r}",
                        ),
                        action,
                    )

    if anvil_data is None:
        return (
            GateCheckResult(
                name="revert_budget_healthy",
                passed=False,
                detail="Cannot confirm healthy revert budget without verified Anvil E2E evidence.",
                required_threshold=threshold,
                observed_value="missing_anvil_evidence",
            ),
            action,
        )

    drills = anvil_data.get("drills")
    if not isinstance(drills, dict):
        return (
            GateCheckResult(
                name="revert_budget_healthy",
                passed=False,
                detail="Anvil report missing drills structure for revert budget inspection.",
                required_threshold=threshold,
                observed_value="malformed_anvil_drills",
            ),
            action,
        )

    early_drill = drills.get("executor_early_abort_under_65k_gas")
    vault_drill = drills.get("vault_reactor_fill_and_zero_allowances")
    if not isinstance(early_drill, dict) or early_drill.get("revert_budget_status") != "active":
        status_val = (
            early_drill.get("revert_budget_status") if isinstance(early_drill, dict) else None
        )
        return (
            GateCheckResult(
                name="revert_budget_healthy",
                passed=False,
                detail=f"Anvil early-abort drill revert_budget_status={status_val!r} != 'active'.",
                required_threshold=threshold,
                observed_value=f"anvil_revert_budget={status_val!r}",
            ),
            action,
        )
    allowances = vault_drill.get("allowance_residuals") if isinstance(vault_drill, dict) else None
    if (
        not isinstance(allowances, dict)
        or not allowances
        or any(type(v) is not int or v != 0 for v in allowances.values())
    ):
        return (
            GateCheckResult(
                name="revert_budget_healthy",
                passed=False,
                detail="Anvil vault drill reports non-zero or missing allowance residuals.",
                required_threshold=threshold,
                observed_value="nonzero_allowance_residual",
            ),
            action,
        )

    return (
        GateCheckResult(
            name="revert_budget_healthy",
            passed=True,
            detail="Revert budget is active across evidence with zero unresolved residuals.",
            required_threshold=threshold,
            observed_value="revert_budget_status='active',unresolved_residuals=0",
        ),
        None,
    )


def _is_clean_provider_label(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    lowered = value.strip().lower()
    if any(marker in lowered for marker in _FORBIDDEN_PROVIDER_MARKERS):
        return False
    return _RAW_HEX_KEY_RE.search(value) is None


def _validate_distinct_rpc_providers(
    primary: object,
    secondary: object,
    *,
    threshold: str,
) -> tuple[str, str] | GateCheckResult:
    if not _is_clean_provider_label(primary) or not _is_clean_provider_label(secondary):
        return GateCheckResult(
            name="dual_rpc_quorum_verified",
            passed=False,
            detail="Primary and secondary RPC provider identifiers must be non-placeholder.",
            required_threshold=threshold,
            observed_value="invalid_provider_labels",
        )
    primary_clean = str(primary).strip()
    secondary_clean = str(secondary).strip()
    if primary_clean.lower() == secondary_clean.lower():
        return GateCheckResult(
            name="dual_rpc_quorum_verified",
            passed=False,
            detail="Secondary RPC provider must be independent and distinct from primary.",
            required_threshold=threshold,
            observed_value="duplicate_provider",
        )
    return (primary_clean, secondary_clean)


def _validate_quorum_hash32(
    raw_value: object,
    *,
    field_name: str,
    threshold: str,
) -> str | GateCheckResult:
    if not isinstance(raw_value, str):
        return GateCheckResult(
            name="dual_rpc_quorum_verified",
            passed=False,
            detail=f"DualRpcQuorumReport {field_name} must be a 32-byte hex string.",
            required_threshold=threshold,
            observed_value=f"invalid_{field_name}",
        )
    try:
        return normalize_hash32(raw_value, field_name)
    except ValueError:
        return GateCheckResult(
            name="dual_rpc_quorum_verified",
            passed=False,
            detail=f"DualRpcQuorumReport {field_name} must be a valid 32-byte hex hash.",
            required_threshold=threshold,
            observed_value=f"invalid_{field_name}",
        )


def _eval_dual_rpc_quorum_report(
    report: dict[str, Any],
    *,
    threshold: str,
    action: str,
) -> tuple[GateCheckResult, str | None]:
    chain_id = report.get("chain_id")
    if type(chain_id) is not int or chain_id != BASE_CHAIN_ID:
        return (
            GateCheckResult(
                name="dual_rpc_quorum_verified",
                passed=False,
                detail="DualRpcQuorumReport chain_id must equal Base (8453).",
                required_threshold=threshold,
                observed_value=f"chain_id={chain_id!r}",
            ),
            action,
        )

    exec_eligible = report.get("execution_eligible")
    if exec_eligible is not False:
        return (
            GateCheckResult(
                name="dual_rpc_quorum_verified",
                passed=False,
                detail="DualRpcQuorumReport must retain execution_eligible=False.",
                required_threshold=threshold,
                observed_value=f"execution_eligible={exec_eligible!r}",
            ),
            action,
        )

    declared_sha = report.get("report_sha256")
    body = {k: v for k, v in report.items() if k != "report_sha256"}
    expected_sha = sha256_hex(canonical_json(body))
    if not isinstance(declared_sha, str) or declared_sha != expected_sha:
        return (
            GateCheckResult(
                name="dual_rpc_quorum_verified",
                passed=False,
                detail="DualRpcQuorumReport report_sha256 integrity check failed.",
                required_threshold=threshold,
                observed_value="checksum_mismatch",
            ),
            action,
        )

    provider_check = _validate_distinct_rpc_providers(
        report.get("primary_rpc_provider"),
        report.get("secondary_rpc_provider"),
        threshold=threshold,
    )
    if isinstance(provider_check, GateCheckResult):
        return provider_check, action
    primary_clean, secondary_clean = provider_check

    secondary_ok = report.get("secondary_rpc_verified")
    if secondary_ok is not True:
        return (
            GateCheckResult(
                name="dual_rpc_quorum_verified",
                passed=False,
                detail="DualRpcQuorumReport requires secondary_rpc_verified=True.",
                required_threshold=threshold,
                observed_value=f"secondary_rpc_verified={secondary_ok!r}",
            ),
            action,
        )

    block_number = report.get("pinned_block_number")
    if type(block_number) is not int or block_number < 0:
        return (
            GateCheckResult(
                name="dual_rpc_quorum_verified",
                passed=False,
                detail="DualRpcQuorumReport pinned_block_number must be a non-negative integer.",
                required_threshold=threshold,
                observed_value=f"pinned_block_number={block_number!r}",
            ),
            action,
        )

    block_hash_check = _validate_quorum_hash32(
        report.get("pinned_block_hash"),
        field_name="pinned_block_hash",
        threshold=threshold,
    )
    if isinstance(block_hash_check, GateCheckResult):
        return block_hash_check, action

    obs_parts = [
        f"providers={primary_clean}+{secondary_clean}",
        f"block={block_number}",
        f"block_hash={block_hash_check[:14]}",
    ]
    if "state_root" in report and report["state_root"] is not None:
        state_root_check = _validate_quorum_hash32(
            report["state_root"],
            field_name="state_root",
            threshold=threshold,
        )
        if isinstance(state_root_check, GateCheckResult):
            return state_root_check, action
        obs_parts.append(f"state_root={state_root_check[:14]}")

    if "base_time_ms" in report and report["base_time_ms"] is not None:
        base_time_ms = report["base_time_ms"]
        if type(base_time_ms) is not int or base_time_ms < 0:
            return (
                GateCheckResult(
                    name="dual_rpc_quorum_verified",
                    passed=False,
                    detail="DualRpcQuorumReport base_time_ms must be a non-negative integer.",
                    required_threshold=threshold,
                    observed_value=f"base_time_ms={base_time_ms!r}",
                ),
                action,
            )
        obs_parts.append(f"base_time_ms={base_time_ms}")

    return (
        GateCheckResult(
            name="dual_rpc_quorum_verified",
            passed=True,
            detail=(
                f"Dual RPC quorum verified on Base ({primary_clean} / {secondary_clean}) "
                f"at pinned block {block_number}."
            ),
            required_threshold=threshold,
            observed_value=",".join(obs_parts),
        ),
        None,
    )


def _eval_legacy_dual_rpc_section(
    proof: dict[str, Any],
    *,
    threshold: str,
    action: str,
) -> tuple[GateCheckResult, str | None]:
    provider_check = _validate_distinct_rpc_providers(
        proof.get("primary_rpc_provider"),
        proof.get("secondary_rpc_provider"),
        threshold=threshold,
    )
    if isinstance(provider_check, GateCheckResult):
        return provider_check, action
    primary_clean, secondary_clean = provider_check

    secondary_ok = proof.get("secondary_rpc_verified")
    if secondary_ok is not True:
        return (
            GateCheckResult(
                name="dual_rpc_quorum_verified",
                passed=False,
                detail="secondary_rpc_verified must be True.",
                required_threshold=threshold,
                observed_value=f"secondary_rpc_verified={secondary_ok!r}",
            ),
            action,
        )

    return (
        GateCheckResult(
            name="dual_rpc_quorum_verified",
            passed=True,
            detail=(
                f"Distinct primary/secondary RPC providers "
                f"({primary_clean} / {secondary_clean}) verified."
            ),
            required_threshold=threshold,
            observed_value=f"providers={primary_clean}+{secondary_clean}",
        ),
        None,
    )


def _validate_authority_proof_integrity(
    payload: dict[str, Any],
    *,
    gate_name: str,
    threshold: str,
) -> GateCheckResult | None:
    schema = payload.get("schema")
    if "schema" in payload and schema not in PROFIT_AUTHORITY_INFRA_SCHEMAS:
        return GateCheckResult(
            name=gate_name,
            passed=False,
            detail=f"Unsupported authority proof schema: {schema!r}.",
            required_threshold=threshold,
            observed_value=f"schema={schema!r}",
        )
    if "execution_eligible" in payload and payload.get("execution_eligible") is not False:
        return GateCheckResult(
            name=gate_name,
            passed=False,
            detail="Authority proof must retain execution_eligible=False.",
            required_threshold=threshold,
            observed_value=f"execution_eligible={payload.get('execution_eligible')!r}",
        )
    if "account" in payload:
        raw_account = payload.get("account")
        if not isinstance(raw_account, str):
            return GateCheckResult(
                name=gate_name,
                passed=False,
                detail="Authority proof account must be an EVM address string.",
                required_threshold=threshold,
                observed_value=f"account={raw_account!r}",
            )
        try:
            norm_account = normalize_address(raw_account)
        except ValueError:
            return GateCheckResult(
                name=gate_name,
                passed=False,
                detail="Authority proof account is not a valid EVM address.",
                required_threshold=threshold,
                observed_value="invalid_account",
            )
        if norm_account == ZERO_ADDRESS:
            return GateCheckResult(
                name=gate_name,
                passed=False,
                detail="Authority proof account cannot be the zero address.",
                required_threshold=threshold,
                observed_value="zero_account",
            )
    if "authority_status" in payload:
        status = payload.get("authority_status")
        if not isinstance(status, dict):
            return GateCheckResult(
                name=gate_name,
                passed=False,
                detail="Authority proof authority_status must be an object.",
                required_threshold=threshold,
                observed_value="malformed_authority_status",
            )
        if status.get("halted") is True or status.get("stop_present") is True:
            return GateCheckResult(
                name=gate_name,
                passed=False,
                detail="Authority status reports halted=True or stop_present=True.",
                required_threshold=threshold,
                observed_value="authority_halted_or_stopped",
            )
    if "proof_sha256" in payload or schema in PROFIT_AUTHORITY_INFRA_SCHEMAS:
        declared_sha = payload.get("proof_sha256")
        body = {k: v for k, v in payload.items() if k != "proof_sha256"}
        expected_sha = sha256_hex(canonical_json(body))
        if not isinstance(declared_sha, str) or declared_sha != expected_sha:
            return GateCheckResult(
                name=gate_name,
                passed=False,
                detail="Authority proof proof_sha256 integrity check failed.",
                required_threshold=threshold,
                observed_value="checksum_mismatch",
            )
    return None


def _eval_dual_rpc_quorum_verified(
    signoff_data: dict[str, Any] | None,
    signoff_err: str | None,
    infra_data: dict[str, Any] | None,
    infra_err: str | None,
) -> tuple[GateCheckResult, str | None]:
    threshold = "distinct primary & secondary RPC verified with secondary_rpc_verified=True"
    action = (
        "Provision an independent secondary Base archive/simulation RPC, verify dual-RPC "
        "quorum against a pinned Base block, and pass DualRpcQuorumReport via --infra-proof "
        "(or combined authority proof / legacy metadata via --operator-signoff)."
    )

    if infra_err is not None and "not supplied" not in infra_err:
        return (
            GateCheckResult(
                name="dual_rpc_quorum_verified",
                passed=False,
                detail=infra_err,
                required_threshold=threshold,
                observed_value="invalid_infra_proof",
            ),
            action,
        )

    if infra_data is not None:
        if infra_data.get("schema_version") == DUAL_RPC_QUORUM_SCHEMA_VERSION:
            return _eval_dual_rpc_quorum_report(infra_data, threshold=threshold, action=action)
        if "schema_version" in infra_data:
            return (
                GateCheckResult(
                    name="dual_rpc_quorum_verified",
                    passed=False,
                    detail=(
                        f"Unsupported infra_proof schema_version: "
                        f"{infra_data.get('schema_version')!r}"
                    ),
                    required_threshold=threshold,
                    observed_value=f"schema={infra_data.get('schema_version')!r}",
                ),
                action,
            )
        integrity_err = _validate_authority_proof_integrity(
            infra_data,
            gate_name="dual_rpc_quorum_verified",
            threshold=threshold,
        )
        if integrity_err is not None:
            return integrity_err, action
        if "dual_rpc_quorum" in infra_data:
            nested = infra_data.get("dual_rpc_quorum")
            if not isinstance(nested, dict):
                return (
                    GateCheckResult(
                        name="dual_rpc_quorum_verified",
                        passed=False,
                        detail="infra_proof.dual_rpc_quorum must be an object.",
                        required_threshold=threshold,
                        observed_value="malformed_dual_rpc_quorum",
                    ),
                    action,
                )
            if nested.get("schema_version") == DUAL_RPC_QUORUM_SCHEMA_VERSION:
                return _eval_dual_rpc_quorum_report(nested, threshold=threshold, action=action)
            return _eval_legacy_dual_rpc_section(nested, threshold=threshold, action=action)
        return _eval_legacy_dual_rpc_section(infra_data, threshold=threshold, action=action)

    if signoff_data is not None:
        embedded = signoff_data.get("dual_provider_and_isolated_signer")
        if isinstance(embedded, dict):
            return _eval_legacy_dual_rpc_section(embedded, threshold=threshold, action=action)

    detail = (
        signoff_err
        if (signoff_err is not None and "not supplied" not in signoff_err)
        else "Dual-RPC quorum proof not supplied."
    )
    return (
        GateCheckResult(
            name="dual_rpc_quorum_verified",
            passed=False,
            detail=detail,
            required_threshold=threshold,
            observed_value="unprovisioned",
        ),
        action,
    )


def _eval_isolated_signer_authority_ready(
    signoff_data: dict[str, Any] | None,
    signoff_err: str | None,
    infra_data: dict[str, Any] | None,
    infra_err: str | None,
    signer_data: dict[str, Any] | None = None,
    signer_err: str | None = None,
) -> tuple[GateCheckResult, str | None]:
    threshold = (
        "isolated_signer_verified=True with non-empty isolated_key_volume and authority_socket_path"
    )
    action = (
        "Provision the Docker isolated signer key volume (Foundry encrypted keystore / "
        "software EOA) and Unix authority socket via run_profit_authority --status-only "
        "--out-proof, and supply --signer-proof (or --infra-proof / --operator-signoff)."
    )

    if signer_err is not None and "not supplied" not in signer_err:
        return (
            GateCheckResult(
                name="isolated_signer_authority_ready",
                passed=False,
                detail=signer_err,
                required_threshold=threshold,
                observed_value="invalid_signer_proof",
            ),
            action,
        )

    isolated_signer_section: dict[str, Any] | None = None
    if signer_data is not None:
        if "schema_version" in signer_data:
            return (
                GateCheckResult(
                    name="isolated_signer_authority_ready",
                    passed=False,
                    detail=(
                        f"Unsupported signer_proof schema_version: "
                        f"{signer_data.get('schema_version')!r}"
                    ),
                    required_threshold=threshold,
                    observed_value=f"schema={signer_data.get('schema_version')!r}",
                ),
                action,
            )
        integrity_err = _validate_authority_proof_integrity(
            signer_data,
            gate_name="isolated_signer_authority_ready",
            threshold=threshold,
        )
        if integrity_err is not None:
            return integrity_err, action
        if "isolated_signer" in signer_data:
            raw_sec = signer_data.get("isolated_signer")
            if not isinstance(raw_sec, dict):
                return (
                    GateCheckResult(
                        name="isolated_signer_authority_ready",
                        passed=False,
                        detail="signer_proof.isolated_signer must be an object.",
                        required_threshold=threshold,
                        observed_value="malformed_isolated_signer",
                    ),
                    action,
                )
            isolated_signer_section = raw_sec
        else:
            isolated_signer_section = signer_data
    elif infra_err is not None and "not supplied" not in infra_err:
        return (
            GateCheckResult(
                name="isolated_signer_authority_ready",
                passed=False,
                detail=infra_err,
                required_threshold=threshold,
                observed_value="invalid_infra_proof",
            ),
            action,
        )
    elif (
        infra_data is not None
        and infra_data.get("schema_version") != DUAL_RPC_QUORUM_SCHEMA_VERSION
    ):
        if "schema_version" in infra_data:
            return (
                GateCheckResult(
                    name="isolated_signer_authority_ready",
                    passed=False,
                    detail=(
                        f"Unsupported infra_proof schema_version: "
                        f"{infra_data.get('schema_version')!r}"
                    ),
                    required_threshold=threshold,
                    observed_value=f"schema={infra_data.get('schema_version')!r}",
                ),
                action,
            )
        integrity_err = _validate_authority_proof_integrity(
            infra_data,
            gate_name="isolated_signer_authority_ready",
            threshold=threshold,
        )
        if integrity_err is not None:
            return integrity_err, action
        if "isolated_signer" in infra_data:
            raw_sec = infra_data.get("isolated_signer")
            if not isinstance(raw_sec, dict):
                return (
                    GateCheckResult(
                        name="isolated_signer_authority_ready",
                        passed=False,
                        detail="infra_proof.isolated_signer must be an object.",
                        required_threshold=threshold,
                        observed_value="malformed_isolated_signer",
                    ),
                    action,
                )
            isolated_signer_section = raw_sec
        else:
            isolated_signer_section = infra_data

    if isolated_signer_section is None and signoff_data is not None:
        if "isolated_signer" in signoff_data:
            raw_signoff_sec = signoff_data.get("isolated_signer")
            if not isinstance(raw_signoff_sec, dict):
                return (
                    GateCheckResult(
                        name="isolated_signer_authority_ready",
                        passed=False,
                        detail="operator_signoff.isolated_signer must be an object.",
                        required_threshold=threshold,
                        observed_value="malformed_isolated_signer",
                    ),
                    action,
                )
            isolated_signer_section = raw_signoff_sec
        else:
            embedded = signoff_data.get("dual_provider_and_isolated_signer")
            if isinstance(embedded, dict):
                isolated_signer_section = embedded

    if isolated_signer_section is None:
        detail = (
            signoff_err
            if (signoff_err is not None and "not supplied" not in signoff_err)
            else "Isolated Docker signer authority socket and key-volume proof not supplied."
        )
        return (
            GateCheckResult(
                name="isolated_signer_authority_ready",
                passed=False,
                detail=detail,
                required_threshold=threshold,
                observed_value="unprovisioned",
            ),
            action,
        )

    signer_ok = isolated_signer_section.get("isolated_signer_verified")
    key_volume = isolated_signer_section.get("isolated_key_volume")
    socket_path = isolated_signer_section.get("authority_socket_path")

    if signer_ok is not True:
        return (
            GateCheckResult(
                name="isolated_signer_authority_ready",
                passed=False,
                detail="isolated_signer_verified must be True.",
                required_threshold=threshold,
                observed_value=f"isolated_signer_verified={signer_ok!r}",
            ),
            action,
        )
    if (
        not isinstance(key_volume, str)
        or not key_volume.strip()
        or not isinstance(socket_path, str)
        or not socket_path.strip()
        or _RAW_HEX_KEY_RE.search(key_volume) is not None
        or _RAW_HEX_KEY_RE.search(socket_path) is not None
    ):
        return (
            GateCheckResult(
                name="isolated_signer_authority_ready",
                passed=False,
                detail="Isolated key volume name and authority socket path must be non-empty.",
                required_threshold=threshold,
                observed_value="missing_signer_isolation_metadata",
            ),
            action,
        )

    clean_volume = key_volume.strip()
    clean_socket = socket_path.strip()
    return (
        GateCheckResult(
            name="isolated_signer_authority_ready",
            passed=True,
            detail=(
                f"Isolated signer authority socket ({clean_socket}) and key volume "
                f"({clean_volume}) verified."
            ),
            required_threshold=threshold,
            observed_value=f"key_volume={clean_volume},socket={clean_socket}",
        ),
        None,
    )


def _extract_signoff_limit_micros(
    signoff_data: dict[str, Any],
    *,
    micros_key: str,
    usd_key: str,
) -> tuple[int | None, str | None]:
    has_micros = micros_key in signoff_data
    has_usd = usd_key in signoff_data
    if not has_micros and not has_usd:
        return None, f"Owner signoff manifest missing {micros_key} or {usd_key}."

    micros_val: int | None = None
    if has_micros:
        raw_micros = signoff_data[micros_key]
        if type(raw_micros) is not int or raw_micros <= 0:
            return None, f"{micros_key} must be a positive integer."
        micros_val = raw_micros

    usd_as_micros: int | None = None
    raw_usd: object = None
    if has_usd:
        raw_usd = signoff_data[usd_key]
        if type(raw_usd) is not int or raw_usd <= 0:
            return None, f"{usd_key} must be a positive integer."
        usd_as_micros = raw_usd * USD_MICROS_SCALE

    if micros_val is not None and usd_as_micros is not None and micros_val != usd_as_micros:
        return None, f"Conflicting {micros_key} ({micros_val}) and {usd_key} ({raw_usd})."

    return (micros_val if micros_val is not None else usd_as_micros), None


def _eval_owner_scope_release_manifest_present(
    signoff_data: dict[str, Any] | None,
    signoff_err: str | None,
    *,
    active_risk_config: CapitalRiskConfig | None,
    risk_config_err: str | None,
) -> tuple[GateCheckResult, str | None]:
    if active_risk_config is None:
        return (
            GateCheckResult(
                name="owner_scope_release_manifest_present",
                passed=False,
                detail=risk_config_err or "Invalid capital risk configuration.",
                required_threshold="valid oasis.capital-risk-policy.v1 config",
                observed_value="invalid_risk_config",
            ),
            (
                "Supply a valid capital risk policy JSON (--risk-config) matching "
                "oasis.capital-risk-policy.v1."
            ),
        )

    config_sha = active_risk_config.config_sha256
    max_cap_micros = active_risk_config.total_capital_usd_micros
    max_daily_micros = active_risk_config.max_daily_loss_usd_micros
    max_cum_micros = active_risk_config.max_cumulative_loss_usd_micros
    threshold = (
        f"owner_scope_approved=True, capital_usd_micros<={max_cap_micros}, "
        f"daily_loss_usd_micros<={max_daily_micros}, "
        f"cumulative_loss_usd_micros<={max_cum_micros}"
    )
    action = (
        "Generate an explicit owner-attested two-zone software scope manifest via "
        "scripts/create_software_scope_signoff.py --approve-software-scope and pass it via "
        f"--operator-signoff within policy limits (capital_usd_micros<={max_cap_micros}, "
        f"daily_loss_usd_micros<={max_daily_micros}, "
        f"cumulative_loss_usd_micros<={max_cum_micros})."
    )
    if signoff_data is None:
        return (
            GateCheckResult(
                name="owner_scope_release_manifest_present",
                passed=False,
                detail=signoff_err or "Owner scope release approval manifest not supplied.",
                required_threshold=threshold,
                observed_value=f"absent,risk_config_sha256={config_sha}",
            ),
            action,
        )

    schema_version = signoff_data.get("schema_version")
    if "schema_version" in signoff_data and schema_version != SOFTWARE_SCOPE_SIGNOFF_SCHEMA_VERSION:
        return (
            GateCheckResult(
                name="owner_scope_release_manifest_present",
                passed=False,
                detail=f"Unsupported owner signoff schema_version: {schema_version!r}.",
                required_threshold=threshold,
                observed_value=f"schema={schema_version!r},risk_config_sha256={config_sha}",
            ),
            action,
        )
    if "execution_eligible" in signoff_data and signoff_data.get("execution_eligible") is not False:
        return (
            GateCheckResult(
                name="owner_scope_release_manifest_present",
                passed=False,
                detail="Owner signoff manifest must retain execution_eligible=False.",
                required_threshold=threshold,
                observed_value=(
                    f"execution_eligible={signoff_data.get('execution_eligible')!r},"
                    f"risk_config_sha256={config_sha}"
                ),
            ),
            action,
        )
    if "manifest_sha256" in signoff_data or schema_version == SOFTWARE_SCOPE_SIGNOFF_SCHEMA_VERSION:
        declared_manifest_sha = signoff_data.get("manifest_sha256")
        manifest_body = {k: v for k, v in signoff_data.items() if k != "manifest_sha256"}
        expected_manifest_sha = sha256_hex(canonical_json(manifest_body))
        if (
            not isinstance(declared_manifest_sha, str)
            or declared_manifest_sha != expected_manifest_sha
        ):
            return (
                GateCheckResult(
                    name="owner_scope_release_manifest_present",
                    passed=False,
                    detail="Owner signoff manifest_sha256 integrity check failed.",
                    required_threshold=threshold,
                    observed_value=f"checksum_mismatch,risk_config_sha256={config_sha}",
                ),
                action,
            )

    if "rehearsal_only" in signoff_data and type(signoff_data.get("rehearsal_only")) is not bool:
        return (
            GateCheckResult(
                name="owner_scope_release_manifest_present",
                passed=False,
                detail="Owner signoff rehearsal_only must be a boolean if supplied.",
                required_threshold=threshold,
                observed_value=(
                    f"rehearsal_only={signoff_data.get('rehearsal_only')!r},"
                    f"risk_config_sha256={config_sha}"
                ),
            ),
            action,
        )
    if signoff_data.get("rehearsal_only") is True:
        return (
            GateCheckResult(
                name="owner_scope_release_manifest_present",
                passed=False,
                detail=(
                    "Manifest is marked rehearsal_only=True; rehearsal artifacts never authorize "
                    "live release."
                ),
                required_threshold=threshold,
                observed_value=f"rehearsal_only=True,risk_config_sha256={config_sha}",
            ),
            action,
        )

    approved = signoff_data.get("owner_scope_approved")
    owner_id = signoff_data.get("owner_identity")
    scope_id = signoff_data.get("scope_id")
    approved_at = signoff_data.get("approved_at_utc")
    attestation = signoff_data.get("signature_or_attestation")

    if approved is not True:
        return (
            GateCheckResult(
                name="owner_scope_release_manifest_present",
                passed=False,
                detail="Owner signoff manifest does not set owner_scope_approved=True.",
                required_threshold=threshold,
                observed_value=f"owner_scope_approved={approved!r},risk_config_sha256={config_sha}",
            ),
            action,
        )
    for field_name, field_val in (
        ("owner_identity", owner_id),
        ("scope_id", scope_id),
        ("signature_or_attestation", attestation),
    ):
        if not isinstance(field_val, str) or not field_val.strip():
            return (
                GateCheckResult(
                    name="owner_scope_release_manifest_present",
                    passed=False,
                    detail=f"Owner signoff manifest missing non-empty {field_name}.",
                    required_threshold=threshold,
                    observed_value=f"missing_{field_name},risk_config_sha256={config_sha}",
                ),
                action,
            )
        if _RAW_HEX_KEY_RE.search(field_val) is not None:
            return (
                GateCheckResult(
                    name="owner_scope_release_manifest_present",
                    passed=False,
                    detail=f"Owner signoff {field_name} cannot contain 64-hex key material.",
                    required_threshold=threshold,
                    observed_value=f"unsafe_{field_name},risk_config_sha256={config_sha}",
                ),
                action,
            )

    if str(attestation).strip().lower() == "rehearsal-non-authorizing":
        return (
            GateCheckResult(
                name="owner_scope_release_manifest_present",
                passed=False,
                detail="Rehearsal attestation marker never authorizes live release.",
                required_threshold=threshold,
                observed_value=f"rehearsal_attestation,risk_config_sha256={config_sha}",
            ),
            action,
        )

    if "risk_config_sha256" in signoff_data:
        declared_risk_sha = signoff_data.get("risk_config_sha256")
        if not isinstance(declared_risk_sha, str) or declared_risk_sha != config_sha:
            return (
                GateCheckResult(
                    name="owner_scope_release_manifest_present",
                    passed=False,
                    detail=(
                        "Owner signoff risk_config_sha256 does not match active CapitalRiskConfig."
                    ),
                    required_threshold=threshold,
                    observed_value=(
                        f"declared_risk_config_sha256={declared_risk_sha!r},"
                        f"risk_config_sha256={config_sha}"
                    ),
                ),
                action,
            )

    if "admin_eoa" in signoff_data or "operator_eoa" in signoff_data:
        raw_admin = signoff_data.get("admin_eoa")
        raw_operator = signoff_data.get("operator_eoa")
        if (
            not isinstance(raw_admin, str)
            or not raw_admin.strip()
            or not isinstance(raw_operator, str)
            or not raw_operator.strip()
        ):
            return (
                GateCheckResult(
                    name="owner_scope_release_manifest_present",
                    passed=False,
                    detail=(
                        "Owner signoff admin_eoa and operator_eoa must both be non-empty addresses."
                    ),
                    required_threshold=threshold,
                    observed_value=f"invalid_eoa_pair,risk_config_sha256={config_sha}",
                ),
                action,
            )
        try:
            norm_admin = normalize_address(raw_admin.strip())
            norm_operator = normalize_address(raw_operator.strip())
        except ValueError as exc:
            return (
                GateCheckResult(
                    name="owner_scope_release_manifest_present",
                    passed=False,
                    detail=f"Owner signoff contains invalid EOA address: {exc}",
                    required_threshold=threshold,
                    observed_value=f"invalid_eoa_syntax,risk_config_sha256={config_sha}",
                ),
                action,
            )
        if norm_admin == ZERO_ADDRESS or norm_operator == ZERO_ADDRESS:
            return (
                GateCheckResult(
                    name="owner_scope_release_manifest_present",
                    passed=False,
                    detail="Owner signoff admin_eoa and operator_eoa cannot be zero address.",
                    required_threshold=threshold,
                    observed_value=f"zero_eoa,risk_config_sha256={config_sha}",
                ),
                action,
            )
        if norm_admin == norm_operator:
            return (
                GateCheckResult(
                    name="owner_scope_release_manifest_present",
                    passed=False,
                    detail=(
                        "Two-zone custody violation: admin_eoa (Cold) must differ from "
                        "operator_eoa (Hot)."
                    ),
                    required_threshold=threshold,
                    observed_value=f"cold_hot_eoa_collision,risk_config_sha256={config_sha}",
                ),
                action,
            )

    if not isinstance(approved_at, str) or not approved_at.strip():
        return (
            GateCheckResult(
                name="owner_scope_release_manifest_present",
                passed=False,
                detail="Owner signoff manifest missing approved_at_utc ISO-8601 timestamp.",
                required_threshold=threshold,
                observed_value=f"missing_approved_at_utc,risk_config_sha256={config_sha}",
            ),
            action,
        )
    try:
        parsed_dt = datetime.fromisoformat(approved_at)
    except ValueError:
        parsed_dt = None
    if parsed_dt is None or parsed_dt.tzinfo is None or parsed_dt.utcoffset() is None:
        return (
            GateCheckResult(
                name="owner_scope_release_manifest_present",
                passed=False,
                detail="approved_at_utc must be timezone-aware ISO-8601.",
                required_threshold=threshold,
                observed_value=f"approved_at_utc={approved_at!r},risk_config_sha256={config_sha}",
            ),
            action,
        )

    cap_micros, cap_err = _extract_signoff_limit_micros(
        signoff_data,
        micros_key="capital_limit_usd_micros",
        usd_key="capital_limit_usd",
    )
    daily_micros, daily_err = _extract_signoff_limit_micros(
        signoff_data,
        micros_key="daily_loss_limit_usd_micros",
        usd_key="daily_loss_limit_usd",
    )
    cum_micros, cum_err = _extract_signoff_limit_micros(
        signoff_data,
        micros_key="cumulative_loss_limit_usd_micros",
        usd_key="cumulative_loss_limit_usd",
    )
    limit_err = cap_err or daily_err or cum_err
    if limit_err is not None or cap_micros is None or daily_micros is None or cum_micros is None:
        return (
            GateCheckResult(
                name="owner_scope_release_manifest_present",
                passed=False,
                detail=limit_err or "Owner signoff manifest contains invalid capital/loss limits.",
                required_threshold=threshold,
                observed_value=f"invalid_limits,risk_config_sha256={config_sha}",
            ),
            action,
        )

    if (
        cap_micros > max_cap_micros
        or daily_micros > max_daily_micros
        or cum_micros > max_cum_micros
    ):
        return (
            GateCheckResult(
                name="owner_scope_release_manifest_present",
                passed=False,
                detail=(
                    "Approved capital/loss limits exceed active CapitalRiskConfig policy "
                    f"(cap_usd_micros={cap_micros}<={max_cap_micros}, "
                    f"daily_usd_micros={daily_micros}<={max_daily_micros}, "
                    f"cum_usd_micros={cum_micros}<={max_cum_micros})."
                ),
                required_threshold=threshold,
                observed_value=(
                    f"cap_usd_micros={cap_micros},daily_usd_micros={daily_micros},"
                    f"cum_usd_micros={cum_micros},risk_config_sha256={config_sha}"
                ),
            ),
            action,
        )

    return (
        GateCheckResult(
            name="owner_scope_release_manifest_present",
            passed=True,
            detail=(
                f"Owner scope '{scope_id}' approved by '{owner_id}' "
                f"(cap_usd_micros={cap_micros}, daily_usd_micros={daily_micros}, "
                f"cum_usd_micros={cum_micros}, risk_config_sha256={config_sha[:12]})."
            ),
            required_threshold=threshold,
            observed_value=(
                f"scope={scope_id},owner={owner_id},cap_usd_micros={cap_micros},"
                f"risk_config_sha256={config_sha}"
            ),
        ),
        None,
    )


def evaluate_profit_engine_readiness(
    *,
    contracts_out_dir: Path | None = None,
    anvil_report_path: Path | None = None,
    radar_report_path: Path | None = None,
    shadow_summary_path: Path | None = None,
    operator_signoff_path: Path | None = None,
    infra_proof_path: Path | None = None,
    signer_proof_path: Path | None = None,
    min_shadow_ticks: int = DEFAULT_MIN_SHADOW_TICKS,
    min_lcb_net_edge_bps: int = DEFAULT_MIN_LCB_NET_EDGE_BPS,
    min_oos_trades: int = DEFAULT_MIN_OOS_TRADES,
    oos_execution_summary: Mapping[str, Any] | None = None,
    risk_config: CapitalRiskConfig | None = None,
    risk_config_path: Path | None = None,
) -> ProfitEngineReadinessReport:
    """Evaluate all 9 fail-closed release readiness gates from retained evidence files."""
    if type(min_shadow_ticks) is not int or min_shadow_ticks <= 0:
        raise ValueError("min_shadow_ticks must be a positive integer")
    if type(min_lcb_net_edge_bps) is not int or min_lcb_net_edge_bps < 0:
        raise ValueError("min_lcb_net_edge_bps must be a non-negative integer")
    if type(min_oos_trades) is not int or min_oos_trades <= 0:
        raise ValueError("min_oos_trades must be a positive integer")
    if oos_execution_summary is not None and not isinstance(oos_execution_summary, Mapping):
        raise TypeError("oos_execution_summary must be a Mapping or None")
    if risk_config is not None and not isinstance(risk_config, CapitalRiskConfig):
        raise TypeError("risk_config must be a CapitalRiskConfig")
    if risk_config is not None and risk_config_path is not None:
        raise ValueError("specify either risk_config or risk_config_path, not both")

    active_risk_config: CapitalRiskConfig | None
    risk_config_err: str | None = None
    if risk_config is not None:
        active_risk_config = risk_config
    elif risk_config_path is not None:
        try:
            active_risk_config = load_capital_risk_config(risk_config_path)
        except (OSError, ValueError, TypeError) as exc:
            active_risk_config = None
            risk_config_err = f"risk_config invalid: {exc}"
    else:
        active_risk_config = CapitalRiskConfig(
            total_capital_usd_micros=MAX_APPROVED_CAPITAL_USD * USD_MICROS_SCALE,
            max_daily_loss_usd_micros=MAX_APPROVED_DAILY_LOSS_USD * USD_MICROS_SCALE,
            max_cumulative_loss_usd_micros=MAX_APPROVED_CUMULATIVE_LOSS_USD * USD_MICROS_SCALE,
        )

    out_dir = contracts_out_dir if contracts_out_dir is not None else DEFAULT_CONTRACTS_OUT_DIR
    anvil_data, anvil_err = _load_json_object(anvil_report_path, "anvil_report")
    radar_data, radar_err = _load_json_object(radar_report_path, "radar_report")
    shadow_data, shadow_err = _load_json_object(shadow_summary_path, "shadow_summary")
    signoff_data, signoff_err = _load_json_object(operator_signoff_path, "operator_signoff")
    infra_data, infra_err = _load_json_object(infra_proof_path, "infra_proof")
    signer_data, signer_err = _load_json_object(signer_proof_path, "signer_proof")

    gate5_result, gate5_action, oos_evidence = _eval_positive_cost_adjusted_lcb_edge(
        shadow_data,
        shadow_err,
        min_lcb_net_edge_bps=min_lcb_net_edge_bps,
        min_oos_trades=min_oos_trades,
        oos_execution_summary=oos_execution_summary,
    )
    evidence: dict[str, Any] = {}
    if oos_evidence is not None:
        evidence["oos_execution"] = oos_evidence

    evaluations = (
        _eval_compiled_contracts_pinned(out_dir),
        _eval_anvil_e2e_settlement_reconciled(anvil_data, anvil_err),
        _eval_base_time_predeploy_verified(radar_data, radar_err),
        _eval_untouched_shadow_sample_size(
            shadow_data, shadow_err, min_shadow_ticks=min_shadow_ticks
        ),
        (gate5_result, gate5_action),
        _eval_revert_budget_healthy(anvil_data, radar_data, shadow_data),
        _eval_dual_rpc_quorum_verified(signoff_data, signoff_err, infra_data, infra_err),
        _eval_isolated_signer_authority_ready(
            signoff_data,
            signoff_err,
            infra_data,
            infra_err,
            signer_data,
            signer_err,
        ),
        _eval_owner_scope_release_manifest_present(
            signoff_data,
            signoff_err,
            active_risk_config=active_risk_config,
            risk_config_err=risk_config_err,
        ),
    )

    gates = tuple(result for result, _ in evaluations)
    actions = tuple(action for _, action in evaluations if action is not None)
    passed_count = sum(1 for gate in gates if gate.passed)
    blocked_gates = tuple(gate.name for gate in gates if not gate.passed)
    release_permitted = passed_count == len(GATE_NAMES)

    unsigned_payload = {
        "schema_version": READINESS_SCHEMA_VERSION,
        "release_permitted": release_permitted,
        "execution_eligible": False,
        "passed_count": passed_count,
        "total_gates": len(GATE_NAMES),
        "gates": [asdict(gate) for gate in gates],
        "blocked_gates": list(blocked_gates),
        "next_operator_actions": list(actions),
        "evidence": evidence,
    }
    report_sha256 = sha256_hex(canonical_json(unsigned_payload))

    return ProfitEngineReadinessReport(
        release_permitted=release_permitted,
        passed_count=passed_count,
        total_gates=len(GATE_NAMES),
        gates=gates,
        blocked_gates=blocked_gates,
        next_operator_actions=actions,
        report_sha256=report_sha256,
        schema_version=READINESS_SCHEMA_VERSION,
        execution_eligible=False,
        evidence=evidence,
    )


def verify_profit_engine_readiness_report(path: Path) -> tuple[bool, str]:
    """Verify an emitted 9-gate readiness report before allowing live profit-engine actions."""
    data, err = _load_json_object(path, "readiness_report")
    if data is None:
        return False, err or "readiness report unavailable"
    if data.get("schema_version") != READINESS_SCHEMA_VERSION:
        return False, "readiness report schema_version mismatch"
    if data.get("execution_eligible") is not False:
        return False, "readiness report execution_eligible must remain False"
    if data.get("release_permitted") is not True:
        return False, "readiness report does not permit release"
    if data.get("passed_count") != len(GATE_NAMES) or data.get("total_gates") != len(GATE_NAMES):
        return False, "readiness report does not have all gates passed"
    gates = data.get("gates")
    if not isinstance(gates, list) or len(gates) != len(GATE_NAMES):
        return False, "readiness report gates malformed"
    if any(not isinstance(gate, dict) or gate.get("passed") is not True for gate in gates):
        return False, "readiness report contains a blocked gate"
    declared_sha = data.get("report_sha256")
    body = {key: value for key, value in data.items() if key != "report_sha256"}
    if not isinstance(declared_sha, str) or declared_sha != sha256_hex(canonical_json(body)):
        return False, "readiness report report_sha256 integrity check failed"
    return True, "verified"


__all__ = [
    "ANVIL_E2E_SCHEMA_VERSION",
    "BASE_CHAIN_ID",
    "BASE_TIME_PREDEPLOY",
    "DEFAULT_CONTRACTS_OUT_DIR",
    "DEFAULT_MIN_LCB_NET_EDGE_BPS",
    "DEFAULT_MIN_OOS_TRADES",
    "DEFAULT_MIN_SHADOW_TICKS",
    "DUAL_RPC_QUORUM_SCHEMA_VERSION",
    "GATE_NAMES",
    "MAX_APPROVED_CAPITAL_USD",
    "MAX_APPROVED_CUMULATIVE_LOSS_USD",
    "MAX_APPROVED_DAILY_LOSS_USD",
    "MAX_EARLY_ABORT_GAS",
    "PROFIT_AUTHORITY_INFRA_SCHEMAS",
    "READINESS_SCHEMA_VERSION",
    "REQUIRED_ANVIL_DRILLS",
    "SOFTWARE_SCOPE_SIGNOFF_SCHEMA_VERSION",
    "GateCheckResult",
    "ProfitEngineReadinessReport",
    "evaluate_profit_engine_readiness",
    "verify_profit_engine_readiness_report",
]
