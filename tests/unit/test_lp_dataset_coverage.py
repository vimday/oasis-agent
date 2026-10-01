"""Short, discontinuous or unverifiable inputs cannot stand in for an OOS dataset."""

import json
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.lp_dataset_coverage import assess_dataset_coverage
from oasis_agent.models.common import canonical_json
from scripts.check_lp_dataset_coverage import check
from tests.unit.test_lp_features import agreed
from tests.unit.test_market_window import MANAGER, POOL, build, inputs


def test_short_real_replay_is_a_rejection_not_a_profitability_result(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        window = build(store, inputs(store, final_swap=False))
        report = assess_dataset_coverage((window,))
        assert not report.coverage_passed
        assert {"development_coverage_below_21_days", "untouched_coverage_below_7_days"} <= set(
            report.reasons
        )
        assert not report.execution_eligible and not report.out_of_sample_accepted
        assert assess_dataset_coverage((window,)).payload == report.payload
        with pytest.raises(ValueError, match="head did not advance"):
            assess_dataset_coverage((window, window))
    finally:
        store.close()


def test_two_distant_snapshots_do_not_prove_continuous_coverage(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        first = build(store, inputs(store, final_swap=False))
        price = agreed(
            "sqrt_price_x96", first.prices[-1].value, first.decision_block.number + 35 * 86400
        )
        last = replace(first, decision_block=price.block, prices=(*first.prices, price))
        report = assess_dataset_coverage((first, last))
        assert "decision_cadence_exceeds_30s_or_changed" in report.reasons
        assert "price_observation_gap" in report.reasons
        assert not report.coverage_passed
    finally:
        store.close()


def test_cli_replays_raw_inputs_and_retains_only_a_diagnostic(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        swaps, prices = inputs(store, final_swap=False)
    finally:
        store.close()
    (tmp_path / "request.json").write_bytes(
        canonical_json(
            {
                "swap_references": [asdict(item) for item in swaps],
                "price_references": [asdict(item) for item in prices],
                "pool": asdict(POOL),
                "pool_manager": MANAGER,
                "horizon_seconds": 1,
                "max_lateness_seconds": 1,
            }
        )
    )
    request = tmp_path / "dataset.json"
    request.write_text(
        json.dumps(
            {
                "schema": "oasis.lp-coverage-input.v1",
                "windows": [{"request": "request.json", "evidence": "evidence.db"}],
            }
        )
    )
    output = tmp_path / "report.json"
    assert check(request, output) is False
    assert json.loads(output.read_bytes())["out_of_sample_accepted"] is False
    with pytest.raises(ValueError, match="new file"):
        check(request, output)
    (tmp_path / "evidence.db").unlink()
    with pytest.raises(ValueError, match="evidence database"):
        check(request, tmp_path / "missing.json")
    assert not (tmp_path / "missing.json").exists()
