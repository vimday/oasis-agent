"""Fail-closed 8-gate canary readiness evaluator for the Oasis profit engine.

Evaluates compiled Solidity artifacts, loopback Anvil E2E settlement results,
BaseTime predeploy verification, public-shadow sample size and cost-adjusted
LCB edge, rolling revert-budget health, dual-provider + isolated-signer proof,
and owner-signed scope approval. No input or passing verdict grants signing or
broadcast authority (`execution_eligible` is permanently `False`).
"""

import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

from ..models.common import canonical_json, normalize_address, normalize_hash32, sha256_hex

READINESS_SCHEMA_VERSION: Final[str] = "oasis.profit-engine-readiness.v1"
ANVIL_E2E_SCHEMA_VERSION: Final[str] = "oasis-anvil-profit-e2e-v1"
BASE_CHAIN_ID: Final[int] = 8453
BASE_TIME_PREDEPLOY: Final[str] = "0x4200000000000000000000000000000000000030"
MAX_EARLY_ABORT_GAS: Final[int] = 65_000
DEFAULT_MIN_SHADOW_TICKS: Final[int] = 100
DEFAULT_MIN_LCB_NET_EDGE_BPS: Final[int] = 5
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
    "dual_provider_and_isolated_signer_ready",
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
    """Immutable 8-gate profit-engine readiness verdict."""

    release_permitted: bool
    passed_count: int
    total_gates: int
    gates: tuple[GateCheckResult, ...]
    blocked_gates: tuple[str, ...]
    next_operator_actions: tuple[str, ...]
    report_sha256: str
    schema_version: str = READINESS_SCHEMA_VERSION
    execution_eligible: Literal[False] = False

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


def _eval_positive_cost_adjusted_lcb_edge(
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


def _eval_dual_provider_and_isolated_signer_ready(
    signoff_data: dict[str, Any] | None,
    signoff_err: str | None,
    infra_data: dict[str, Any] | None,
    infra_err: str | None,
) -> tuple[GateCheckResult, str | None]:
    threshold = "distinct primary & secondary RPC verified and isolated key/socket verified"
    action = (
        "Provision an independent secondary archive/simulation RPC and isolated signer key "
        "volume/authority socket, and supply proof in --operator-signoff or --infra-proof."
    )

    if infra_err is not None and "not supplied" not in infra_err:
        return (
            GateCheckResult(
                name="dual_provider_and_isolated_signer_ready",
                passed=False,
                detail=infra_err,
                required_threshold=threshold,
                observed_value="invalid_infra_proof",
            ),
            action,
        )

    proof: dict[str, Any] | None = infra_data
    if proof is None and signoff_data is not None:
        embedded = signoff_data.get("dual_provider_and_isolated_signer")
        if isinstance(embedded, dict):
            proof = embedded

    if proof is None:
        detail = (
            signoff_err
            if (signoff_err is not None and "not supplied" not in signoff_err)
            else "Dual-provider RPC and isolated signer provisioning proof not supplied."
        )
        return (
            GateCheckResult(
                name="dual_provider_and_isolated_signer_ready",
                passed=False,
                detail=detail,
                required_threshold=threshold,
                observed_value="unprovisioned",
            ),
            action,
        )

    primary = proof.get("primary_rpc_provider")
    secondary = proof.get("secondary_rpc_provider")
    secondary_ok = proof.get("secondary_rpc_verified")
    key_volume = proof.get("isolated_key_volume")
    socket_path = proof.get("authority_socket_path")
    signer_ok = proof.get("isolated_signer_verified")

    if not _is_clean_provider_label(primary) or not _is_clean_provider_label(secondary):
        return (
            GateCheckResult(
                name="dual_provider_and_isolated_signer_ready",
                passed=False,
                detail="Primary and secondary RPC provider identifiers must be non-placeholder.",
                required_threshold=threshold,
                observed_value="invalid_provider_labels",
            ),
            action,
        )
    if str(primary).strip().lower() == str(secondary).strip().lower():
        return (
            GateCheckResult(
                name="dual_provider_and_isolated_signer_ready",
                passed=False,
                detail="Secondary RPC provider must be independent and distinct from primary.",
                required_threshold=threshold,
                observed_value="duplicate_provider",
            ),
            action,
        )
    if secondary_ok is not True or signer_ok is not True:
        return (
            GateCheckResult(
                name="dual_provider_and_isolated_signer_ready",
                passed=False,
                detail="Both secondary_rpc_verified and isolated_signer_verified must be True.",
                required_threshold=threshold,
                observed_value=(
                    f"secondary_rpc_verified={secondary_ok!r},"
                    f"isolated_signer_verified={signer_ok!r}"
                ),
            ),
            action,
        )
    if (
        not isinstance(key_volume, str)
        or not key_volume.strip()
        or not isinstance(socket_path, str)
        or not socket_path.strip()
        or _RAW_HEX_KEY_RE.search(key_volume) is not None
    ):
        return (
            GateCheckResult(
                name="dual_provider_and_isolated_signer_ready",
                passed=False,
                detail="Isolated key volume name and authority socket path must be non-empty.",
                required_threshold=threshold,
                observed_value="missing_signer_isolation_metadata",
            ),
            action,
        )

    return (
        GateCheckResult(
            name="dual_provider_and_isolated_signer_ready",
            passed=True,
            detail=(
                f"Distinct RPC providers ({primary} / {secondary}) and isolated signer "
                f"volume/socket verified."
            ),
            required_threshold=threshold,
            observed_value=f"providers={primary}+{secondary},key_volume={key_volume}",
        ),
        None,
    )


def _eval_owner_scope_release_manifest_present(
    signoff_data: dict[str, Any] | None,
    signoff_err: str | None,
) -> tuple[GateCheckResult, str | None]:
    threshold = (
        f"owner_scope_approved=True, capital<={MAX_APPROVED_CAPITAL_USD}, "
        f"daily_loss<={MAX_APPROVED_DAILY_LOSS_USD}, "
        f"cumulative_loss<={MAX_APPROVED_CUMULATIVE_LOSS_USD}"
    )
    action = (
        "Obtain an explicit owner-signed scope approval manifest (--operator-signoff) for "
        "this run within USD 500 capital / USD 200 daily loss limits."
    )
    if signoff_data is None:
        return (
            GateCheckResult(
                name="owner_scope_release_manifest_present",
                passed=False,
                detail=signoff_err or "Owner scope release approval manifest not supplied.",
                required_threshold=threshold,
                observed_value="absent",
            ),
            action,
        )

    approved = signoff_data.get("owner_scope_approved")
    owner_id = signoff_data.get("owner_identity")
    scope_id = signoff_data.get("scope_id")
    approved_at = signoff_data.get("approved_at_utc")
    cap_usd = signoff_data.get("capital_limit_usd")
    daily_usd = signoff_data.get("daily_loss_limit_usd")
    cum_usd = signoff_data.get("cumulative_loss_limit_usd")
    attestation = signoff_data.get("signature_or_attestation")

    if approved is not True:
        return (
            GateCheckResult(
                name="owner_scope_release_manifest_present",
                passed=False,
                detail="Owner signoff manifest does not set owner_scope_approved=True.",
                required_threshold=threshold,
                observed_value=f"owner_scope_approved={approved!r}",
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
                    observed_value=f"missing_{field_name}",
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
                observed_value="missing_approved_at_utc",
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
                observed_value=f"approved_at_utc={approved_at!r}",
            ),
            action,
        )

    if (
        type(cap_usd) is not int
        or not 0 < cap_usd <= MAX_APPROVED_CAPITAL_USD
        or type(daily_usd) is not int
        or not 0 < daily_usd <= MAX_APPROVED_DAILY_LOSS_USD
        or type(cum_usd) is not int
        or not 0 < cum_usd <= MAX_APPROVED_CUMULATIVE_LOSS_USD
    ):
        return (
            GateCheckResult(
                name="owner_scope_release_manifest_present",
                passed=False,
                detail=(
                    "Approved capital/loss limits must stay within bounded canary limits "
                    "(USD 500 capital, USD 200 daily loss, USD 500 cumulative loss)."
                ),
                required_threshold=threshold,
                observed_value=f"cap={cap_usd!r},daily={daily_usd!r},cum={cum_usd!r}",
            ),
            action,
        )

    return (
        GateCheckResult(
            name="owner_scope_release_manifest_present",
            passed=True,
            detail=(
                f"Owner scope '{scope_id}' approved by '{owner_id}' "
                f"(cap=${cap_usd}, daily=${daily_usd}, cum=${cum_usd})."
            ),
            required_threshold=threshold,
            observed_value=f"scope={scope_id},owner={owner_id},cap_usd={cap_usd}",
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
    min_shadow_ticks: int = DEFAULT_MIN_SHADOW_TICKS,
    min_lcb_net_edge_bps: int = DEFAULT_MIN_LCB_NET_EDGE_BPS,
) -> ProfitEngineReadinessReport:
    """Evaluate all 8 fail-closed release readiness gates from retained evidence files."""
    if type(min_shadow_ticks) is not int or min_shadow_ticks <= 0:
        raise ValueError("min_shadow_ticks must be a positive integer")
    if type(min_lcb_net_edge_bps) is not int or min_lcb_net_edge_bps < 0:
        raise ValueError("min_lcb_net_edge_bps must be a non-negative integer")

    out_dir = contracts_out_dir if contracts_out_dir is not None else DEFAULT_CONTRACTS_OUT_DIR
    anvil_data, anvil_err = _load_json_object(anvil_report_path, "anvil_report")
    radar_data, radar_err = _load_json_object(radar_report_path, "radar_report")
    shadow_data, shadow_err = _load_json_object(shadow_summary_path, "shadow_summary")
    signoff_data, signoff_err = _load_json_object(operator_signoff_path, "operator_signoff")
    infra_data, infra_err = _load_json_object(infra_proof_path, "infra_proof")

    evaluations = (
        _eval_compiled_contracts_pinned(out_dir),
        _eval_anvil_e2e_settlement_reconciled(anvil_data, anvil_err),
        _eval_base_time_predeploy_verified(radar_data, radar_err),
        _eval_untouched_shadow_sample_size(
            shadow_data, shadow_err, min_shadow_ticks=min_shadow_ticks
        ),
        _eval_positive_cost_adjusted_lcb_edge(
            shadow_data, shadow_err, min_lcb_net_edge_bps=min_lcb_net_edge_bps
        ),
        _eval_revert_budget_healthy(anvil_data, radar_data, shadow_data),
        _eval_dual_provider_and_isolated_signer_ready(
            signoff_data, signoff_err, infra_data, infra_err
        ),
        _eval_owner_scope_release_manifest_present(signoff_data, signoff_err),
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
    )


__all__ = [
    "ANVIL_E2E_SCHEMA_VERSION",
    "BASE_CHAIN_ID",
    "BASE_TIME_PREDEPLOY",
    "DEFAULT_CONTRACTS_OUT_DIR",
    "DEFAULT_MIN_LCB_NET_EDGE_BPS",
    "DEFAULT_MIN_SHADOW_TICKS",
    "GATE_NAMES",
    "MAX_APPROVED_CAPITAL_USD",
    "MAX_APPROVED_CUMULATIVE_LOSS_USD",
    "MAX_APPROVED_DAILY_LOSS_USD",
    "MAX_EARLY_ABORT_GAS",
    "READINESS_SCHEMA_VERSION",
    "REQUIRED_ANVIL_DRILLS",
    "GateCheckResult",
    "ProfitEngineReadinessReport",
    "evaluate_profit_engine_readiness",
]
