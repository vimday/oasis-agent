"""Pool-flow diagnostics preserve signs, fractions and incomplete sample counts."""

import json
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.models.common import canonical_json
from scripts.report_lp_market_economics import Q192, report, summarize
from tests.unit.test_lp_features import agreed
from tests.unit.test_market_window import MANAGER, POOL, build, inputs


def test_directional_markouts_do_not_net_away_adverse_flow(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        window = build(store, inputs(store))
        result = summarize(window)
        left, right = result["directions"]
        rows = {row["direction"]: row for row in (left, right)}
        assert sum(row["matured_swaps"] for row in rows.values()) == 2
        assert result["pending_count"] == 1
        assert result["markouts_complete"] is False
        assert sum(row["pool_markout1_numerator"] for row in rows.values()) == -55 * Q192
        assert sum(row["adverse_markout1_numerator"] for row in rows.values()) == 65 * Q192
        assert sum(row["favorable_markout1_numerator"] for row in rows.values()) == 10 * Q192
        assert result["execution_eligible"] is result["out_of_sample_accepted"] is False
        event = window.matured[0]
        fractional = agreed("future_price_x192", 1, event.value.future_price_x192.block.number)
        changed = replace(event, value=replace(event.value, future_price_x192=fractional))
        exact = summarize(replace(window, matured=(changed,)))
        assert sum(row["pool_markout1_numerator"] for row in exact["directions"]) == (
            event.value.amount0 + event.value.amount1 * Q192
        )
    finally:
        store.close()


def test_raw_cli_replay_and_missing_evidence_refusal(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        swaps, prices = inputs(store, empty=True)
    finally:
        store.close()
    request = tmp_path / "request.json"
    request.write_bytes(
        canonical_json(
            {
                "swap_references": [asdict(r) for r in swaps],
                "price_references": [asdict(r) for r in prices],
                "pool": asdict(POOL),
                "pool_manager": MANAGER,
                "horizon_seconds": 1,
                "max_lateness_seconds": 1,
            }
        )
    )
    output = tmp_path / "report.json"
    result = report(request, tmp_path / "evidence.db", output)
    assert result["matured_count"] == result["swap_count"] == 0
    assert json.loads(output.read_bytes())["execution_eligible"] is False
    with pytest.raises(ValueError, match="new report"):
        report(request, tmp_path / "evidence.db", output)
    with pytest.raises(ValueError, match="existing evidence"):
        report(request, tmp_path / "absent.db", tmp_path / "absent.json")
    assert not (tmp_path / "absent.json").exists()
