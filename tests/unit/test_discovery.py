"""Multi-token discovery must not turn attention, duplicate feeds or missing data into orders."""

import json
import urllib.error

import pytest

from oasis_agent.launch.discovery import (
    DiscoveryPolicy,
    DiscoveryRequest,
    DiscoverySignal,
    discover,
)
from oasis_agent.launch.discovery_import import import_profiles, import_ranking_discovery
from scripts import discover_tokens


def signal(provider="gmgn", index=1, **fields):
    return DiscoverySignal.model_validate(
        {
            "provider": provider,
            "chain_id": 8453,
            "token": "0x" + f"{index:040x}",
            "kind": "trending",
            "observed_at": 990,
            "retrieved_at": 995,
            "rank": 1,
            "window_seconds": 300,
            "raw_sha256": "a" * 64,
            **fields,
        }
    )


def run(signals, **policy):
    return discover(
        DiscoveryRequest(
            evaluated_at=1000,
            policy=DiscoveryPolicy(chains=(8453,), **policy),
            signals=tuple(signals),
        )
    )


def test_many_tokens_no_hardcoded_target_and_duplicates_cannot_amplify():
    signals = [signal(p, i) for i in range(1, 51) for p in ("gmgn", "okx")]
    baseline = run(signals)
    assert len(baseline.items) == 50
    assert all(not item.execution_eligible for item in baseline.items)
    assert baseline.items == run(signals + signals).items
    assert baseline.items == run(signals[::-1]).items
    assert baseline.items[0].attention_bps == 10000
    assert all("pair_age_unknown" in item.blockers for item in baseline.items)


def test_cross_chain_tokens_never_merge_and_stale_future_are_excluded():
    result = run(
        [
            signal(),
            signal("okx", chain_id=1),
            signal(index=2, observed_at=800),
            signal(index=3, retrieved_at=1001),
        ]
    )
    assert len(result.items) == 1
    assert result.omitted_chain == 1
    assert result.omitted_stale_or_future == 2
    assert result.items[0].attention_bps == 5000


def test_fomo_social_does_not_fake_independent_market_confirmation():
    result = run([signal(), signal("fomo", kind="social")])
    assert result.items[0].attention_bps == 5000
    assert "gmgn_okx_rank_confluence_missing" in result.items[0].blockers


def test_conflicting_rank_never_picks_best_value():
    result = run([signal(), signal(rank=90), signal("okx")])
    assert result.items[0].next_action == "review_identity"
    assert "conflicting_source_ranks" in result.items[0].blockers


def test_incompatible_ranking_windows_do_not_form_confluence():
    result = run([signal(), signal("okx", window_seconds=3600)])
    assert result.items[0].attention_bps == 5000
    assert "gmgn_okx_rank_confluence_missing" in result.items[0].blockers


@pytest.mark.parametrize("created,age", [(990, "immature"), (800, "new"), (None, "unknown")])
def test_pair_age_is_explicit(created, age):
    assert run([signal(created_at=created)]).items[0].age_class == age


def test_conflicting_birth_and_capacity_are_reported():
    result = run(
        [signal(created_at=800), signal("okx", created_at=900), signal(index=2)], max_candidates=1
    )
    assert result.deferred_capacity == 1
    assert result.items[0].age_class == "conflicting"
    assert result.items[0].next_action == "review_identity"


def test_public_profile_is_not_birth_or_popularity():
    raw = json.dumps(
        [
            {"chainId": "base", "tokenAddress": "0x" + "AB" * 20},
            {"chainId": "solana", "tokenAddress": "unsupported"},
        ]
    ).encode()
    signals = import_profiles(raw, retrieved_at=1000)
    assert len(signals) == 1 and signals[0].token == "0x" + "ab" * 20
    result = run(signals)
    assert result.items[0].attention_bps == 0 and result.items[0].age_class == "unknown"


def test_profile_malformed_or_duplicate_keys_rejected():
    for raw in (
        b"{}",
        b"[null]",
        b'[{"chainId":"base","chainId":"base"}]',
        b'[{"chainId":"base","tokenAddress":"invalid"}]',
    ):
        with pytest.raises(ValueError):
            import_profiles(raw, retrieved_at=1000)


def test_official_rank_import_preserves_provenance_and_window():
    raw = json.dumps(
        {"code": "0", "data": [{"chainIndex": "8453", "tokenContractAddress": "0x" + "12" * 20}]}
    ).encode()
    rows = import_ranking_discovery(
        raw, provider="okx", chain_id=8453, window_seconds=300, observed_at=990, retrieved_at=995
    )
    assert rows[0].rank == 1 and rows[0].window_seconds == 300
    assert run(rows).items[0].sources == ("okx",)


def test_live_http_failure_is_retained_not_empty_success(tmp_path, monkeypatch):
    class Offline:
        def open(self, *args, **kwargs):
            raise urllib.error.HTTPError("https://example.invalid", 403, "private detail", {}, None)

    monkeypatch.setattr(discover_tokens.urllib.request, "build_opener", lambda *args: Offline())
    output = tmp_path / "capture"
    with pytest.raises(ValueError, match="acquisition"):
        discover_tokens.capture_profiles(output, DiscoveryPolicy(chains=(8453,)))
    failure = json.loads((output / "failure.json").read_bytes())
    assert failure["http_status"] == 403
    assert not (output / "result.json").exists()
    assert "private detail" not in (output / "failure.json").read_text()


def test_same_time_window_conflict_is_order_independent():
    signals = [signal(), signal(window_seconds=3600), signal("okx")]
    assert run(signals).items == run(signals[::-1]).items
    assert run(signals).items[0].next_action == "review_identity"
