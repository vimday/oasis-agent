import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from oasis_agent.intelligence.eqn_client import EqnClient
from oasis_agent.intents.uniswapx_client import UniswapXOrderbookClient
from oasis_agent.models.common import sha256_hex
from oasis_agent.server.app import create_app
from oasis_agent.server.config import load_server_config
from scripts.run_opportunity_radar import main as radar_main
from scripts.run_opportunity_radar import run_radar

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_opportunity_radar_synthetic_end_to_end_and_console_api(tmp_path: Path) -> None:
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

    assert report["schema_version"] == "oasis-opportunity-radar-v2"
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

    evm_liq_sec = report["evm_log_state_sync_and_liquidation"]
    assert evm_liq_sec["sync_events_applied"] == 2
    assert evm_liq_sec["reorg_detected"] is True
    assert evm_liq_sec["rollback_restored_head"] is True
    assert evm_liq_sec["liquidation_candidates_found"] >= 1
    assert evm_liq_sec["top_liquidation"]["net_profit_debt_raw"] > 0
    assert evm_liq_sec["execution_eligible"] is False

    base_time_sec = report["base_time_predeploy_verification"]
    assert base_time_sec["status"] == "synthetic_verified"
    assert base_time_sec["header_matches_predeploy"] is True
    assert base_time_sec["requests_used"] == 3
    assert base_time_sec["execution_eligible"] is False

    eqn_sec = report["eqn_intelligence_and_launchpad_gate"]
    assert eqn_sec["eqn_news"]["status"] == "synthetic_replay"
    assert eqn_sec["eqn_news"]["catalog_channels"] == 14
    assert eqn_sec["eqn_news"]["risk_freeze_count"] == 1
    assert eqn_sec["eqn_news"]["frozen_symbols"] == ["BAD"]
    assert len(eqn_sec["eqn_news"]["nominated_verified_addresses"]) == 1
    assert len(eqn_sec["eqn_news"]["top_items"]) == 2
    assert eqn_sec["launchpad_safety_gate"]["all_expected_statuses_ok"] is True
    assert eqn_sec["launchpad_safety_gate"]["spot_agent_gate_drill"]["parity_ok"] is True

    intent_sec = report["uniswapx_dutch_v3_intent_shadow"]
    assert intent_sec["deprecated_priority_reactor_rejected"] is True
    assert intent_sec["approved"] is True
    assert intent_sec["worst_case_net_usd_micros"] > 0
    assert intent_sec["live_orderbook_poll"]["status"] == "synthetic_skip"
    assert intent_sec["execution_eligible"] is False

    cross_sec = report["aleph_cross_venue_and_saga"]
    assert cross_sec["aleph_shm_v12"]["cross_language_match"] is True
    assert cross_sec["cex_dex_lead_lag"]["approved"] is True
    assert cross_sec["funding_basis"]["approved"] is True
    assert cross_sec["hedge_group_saga"]["ack_generated_hedge"] is False
    assert cross_sec["hedge_group_saga"]["first_fill_generated_hedge"] is True
    assert cross_sec["hedge_group_saga"]["duplicate_fill_was_noop"] is True
    assert cross_sec["hedge_group_saga"]["halted_cleanly"] is True

    port_sec = report["stage7_portfolio_allocator"]
    assert port_sec["execution_eligible"] is False
    assert port_sec["segregated_reserves_usd_micros"] == 150_000_000
    assert port_sec["deployable_budget_usd_micros"] == 350_000_000
    decisions = {d["strategy_id"]: d for d in port_sec["decisions"]}
    assert decisions["base_launchpad_unvetted_legacy"]["action"] == "scale_to_zero"
    assert decisions["base_launchpad_unvetted_legacy"]["target_usd_micros"] == 0

    # End-to-end compatibility check against the FastAPI /api/v1/radar/* read model
    cfg_path = tmp_path / "server.toml"
    ops_db = tmp_path / "ops.db"
    cfg_path.write_text(
        f'ops_token_required = false\n[databases]\nops = "{ops_db}"\nradar_run = "{out_dir}"\n',
        encoding="utf-8",
    )
    cfg = load_server_config(cfg_path)
    app = create_app(replace(cfg, databases=replace(cfg.databases, radar_run=out_dir)))
    with TestClient(app) as client:
        r_sum = client.get("/api/v1/radar/summary")
        assert r_sum.status_code == 200
        assert r_sum.json()["engine_count"] == 7
        assert r_sum.json()["portfolio_available"] is True

        r_cat = client.get("/api/v1/radar/catalysts", params={"actionable_only": "true"})
        assert r_cat.status_code == 200
        assert len(r_cat.json()["items"]) == 1
        assert r_cat.json()["items"][0]["id"] == "radar-synth-oi-1"

        r_eng = client.get("/api/v1/radar/engines")
        assert r_eng.status_code == 200
        assert "base_time_predeploy_verification" in r_eng.json()["engines"]

        r_port = client.get("/api/v1/radar/portfolio")
        assert r_port.status_code == 200
        assert r_port.json()["available"] is True
        assert r_port.json()["portfolio"]["report_sha256"] == port_sec["report_sha256"]

    # Exclusive file guard: rerunning into same out_dir refuses overwrite
    rerun_code = radar_main(["--mode", "synthetic", "--out", str(out_dir), "--now-ms", str(now_ms)])
    assert rerun_code == 2


def test_opportunity_radar_public_shadow_handles_mock_upstreams_and_unreachable_ssh(
    tmp_path: Path,
) -> None:
    now_ms = 1_795_000_000_000

    def _eqn_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream unavailable")

    def _ux_transport(_url: str, _timeout: float) -> bytes:
        return b'{"orders": []}'

    def _rpc_fail(_url: str, _body: bytes, _timeout: float) -> bytes:
        raise OSError("rpc unreachable in unit test")

    with EqnClient(
        max_requests=1,
        timeout_s=2.0,
        transport=httpx.MockTransport(_eqn_handler),
    ) as client:
        ux_client = UniswapXOrderbookClient(max_requests=1, transport=_ux_transport)
        out_dir = tmp_path / "radar-shadow-degraded"
        manifest = run_radar(
            mode="public-shadow",
            out_dir=out_dir,
            ssh_host="nonexistent-aleph-alias-for-test",
            now_ms=now_ms,
            bench_iters=5,
            eqn_client=client,
            base_time_transport=_rpc_fail,
            uniswapx_client=ux_client,
        )

    assert manifest["execution_eligible"] is False
    report = json.loads((out_dir / "radar_report.json").read_text())
    assert report["eqn_intelligence_and_launchpad_gate"]["eqn_news"]["status"] == "error"
    assert report["base_time_predeploy_verification"]["status"] == "error"
    assert report["uniswapx_dutch_v3_intent_shadow"]["live_orderbook_poll"]["status"] == "ok"
    assert report["uniswapx_dutch_v3_intent_shadow"]["live_orderbook_poll"]["order_count"] == 0
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
