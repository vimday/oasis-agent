import pytest

from oasis_agent.launch.batch_scoring import BatchRequest, score_batch
from oasis_agent.launch.discovery import DiscoveryPolicy, DiscoveryRequest, DiscoverySignal
from oasis_agent.launch.scoring import ScorePolicy

from .test_token_scoring import candidate, changed


def request(candidates=None):
    c = candidate()
    signals = tuple(
        DiscoverySignal(
            **trend.model_dump(),
            kind="trending",
            created_at=800,
        )
        for trend in c.trends
    )
    return BatchRequest(
        discovery=DiscoveryRequest(
            evaluated_at=1000,
            policy=DiscoveryPolicy(chains=(8453,)),
            signals=signals,
        ),
        policy=ScorePolicy(),
        candidates=(c,) if candidates is None else candidates,
    )


def test_dynamic_discovery_binds_shared_score_and_never_executes():
    result = score_batch(request())
    assert result.items[0].score.action == "simulate_entry"
    assert not result.items[0].execution_eligible
    assert result.held_position_scores == ()


def test_missing_enrichment_never_becomes_zero_score_or_pass():
    result = score_batch(request(()))
    assert result.items[0].score is None
    assert result.items[0].status == "needs_enrichment"


def test_duplicate_or_swapped_enrichment_rejected():
    c = candidate()
    with pytest.raises(ValueError, match="duplicate"):
        score_batch(request((c, c)))
    with pytest.raises(ValueError, match="outside"):
        score_batch(request((changed(c, token="0x" + "34" * 20),)))
    data = c.model_dump(mode="json")
    data["trends"][0]["raw_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="ranking"):
        score_batch(request((type(c).model_validate(data),)))


def test_held_position_exits_remain_monitored_after_leaving_discovery():
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
    r = request((c,))
    data = r.model_dump(mode="json")
    data["discovery"]["signals"] = []
    result = score_batch(BatchRequest.model_validate(data))
    assert result.items == ()
    assert result.held_position_scores[0].exit_reason == "stop_loss"
    assert result.held_position_scores[0].action == "simulate_exit"
