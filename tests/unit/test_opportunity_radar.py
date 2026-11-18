import json
import subprocess
import sys
from pathlib import Path

import httpx

from oasis_agent.intelligence.eqn_client import EqnClient
from oasis_agent.models.common import sha256_hex
from scripts.run_opportunity_radar import main as radar_main
from scripts.run_opportunity_radar import run_radar

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_opportunity_radar_synthetic_end_to_end(tmp_path: Path) -> None:
    out_dir = tmp_path / "radar-synth"
    now_ms = 1_795_000_000_000
    exit_code = radar_main(
        [
            "--mode",
            "synthetic",
            "--out",
            str(out_dir),
            "--now-ms",
            str(now_ms),
            "--bench-iters",
            "10",
        ]
    )
    assert exit_code == 0

    report_bytes = (out_dir / "radar_report.json").read_bytes()
    manifest_bytes = (out_dir / "evidence_manifest.json").read_bytes()
    report = json.loads(report_bytes)
    manifest = json.loads(manifest_bytes)

    assert report["schema_version"] == "oasis-opportunity-radar-v1"
    assert report["execution_eligible"] is False
    assert manifest["execution_eligible"] is False
    assert manifest["radar_report_sha256"] == sha256_hex(report_bytes)

    rust_sec = report["rust_fastpath_and_atomic_executor"]
    assert rust_sec["cycles_evaluated"] == 6
    assert rust_sec["candidates_found"] >= 1
    assert int(rust_sec["top_candidate"]["net_profit"]) > 0
    assert rust_sec["differential"]["bit_exact"] is True
    assert rust_sec["differential"]["mismatches"] == 0
    assert rust_sec["atomic_executor_bundle"]["calldata_bytes"] > 4
    assert rust_sec["atomic_executor_bundle"]["revert_budget_status"] == "active"

    eqn_sec = report["eqn_intelligence_and_launchpad_gate"]
    assert eqn_sec["eqn_news"]["status"] == "synthetic_replay"
    assert eqn_sec["eqn_news"]["catalog_channels"] == 14
    assert eqn_sec["eqn_news"]["risk_freeze_count"] == 1
    assert eqn_sec["eqn_news"]["frozen_symbols"] == ["BAD"]
    assert len(eqn_sec["eqn_news"]["nominated_verified_addresses"]) == 1
    assert eqn_sec["launchpad_safety_gate"]["all_expected_statuses_ok"] is True

    intent_sec = report["uniswapx_dutch_v3_intent_shadow"]
    assert intent_sec["deprecated_priority_reactor_rejected"] is True
    assert intent_sec["approved"] is True
    assert intent_sec["worst_case_net_usd_micros"] > 0
    assert intent_sec["execution_eligible"] is False

    cross_sec = report["aleph_cross_venue_and_saga"]
    assert cross_sec["aleph_shm_v12"]["cross_language_match"] is True
    assert cross_sec["cex_dex_lead_lag"]["approved"] is True
    assert cross_sec["funding_basis"]["approved"] is True
    assert cross_sec["hedge_group_saga"]["ack_generated_hedge"] is False
    assert cross_sec["hedge_group_saga"]["first_fill_generated_hedge"] is True
    assert cross_sec["hedge_group_saga"]["duplicate_fill_was_noop"] is True
    assert cross_sec["hedge_group_saga"]["halted_cleanly"] is True

    # Exclusive file guard: rerunning into same out_dir refuses overwrite
    rerun_code = radar_main(["--mode", "synthetic", "--out", str(out_dir), "--now-ms", str(now_ms)])
    assert rerun_code == 2


def test_opportunity_radar_public_shadow_handles_mock_eqn_and_unreachable_ssh(
    tmp_path: Path,
) -> None:
    now_ms = 1_795_000_000_000

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream unavailable")

    with EqnClient(
        max_requests=1,
        timeout_s=2.0,
        transport=httpx.MockTransport(_handler),
    ) as client:
        out_dir = tmp_path / "radar-shadow-degraded"
        manifest = run_radar(
            mode="public-shadow",
            out_dir=out_dir,
            ssh_host="nonexistent-aleph-alias-for-test",
            now_ms=now_ms,
            bench_iters=5,
            eqn_client=client,
        )

    assert manifest["execution_eligible"] is False
    report = json.loads((out_dir / "radar_report.json").read_text())
    assert report["eqn_intelligence_and_launchpad_gate"]["eqn_news"]["status"] == "error"
    assert report["aleph_cross_venue_and_saga"]["remote_lakehouse_probe"]["probed"] is True
    assert report["aleph_cross_venue_and_saga"]["remote_lakehouse_probe"]["reachable"] is False


def test_opportunity_radar_cli_subprocess(tmp_path: Path) -> None:
    out_dir = tmp_path / "cli-out"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.run_opportunity_radar",
            "--mode",
            "synthetic",
            "--out",
            str(out_dir),
            "--now-ms",
            "1795000000000",
            "--bench-iters",
            "5",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "execution_eligible=false" in proc.stdout
    assert (out_dir / "radar_report.json").is_file()
