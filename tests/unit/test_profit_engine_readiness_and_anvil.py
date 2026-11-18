"""Unit & Anvil E2E tests for profit-engine canary readiness gates and contract settlement."""

import json
import os
import shutil
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from oasis_agent.live.canary_readiness import (
    ANVIL_E2E_SCHEMA_VERSION,
    BASE_CHAIN_ID,
    BASE_TIME_PREDEPLOY,
    DUAL_RPC_QUORUM_SCHEMA_VERSION,
    GATE_NAMES,
    READINESS_SCHEMA_VERSION,
    GateCheckResult,
    ProfitEngineReadinessReport,
    evaluate_profit_engine_readiness,
)
from oasis_agent.models.common import canonical_json, sha256_hex
from scripts.check_profit_engine_readiness import main as readiness_cli_main
from scripts.verify_atomic_vault_anvil import main as anvil_cli_main

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNTH_BLOCK_HASH = "0x" + "ab" * 32
SYNTH_STATE_ROOT = "0x" + "cd" * 32
SYNTH_TOKEN_ADDR = "0x" + "11" * 20
SYNTH_TX_HASH = "0x" + "22" * 32
SYNTH_BYTECODE_HEX = "0x" + "60806040" * 8


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _make_synthetic_contracts_out(root: Path) -> Path:
    exec_path = root / "OasisAtomicExecutor.sol" / "OasisAtomicExecutor.json"
    vault_path = root / "OasisIntentVault.sol" / "OasisIntentVault.json"
    _write_json(
        exec_path,
        {
            "abi": [{"type": "function", "name": "executeCycle", "inputs": [], "outputs": []}],
            "bytecode": {"object": SYNTH_BYTECODE_HEX},
            "deployedBytecode": {"object": SYNTH_BYTECODE_HEX},
            "methodIdentifiers": {
                "executeCycle((uint64,uint64,address,uint256,uint256,"
                "(address,uint160,uint160),"
                "(address,address,address,bool,uint160,uint24)[]))": "9f3529e3"
            },
        },
    )
    _write_json(
        vault_path,
        {
            "abi": [
                {"type": "function", "name": "executeReactorFill", "inputs": [], "outputs": []}
            ],
            "bytecode": {"object": SYNTH_BYTECODE_HEX},
            "deployedBytecode": {"object": SYNTH_BYTECODE_HEX},
            "methodIdentifiers": {
                "executeReactorFill((address,address,uint256,(address,uint160,uint160),"
                "(address,address,address,address,uint256,uint256,bytes)[]),bytes)": "00d8532d"
            },
        },
    )
    return root


def _make_synthetic_anvil_report(
    path: Path,
    *,
    early_abort_gas: int = 58_204,
    allowance_residual: int = 0,
    revert_budget_status: str = "active",
    all_drills_passed: bool = True,
) -> Path:
    payload: dict[str, Any] = {
        "schema_version": ANVIL_E2E_SCHEMA_VERSION,
        "environment": "loopback_anvil_ephemeral",
        "chain_id": BASE_CHAIN_ID,
        "anvil_client_version": "anvil/v1.8.4",
        "execution_eligible": False,
        "all_drills_passed": all_drills_passed,
        "drills": {
            "executor_two_hop_profit_and_sweep": {
                "passed": True,
                "tx_hash": SYNTH_TX_HASH,
                "sweep_tx_hash": SYNTH_TX_HASH,
                "gas_used": 191_538,
                "sweep_gas_used": 66_622,
                "net_profit_raw": 9_550_169_617_820_656_117,
            },
            "executor_early_abort_under_65k_gas": {
                "passed": True,
                "tx_hash": SYNTH_TX_HASH,
                "gas_used": early_abort_gas,
                "early_aborted_event_verified": True,
                "profit_token_delta_raw": 0,
                "revert_budget_outcome": "early_abort_probe",
                "revert_budget_status": revert_budget_status,
            },
            "vault_erc1271_and_epoch_revocation": {
                "passed": True,
                "epoch_before_rotation": 1,
                "epoch_after_rotation": 2,
                "pre_rotation_magic": "0x1626ba7e",
                "post_rotation_magic": "0xffffffff",
            },
            "vault_reactor_fill_and_zero_allowances": {
                "passed": True,
                "tx_hash": SYNTH_TX_HASH,
                "gas_used": 271_931,
                "net_profit_raw": 100_000_000,
                "allowance_residuals": {
                    "vault_router_token_in": allowance_residual,
                    "vault_router_token_out": 0,
                    "vault_reactor_token_in": 0,
                    "vault_reactor_token_out": 0,
                },
            },
        },
    }
    payload["report_sha256"] = sha256_hex(canonical_json(payload))
    return _write_json(path, payload)


def _make_synthetic_radar_report(
    path: Path,
    *,
    basetime_status: str = "ok",
    header_matches: bool = True,
    revert_budget_status: str = "active",
) -> Path:
    return _write_json(
        path,
        {
            "schema_version": "oasis-opportunity-radar-v2",
            "mode": "public-shadow",
            "execution_eligible": False,
            "rust_fastpath_and_atomic_executor": {
                "atomic_executor_bundle": {
                    "revert_budget_status": revert_budget_status,
                }
            },
            "base_time_predeploy_verification": {
                "status": basetime_status,
                "chain_id": BASE_CHAIN_ID,
                "predeploy_address": BASE_TIME_PREDEPLOY,
                "block_number": 58_978_574,
                "block_hash": SYNTH_BLOCK_HASH,
                "header_matches_predeploy": header_matches,
                "execution_eligible": False,
            },
        },
    )


def _make_synthetic_shadow_summary(
    path: Path,
    *,
    mode: str = "public-shadow",
    ticks_completed: object = 120,
    positive_net_ticks: int = 18,
    lcb_net_edge_bps: int = 12,
    revert_budget_status: str = "active",
    unresolved_residuals: int = 0,
) -> Path:
    return _write_json(
        path,
        {
            "schema_version": "oasis.base-shadow-summary.v1",
            "mode": mode,
            "ticks_completed": ticks_completed,
            "positive_net_ticks": positive_net_ticks,
            "lcb_net_edge_bps": lcb_net_edge_bps,
            "revert_budget_status": revert_budget_status,
            "unresolved_residuals": unresolved_residuals,
            "execution_eligible": False,
        },
    )


def _make_synthetic_operator_signoff(
    path: Path,
    *,
    owner_scope_approved: bool = True,
    capital_limit_usd: object = 500,
    primary_rpc: str = "base-publicnode-archive",
    secondary_rpc: str = "alchemy-base-archive",
    secondary_verified: bool = True,
    signer_verified: bool = True,
    isolated_key_volume: str = "oasis-canary-keys",
    authority_socket_path: str = "/run/oasis/authority.sock",
) -> Path:
    return _write_json(
        path,
        {
            "owner_scope_approved": owner_scope_approved,
            "owner_identity": "oasis-owner",
            "scope_id": "base-canary-scope-20261118",
            "approved_at_utc": "2026-11-18T08:00:00+00:00",
            "capital_limit_usd": capital_limit_usd,
            "daily_loss_limit_usd": 200,
            "cumulative_loss_limit_usd": 500,
            "signature_or_attestation": "owner-ed25519-scope-attestation-ref-01",
            "dual_provider_and_isolated_signer": {
                "primary_rpc_provider": primary_rpc,
                "secondary_rpc_provider": secondary_rpc,
                "secondary_rpc_verified": secondary_verified,
                "isolated_key_volume": isolated_key_volume,
                "authority_socket_path": authority_socket_path,
                "isolated_signer_verified": signer_verified,
            },
        },
    )


def _make_synthetic_dual_rpc_quorum_report(
    path: Path,
    *,
    chain_id: object = BASE_CHAIN_ID,
    execution_eligible: object = False,
    primary_rpc: str = "base-publicnode-archive",
    secondary_rpc: str = "alchemy-base-archive",
    secondary_verified: object = True,
    pinned_block_number: object = 58_990_500,
    pinned_block_hash: object = SYNTH_BLOCK_HASH,
    state_root: object = SYNTH_STATE_ROOT,
    base_time_ms: object = 1_794_988_500_000,
    tamper_sha: bool = False,
) -> Path:
    payload: dict[str, Any] = {
        "schema_version": DUAL_RPC_QUORUM_SCHEMA_VERSION,
        "chain_id": chain_id,
        "execution_eligible": execution_eligible,
        "primary_rpc_provider": primary_rpc,
        "secondary_rpc_provider": secondary_rpc,
        "secondary_rpc_verified": secondary_verified,
        "pinned_block_number": pinned_block_number,
        "pinned_block_hash": pinned_block_hash,
    }
    if state_root is not None:
        payload["state_root"] = state_root
    if base_time_ms is not None:
        payload["base_time_ms"] = base_time_ms
    digest = sha256_hex(canonical_json(payload))
    if tamper_sha:
        digest = ("0" if digest[0] != "0" else "1") + digest[1:]
    payload["report_sha256"] = digest
    return _write_json(path, payload)


def test_readiness_all_failing_default_and_immutability(tmp_path: Path) -> None:
    report = evaluate_profit_engine_readiness(contracts_out_dir=tmp_path / "missing-contracts")

    assert isinstance(report, ProfitEngineReadinessReport)
    assert report.schema_version == READINESS_SCHEMA_VERSION
    assert report.release_permitted is False
    assert report.execution_eligible is False
    assert report.passed_count == 0
    assert report.total_gates == 9
    assert len(report.gates) == 9
    assert tuple(g.name for g in report.gates) == GATE_NAMES
    assert report.blocked_gates == GATE_NAMES
    assert len(report.next_operator_actions) == 9
    assert len(report.report_sha256) == 64

    with pytest.raises(FrozenInstanceError):
        report.release_permitted = True  # type: ignore[misc]

    # execution_eligible=True is rejected even on direct dataclass construction
    with pytest.raises(ValueError, match="execution_eligible"):
        ProfitEngineReadinessReport(
            release_permitted=False,
            passed_count=0,
            total_gates=9,
            gates=report.gates,
            blocked_gates=report.blocked_gates,
            next_operator_actions=report.next_operator_actions,
            report_sha256=report.report_sha256,
            execution_eligible=True,  # type: ignore[arg-type]
        )

    # GateCheckResult rejects empty names/details
    with pytest.raises(ValueError, match="name"):
        GateCheckResult(
            name="",
            passed=False,
            detail="bad",
            required_threshold="req",
            observed_value="obs",
        )


def test_readiness_synthetic_all_passing_still_keeps_execution_eligible_false(
    tmp_path: Path,
) -> None:
    contracts_dir = _make_synthetic_contracts_out(tmp_path / "contracts_out")
    anvil_path = _make_synthetic_anvil_report(tmp_path / "anvil_e2e_report.json")
    radar_path = _make_synthetic_radar_report(tmp_path / "radar_report.json")
    shadow_path = _make_synthetic_shadow_summary(tmp_path / "shadow_summary.json")
    signoff_path = _make_synthetic_operator_signoff(tmp_path / "operator_signoff.json")

    report = evaluate_profit_engine_readiness(
        contracts_out_dir=contracts_dir,
        anvil_report_path=anvil_path,
        radar_report_path=radar_path,
        shadow_summary_path=shadow_path,
        operator_signoff_path=signoff_path,
    )

    assert report.release_permitted is True
    assert report.execution_eligible is False
    assert report.passed_count == 9
    assert report.total_gates == 9
    assert report.blocked_gates == ()
    assert report.next_operator_actions == ()
    assert all(gate.passed is True for gate in report.gates)

    parsed = json.loads(report.to_json())
    assert parsed["release_permitted"] is True
    assert parsed["execution_eligible"] is False
    assert parsed["passed_count"] == 9
    assert parsed["total_gates"] == 9


def test_readiness_partial_smoke_sample_blocks_sample_size_and_lcb_edge(tmp_path: Path) -> None:
    contracts_dir = _make_synthetic_contracts_out(tmp_path / "contracts_out")
    anvil_path = _make_synthetic_anvil_report(tmp_path / "anvil_e2e_report.json")
    radar_path = _make_synthetic_radar_report(tmp_path / "radar_report.json")
    smoke_shadow = _make_synthetic_shadow_summary(
        tmp_path / "shadow_smoke.json",
        mode="public-shadow",
        ticks_completed=3,
        positive_net_ticks=0,
        lcb_net_edge_bps=-3,
    )
    signoff_path = _make_synthetic_operator_signoff(tmp_path / "operator_signoff.json")

    report = evaluate_profit_engine_readiness(
        contracts_out_dir=contracts_dir,
        anvil_report_path=anvil_path,
        radar_report_path=radar_path,
        shadow_summary_path=smoke_shadow,
        operator_signoff_path=signoff_path,
    )

    assert report.release_permitted is False
    assert report.execution_eligible is False
    assert report.passed_count == 7
    assert report.total_gates == 9
    assert report.blocked_gates == (
        "untouched_shadow_sample_size",
        "positive_cost_adjusted_lcb_edge",
    )
    by_name = {gate.name: gate for gate in report.gates}
    assert "97 ticks remaining" in by_name["untouched_shadow_sample_size"].detail
    assert "97 additional" in " ".join(report.next_operator_actions)
    assert by_name["revert_budget_healthy"].passed is True


def test_readiness_individual_fail_closed_gate_rejections(tmp_path: Path) -> None:
    contracts_dir = _make_synthetic_contracts_out(tmp_path / "contracts_out")
    anvil_path = _make_synthetic_anvil_report(tmp_path / "anvil_e2e_report.json")
    radar_path = _make_synthetic_radar_report(tmp_path / "radar_report.json")
    shadow_path = _make_synthetic_shadow_summary(tmp_path / "shadow_summary.json")
    signoff_path = _make_synthetic_operator_signoff(tmp_path / "operator_signoff.json")

    # 1. Corrupt JSON file fails cleanly without raising
    corrupt_radar = tmp_path / "corrupt_radar.json"
    corrupt_radar.write_text("{not-json", encoding="utf-8")
    r_corrupt = evaluate_profit_engine_readiness(
        contracts_out_dir=contracts_dir,
        anvil_report_path=anvil_path,
        radar_report_path=corrupt_radar,
        shadow_summary_path=shadow_path,
        operator_signoff_path=signoff_path,
    )
    assert r_corrupt.blocked_gates == ("base_time_predeploy_verified",)

    # 2. Synthetic radar mode ('synthetic_verified' instead of live 'ok') fails BaseTime gate
    synth_radar = _make_synthetic_radar_report(
        tmp_path / "synth_radar.json", basetime_status="synthetic_verified"
    )
    r_synth = evaluate_profit_engine_readiness(
        contracts_out_dir=contracts_dir,
        anvil_report_path=anvil_path,
        radar_report_path=synth_radar,
        shadow_summary_path=shadow_path,
        operator_signoff_path=signoff_path,
    )
    assert r_synth.blocked_gates == ("base_time_predeploy_verified",)

    # 3. Standalone BaseTimeVerificationReceipt passes BaseTime gate
    standalone_basetime = _write_json(
        tmp_path / "basetime_receipt.json",
        {
            "chain_id": BASE_CHAIN_ID,
            "predeploy_address": BASE_TIME_PREDEPLOY,
            "block_number": 58_978_574,
            "block_hash": SYNTH_BLOCK_HASH,
            "header_timestamp_ms": 1_794_986_090_400,
            "predeploy_timestamp_ms": 1_794_986_090_400,
            "rpc_origin": "https://base-rpc.publicnode.com",
        },
    )
    r_standalone = evaluate_profit_engine_readiness(
        contracts_out_dir=contracts_dir,
        anvil_report_path=anvil_path,
        radar_report_path=standalone_basetime,
        shadow_summary_path=shadow_path,
        operator_signoff_path=signoff_path,
    )
    assert r_standalone.release_permitted is True

    # 4. Anvil early abort >= 65_000 gas or non-zero allowance fails Gate 2 (& Gate 6 if residual)
    bad_gas_anvil = _make_synthetic_anvil_report(
        tmp_path / "bad_gas_anvil.json", early_abort_gas=65_000
    )
    r_bad_gas = evaluate_profit_engine_readiness(
        contracts_out_dir=contracts_dir,
        anvil_report_path=bad_gas_anvil,
        radar_report_path=radar_path,
        shadow_summary_path=shadow_path,
        operator_signoff_path=signoff_path,
    )
    assert "anvil_e2e_settlement_reconciled" in r_bad_gas.blocked_gates

    bad_allowance_anvil = _make_synthetic_anvil_report(
        tmp_path / "bad_allow_anvil.json", allowance_residual=1
    )
    r_bad_allow = evaluate_profit_engine_readiness(
        contracts_out_dir=contracts_dir,
        anvil_report_path=bad_allowance_anvil,
        radar_report_path=radar_path,
        shadow_summary_path=shadow_path,
        operator_signoff_path=signoff_path,
    )
    assert set(r_bad_allow.blocked_gates) == {
        "anvil_e2e_settlement_reconciled",
        "revert_budget_healthy",
    }

    # 5. Halted revert budget or unresolved residual in shadow fails Gate 6
    halted_shadow = _make_synthetic_shadow_summary(
        tmp_path / "halted_shadow.json", revert_budget_status="halt", unresolved_residuals=1
    )
    r_halted = evaluate_profit_engine_readiness(
        contracts_out_dir=contracts_dir,
        anvil_report_path=anvil_path,
        radar_report_path=radar_path,
        shadow_summary_path=halted_shadow,
        operator_signoff_path=signoff_path,
    )
    assert r_halted.blocked_gates == ("revert_budget_healthy",)

    # 6. Duplicate RPC providers & excessive capital limit fail Gates 7 & 9 independently of Gate 8
    bad_signoff = _make_synthetic_operator_signoff(
        tmp_path / "bad_signoff.json",
        primary_rpc="base-rpc",
        secondary_rpc="base-rpc",
        capital_limit_usd=501,
    )
    r_bad_signoff = evaluate_profit_engine_readiness(
        contracts_out_dir=contracts_dir,
        anvil_report_path=anvil_path,
        radar_report_path=radar_path,
        shadow_summary_path=shadow_path,
        operator_signoff_path=bad_signoff,
    )
    assert r_bad_signoff.blocked_gates == (
        "dual_rpc_quorum_verified",
        "owner_scope_release_manifest_present",
    )

    # 6b. Unverified isolated signer or raw hex key in key volume fails Gate 8 independently
    unverified_signer_signoff = _make_synthetic_operator_signoff(
        tmp_path / "unverified_signer.json",
        signer_verified=False,
    )
    r_bad_signer = evaluate_profit_engine_readiness(
        contracts_out_dir=contracts_dir,
        anvil_report_path=anvil_path,
        radar_report_path=radar_path,
        shadow_summary_path=shadow_path,
        operator_signoff_path=unverified_signer_signoff,
    )
    assert r_bad_signer.blocked_gates == ("isolated_signer_authority_ready",)

    raw_hex_volume_signoff = _make_synthetic_operator_signoff(
        tmp_path / "raw_hex_volume.json",
        isolated_key_volume=f"0x{1:064x}",
    )
    r_raw_hex_volume = evaluate_profit_engine_readiness(
        contracts_out_dir=contracts_dir,
        anvil_report_path=anvil_path,
        radar_report_path=radar_path,
        shadow_summary_path=shadow_path,
        operator_signoff_path=raw_hex_volume_signoff,
    )
    assert r_raw_hex_volume.blocked_gates == ("isolated_signer_authority_ready",)

    # 7. Boolean smuggling (ticks_completed=True) and symlink input path fail closed
    bool_shadow = _make_synthetic_shadow_summary(
        tmp_path / "bool_shadow.json", ticks_completed=True
    )
    r_bool = evaluate_profit_engine_readiness(
        contracts_out_dir=contracts_dir,
        anvil_report_path=anvil_path,
        radar_report_path=radar_path,
        shadow_summary_path=bool_shadow,
        operator_signoff_path=signoff_path,
    )
    assert r_bool.blocked_gates == ("untouched_shadow_sample_size",)

    symlink_shadow = tmp_path / "symlink_shadow.json"
    os.symlink(shadow_path, symlink_shadow)
    r_symlink = evaluate_profit_engine_readiness(
        contracts_out_dir=contracts_dir,
        anvil_report_path=anvil_path,
        radar_report_path=radar_path,
        shadow_summary_path=symlink_shadow,
        operator_signoff_path=signoff_path,
    )
    assert r_symlink.blocked_gates == (
        "untouched_shadow_sample_size",
        "positive_cost_adjusted_lcb_edge",
    )


def test_readiness_cli_exit_codes_and_require_ready(tmp_path: Path) -> None:
    contracts_dir = _make_synthetic_contracts_out(tmp_path / "contracts_out")
    anvil_path = _make_synthetic_anvil_report(tmp_path / "anvil_e2e_report.json")
    radar_path = _make_synthetic_radar_report(tmp_path / "radar_report.json")
    shadow_path = _make_synthetic_shadow_summary(tmp_path / "shadow_summary.json")
    signoff_path = _make_synthetic_operator_signoff(tmp_path / "operator_signoff.json")

    blocked_out = tmp_path / "out_blocked.json"
    # Without --require-ready: blocked report still writes and returns 0
    rc_no_flag = readiness_cli_main(
        [
            "--contracts-out-dir",
            str(contracts_dir),
            "--anvil-report",
            str(anvil_path),
            "--radar-report",
            str(radar_path),
            "--out",
            str(blocked_out),
        ]
    )
    assert rc_no_flag == 0
    blocked_doc = json.loads(blocked_out.read_text(encoding="utf-8"))
    assert blocked_doc["release_permitted"] is False
    assert blocked_doc["execution_eligible"] is False

    # With --require-ready when blocked: writes report and returns 2
    rc_req_blocked = readiness_cli_main(
        [
            "--contracts-out-dir",
            str(contracts_dir),
            "--anvil-report",
            str(anvil_path),
            "--radar-report",
            str(radar_path),
            "--out",
            str(blocked_out),
            "--require-ready",
        ]
    )
    assert rc_req_blocked == 2

    # With --require-ready when all 9 gates pass: returns 0 (subprocess module check)
    ready_out = tmp_path / "out_ready.json"
    sub_env = dict(os.environ)
    sub_env["PYTHONPATH"] = str(REPO_ROOT / "src")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.check_profit_engine_readiness",
            "--contracts-out-dir",
            str(contracts_dir),
            "--anvil-report",
            str(anvil_path),
            "--radar-report",
            str(radar_path),
            "--shadow-summary",
            str(shadow_path),
            "--operator-signoff",
            str(signoff_path),
            "--out",
            str(ready_out),
            "--require-ready",
        ],
        cwd=REPO_ROOT,
        env=sub_env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    ready_doc = json.loads(ready_out.read_text(encoding="utf-8"))
    assert ready_doc["release_permitted"] is True
    assert ready_doc["execution_eligible"] is False
    assert ready_doc["passed_count"] == 9
    assert ready_doc["total_gates"] == 9


def test_readiness_dual_rpc_quorum_report_passes_gate7_while_leaving_gates_8_9_blocked(
    tmp_path: Path,
) -> None:
    contracts_dir = _make_synthetic_contracts_out(tmp_path / "contracts_out")
    anvil_path = _make_synthetic_anvil_report(tmp_path / "anvil_e2e_report.json")
    radar_path = _make_synthetic_radar_report(tmp_path / "radar_report.json")
    shadow_path = _make_synthetic_shadow_summary(tmp_path / "shadow_summary.json")
    quorum_path = _make_synthetic_dual_rpc_quorum_report(tmp_path / "dual_rpc_quorum.json")

    out_path = tmp_path / "cli_quorum_only.json"
    rc = readiness_cli_main(
        [
            "--contracts-out-dir",
            str(contracts_dir),
            "--anvil-report",
            str(anvil_path),
            "--radar-report",
            str(radar_path),
            "--shadow-summary",
            str(shadow_path),
            "--infra-proof",
            str(quorum_path),
            "--out",
            str(out_path),
            "--require-ready",
        ]
    )
    assert rc == 2
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert doc["release_permitted"] is False
    assert doc["execution_eligible"] is False
    assert doc["passed_count"] == 7
    assert doc["total_gates"] == 9
    assert doc["blocked_gates"] == [
        "isolated_signer_authority_ready",
        "owner_scope_release_manifest_present",
    ]
    by_name = {g["name"]: g for g in doc["gates"]}
    assert by_name["dual_rpc_quorum_verified"]["passed"] is True
    obs = by_name["dual_rpc_quorum_verified"]["observed_value"]
    assert "block=58990500" in obs
    assert f"block_hash={SYNTH_BLOCK_HASH[:14]}" in obs
    assert f"state_root={SYNTH_STATE_ROOT[:14]}" in obs
    assert "base_time_ms=1794988500000" in obs
    assert by_name["isolated_signer_authority_ready"]["passed"] is False
    assert "Docker isolated signer" in " ".join(doc["next_operator_actions"])


def test_readiness_dual_rpc_quorum_report_tampered_sha_and_bad_fields_fail_closed(
    tmp_path: Path,
) -> None:
    contracts_dir = _make_synthetic_contracts_out(tmp_path / "contracts_out")
    anvil_path = _make_synthetic_anvil_report(tmp_path / "anvil_e2e_report.json")
    radar_path = _make_synthetic_radar_report(tmp_path / "radar_report.json")
    shadow_path = _make_synthetic_shadow_summary(tmp_path / "shadow_summary.json")
    signoff_path = _make_synthetic_operator_signoff(tmp_path / "operator_signoff.json")

    tampered_quorum = _make_synthetic_dual_rpc_quorum_report(
        tmp_path / "tampered_quorum.json", tamper_sha=True
    )
    r_tampered = evaluate_profit_engine_readiness(
        contracts_out_dir=contracts_dir,
        anvil_report_path=anvil_path,
        radar_report_path=radar_path,
        shadow_summary_path=shadow_path,
        operator_signoff_path=signoff_path,
        infra_proof_path=tampered_quorum,
    )
    assert r_tampered.release_permitted is False
    assert r_tampered.blocked_gates == ("dual_rpc_quorum_verified",)
    gate7 = {g.name: g for g in r_tampered.gates}["dual_rpc_quorum_verified"]
    assert gate7.observed_value == "checksum_mismatch"

    bad_cases: tuple[tuple[str, dict[str, Any]], ...] = (
        ("wrong_chain.json", {"chain_id": 1}),
        ("exec_eligible.json", {"execution_eligible": True}),
        ("unverified_secondary.json", {"secondary_verified": False}),
        ("duplicate_rpc.json", {"secondary_rpc": "base-publicnode-archive"}),
        ("bad_block_hash.json", {"pinned_block_hash": "0x1234"}),
        ("bool_block_num.json", {"pinned_block_number": True}),
        ("bad_state_root.json", {"state_root": "not-hex"}),
        ("bool_base_time.json", {"base_time_ms": True}),
        ("negative_base_time.json", {"base_time_ms": -1}),
    )
    for filename, overrides in bad_cases:
        bad_path = _make_synthetic_dual_rpc_quorum_report(tmp_path / filename, **overrides)
        r_bad = evaluate_profit_engine_readiness(
            contracts_out_dir=contracts_dir,
            anvil_report_path=anvil_path,
            radar_report_path=radar_path,
            shadow_summary_path=shadow_path,
            operator_signoff_path=signoff_path,
            infra_proof_path=bad_path,
        )
        assert r_bad.release_permitted is False
        assert r_bad.blocked_gates == ("dual_rpc_quorum_verified",)


def test_readiness_legacy_infra_proof_and_isolated_signer_failures(tmp_path: Path) -> None:
    contracts_dir = _make_synthetic_contracts_out(tmp_path / "contracts_out")
    anvil_path = _make_synthetic_anvil_report(tmp_path / "anvil_e2e_report.json")
    radar_path = _make_synthetic_radar_report(tmp_path / "radar_report.json")
    shadow_path = _make_synthetic_shadow_summary(tmp_path / "shadow_summary.json")
    signoff_without_infra = _write_json(
        tmp_path / "signoff_minimal.json",
        {
            "owner_scope_approved": True,
            "owner_identity": "oasis-owner",
            "scope_id": "base-canary-scope-20261118",
            "approved_at_utc": "2026-11-18T08:00:00+00:00",
            "capital_limit_usd": 500,
            "daily_loss_limit_usd": 200,
            "cumulative_loss_limit_usd": 500,
            "signature_or_attestation": "owner-ed25519-scope-attestation-ref-01",
        },
    )

    legacy_infra = _write_json(
        tmp_path / "legacy_infra.json",
        {
            "primary_rpc_provider": "base-publicnode-archive",
            "secondary_rpc_provider": "alchemy-base-archive",
            "secondary_rpc_verified": True,
            "isolated_key_volume": "oasis-canary-keys",
            "authority_socket_path": "/run/oasis/authority.sock",
            "isolated_signer_verified": True,
        },
    )
    r_legacy_ok = evaluate_profit_engine_readiness(
        contracts_out_dir=contracts_dir,
        anvil_report_path=anvil_path,
        radar_report_path=radar_path,
        shadow_summary_path=shadow_path,
        operator_signoff_path=signoff_without_infra,
        infra_proof_path=legacy_infra,
    )
    assert r_legacy_ok.release_permitted is True
    assert r_legacy_ok.execution_eligible is False
    assert r_legacy_ok.passed_count == 9

    bad_socket_infra = _write_json(
        tmp_path / "bad_socket_infra.json",
        {
            "primary_rpc_provider": "base-publicnode-archive",
            "secondary_rpc_provider": "alchemy-base-archive",
            "secondary_rpc_verified": True,
            "isolated_key_volume": "oasis-canary-keys",
            "authority_socket_path": "   ",
            "isolated_signer_verified": True,
        },
    )
    r_bad_socket = evaluate_profit_engine_readiness(
        contracts_out_dir=contracts_dir,
        anvil_report_path=anvil_path,
        radar_report_path=radar_path,
        shadow_summary_path=shadow_path,
        operator_signoff_path=signoff_without_infra,
        infra_proof_path=bad_socket_infra,
    )
    assert r_bad_socket.release_permitted is False
    assert r_bad_socket.blocked_gates == ("isolated_signer_authority_ready",)


def test_readiness_combined_signoff_and_dual_rpc_quorum_achieves_9_of_9(
    tmp_path: Path,
) -> None:
    contracts_dir = _make_synthetic_contracts_out(tmp_path / "contracts_out")
    anvil_path = _make_synthetic_anvil_report(tmp_path / "anvil_e2e_report.json")
    radar_path = _make_synthetic_radar_report(tmp_path / "radar_report.json")
    shadow_path = _make_synthetic_shadow_summary(tmp_path / "shadow_summary.json")
    signoff_path = _make_synthetic_operator_signoff(tmp_path / "operator_signoff.json")
    quorum_path = _make_synthetic_dual_rpc_quorum_report(
        tmp_path / "dual_rpc_quorum.json",
        state_root=None,
        base_time_ms=None,
    )

    report = evaluate_profit_engine_readiness(
        contracts_out_dir=contracts_dir,
        anvil_report_path=anvil_path,
        radar_report_path=radar_path,
        shadow_summary_path=shadow_path,
        operator_signoff_path=signoff_path,
        infra_proof_path=quorum_path,
    )
    assert report.release_permitted is True
    assert report.execution_eligible is False
    assert report.passed_count == 9
    assert report.total_gates == 9
    assert report.blocked_gates == ()
    assert report.next_operator_actions == ()

    by_name = {gate.name: gate for gate in report.gates}
    assert by_name["dual_rpc_quorum_verified"].passed is True
    assert "block=58990500" in by_name["dual_rpc_quorum_verified"].observed_value
    assert by_name["isolated_signer_authority_ready"].passed is True
    assert "key_volume=oasis-canary-keys" in (
        by_name["isolated_signer_authority_ready"].observed_value
    )
    assert by_name["owner_scope_release_manifest_present"].passed is True


def test_run_anvil_profit_e2e_against_real_anvil(tmp_path: Path) -> None:
    if shutil.which("anvil") is None:
        pytest.skip("anvil absent in Python-only container; executed locally and in contracts job")

    report_path = tmp_path / "anvil_e2e_report.json"
    rc = anvil_cli_main(["--out", str(report_path)])
    assert rc == 0
    assert report_path.is_file()

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == ANVIL_E2E_SCHEMA_VERSION
    assert report["environment"] == "loopback_anvil_ephemeral"
    assert report["chain_id"] == BASE_CHAIN_ID
    assert report["execution_eligible"] is False
    assert report["all_drills_passed"] is True

    body = {k: v for k, v in report.items() if k != "report_sha256"}
    assert report["report_sha256"] == sha256_hex(canonical_json(body))

    drills = report["drills"]
    d1 = drills["executor_two_hop_profit_and_sweep"]
    assert d1["passed"] is True
    assert d1["cycle_executed_event_verified"] is True
    assert d1["net_profit_raw"] >= d1["min_net_profit_raw"]
    assert d1["swept_to_revenue_sink_raw"] == d1["net_profit_raw"]
    assert d1["treasury_delta_raw"] == 0
    assert d1["operator_token_delta_raw"] == 0
    assert d1["native_gas_delta_wei"] == d1["gas_used"] * d1["effective_gas_price_wei"]

    d2 = drills["executor_early_abort_under_65k_gas"]
    assert d2["passed"] is True
    assert d2["early_aborted_event_verified"] is True
    assert 0 < d2["gas_used"] < 65_000
    assert d2["profit_token_delta_raw"] == 0
    assert d2["revert_budget_outcome"] == "early_abort_probe"
    assert d2["revert_budget_status"] == "active"

    d3 = drills["vault_erc1271_and_epoch_revocation"]
    assert d3["passed"] is True
    assert d3["epoch_before_rotation"] == 1
    assert d3["epoch_after_rotation"] == 2
    assert d3["pre_rotation_magic"] == "0x1626ba7e"
    assert d3["post_rotation_magic"] == "0xffffffff"
    assert d3["epoch2_magic"] == "0x1626ba7e"

    d4 = drills["vault_reactor_fill_and_zero_allowances"]
    assert d4["passed"] is True
    assert d4["reactor_fill_executed_event_verified"] is True
    assert (
        d4["vault_balance_after_raw"] >= d4["vault_balance_before_raw"] + d4["min_net_profit_raw"]
    )
    assert d4["native_gas_delta_wei"] == d4["gas_used"] * d4["effective_gas_price_wei"]
    assert all(val == 0 for val in d4["allowance_residuals"].values())

    # Cross-check real Anvil report directly against readiness evaluator
    readiness = evaluate_profit_engine_readiness(anvil_report_path=report_path)
    gate_map = {g.name: g for g in readiness.gates}
    assert gate_map["compiled_contracts_pinned"].passed is True
    assert gate_map["anvil_e2e_settlement_reconciled"].passed is True
    assert gate_map["revert_budget_healthy"].passed is True
    assert readiness.release_permitted is False
    assert readiness.execution_eligible is False

    # Immutable --out guard refuses to overwrite existing Anvil report
    assert anvil_cli_main(["--out", str(report_path)]) == 2
