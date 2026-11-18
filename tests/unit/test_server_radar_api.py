"""Tests for the read-only opportunity radar API (/api/v1/radar/*)."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from oasis_agent.errors import ConfigurationError
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.server.config import load_server_config
from oasis_agent.server.console_contract import CONSOLE_VIEWS
from oasis_agent.server.routers_radar import _RADAR_JSON_LIMIT

from .server_helpers import make_server_config
from .test_server_api import client_for

RADAR_ROUTES = (
    "/api/v1/radar/summary",
    "/api/v1/radar/catalysts",
    "/api/v1/radar/engines",
    "/api/v1/radar/portfolio",
    "/api/v1/radar/capacity",
)
NOW_MS = 1_795_000_000_000


def _v1_report(*, now_ms: int = NOW_MS) -> dict[str, Any]:
    """Build a Wave 2 v1-shaped opportunity radar report without top_items or Stage 7."""
    return {
        "schema_version": "oasis-opportunity-radar-v1",
        "mode": "synthetic",
        "observed_at_ms": now_ms,
        "execution_eligible": False,
        "rust_fastpath_and_atomic_executor": {
            "cycles_evaluated": 6,
            "candidates_found": 1,
            "differential": {"bit_exact": True, "mismatches": 0},
        },
        "eqn_intelligence_and_launchpad_gate": {
            "eqn_news": {
                "status": "synthetic_replay",
                "error": None,
                "catalog_channels": 14,
                "channel": "all",
                "raw_sha256": sha256_hex(b"eqn-raw-v1"),
                "item_count": 2,
                "counts_by_kind": {
                    "oi_funding_crowding": 1,
                    "protocol_incident_freeze": 1,
                },
                "risk_freeze_count": 1,
                "nominated_verified_addresses": ["0x" + "11" * 20],
                "frozen_symbols": ["BAD"],
                "unmapped_symbols": [],
            },
            "launchpad_safety_gate": {
                "defer_case": {"status": "defer_until_decay", "earliest_safe_entry_ms": now_ms + 8},
                "approved_case": {"status": "approved", "effective_buy_tax_bps": 120},
                "soft_rug_case": {"status": "rejected", "reasons": ["creator_prior_rug"]},
                "all_expected_statuses_ok": True,
            },
        },
        "uniswapx_dutch_v3_intent_shadow": {
            "approved": True,
            "net_edge_bps": 86,
            "execution_eligible": False,
        },
        "aleph_cross_venue_and_saga": {
            "aleph_shm_v12": {"cross_language_match": True},
            "hedge_group_saga": {"halted_cleanly": True},
        },
    }


def _v2_report(*, now_ms: int = NOW_MS) -> dict[str, Any]:
    """Build a Wave 4 v2-shaped radar report with top_items and Stage 7 portfolio allocator."""
    report = _v1_report(now_ms=now_ms)
    report["schema_version"] = "oasis-opportunity-radar-v2"
    report["eqn_intelligence_and_launchpad_gate"]["eqn_news"]["top_items"] = [
        {
            "item_id": "cat-1",
            "symbol": "ETH",
            "kind": "oi_funding_crowding",
            "actionable": True,
            "confidence_bps": 8500,
        },
        {
            "item_id": "cat-2",
            "symbol": "BAD",
            "kind": "protocol_incident_freeze",
            "actionable": False,
            "confidence_bps": 9500,
        },
        {
            "item_id": "cat-3",
            "symbol": "AERO",
            "kind": "tier1_listing",
            # Non-bool truthy value must NOT pass strict `is True` actionable filter.
            "actionable": "yes",
            "confidence_bps": 7200,
        },
        {
            "item_id": "cat-4",
            "symbol": "VIRTUAL",
            "kind": "fast_price_shock",
            "actionable": True,
            "confidence_bps": 7800,
        },
    ]
    report["evm_log_state_sync_and_liquidation"] = {
        "chain_id": 8453,
        "synced_pools": 12,
        "liquidation_candidates": 1,
    }
    report["base_time_predeploy_verification"] = {
        "chain_id": 8453,
        "block_timestamp_verified": True,
    }
    report["stage7_portfolio_allocator"] = {
        "execution_eligible": False,
        "total_capital_usd_micros": 500_000_000,
        "allocated_usd_micros": 125_000_000,
        "selected_sleeve_count": 2,
    }
    return report


def _write_radar_bundle(
    run_dir: Path,
    report: dict[str, Any],
    *,
    manifest_overrides: dict[str, Any] | None = None,
) -> tuple[Path, str]:
    run_dir.mkdir(parents=True, exist_ok=True)
    report_bytes = canonical_json(report) + b"\n"
    digest = sha256_hex(report_bytes)
    (run_dir / "radar_report.json").write_bytes(report_bytes)
    manifest: dict[str, Any] = {
        "schema_version": "oasis-opportunity-radar-manifest-v1",
        "mode": report.get("mode", "synthetic"),
        "observed_at_ms": report.get("observed_at_ms", NOW_MS),
        "execution_eligible": False,
        "radar_report_sha256": digest,
        "aleph_shm_probe_sha256": sha256_hex(b"aleph-shm-probe"),
        "eqn_raw_sha256": sha256_hex(b"eqn-raw"),
    }
    if manifest_overrides is not None:
        manifest.update(manifest_overrides)
    (run_dir / "evidence_manifest.json").write_bytes(canonical_json(manifest) + b"\n")
    return run_dir, digest


def _radar_client(tmp_path: Path, radar_run: Path | None) -> Any:
    root = tmp_path / "server"
    root.mkdir(exist_ok=True)
    config = make_server_config(root)
    object.__setattr__(config, "databases", replace(config.databases, radar_run=radar_run))
    return client_for(root, config=config)


def test_server_config_parses_radar_run_strictly_without_startup_existence_check(
    tmp_path: Path,
) -> None:
    empty_cfg = tmp_path / "empty.toml"
    empty_cfg.write_text("", encoding="utf-8")
    assert load_server_config(empty_cfg).databases.radar_run is None

    configured_cfg = tmp_path / "radar.toml"
    configured_cfg.write_text(
        '[databases]\nradar_run = "data/radar/nonexistent-at-startup"\n',
        encoding="utf-8",
    )
    parsed = load_server_config(configured_cfg)
    assert parsed.databases.radar_run == Path("data/radar/nonexistent-at-startup")

    bad_cfg = tmp_path / "bad.toml"
    bad_cfg.write_text('[databases]\nradar_run = ""\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match=r"databases\.radar_run"):
        load_server_config(bad_cfg)


def test_unconfigured_radar_run_returns_404_and_rejects_mutations(tmp_path: Path) -> None:
    with _radar_client(tmp_path, None) as client:
        for route in RADAR_ROUTES:
            resp = client.get(route)
            assert resp.status_code == 404
            assert resp.json()["detail"] == "opportunity radar run not configured"
            assert client.post(route).status_code == 405


def test_v1_bundle_serves_all_radar_endpoints_and_etag_304(tmp_path: Path) -> None:
    run_dir, digest = _write_radar_bundle(tmp_path / "radar-v1", _v1_report())
    with _radar_client(tmp_path, run_dir) as client:
        summary_resp = client.get("/api/v1/radar/summary")
        assert summary_resp.status_code == 200
        summary = summary_resp.json()
        assert summary["schema_version"] == "oasis-opportunity-radar-v1"
        assert summary["mode"] == "synthetic"
        assert summary["observed_at_ms"] == NOW_MS
        assert summary["radar_report_sha256"] == digest
        assert summary["execution_eligible"] is False
        assert summary["engine_count"] == 4
        assert summary["engine_names"] == [
            "aleph_cross_venue_and_saga",
            "eqn_intelligence_and_launchpad_gate",
            "rust_fastpath_and_atomic_executor",
            "uniswapx_dutch_v3_intent_shadow",
        ]
        assert summary["eqn_news_summary"]["item_count"] == 2
        assert summary["portfolio_available"] is False

        # ETag conditional GET -> 304
        etag = summary_resp.headers["etag"]
        assert etag.startswith('"')
        cached = client.get("/api/v1/radar/summary", headers={"If-None-Match": etag})
        assert cached.status_code == 304
        assert cached.content == b""

        catalysts = client.get("/api/v1/radar/catalysts").json()
        assert catalysts["execution_eligible"] is False
        assert catalysts["radar_report_sha256"] == digest
        assert catalysts["status"] == "synthetic_replay"
        assert catalysts["item_count"] == 2
        assert catalysts["risk_freeze_count"] == 1
        assert catalysts["items"] == []
        assert catalysts["launchpad_safety_gate"]["all_expected_statuses_ok"] is True

        engines_all = client.get("/api/v1/radar/engines").json()
        assert engines_all["execution_eligible"] is False
        assert engines_all["radar_report_sha256"] == digest
        assert engines_all["schema_version"] == "oasis-opportunity-radar-v1"
        assert set(engines_all["engines"]) == set(summary["engine_names"])

        engine_one = client.get(
            "/api/v1/radar/engines",
            params={"name": "uniswapx_dutch_v3_intent_shadow"},
        ).json()
        assert list(engine_one["engines"]) == ["uniswapx_dutch_v3_intent_shadow"]
        assert engine_one["engines"]["uniswapx_dutch_v3_intent_shadow"]["net_edge_bps"] == 86

        missing_engine = client.get("/api/v1/radar/engines", params={"name": "no_such_engine"})
        assert missing_engine.status_code == 404
        assert missing_engine.json()["detail"] == "radar engine not found"

        portfolio_resp = client.get("/api/v1/radar/portfolio")
        assert portfolio_resp.status_code == 200
        assert portfolio_resp.json() == {
            "available": False,
            "execution_eligible": False,
            "radar_report_sha256": digest,
            "portfolio": None,
        }

        capacity_resp = client.get("/api/v1/radar/capacity")
        assert capacity_resp.status_code == 200
        capacity_body = capacity_resp.json()
        assert capacity_body["radar_report_sha256"] == digest
        assert capacity_body["execution_eligible"] is False
        assert capacity_body["release_permitted"] is False
        assert capacity_body["integrity"] == {
            "status": "verified",
            "manifest_verified": True,
            "radar_report_sha256": digest,
        }
        assert capacity_body["freshness"]["status"] == "verified_snapshot"
        assert capacity_body["freshness"]["observed_at_ms"] == NOW_MS
        assert capacity_body["capital_policy"]["available"] is False
        assert capacity_body["capital_policy"]["total_capital_usd_micros"] is None
        assert capacity_body["headroom"]["available"] is False
        assert capacity_body["headroom"]["can_deploy"] is False
        assert capacity_body["headroom"]["blocked_reason"] == "headroom_unavailable"
        assert capacity_body["oos_telemetry"]["available"] is False
        assert capacity_body["oos_telemetry"]["oos_trades_count"] is None
        assert capacity_body["pair_capacities"] == {
            "available": False,
            "pair_count": 0,
            "items": [],
        }
        cap_etag = capacity_resp.headers["etag"]
        assert (
            client.get("/api/v1/radar/capacity", headers={"If-None-Match": cap_etag}).status_code
            == 304
        )


def test_v2_bundle_filters_catalysts_and_serves_stage7_portfolio(tmp_path: Path) -> None:
    run_dir, digest = _write_radar_bundle(tmp_path / "radar-v2", _v2_report())
    with _radar_client(tmp_path, run_dir) as client:
        summary = client.get("/api/v1/radar/summary").json()
        assert summary["schema_version"] == "oasis-opportunity-radar-v2"
        assert summary["engine_count"] == 7
        assert summary["portfolio_available"] is True
        assert summary["execution_eligible"] is False

        all_items = client.get("/api/v1/radar/catalysts").json()["items"]
        assert [item["item_id"] for item in all_items] == ["cat-1", "cat-2", "cat-3", "cat-4"]

        actionable = client.get(
            "/api/v1/radar/catalysts",
            params={"actionable_only": True},
        ).json()["items"]
        assert [item["item_id"] for item in actionable] == ["cat-1", "cat-4"]

        bounded = client.get(
            "/api/v1/radar/catalysts",
            params={"actionable_only": True, "limit": 1},
        ).json()["items"]
        assert [item["item_id"] for item in bounded] == ["cat-1"]

        assert client.get("/api/v1/radar/catalysts", params={"limit": 0}).status_code == 422
        assert client.get("/api/v1/radar/catalysts", params={"limit": 201}).status_code == 422

        portfolio = client.get("/api/v1/radar/portfolio").json()
        assert portfolio["available"] is True
        assert portfolio["execution_eligible"] is False
        assert portfolio["radar_report_sha256"] == digest
        assert portfolio["portfolio"]["selected_sleeve_count"] == 2
        assert portfolio["portfolio"]["allocated_usd_micros"] == 125_000_000


def _damage_missing_dir(run_dir: Path) -> None:
    for item in run_dir.iterdir():
        item.unlink()
    run_dir.rmdir()


def _damage_unlink(name: str) -> Any:
    return lambda run_dir: (run_dir / name).unlink()


def _damage_symlink(name: str) -> Any:
    def _apply(run_dir: Path) -> None:
        target = run_dir / f"{name}.real"
        (run_dir / name).replace(target)
        (run_dir / name).symlink_to(target)

    return _apply


def _damage_directory(name: str) -> Any:
    def _apply(run_dir: Path) -> None:
        (run_dir / name).unlink()
        (run_dir / name).mkdir()

    return _apply


def _damage_write_bytes(name: str, payload: bytes) -> Any:
    return lambda run_dir: (run_dir / name).write_bytes(payload)


def _damage_tamper_byte(run_dir: Path) -> None:
    raw = bytearray((run_dir / "radar_report.json").read_bytes())
    raw[-2] = ord(" ")
    (run_dir / "radar_report.json").write_bytes(bytes(raw))


def _damage_rehash_report(mutation: dict[str, Any]) -> Any:
    def _apply(run_dir: Path) -> None:
        report = json.loads((run_dir / "radar_report.json").read_text(encoding="utf-8"))
        report.update(mutation)
        _write_radar_bundle(run_dir, report)

    return _apply


def _damage_drop_all_engines(run_dir: Path) -> None:
    report = {
        "schema_version": "oasis-opportunity-radar-v1",
        "mode": "synthetic",
        "observed_at_ms": NOW_MS,
        "execution_eligible": False,
    }
    _write_radar_bundle(run_dir, report)


def _damage_oversized_report(run_dir: Path) -> None:
    report = _v1_report()
    base = canonical_json(report)
    padding_needed = (_RADAR_JSON_LIMIT + 1) - len(base)
    (run_dir / "radar_report.json").write_bytes(b" " * padding_needed + base)


def _damage_oversized_manifest(run_dir: Path) -> None:
    raw = (run_dir / "evidence_manifest.json").read_bytes()
    padding_needed = (_RADAR_JSON_LIMIT + 1) - len(raw)
    (run_dir / "evidence_manifest.json").write_bytes(b" " * padding_needed + raw)


FAIL_CLOSED_CASES = {
    "missing_run_dir": _damage_missing_dir,
    "missing_manifest": _damage_unlink("evidence_manifest.json"),
    "missing_report": _damage_unlink("radar_report.json"),
    "symlink_manifest": _damage_symlink("evidence_manifest.json"),
    "symlink_report": _damage_symlink("radar_report.json"),
    "directory_report": _damage_directory("radar_report.json"),
    "invalid_utf8_report": _damage_write_bytes("radar_report.json", b"\xff\xfe"),
    "invalid_json_manifest": _damage_write_bytes("evidence_manifest.json", b"{"),
    "nan_json_report": _damage_write_bytes("radar_report.json", b'{"execution_eligible":NaN}'),
    "non_dict_report": _damage_write_bytes("radar_report.json", b"[]"),
    "non_dict_manifest": _damage_write_bytes("evidence_manifest.json", b"[]"),
    "hash_mismatch_tampered_byte": _damage_tamper_byte,
    "report_execution_eligible_true_rehashed": _damage_rehash_report({"execution_eligible": True}),
    "report_execution_eligible_zero_rehashed": _damage_rehash_report({"execution_eligible": 0}),
    "manifest_execution_eligible_true": lambda d: _write_radar_bundle(
        d, _v1_report(), manifest_overrides={"execution_eligible": True}
    ),
    "observed_at_ms_bool_rehashed": _damage_rehash_report({"observed_at_ms": True}),
    "observed_at_ms_str_rehashed": _damage_rehash_report({"observed_at_ms": "1795000000000"}),
    "no_engine_sections": _damage_drop_all_engines,
    "non_dict_engine_section": _damage_rehash_report({"bad_engine": ["not-an-object"]}),
    "engine_execution_eligible_true": _damage_rehash_report(
        {"uniswapx_dutch_v3_intent_shadow": {"execution_eligible": True}}
    ),
    "oversized_report": _damage_oversized_report,
    "oversized_manifest": _damage_oversized_manifest,
}


@pytest.mark.parametrize("case", sorted(FAIL_CLOSED_CASES))
def test_corrupt_or_unauthorized_radar_bundle_fails_closed_503(tmp_path: Path, case: str) -> None:
    run_dir, _ = _write_radar_bundle(tmp_path / "radar-bad", _v1_report())
    FAIL_CLOSED_CASES[case](run_dir)
    with _radar_client(tmp_path, run_dir) as client:
        for route in RADAR_ROUTES:
            resp = client.get(route)
            assert resp.status_code == 503
            assert resp.json() == {"detail": "opportunity radar bundle unavailable or corrupt"}
            assert str(tmp_path) not in resp.text


@pytest.mark.parametrize(
    "eqn_mutation",
    [
        {"eqn_news": ["not-a-dict"]},
        {"launchpad_safety_gate": "not-a-dict"},
        {"eqn_news": {"status": "ok", "top_items": "not-a-list"}},
        {"eqn_news": {"status": "ok", "top_items": [123]}},
    ],
)
def test_catalysts_fails_closed_on_malformed_eqn_sections(
    tmp_path: Path, eqn_mutation: dict[str, Any]
) -> None:
    report = _v1_report()
    report["eqn_intelligence_and_launchpad_gate"].update(eqn_mutation)
    run_dir, _ = _write_radar_bundle(tmp_path / "radar-bad-eqn", report)
    with _radar_client(tmp_path, run_dir) as client:
        resp = client.get("/api/v1/radar/catalysts")
        assert resp.status_code == 503
        assert resp.json()["detail"] == "opportunity radar bundle unavailable or corrupt"


def test_openapi_routes_match_console_views_and_meta_for_radar(tmp_path: Path) -> None:
    with _radar_client(tmp_path, None) as client:
        openapi_paths = client.get("/openapi.json").json()["paths"]
        api_paths = {path for path in openapi_paths if path.startswith("/api/v1")}
        for route in RADAR_ROUTES:
            assert route in api_paths
            assert set(openapi_paths[route].keys()) == {"get"}
            assert CONSOLE_VIEWS.get(route) == "research"
        assert api_paths <= set(CONSOLE_VIEWS)

        meta_endpoints = {ep["path"]: ep for ep in client.get("/api/v1/meta").json()["endpoints"]}
        for route in RADAR_ROUTES:
            assert meta_endpoints[route]["methods"] == ["GET"]
            assert meta_endpoints[route]["console_view"] == "research"


def test_capacity_endpoint_serves_policy_headroom_oos_and_pair_capacities(
    tmp_path: Path,
) -> None:
    report = _v2_report()
    report["capacity_and_readiness"] = {
        "execution_eligible": False,
        "release_permitted": False,
        "capital_policy": {
            "schema_version": "oasis.capital-risk-policy.v1",
            "total_capital_usd_micros": 500_000_000,
            "max_daily_loss_usd_micros": 200_000_000,
            "max_cumulative_loss_usd_micros": 500_000_000,
            "reserve_buffer_bps": 1000,
            "max_single_clip_share_bps": 5000,
            "min_clip_usd_micros": 10_000_000,
            "assumed_tail_stress_bps": 250,
            "max_feed_age_seconds": 1800,
            "config_sha256": sha256_hex(b"risk-policy"),
        },
        "headroom": {
            "deployable_capital_usd_micros": 450_000_000,
            "deployable_notional_usd_micros": 225_000_000,
            "max_clip_usd_micros": 225_000_000,
            "min_clip_usd_micros": 10_000_000,
            "reserve_floor_usd_micros": 50_000_000,
            "remaining_daily_loss_usd_micros": 199_689_941,
            "remaining_cumulative_loss_usd_micros": 499_689_941,
            "locked_inventory_usd_micros": 0,
            "pending_commitments_usd_micros": 0,
            "can_deploy": True,
            "block_reason": None,
        },
        "oos_telemetry": {
            "oos_trades_count": 42,
            "oos_positive_trades_count": 14,
            "oos_lcb_net_edge_bps": -3,
            "oos_mean_net_edge_bps": -1.81,
            "oos_total_net_profit_usd": -0.177,
            "oos_total_net_profit_usd_micros": -177_000,
            "oos_max_drawdown_usd": 0.45,
            "ticks_completed": 100,
            "window_label": "wave12_100tick",
        },
        "pair_capacities": {
            "ETH-USD": {
                "coinbase_book_capacity_usdc": 225_000_000,
                "optimal_notional_usdc": 120_000_000,
            },
            "WELL-USD": {
                "coinbase_book_capacity_usdc": 19_000_000,
                "optimal_notional_usdc": 15_000_000,
            },
        },
        "freshness": {
            "status": "fresh",
            "max_age_seconds": 1800,
            "stale": False,
        },
    }
    run_dir, digest = _write_radar_bundle(tmp_path / "radar-cap", report)
    with _radar_client(tmp_path, run_dir) as client:
        resp = client.get("/api/v1/radar/capacity")
        assert resp.status_code == 200
        body = resp.json()
        assert body["radar_report_sha256"] == digest
        assert body["execution_eligible"] is False
        assert body["release_permitted"] is False
        assert body["integrity"]["status"] == "verified"
        assert body["integrity"]["manifest_verified"] is True
        assert body["freshness"]["status"] == "fresh"
        assert body["freshness"]["stale"] is False
        assert body["freshness"]["max_age_seconds"] == 1800
        assert body["capital_policy"]["available"] is True
        assert body["capital_policy"]["total_capital_usd_micros"] == 500_000_000
        assert body["capital_policy"]["max_daily_loss_usd_micros"] == 200_000_000
        assert body["headroom"]["available"] is True
        assert body["headroom"]["deployable_notional_usd_micros"] == 225_000_000
        assert body["headroom"]["remaining_daily_loss_usd_micros"] == 199_689_941
        assert body["headroom"]["can_deploy"] is True
        assert body["headroom"]["blocked_reason"] is None
        assert body["oos_telemetry"]["available"] is True
        assert body["oos_telemetry"]["oos_trades_count"] == 42
        assert body["oos_telemetry"]["oos_lcb_net_edge_bps"] == -3
        assert body["oos_telemetry"]["oos_total_net_profit_usd"] == -0.177
        assert body["pair_capacities"]["available"] is True
        assert body["pair_capacities"]["pair_count"] == 2
        assert [item["pair"] for item in body["pair_capacities"]["items"]] == [
            "ETH-USD",
            "WELL-USD",
        ]

        etag = resp.headers["etag"]
        assert (
            client.get("/api/v1/radar/capacity", headers={"If-None-Match": etag}).status_code == 304
        )


@pytest.mark.parametrize(
    "bad_section",
    [
        {"capital_policy": ["not-an-object"]},
        {"headroom": {"execution_eligible": True}},
        {"headroom": {"release_permitted": True}},
        {"oos_telemetry": {"oos_trades_count": True}},
        {"oos_telemetry": {"oos_trades_count": -1}},
        {"pair_capacities": "invalid"},
        {"pair_capacities": ["not-an-object"]},
        {"freshness": {"stale": "yes"}},
    ],
)
def test_capacity_endpoint_fails_closed_on_malformed_or_authorizing_sections(
    tmp_path: Path,
    bad_section: dict[str, Any],
) -> None:
    report = _v2_report()
    report["capacity_and_readiness"] = bad_section
    run_dir, _ = _write_radar_bundle(tmp_path / "radar-bad-cap", report)
    with _radar_client(tmp_path, run_dir) as client:
        resp = client.get("/api/v1/radar/capacity")
        assert resp.status_code == 503
        assert resp.json() == {"detail": "opportunity radar bundle unavailable or corrupt"}
