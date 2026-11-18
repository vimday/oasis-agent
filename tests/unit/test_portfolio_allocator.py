"""Unit tests for Stage 7 cross-strategy portfolio capital allocator."""

from dataclasses import replace

import pytest

from oasis_agent.models.common import sha256_hex
from oasis_agent.portfolio import (
    ExposureDomainCap,
    PortfolioAllocationReport,
    PortfolioAllocator,
    PortfolioAllocatorConfig,
    PortfolioReserves,
    StrategyTrackCandidate,
)

USD = 1_000_000


def _reserves(
    *,
    lp_gas: int = 50 * USD,
    perp_margin: int = 100 * USD,
    incident: int = 50 * USD,
    ops: int = 0,
) -> PortfolioReserves:
    return PortfolioReserves(
        lp_unwind_gas_usd_micros=lp_gas,
        perp_margin_buffer_usd_micros=perp_margin,
        incident_cash_usd_micros=incident,
        ops_reserve_usd_micros=ops,
    )


def _track(
    strategy_id: str = "strat_a",
    *,
    stage: int = 1,
    current: int = 0,
    capacity: int = 500 * USD,
    max_step_up: int = 500 * USD,
    lcb_net_edge_bps: int = 120,
    expected_drawdown_bps: int = 100,
    tail_loss_99_bps: int = 200,
    evidence_age_seconds: int = 10,
    max_evidence_age_seconds: int = 60,
    reconciliation_residual_raw: int = 0,
    error_budget_halted: bool = False,
    exposures: tuple[tuple[str, str], ...] = (("chain", "base"),),
    live_authorized: bool = False,
) -> StrategyTrackCandidate:
    return StrategyTrackCandidate(
        strategy_id=strategy_id,
        stage=stage,
        current_allocation_usd_micros=current,
        capacity_usd_micros=capacity,
        max_step_up_usd_micros=max_step_up,
        lcb_net_edge_bps=lcb_net_edge_bps,
        expected_drawdown_bps=expected_drawdown_bps,
        tail_loss_99_bps=tail_loss_99_bps,
        evidence_age_seconds=evidence_age_seconds,
        max_evidence_age_seconds=max_evidence_age_seconds,
        reconciliation_residual_raw=reconciliation_residual_raw,
        error_budget_halted=error_budget_halted,
        exposures=exposures,  # type: ignore[arg-type]
        live_authorized=live_authorized,
    )


def test_reserves_segregated_before_allocation_and_conserved() -> None:
    reserves = _reserves(lp_gas=50 * USD, perp_margin=100 * USD, incident=50 * USD, ops=25 * USD)
    assert reserves.total_usd_micros == 225 * USD

    allocator = PortfolioAllocator(
        PortfolioAllocatorConfig(max_single_strategy_share_bps=10_000),
    )
    report = allocator.allocate(
        total_nav_usd_micros=1_000 * USD,
        reserves=reserves,
        tracks=[
            _track("s1", capacity=400 * USD, max_step_up=400 * USD),
            _track("s2", capacity=500 * USD, max_step_up=500 * USD, lcb_net_edge_bps=80),
        ],
    )
    assert isinstance(report, PortfolioAllocationReport)
    assert report.segregated_reserves_usd_micros == 225 * USD
    assert report.deployable_budget_usd_micros == 775 * USD
    assert report.reserve_deficit_usd_micros == 0
    assert report.allocated_total_usd_micros == 775 * USD
    assert report.unallocated_cash_usd_micros == 0
    assert report.execution_eligible is False

    by_id = {d.strategy_id: d for d in report.decisions}
    assert by_id["s1"].target_usd_micros == 400 * USD
    assert by_id["s2"].target_usd_micros == 375 * USD
    assert "deployable_nav_exhausted" in by_id["s2"].binding_constraints
    assert all(d.execution_eligible is False for d in report.decisions)


def test_reserve_deficit_halts_all_tracks_and_scales_to_zero_immediately() -> None:
    reserves = _reserves(lp_gas=100 * USD, perp_margin=150 * USD, incident=100 * USD)
    allocator = PortfolioAllocator()

    report = allocator.allocate(
        total_nav_usd_micros=300 * USD,
        reserves=reserves,
        tracks=[
            _track("funded", current=200 * USD, max_step_up=10 * USD, live_authorized=True),
            _track("unfunded", current=0, max_step_up=10 * USD),
        ],
    )
    assert report.segregated_reserves_usd_micros == 350 * USD
    assert report.reserve_deficit_usd_micros == 50 * USD
    assert report.deployable_budget_usd_micros == 0
    assert report.allocated_total_usd_micros == 0
    assert report.unallocated_cash_usd_micros == 0

    by_id = {d.strategy_id: d for d in report.decisions}
    assert by_id["funded"].target_usd_micros == 0
    assert by_id["funded"].delta_usd_micros == -200 * USD
    assert by_id["funded"].action == "scale_to_zero"
    assert "portfolio_reserve_deficit" in by_id["funded"].reasons
    assert by_id["funded"].execution_eligible is False

    assert by_id["unfunded"].target_usd_micros == 0
    assert by_id["unfunded"].action == "blocked_zero"
    assert "portfolio_reserve_deficit" in by_id["unfunded"].reasons


def test_exact_reserve_boundary_zero_deficit_zero_deployable() -> None:
    reserves = _reserves(lp_gas=100 * USD, perp_margin=100 * USD, incident=100 * USD)
    allocator = PortfolioAllocator()
    report = allocator.allocate(
        total_nav_usd_micros=300 * USD,
        reserves=reserves,
        tracks=[_track("healthy", current=50 * USD)],
    )
    assert report.reserve_deficit_usd_micros == 0
    assert report.deployable_budget_usd_micros == 0
    assert report.allocated_total_usd_micros == 0
    decision = report.decisions[0]
    assert decision.target_usd_micros == 0
    assert decision.action == "scale_down"
    assert "deployable_nav_exhausted" in decision.binding_constraints


@pytest.mark.parametrize(
    ("override", "expected_reason"),
    [
        ({"error_budget_halted": True}, "error_or_revert_budget_halted"),
        ({"reconciliation_residual_raw": 1}, "reconciliation_residual_nonzero"),
        ({"reconciliation_residual_raw": -5}, "reconciliation_residual_nonzero"),
        ({"evidence_age_seconds": 61, "max_evidence_age_seconds": 60}, "stale_evidence"),
        ({"lcb_net_edge_bps": 0}, "negative_or_insufficient_lcb_edge"),
        ({"lcb_net_edge_bps": -45}, "negative_or_insufficient_lcb_edge"),
        ({"tail_loss_99_bps": 2_001}, "tail_loss_exceeds_ceiling"),
    ],
)
def test_instant_scale_to_zero_on_all_five_track_disqualifiers(
    override: dict[str, object],
    expected_reason: str,
) -> None:
    allocator = PortfolioAllocator()
    bad_funded = _track(
        "bad_funded",
        current=250 * USD,
        max_step_up=5 * USD,  # small step-up must never slow down a cut to zero
        **override,  # type: ignore[arg-type]
    )
    bad_zero = _track(
        "bad_zero",
        current=0,
        max_step_up=5 * USD,
        **override,  # type: ignore[arg-type]
    )
    good = _track("good", current=0, capacity=200 * USD, max_step_up=200 * USD)

    report = allocator.allocate(
        total_nav_usd_micros=1_000 * USD,
        reserves=_reserves(),
        tracks=[bad_funded, bad_zero, good],
    )
    by_id = {d.strategy_id: d for d in report.decisions}

    assert by_id["bad_funded"].target_usd_micros == 0
    assert by_id["bad_funded"].delta_usd_micros == -250 * USD
    assert by_id["bad_funded"].action == "scale_to_zero"
    assert by_id["bad_funded"].risk_adjusted_score == 0
    assert expected_reason in by_id["bad_funded"].reasons

    assert by_id["bad_zero"].target_usd_micros == 0
    assert by_id["bad_zero"].action == "blocked_zero"
    assert expected_reason in by_id["bad_zero"].reasons

    # Healthy track still receives its allowed allocation.
    assert by_id["good"].target_usd_micros == 200 * USD
    assert by_id["good"].action == "scale_up"


def test_boundary_values_for_evidence_age_edge_and_tail_loss_remain_eligible() -> None:
    allocator = PortfolioAllocator(
        PortfolioAllocatorConfig(
            min_lcb_edge_bps=1,
            max_tail_loss_99_bps=2_000,
            max_single_strategy_share_bps=10_000,
        )
    )
    boundary = _track(
        "boundary",
        evidence_age_seconds=60,
        max_evidence_age_seconds=60,
        lcb_net_edge_bps=1,
        tail_loss_99_bps=2_000,
        capacity=100 * USD,
        max_step_up=100 * USD,
    )
    report = allocator.allocate(
        total_nav_usd_micros=500 * USD,
        reserves=_reserves(),
        tracks=[boundary],
    )
    assert report.decisions[0].target_usd_micros == 100 * USD
    assert report.decisions[0].action == "scale_up"


def test_multi_step_slow_scale_up_and_fast_automatic_scale_down() -> None:
    allocator = PortfolioAllocator(
        PortfolioAllocatorConfig(max_single_strategy_share_bps=10_000),
    )
    reserves = _reserves(lp_gas=50 * USD, perp_margin=50 * USD, incident=100 * USD)

    # Step 1: 0 -> 50M (capped by slow_scale_up_step_cap)
    t1 = _track("lp", current=0, capacity=180 * USD, max_step_up=50 * USD)
    r1 = allocator.allocate(1_000 * USD, reserves, [t1])
    d1 = r1.decisions[0]
    assert d1.target_usd_micros == 50 * USD
    assert d1.action == "scale_up"
    assert "slow_scale_up_step_cap" in d1.binding_constraints

    # Step 2: 50M -> 100M
    t2 = replace(t1, current_allocation_usd_micros=d1.target_usd_micros)
    r2 = allocator.allocate(1_000 * USD, reserves, [t2])
    assert r2.decisions[0].target_usd_micros == 100 * USD
    assert r2.decisions[0].action == "scale_up"

    # Step 3: 100M -> 150M
    t3 = replace(t1, current_allocation_usd_micros=100 * USD)
    r3 = allocator.allocate(1_000 * USD, reserves, [t3])
    assert r3.decisions[0].target_usd_micros == 150 * USD
    assert r3.decisions[0].action == "scale_up"

    # Step 4: 150M -> 180M (capped by capacity_cap)
    t4 = replace(t1, current_allocation_usd_micros=150 * USD)
    r4 = allocator.allocate(1_000 * USD, reserves, [t4])
    assert r4.decisions[0].target_usd_micros == 180 * USD
    assert r4.decisions[0].action == "scale_up"
    assert "capacity_cap" in r4.decisions[0].binding_constraints

    # Step 5: steady hold at 180M
    t5 = replace(t1, current_allocation_usd_micros=180 * USD)
    r5 = allocator.allocate(1_000 * USD, reserves, [t5])
    assert r5.decisions[0].target_usd_micros == 180 * USD
    assert r5.decisions[0].action == "hold"

    # Fast automatic scale-down when capacity shrinks to 60M (ignores 50M step size)
    t_shrink = replace(t5, capacity_usd_micros=60 * USD)
    r_shrink = allocator.allocate(1_000 * USD, reserves, [t_shrink])
    assert r_shrink.decisions[0].target_usd_micros == 60 * USD
    assert r_shrink.decisions[0].delta_usd_micros == -120 * USD
    assert r_shrink.decisions[0].action == "scale_down"
    assert "capacity_cap" in r_shrink.decisions[0].binding_constraints


def test_risk_adjusted_scoring_tie_breaking_and_single_strategy_share_cap() -> None:
    allocator = PortfolioAllocator(
        PortfolioAllocatorConfig(max_single_strategy_share_bps=5_000),
    )
    reserves = _reserves(lp_gas=100 * USD, perp_margin=100 * USD, incident=0)
    # deployable = 1000 - 200 = 800 USD -> 50% share cap = 400 USD

    # High nominal edge (500 bps) but huge drawdown+tail -> risk_denom = 1000 + 2*1500 = 4000
    # score = 500 * 10000 // 4000 = 1250
    high_risk = _track(
        "meme_high_vol",
        stage=6,
        lcb_net_edge_bps=500,
        expected_drawdown_bps=1_000,
        tail_loss_99_bps=1_500,
        capacity=600 * USD,
        max_step_up=600 * USD,
    )
    # Lower nominal edge (120 bps) but tight tail risk -> risk_denom = max(100, 20 + 2*30) = 100
    # score = 120 * 10000 // 100 = 12000 (wins priority!)
    low_risk = _track(
        "atomic_arb",
        stage=2,
        lcb_net_edge_bps=120,
        expected_drawdown_bps=20,
        tail_loss_99_bps=30,
        capacity=600 * USD,
        max_step_up=600 * USD,
    )
    # Equal score tie with atomic_arb -> lexicographic strategy_id orders "atomic_arb" < "zz_arb"
    tied_arb = replace(low_risk, strategy_id="zz_arb")

    report = allocator.allocate(
        total_nav_usd_micros=1_000 * USD,
        reserves=reserves,
        tracks=[high_risk, tied_arb, low_risk],
    )
    assert [d.strategy_id for d in report.decisions] == [
        "atomic_arb",
        "zz_arb",
        "meme_high_vol",
    ]
    assert report.decisions[0].risk_adjusted_score == 12_000
    assert report.decisions[0].target_usd_micros == 400 * USD
    assert "single_strategy_share_cap" in report.decisions[0].binding_constraints

    assert report.decisions[1].target_usd_micros == 400 * USD
    assert report.decisions[2].target_usd_micros == 0
    assert "deployable_nav_exhausted" in report.decisions[2].binding_constraints
    assert report.allocated_total_usd_micros == 800 * USD
    assert report.unallocated_cash_usd_micros == 0


def test_shared_correlation_domain_caps_across_all_seven_domain_kinds() -> None:
    allocator = PortfolioAllocator(
        PortfolioAllocatorConfig(max_single_strategy_share_bps=10_000),
    )
    reserves = _reserves(lp_gas=0, perp_margin=0, incident=100 * USD)
    # Deployable = 900 USD

    # Primary strategy uses base/sequencer/usdc/aerodrome/pyth/fireblocks
    best = _track(
        "best_base",
        stage=2,
        lcb_net_edge_bps=200,
        expected_drawdown_bps=50,
        tail_loss_99_bps=50,  # denom = 150, score = 13333
        capacity=400 * USD,
        max_step_up=400 * USD,
        exposures=(
            ("chain", "base"),
            ("chain", "base"),  # duplicate exposure on same track must not double-count
            ("sequencer", "base_seq"),
            ("token", "weth"),
            ("stablecoin", "usdc"),
            ("venue", "aerodrome"),
            ("oracle", "pyth"),
            ("custodian", "fireblocks"),
        ),
    )
    # Duplicate-risk inferior strategy sharing base/usdc -> clamped to remaining domain headroom
    second_base = _track(
        "second_base",
        stage=6,
        current=150 * USD,
        lcb_net_edge_bps=100,
        expected_drawdown_bps=100,
        tail_loss_99_bps=100,  # denom = 300, score = 3333
        capacity=400 * USD,
        max_step_up=400 * USD,
        exposures=(
            ("chain", "base"),
            ("stablecoin", "usdc"),
            ("venue", "uniswap_v4"),
        ),
    )
    # Independent chain/venue strategy unaffected by base cap
    uncorrelated = _track(
        "robinhood_lp",
        stage=1,
        lcb_net_edge_bps=90,
        expected_drawdown_bps=100,
        tail_loss_99_bps=100,  # denom = 300, score = 3000
        capacity=300 * USD,
        max_step_up=300 * USD,
        exposures=(
            ("chain", "robinhood"),
            ("venue", "uniswap_v4"),
            ("stablecoin", "usdg"),
        ),
    )

    caps = (
        ExposureDomainCap("chain", "base", 450 * USD),
        ExposureDomainCap("sequencer", "base_seq", 500 * USD),
        ExposureDomainCap("token", "weth", 500 * USD),
        ExposureDomainCap("stablecoin", "usdc", 500 * USD),
        ExposureDomainCap("venue", "aerodrome", 400 * USD),
        ExposureDomainCap("oracle", "pyth", 600 * USD),
        ExposureDomainCap("custodian", "fireblocks", 600 * USD),
        ExposureDomainCap("venue", "uniswap_v4", 500 * USD),
        ExposureDomainCap("oracle", "chainlink", 250 * USD),  # unused declared cap stays at 0
    )

    report = allocator.allocate(
        total_nav_usd_micros=1_000 * USD,
        reserves=reserves,
        tracks=[second_base, uncorrelated, best],
        domain_caps=caps,
    )
    by_id = {d.strategy_id: d for d in report.decisions}

    assert by_id["best_base"].target_usd_micros == 400 * USD
    assert by_id["second_base"].target_usd_micros == 50 * USD
    assert by_id["second_base"].action == "scale_down"
    assert by_id["second_base"].binding_constraints == ("domain_cap:chain:base",)
    assert by_id["robinhood_lp"].target_usd_micros == 300 * USD
    assert by_id["robinhood_lp"].action == "scale_up"

    assert report.domain_utilization_usd_micros == {
        "chain:base": 450 * USD,
        "chain:robinhood": 300 * USD,
        "custodian:fireblocks": 400 * USD,
        "oracle:chainlink": 0,
        "oracle:pyth": 400 * USD,
        "sequencer:base_seq": 400 * USD,
        "stablecoin:usdc": 450 * USD,
        "stablecoin:usdg": 300 * USD,
        "token:weth": 400 * USD,
        "venue:aerodrome": 400 * USD,
        "venue:uniswap_v4": 350 * USD,
    }
    assert report.allocated_total_usd_micros == 750 * USD
    assert report.unallocated_cash_usd_micros == 150 * USD


def test_determinism_permutation_invariance_and_hash_sensitivity() -> None:
    allocator = PortfolioAllocator()
    reserves = _reserves()
    t1 = _track("alpha", lcb_net_edge_bps=150, exposures=(("chain", "base"),))
    t2 = _track("beta", lcb_net_edge_bps=90, exposures=(("chain", "robinhood"),))
    t3 = _track("gamma", error_budget_halted=True)
    caps = (
        ExposureDomainCap("chain", "base", 300 * USD),
        ExposureDomainCap("chain", "robinhood", 300 * USD),
    )

    r1 = allocator.allocate(1_000 * USD, reserves, [t1, t2, t3], caps)
    r2 = allocator.allocate(1_000 * USD, reserves, [t3, t2, t1], list(reversed(caps)))
    assert r1 == r2
    assert r1.report_sha256 == r2.report_sha256
    assert len(r1.report_sha256) == 64

    # Changing a single micro alters the SHA-256 commitment.
    r3 = allocator.allocate(1_000 * USD + 1, reserves, [t1, t2, t3], caps)
    assert r3.report_sha256 != r1.report_sha256


def test_strict_validation_rejects_bools_floats_duplicates_and_execution_flag() -> None:
    with pytest.raises(TypeError):
        PortfolioReserves(
            lp_unwind_gas_usd_micros=True,
            perp_margin_buffer_usd_micros=0,
            incident_cash_usd_micros=0,
        )
    with pytest.raises(ValueError):
        PortfolioReserves(
            lp_unwind_gas_usd_micros=-1,
            perp_margin_buffer_usd_micros=0,
            incident_cash_usd_micros=0,
        )
    with pytest.raises(ValueError):
        ExposureDomainCap("bridge", "across", 100)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ExposureDomainCap("chain", "   ", 100)
    with pytest.raises(ValueError):
        _track("bad_stage", stage=8)
    with pytest.raises(TypeError):
        _track("bool_cap", capacity=False)
    with pytest.raises(TypeError):
        _track("float_edge", lcb_net_edge_bps=12.5)  # type: ignore[arg-type]

    allocator = PortfolioAllocator()
    with pytest.raises(ValueError, match="duplicate strategy_id"):
        allocator.allocate(500 * USD, _reserves(), [_track("dup"), _track("dup")])
    with pytest.raises(ValueError, match="duplicate domain cap"):
        allocator.allocate(
            500 * USD,
            _reserves(),
            [_track("a")],
            [
                ExposureDomainCap("chain", "base", 100 * USD),
                ExposureDomainCap("chain", "base", 200 * USD),
            ],
        )
    with pytest.raises(TypeError):
        allocator.allocate(True, _reserves(), [_track("a")])

    # execution_eligible can never be overridden to True at runtime.
    with pytest.raises(ValueError, match="execution_eligible"):
        PortfolioAllocationReport(
            total_nav_usd_micros=100,
            segregated_reserves_usd_micros=0,
            deployable_budget_usd_micros=100,
            allocated_total_usd_micros=0,
            unallocated_cash_usd_micros=100,
            reserve_deficit_usd_micros=0,
            domain_utilization_usd_micros={},
            decisions=(),
            report_sha256=sha256_hex(b"report"),
            execution_eligible=True,  # type: ignore[arg-type]
        )
