"""Unit tests for fail-closed LaunchpadSafetyGate and exact integer decay curves."""

from dataclasses import replace

import pytest

from oasis_agent.launch.launchpad_safety import (
    CLANKER_V4_FEE_SCHEDULE_BPS,
    DecisionStatus,
    LaunchpadArchetype,
    LaunchpadSafetyEvidence,
    LaunchpadSafetyPolicy,
    compute_linear_decay_fee_bps,
    compute_stepped_auction_fee_bps,
    earliest_safe_entry_ms,
    evaluate_launchpad_safety,
)
from oasis_agent.models.common import sha256_hex

BASE_CHAIN_ID = 8453
TOKEN = "0x" + "11" * 20
POOL = "0x" + "22" * 20
BLOCK_HASH = "0x" + "33" * 32
RAW_SHA256 = sha256_hex(b"launchpad-evidence")
GENESIS_S = 1_794_981_000
GENESIS_MS = GENESIS_S * 1_000


def _make_evidence(**overrides: object) -> LaunchpadSafetyEvidence:
    defaults: dict[str, object] = {
        "chain_id": BASE_CHAIN_ID,
        "token": TOKEN,
        "pool": POOL,
        "archetype": LaunchpadArchetype.DOPPLER_MULTICURVE,
        "block_number": 58_000_050,
        "block_hash": BLOCK_HASH,
        "block_timestamp_s": GENESIS_S + 10,
        "block_timestamp_ms": GENESIS_MS + 10_000,
        "observed_at_ms": GENESIS_MS + 10_200,
        "source_count": 2,
        "buy_tax_bps": 120,
        "sell_tax_bps": 120,
        "sell_enabled": True,
        "unlock_timestamp_ms": GENESIS_MS,
        "genesis_timestamp_ms": GENESIS_MS,
        "decay_start_fee_bps": 8_000,
        "decay_end_fee_bps": 120,
        "decay_duration_ms": 10_000,
        "simulated_buy_in": 1_000_000,
        "simulated_buy_out": 988_000,
        "simulated_sell_out": 970_000,
        "exit_liquidity_usd_micros": 50_000_000_000,
        "creator_same_block_buy_bps": 200,
        "bundled_cluster_bps": 800,
        "creator_prior_rug_count": 0,
        "raw_sha256": RAW_SHA256,
    }
    defaults.update(overrides)
    return LaunchpadSafetyEvidence(**defaults)  # type: ignore[arg-type]


def test_linear_and_stepped_decay_curves_are_exact_integers() -> None:
    # Doppler 80% -> 1.2% over 10s
    assert compute_linear_decay_fee_bps(8_000, 120, GENESIS_MS, GENESIS_MS - 200, 10_000) == 8_000
    assert compute_linear_decay_fee_bps(8_000, 120, GENESIS_MS, GENESIS_MS, 10_000) == 8_000
    assert compute_linear_decay_fee_bps(8_000, 120, GENESIS_MS, GENESIS_MS + 5_000, 10_000) == 4_060
    assert compute_linear_decay_fee_bps(8_000, 120, GENESIS_MS, GENESIS_MS + 9_800, 10_000) == 278
    assert compute_linear_decay_fee_bps(8_000, 120, GENESIS_MS, GENESIS_MS + 10_000, 10_000) == 120
    assert compute_linear_decay_fee_bps(8_000, 120, GENESIS_MS, GENESIS_MS + 15_000, 10_000) == 120

    # Exact millisecond threshold vs Base Denim 200ms slot grid
    exact_ms = earliest_safe_entry_ms(
        max_buy_tax_bps=200,
        genesis_timestamp_ms=GENESIS_MS,
        decay_start_fee_bps=8_000,
        decay_end_fee_bps=120,
        decay_duration_ms=10_000,
        slot_step_ms=1,
    )
    assert exact_ms == GENESIS_MS + 9_899
    assert compute_linear_decay_fee_bps(8_000, 120, GENESIS_MS, exact_ms, 10_000) == 200
    assert compute_linear_decay_fee_bps(8_000, 120, GENESIS_MS, exact_ms - 1, 10_000) == 201

    denim_slot_ms = earliest_safe_entry_ms(
        max_buy_tax_bps=200,
        genesis_timestamp_ms=GENESIS_MS,
        decay_start_fee_bps=8_000,
        decay_end_fee_bps=120,
        decay_duration_ms=10_000,
    )
    assert denim_slot_ms == GENESIS_MS + 10_000
    assert denim_slot_ms % 1_000 == 0

    # Clanker v4 5-round auction: 8000 -> 6200 -> 4400 -> 2600 -> 800 -> normal
    assert compute_stepped_auction_fee_bps(CLANKER_V4_FEE_SCHEDULE_BPS, 5, -1, 100) == 8_000
    assert compute_stepped_auction_fee_bps(CLANKER_V4_FEE_SCHEDULE_BPS, 5, 0, 100) == 8_000
    assert compute_stepped_auction_fee_bps(CLANKER_V4_FEE_SCHEDULE_BPS, 5, 5, 100) == 6_200
    assert compute_stepped_auction_fee_bps(CLANKER_V4_FEE_SCHEDULE_BPS, 5, 10, 100) == 4_400
    assert compute_stepped_auction_fee_bps(CLANKER_V4_FEE_SCHEDULE_BPS, 5, 15, 100) == 2_600
    assert compute_stepped_auction_fee_bps(CLANKER_V4_FEE_SCHEDULE_BPS, 5, 20, 100) == 800
    assert compute_stepped_auction_fee_bps(CLANKER_V4_FEE_SCHEDULE_BPS, 5, 25, 100) == 100

    with pytest.raises(TypeError):
        compute_linear_decay_fee_bps(8_000.0, 120, GENESIS_MS, GENESIS_MS, 10_000)  # type: ignore[arg-type]


def test_doppler_10s_linear_decay_defers_until_exact_denim_slot_then_approves() -> None:
    policy = LaunchpadSafetyPolicy()

    # At t = +5.000s, schedule fee is 4060 bps (40.6%), sell simulation reflects active buy tax.
    mid_decay = _make_evidence(
        block_timestamp_s=GENESIS_S + 5,
        block_timestamp_ms=GENESIS_MS + 5_000,
        observed_at_ms=GENESIS_MS + 5_200,
        buy_tax_bps=4_060,
        simulated_buy_in=1_000_000,
        simulated_buy_out=594_000,
        simulated_sell_out=580_000,
    )
    deferred = evaluate_launchpad_safety(mid_decay, policy)
    assert deferred.status == DecisionStatus.DEFER_UNTIL_DECAY
    assert deferred.status == "DEFER_UNTIL_DECAY"
    assert deferred.schedule_fee_bps == 4_060
    assert deferred.effective_buy_tax_bps == 4_060
    assert deferred.earliest_safe_entry_ms == GENESIS_MS + 10_000

    # At t = +10.000s, decay completed at 120 bps <= 200 bps -> APPROVED.
    post_decay = _make_evidence()
    approved = evaluate_launchpad_safety(post_decay, policy)
    assert approved.status == DecisionStatus.APPROVED
    assert approved.schedule_fee_bps == 120
    assert approved.effective_buy_tax_bps == 120
    assert approved.earliest_safe_entry_ms == GENESIS_MS + 10_000
    assert approved.reasons == ()


def test_clanker_v4_stepped_auction_defers_and_approves() -> None:
    policy = LaunchpadSafetyPolicy()
    # 5 rounds * 5 blocks/round * 200ms = 25 blocks = 5_000ms; at block +10 (round 2: 4400).
    during_auction = _make_evidence(
        archetype=LaunchpadArchetype.CLANKER_V4,
        block_number=58_000_010,
        block_timestamp_s=GENESIS_S + 2,
        block_timestamp_ms=GENESIS_MS + 2_000,
        observed_at_ms=GENESIS_MS + 2_100,
        buy_tax_bps=4_400,
        sell_tax_bps=100,
        unlock_timestamp_ms=GENESIS_MS,
        genesis_timestamp_ms=None,
        decay_start_fee_bps=None,
        decay_end_fee_bps=None,
        decay_duration_ms=None,
        auction_fee_schedule_bps=CLANKER_V4_FEE_SCHEDULE_BPS,
        auction_round_blocks=5,
        genesis_block_number=58_000_000,
        normal_fee_bps=100,
        simulated_buy_in=1_000_000,
        simulated_buy_out=560_000,
        simulated_sell_out=550_000,
    )
    res_defer = evaluate_launchpad_safety(during_auction, policy)
    assert res_defer.status == DecisionStatus.DEFER_UNTIL_DECAY
    assert res_defer.schedule_fee_bps == 4_400
    assert res_defer.earliest_safe_entry_ms == GENESIS_MS + 5_000

    post_auction = replace(
        during_auction,
        block_number=58_000_025,
        block_timestamp_s=GENESIS_S + 5,
        block_timestamp_ms=GENESIS_MS + 5_000,
        observed_at_ms=GENESIS_MS + 5_200,
        buy_tax_bps=100,
        simulated_buy_out=990_000,
        simulated_sell_out=975_000,
    )
    res_ok = evaluate_launchpad_safety(post_auction, policy)
    assert res_ok.status == DecisionStatus.APPROVED
    assert res_ok.schedule_fee_bps == 100


def test_zora_flaunch_and_virtuals_schedules() -> None:
    policy = LaunchpadSafetyPolicy()

    # Zora 99% -> 1% over 10s
    zora_early = _make_evidence(
        archetype=LaunchpadArchetype.ZORA_CREATOR,
        block_timestamp_s=GENESIS_S + 1,
        block_timestamp_ms=GENESIS_MS + 1_000,
        observed_at_ms=GENESIS_MS + 1_200,
        buy_tax_bps=8_920,
        sell_tax_bps=100,
        decay_start_fee_bps=9_900,
        decay_end_fee_bps=100,
        decay_duration_ms=10_000,
        simulated_buy_in=1_000_000,
        simulated_buy_out=108_000,
        simulated_sell_out=105_000,
    )
    zora_res = evaluate_launchpad_safety(zora_early, policy)
    assert zora_res.status == DecisionStatus.DEFER_UNTIL_DECAY
    assert zora_res.earliest_safe_entry_ms == GENESIS_MS + 10_000

    # Flaunch 30-min FairLaunch lock (1_800_000 ms), sell_enabled=False while locked
    unlock_ms = GENESIS_MS + 1_800_000
    flaunch_locked = _make_evidence(
        archetype=LaunchpadArchetype.FLAUNCH_FAIRLAUNCH,
        block_timestamp_s=GENESIS_S + 300,
        block_timestamp_ms=GENESIS_MS + 300_000,
        observed_at_ms=GENESIS_MS + 300_200,
        buy_tax_bps=100,
        sell_tax_bps=100,
        sell_enabled=False,
        unlock_timestamp_ms=unlock_ms,
        genesis_timestamp_ms=None,
        decay_start_fee_bps=None,
        decay_end_fee_bps=None,
        decay_duration_ms=None,
        simulated_buy_in=1_000_000,
        simulated_buy_out=990_000,
        simulated_sell_out=0,
    )
    flaunch_res = evaluate_launchpad_safety(flaunch_locked, policy)
    assert flaunch_res.status == DecisionStatus.DEFER_UNTIL_DECAY
    assert flaunch_res.earliest_safe_entry_ms == unlock_ms

    # Sell still disabled AFTER Flaunch unlock -> permanent honeypot rejection!
    flaunch_post_unlock_disabled = replace(
        flaunch_locked,
        block_timestamp_s=GENESIS_S + 1_805,
        block_timestamp_ms=unlock_ms + 5_000,
        observed_at_ms=unlock_ms + 5_200,
    )
    assert (
        evaluate_launchpad_safety(flaunch_post_unlock_disabled, policy).status
        == DecisionStatus.REJECTED
    )

    # Virtuals 98-min (5_880_000 ms) 99% -> 1% schedule: reaches 200 bps at +5_820_000 ms.
    virtuals_early = _make_evidence(
        archetype=LaunchpadArchetype.VIRTUALS_CURVE,
        block_timestamp_s=GENESIS_S + 60,
        block_timestamp_ms=GENESIS_MS + 60_000,
        observed_at_ms=GENESIS_MS + 60_200,
        buy_tax_bps=9_800,
        sell_tax_bps=100,
        decay_start_fee_bps=9_900,
        decay_end_fee_bps=100,
        decay_duration_ms=5_880_000,
        simulated_buy_in=1_000_000,
        simulated_buy_out=20_000,
        simulated_sell_out=19_500,
    )
    virt_res = evaluate_launchpad_safety(virtuals_early, policy)
    assert virt_res.status == DecisionStatus.DEFER_UNTIL_DECAY
    assert virt_res.earliest_safe_entry_ms == GENESIS_MS + 5_820_000

    virtuals_safe = replace(
        virtuals_early,
        block_timestamp_s=GENESIS_S + 5_820,
        block_timestamp_ms=GENESIS_MS + 5_820_000,
        observed_at_ms=GENESIS_MS + 5_820_200,
        buy_tax_bps=200,
        simulated_buy_out=980_000,
        simulated_sell_out=965_000,
    )
    assert evaluate_launchpad_safety(virtuals_safe, policy).status == DecisionStatus.APPROVED


def test_soft_rug_creator_cluster_honeypot_and_liquidity_rejections() -> None:
    policy = LaunchpadSafetyPolicy()

    # Creator same-block snipe > 5% (500 bps), even during decay, must REJECT (not defer).
    sniped = _make_evidence(
        block_timestamp_s=GENESIS_S + 5,
        block_timestamp_ms=GENESIS_MS + 5_000,
        observed_at_ms=GENESIS_MS + 5_200,
        buy_tax_bps=4_060,
        simulated_buy_out=594_000,
        simulated_sell_out=580_000,
        creator_same_block_buy_bps=750,
    )
    res_sniped = evaluate_launchpad_safety(sniped, policy)
    assert res_sniped.status == DecisionStatus.REJECTED
    assert "excessive_creator_snipe" in res_sniped.reasons

    # Bundled cluster > 15% (1500 bps)
    clustered = _make_evidence(bundled_cluster_bps=1_800)
    res_cluster = evaluate_launchpad_safety(clustered, policy)
    assert res_cluster.status == DecisionStatus.REJECTED
    assert "excessive_bundled_cluster" in res_cluster.reasons

    # Prior rug count != 0
    rug_creator = _make_evidence(creator_prior_rug_count=1)
    assert evaluate_launchpad_safety(rug_creator, policy).status == DecisionStatus.REJECTED

    # Exit liquidity below $25k
    low_liq = _make_evidence(exit_liquidity_usd_micros=24_999_999_999)
    assert evaluate_launchpad_safety(low_liq, policy).status == DecisionStatus.REJECTED

    # Hidden sell honeypot during decay (sell output crushed far beyond buy tax)
    honeypot_during_decay = _make_evidence(
        block_timestamp_s=GENESIS_S + 5,
        block_timestamp_ms=GENESIS_MS + 5_000,
        observed_at_ms=GENESIS_MS + 5_200,
        buy_tax_bps=4_060,
        simulated_buy_in=1_000_000,
        simulated_buy_out=594_000,
        simulated_sell_out=100_000,
    )
    res_hp = evaluate_launchpad_safety(honeypot_during_decay, policy)
    assert res_hp.status == DecisionStatus.REJECTED
    assert "excessive_round_trip_loss" in res_hp.reasons

    # Observed buy tax exceeds schedule -> unmodeled hidden buy tax -> REJECTED
    extra_tax = _make_evidence(
        block_timestamp_s=GENESIS_S + 5,
        block_timestamp_ms=GENESIS_MS + 5_000,
        observed_at_ms=GENESIS_MS + 5_200,
        buy_tax_bps=7_500,
    )
    assert evaluate_launchpad_safety(extra_tax, policy).status == DecisionStatus.REJECTED


@pytest.mark.parametrize(
    "missing_field",
    [
        "block_timestamp_ms",
        "buy_tax_bps",
        "sell_tax_bps",
        "sell_enabled",
        "simulated_buy_in",
        "simulated_buy_out",
        "simulated_sell_out",
        "exit_liquidity_usd_micros",
        "creator_same_block_buy_bps",
        "bundled_cluster_bps",
        "creator_prior_rug_count",
        "decay_duration_ms",
    ],
)
def test_missing_field_always_fails_closed(missing_field: str) -> None:
    evidence = _make_evidence(**{missing_field: None})
    decision = evaluate_launchpad_safety(evidence)
    assert decision.status == DecisionStatus.REJECTED
    assert decision.earliest_safe_entry_ms is None


def test_base_denim_timestamp_mismatch_dual_source_staleness_and_unknown() -> None:
    policy = LaunchpadSafetyPolicy()

    # Base Denim millisecond mismatch (block_timestamp_ms // 1000 != block_timestamp_s)
    mismatched = _make_evidence(
        block_timestamp_s=GENESIS_S + 10,
        block_timestamp_ms=GENESIS_MS + 11_000,
        observed_at_ms=GENESIS_MS + 11_200,
    )
    res_mismatch = evaluate_launchpad_safety(mismatched, policy)
    assert res_mismatch.status == DecisionStatus.REJECTED
    assert "base_denim_timestamp_ms_mismatch" in res_mismatch.reasons

    # Single source fails dual-source check
    single_source = _make_evidence(source_count=1)
    res_single = evaluate_launchpad_safety(single_source, policy)
    assert res_single.status == DecisionStatus.REJECTED
    assert "insufficient_sources" in res_single.reasons

    # Stale > 5_000 ms
    stale = _make_evidence(observed_at_ms=GENESIS_MS + 15_001)
    res_stale = evaluate_launchpad_safety(stale, policy)
    assert res_stale.status == DecisionStatus.REJECTED
    assert "stale_evidence" in res_stale.reasons

    # Future block timestamp
    future = _make_evidence(observed_at_ms=GENESIS_MS + 9_999)
    res_future = evaluate_launchpad_safety(future, policy)
    assert res_future.status == DecisionStatus.REJECTED
    assert "future_block_timestamp" in res_future.reasons

    # Unknown custom archetype fails closed by default
    unknown = _make_evidence(archetype=LaunchpadArchetype.UNKNOWN_CUSTOM)
    assert evaluate_launchpad_safety(unknown, policy).status == DecisionStatus.REJECTED
