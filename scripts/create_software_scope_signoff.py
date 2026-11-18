"""Keyless CLI to generate and verify two-zone software EOA scope signoff manifests.

Produces immutable `oasis.software-scope-signoff.v1` JSON documents binding an
active `CapitalRiskConfig` SHA-256 digest, Cold Admin vs Hot Operator EOA
addresses, optional evidence report digests, and either `--rehearsal`
(`owner_scope_approved=False`, `rehearsal_only=True`) or explicit human
`--approve-software-scope --attestation ...`. Never touches private keys and
permanently preserves `execution_eligible=False`.
"""

import argparse
import json
import math
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from eth_utils import is_checksum_address  # type: ignore[attr-defined]

from oasis_agent.live.canary_readiness import (
    MAX_INPUT_BYTES,
    SOFTWARE_SCOPE_SIGNOFF_SCHEMA_VERSION,
)
from oasis_agent.models.common import ZERO_ADDRESS, canonical_json, normalize_address, sha256_hex
from oasis_agent.risk.capital_policy import CapitalRiskConfig, load_capital_risk_config

REHEARSAL_ATTESTATION_MARKER: Final[str] = "rehearsal-non-authorizing"
_RAW_HEX_KEY_RE: Final[re.Pattern[str]] = re.compile(r"0x[0-9a-fA-F]{64}")
_FORBIDDEN_ATTESTATION_MARKERS: Final[tuple[str, ...]] = (
    REHEARSAL_ATTESTATION_MARKER,
    "placeholder",
    "todo",
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


def _validate_eoa_address(raw_value: str, role_label: str) -> str:
    if not isinstance(raw_value, str):
        raise ValueError(f"{role_label} must be a string")
    stripped = raw_value.strip()
    if len(stripped) != 42 or not stripped.startswith("0x"):
        raise ValueError(f"{role_label} must be a 42-char 0x-prefixed EVM address")
    body = stripped[2:]
    if body != body.lower() and body != body.upper() and not is_checksum_address(stripped):
        raise ValueError(f"{role_label} failed EIP-55 checksum validation")
    normalized = normalize_address(stripped)
    if normalized == ZERO_ADDRESS:
        raise ValueError(f"{role_label} cannot be the zero address")
    return normalized


def _validate_safe_text(raw_value: str, label: str) -> str:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    cleaned = raw_value.strip()
    if _RAW_HEX_KEY_RE.search(cleaned) is not None:
        raise ValueError(f"{label} cannot contain 64-hex key material")
    return cleaned


def _load_json_bytes_and_object(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    if path.is_symlink():
        raise ValueError(f"{label} refuses symlink path: {path}")
    if not path.is_file():
        raise FileNotFoundError(f"{label} file not found: {path}")
    raw = path.read_bytes()
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError(f"{label} exceeds maximum byte limit ({MAX_INPUT_BYTES})")
    decoded = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
        parse_float=_finite_float,
    )
    if not isinstance(decoded, dict):
        raise ValueError(f"{label} JSON root must be an object")
    return raw, decoded


def _collect_evidence_digests(
    evidence_paths: dict[str, Path | None],
) -> dict[str, dict[str, str]]:
    digests: dict[str, dict[str, str]] = {}
    for label, path in sorted(evidence_paths.items()):
        if path is None:
            continue
        raw, parsed = _load_json_bytes_and_object(path, label)
        entry: dict[str, str] = {
            "file": path.name,
            "file_sha256": sha256_hex(raw),
        }
        for key in ("report_sha256", "proof_sha256"):
            val = parsed.get(key)
            if isinstance(val, str) and val.strip():
                entry[key] = val.strip()
        digests[label] = entry
    return digests


def build_software_scope_signoff(
    *,
    risk_config: CapitalRiskConfig,
    owner_identity: str,
    scope_id: str,
    admin_eoa: str,
    operator_eoa: str,
    approve_software_scope: bool = False,
    attestation: str | None = None,
    evidence_paths: dict[str, Path | None] | None = None,
    approved_at_utc: str | None = None,
) -> dict[str, Any]:
    """Construct a canonical two-zone software EOA scope signoff manifest."""
    if not isinstance(risk_config, CapitalRiskConfig):
        raise TypeError("risk_config must be a CapitalRiskConfig")
    if type(approve_software_scope) is not bool:
        raise TypeError("approve_software_scope must be a boolean")

    clean_owner = _validate_safe_text(owner_identity, "owner_identity")
    clean_scope = _validate_safe_text(scope_id, "scope_id")
    norm_admin = _validate_eoa_address(admin_eoa, "admin_eoa")
    norm_operator = _validate_eoa_address(operator_eoa, "operator_eoa")
    if norm_admin.lower() == norm_operator.lower():
        raise ValueError("Two-zone custody violation: admin_eoa must differ from operator_eoa")

    if approve_software_scope:
        if attestation is None:
            raise ValueError("--approve-software-scope requires non-empty --attestation")
        clean_attestation = _validate_safe_text(attestation, "attestation")
        if any(marker in clean_attestation.lower() for marker in _FORBIDDEN_ATTESTATION_MARKERS):
            raise ValueError("attestation cannot use rehearsal or placeholder markers")
        rehearsal_only = False
        owner_scope_approved = True
        sig_value = clean_attestation
    else:
        if attestation is not None and attestation.strip():
            raise ValueError("--attestation cannot be paired with rehearsal mode")
        rehearsal_only = True
        owner_scope_approved = False
        sig_value = REHEARSAL_ATTESTATION_MARKER

    timestamp_str = (
        approved_at_utc.strip() if approved_at_utc is not None else datetime.now(UTC).isoformat()
    )
    parsed_dt = datetime.fromisoformat(timestamp_str)
    if parsed_dt.tzinfo is None or parsed_dt.utcoffset() is None:
        raise ValueError("approved_at_utc must be timezone-aware ISO-8601")

    evidence_digests = _collect_evidence_digests(evidence_paths or {})
    manifest: dict[str, Any] = {
        "schema_version": SOFTWARE_SCOPE_SIGNOFF_SCHEMA_VERSION,
        "custody_model": "two_zone_software_eoa",
        "execution_eligible": False,
        "rehearsal_only": rehearsal_only,
        "owner_scope_approved": owner_scope_approved,
        "owner_identity": clean_owner,
        "scope_id": clean_scope,
        "approved_at_utc": timestamp_str,
        "admin_eoa": norm_admin,
        "operator_eoa": norm_operator,
        "capital_limit_usd_micros": risk_config.total_capital_usd_micros,
        "daily_loss_limit_usd_micros": risk_config.max_daily_loss_usd_micros,
        "cumulative_loss_limit_usd_micros": risk_config.max_cumulative_loss_usd_micros,
        "risk_config_sha256": risk_config.config_sha256,
        "evidence_digests": evidence_digests,
        "signature_or_attestation": sig_value,
    }
    manifest["manifest_sha256"] = sha256_hex(canonical_json(manifest))
    return manifest


def verify_software_scope_signoff(
    manifest_path: Path,
    *,
    risk_config: CapitalRiskConfig,
) -> tuple[bool, str, dict[str, Any]]:
    """Verify integrity, EOA plane separation, risk digest, and non-rehearsal approval."""
    _, data = _load_json_bytes_and_object(manifest_path, "manifest")

    if data.get("schema_version") != SOFTWARE_SCOPE_SIGNOFF_SCHEMA_VERSION:
        return False, "schema_version mismatch", data
    if data.get("execution_eligible") is not False:
        return False, "execution_eligible must remain False", data

    declared_sha = data.get("manifest_sha256")
    body = {k: v for k, v in data.items() if k != "manifest_sha256"}
    expected_sha = sha256_hex(canonical_json(body))
    if not isinstance(declared_sha, str) or declared_sha != expected_sha:
        return False, "manifest_sha256 integrity check failed", data

    try:
        norm_admin = _validate_eoa_address(str(data.get("admin_eoa", "")), "admin_eoa")
        norm_operator = _validate_eoa_address(str(data.get("operator_eoa", "")), "operator_eoa")
    except ValueError as exc:
        return False, str(exc), data
    if norm_admin == norm_operator:
        return False, "admin_eoa and operator_eoa must be distinct", data

    approved_at = data.get("approved_at_utc")
    if not isinstance(approved_at, str) or not approved_at.strip():
        return False, "missing approved_at_utc", data
    try:
        parsed_dt = datetime.fromisoformat(approved_at)
    except ValueError:
        return False, "approved_at_utc is not valid ISO-8601", data
    if parsed_dt.tzinfo is None or parsed_dt.utcoffset() is None:
        return False, "approved_at_utc must be timezone-aware ISO-8601", data

    if data.get("risk_config_sha256") != risk_config.config_sha256:
        return False, "risk_config_sha256 mismatch against active CapitalRiskConfig", data

    for key, limit in (
        ("capital_limit_usd_micros", risk_config.total_capital_usd_micros),
        ("daily_loss_limit_usd_micros", risk_config.max_daily_loss_usd_micros),
        ("cumulative_loss_limit_usd_micros", risk_config.max_cumulative_loss_usd_micros),
    ):
        val = data.get(key)
        if type(val) is not int or val <= 0 or val > limit:
            return False, f"{key} missing or exceeds CapitalRiskConfig limit", data

    if data.get("rehearsal_only") is True:
        return False, "manifest is marked rehearsal_only=True (non-authorizing)", data
    if data.get("owner_scope_approved") is not True:
        return False, "owner_scope_approved is not True", data

    attestation = data.get("signature_or_attestation")
    if not isinstance(attestation, str) or not attestation.strip():
        return False, "missing signature_or_attestation", data
    _validate_safe_text(attestation, "signature_or_attestation")
    if any(marker in attestation.lower() for marker in _FORBIDDEN_ATTESTATION_MARKERS):
        return False, "attestation uses rehearsal or placeholder marker", data

    return True, "verified", data


def _write_manifest_once(out_path: Path, manifest: dict[str, Any]) -> None:
    if out_path.is_symlink() or out_path.exists():
        raise ValueError(f"refusing to overwrite existing or symlink path: {out_path}")
    if out_path.parent.is_symlink():
        raise ValueError(f"refusing symlink parent directory: {out_path.parent}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")


def build_parser() -> argparse.ArgumentParser:
    """Construct CLI parser for two-zone software EOA scope signoff manifests."""
    parser = argparse.ArgumentParser(description=__doc__)
    action_group = parser.add_mutually_exclusive_group(required=True)
    action_group.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output JSON path to write new signoff manifest (refuses overwrite/symlink)",
    )
    action_group.add_argument(
        "--verify",
        type=Path,
        default=None,
        help="Verify an existing signoff manifest JSON against --risk-config",
    )

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--rehearsal",
        action="store_true",
        help="Emit non-authorizing rehearsal manifest (default unless --approve-software-scope)",
    )
    mode_group.add_argument(
        "--approve-software-scope",
        action="store_true",
        help="Emit explicit owner-approved scope manifest (requires --attestation)",
    )

    parser.add_argument(
        "--risk-config",
        type=Path,
        default=None,
        help="Optional CapitalRiskConfig JSON path (default: bounded USD 500 canary policy)",
    )
    parser.add_argument("--owner-identity", type=str, default=None, help="Owner identity label")
    parser.add_argument("--scope-id", type=str, default=None, help="Bounded release scope ID")
    parser.add_argument("--admin-eoa", type=str, default=None, help="Cold Admin zone EOA (0x...)")
    parser.add_argument(
        "--operator-eoa", type=str, default=None, help="Hot Canary zone EOA (0x...)"
    )
    parser.add_argument(
        "--attestation",
        type=str,
        default=None,
        help="Non-empty human attestation reference (required with --approve-software-scope)",
    )
    parser.add_argument("--anvil-report", type=Path, default=None, help="Optional Anvil E2E JSON")
    parser.add_argument("--radar-report", type=Path, default=None, help="Optional Radar JSON")
    parser.add_argument(
        "--shadow-summary", type=Path, default=None, help="Optional public-shadow summary JSON"
    )
    parser.add_argument(
        "--infra-proof", type=Path, default=None, help="Optional dual-RPC quorum proof JSON"
    )
    parser.add_argument(
        "--signer-proof", type=Path, default=None, help="Optional isolated signer proof JSON"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run software EOA scope signoff generation or verification CLI."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        risk_cfg = (
            load_capital_risk_config(args.risk_config)
            if args.risk_config is not None
            else CapitalRiskConfig()
        )
    except (OSError, ValueError, TypeError) as exc:
        print(f"invalid --risk-config: {exc}", file=sys.stderr)
        return 2

    if args.verify is not None:
        try:
            ok, reason, data = verify_software_scope_signoff(args.verify, risk_config=risk_cfg)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"verification error: {exc}", file=sys.stderr)
            return 2
        summary = {
            "schema_version": SOFTWARE_SCOPE_SIGNOFF_SCHEMA_VERSION,
            "verified": ok,
            "reason": reason,
            "execution_eligible": False,
            "rehearsal_only": data.get("rehearsal_only"),
            "owner_scope_approved": data.get("owner_scope_approved"),
            "risk_config_sha256": risk_cfg.config_sha256,
            "manifest_sha256": data.get("manifest_sha256"),
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0 if ok else 2

    missing = [
        flag
        for flag, val in (
            ("--owner-identity", args.owner_identity),
            ("--scope-id", args.scope_id),
            ("--admin-eoa", args.admin_eoa),
            ("--operator-eoa", args.operator_eoa),
        )
        if val is None
    ]
    if missing:
        print(f"missing required option(s) for --out: {', '.join(missing)}", file=sys.stderr)
        return 2

    try:
        manifest = build_software_scope_signoff(
            risk_config=risk_cfg,
            owner_identity=args.owner_identity,
            scope_id=args.scope_id,
            admin_eoa=args.admin_eoa,
            operator_eoa=args.operator_eoa,
            approve_software_scope=bool(args.approve_software_scope),
            attestation=args.attestation,
            evidence_paths={
                "anvil_report": args.anvil_report,
                "radar_report": args.radar_report,
                "shadow_summary": args.shadow_summary,
                "infra_proof": args.infra_proof,
                "signer_proof": args.signer_proof,
            },
        )
        _write_manifest_once(args.out, manifest)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"signoff creation refused: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "schema_version": manifest["schema_version"],
                "out": str(args.out),
                "execution_eligible": manifest["execution_eligible"],
                "rehearsal_only": manifest["rehearsal_only"],
                "owner_scope_approved": manifest["owner_scope_approved"],
                "risk_config_sha256": manifest["risk_config_sha256"],
                "manifest_sha256": manifest["manifest_sha256"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
