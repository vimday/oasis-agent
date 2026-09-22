import pytest

from oasis_agent.launch.scoring import Candidate, ScorePolicy, score_candidate


def candidate():
    token = "0x" + "12" * 20
    return Candidate.model_validate(
        {
            "schema_version": "oasis.candidate.v1",
            "chain_id": 8453,
            "token": token,
            "evaluated_at": 1000,
            "trends": [
                {
                    "provider": p,
                    "chain_id": 8453,
                    "token": token,
                    "rank": 1,
                    "window_seconds": 300,
                    "observed_at": 990,
                    "retrieved_at": 995,
                    "raw_sha256": "a" * 64,
                }
                for p in ("gmgn", "okx")
            ],
            "chain": {
                "chain_id": 8453,
                "token": token,
                "block_number": 10,
                "block_hash": "0x" + "ab" * 32,
                "observed_at": 990,
                "evidence_sha256": "b" * 64,
                "holder_count": 1000,
                "holder_coverage": "pass",
                "holder_top10_bps": 1000,
                "common_control_bps": 500,
                "common_control_analysis": "pass",
                "contract_analysis": "pass",
                "sell_simulation": "pass",
                "mint_or_freeze_authority": "pass",
                "liquidity_exit": "pass",
                "quote_impact_bps": 20,
                "round_trip_cost_bps": 100,
                "executable_depth_usd_micros": 500_000_000,
                "liquidity_usd_micros": 100_000_000_000,
                "fdv_usd_micros": 500_000_000_000,
                "unique_buyers": 200,
                "unique_sellers": 50,
                "wash_trading": "pass",
            },
        }
    )


def changed(c, **kwargs):
    return Candidate.model_validate({**c.model_dump(mode="json"), **kwargs})


def test_good_candidate_is_only_a_reproducible_simulation_proposal():
    c = candidate()
    r = score_candidate(c, ScorePolicy())
    assert r.action == "simulate_entry"
    assert r == score_candidate(c, ScorePolicy())
    assert not r.execution_eligible
    assert r.lp_action == "requires_separate_lp_evaluation"


@pytest.mark.parametrize(
    "field",
    [
        "holder_coverage",
        "common_control_analysis",
        "contract_analysis",
        "sell_simulation",
        "mint_or_freeze_authority",
        "liquidity_exit",
        "wash_trading",
    ],
)
@pytest.mark.parametrize("state", ["unknown", "fail"])
def test_heat_never_overrides_failed_or_missing_safety(field, state):
    c = candidate()
    c = changed(c, chain={**c.chain.model_dump(), field: state})
    r = score_candidate(c, ScorePolicy())
    assert r.action == "reject" and field + "_" + state in r.blockers


@pytest.mark.parametrize(
    "field,value",
    [
        ("holder_top10_bps", 4001),
        ("common_control_bps", 1501),
        ("quote_impact_bps", 101),
        ("round_trip_cost_bps", 301),
        ("executable_depth_usd_micros", 1),
        ("holder_count", 1),
        ("observed_at", 1001),
        ("chain_id", 1),
    ],
)
def test_risk_boundaries_fail_closed(field, value):
    c = candidate()
    c = changed(c, chain={**c.chain.model_dump(), field: value})
    assert score_candidate(c, ScorePolicy()).action == "reject"


def test_source_duplicate_staleness_and_window_mismatch():
    c = candidate()
    t = c.trends[0].model_dump()
    for trends in (
        [t, t],
        [t],
        [t, {**c.trends[1].model_dump(), "observed_at": 900}],
        [t, {**c.trends[1].model_dump(), "window_seconds": 3600}],
    ):
        assert score_candidate(changed(c, trends=trends), ScorePolicy()).action == "reject"


@pytest.mark.parametrize(
    "value,high,reason",
    [(80, 100, "stop_loss"), (110, 130, "trailing_stop"), (125, 125, "take_profit")],
)
def test_exit_uses_net_liquidation_value_including_costs(value, high, reason):
    c = changed(
        candidate(),
        position={
            "entry_cost_usd_micros": 100,
            "net_exit_value_usd_micros": value,
            "high_water_exit_value_usd_micros": high,
            "opened_at": 900,
            "observed_at": 995,
            "evidence_sha256": "c" * 64,
        },
    )
    r = score_candidate(c, ScorePolicy())
    assert r.action == "simulate_exit" and r.exit_reason == reason
    c = changed(c, chain={**c.chain.model_dump(), "sell_simulation": "unknown"})
    assert score_candidate(c, ScorePolicy()).action == "halt_review"


def test_more_concentration_never_increases_score():
    c = candidate()
    scores = []
    for concentration in range(0, 10001, 100):
        scores.append(  # noqa: PERF401
            score_candidate(
                changed(c, chain={**c.chain.model_dump(), "holder_top10_bps": concentration}),
                ScorePolicy(),
            ).score_bps
        )
    assert scores == sorted(scores, reverse=True)


def test_float_unknown_fields_and_bad_weights_rejected():
    with pytest.raises(ValueError):
        changed(candidate(), evaluated_at=1000.0)
    with pytest.raises(ValueError):
        changed(candidate(), execute=True)
    with pytest.raises(ValueError):
        ScorePolicy(weights=(1, 1, 1, 1, 1))


def test_lp_forecast_must_cover_costs_hold_and_routing():
    c = changed(
        candidate(),
        lp={
            "expected_fee_usd_micros": 100,
            "all_costs_and_inventory_loss_usd_micros": 60,
            "hold_opportunity_usd_micros": 20,
            "routing": "pass",
            "observed_at": 990,
            "evidence_sha256": "d" * 64,
        },
    )
    assert score_candidate(c, ScorePolicy()).lp_action == "simulate_lp"
    for patch in (
        {"routing": "unknown"},
        {"all_costs_and_inventory_loss_usd_micros": 80},
        {"observed_at": 1},
    ):
        altered = changed(c, lp={**c.lp.model_dump(), **patch})
        assert (
            score_candidate(altered, ScorePolicy()).lp_action == "requires_separate_lp_evaluation"
        )


def test_overlapping_buyers_and_sellers_do_not_inflate_unique_trader_gate():
    c = candidate()
    c = changed(c, chain={**c.chain.model_dump(), "unique_buyers": 60, "unique_sellers": 60})
    result = score_candidate(c, ScorePolicy())
    assert result.action == "reject"
    assert "insufficient_trader_observations" in result.blockers


def test_existing_position_stop_loss_does_not_require_still_trending():
    c = changed(
        candidate(),
        trends=[],
        position={
            "entry_cost_usd_micros": 100_000_000,
            "net_exit_value_usd_micros": 80_000_000,
            "high_water_exit_value_usd_micros": 100_000_000,
            "opened_at": 900,
            "observed_at": 990,
            "evidence_sha256": "e" * 64,
        },
    )
    result = score_candidate(c, ScorePolicy())
    assert result.action == "simulate_exit"
    assert result.exit_reason == "stop_loss"
