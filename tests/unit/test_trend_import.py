import json

import pytest

from oasis_agent.launch.trend_import import import_trends


def wire(provider):
    row = (
        {"chain": "base", "address": "0x" + "ab" * 20, "rank": 1}
        if provider == "gmgn"
        else {"chainIndex": "8453", "tokenContractAddress": "0x" + "ab" * 20}
    )
    return {"code": "0", "data": {"rank": [row]} if provider == "gmgn" else [row]}


@pytest.mark.parametrize("provider", ["gmgn", "okx"])
def test_import_binds_identity_and_raw_digest_without_promoting_safety(provider):
    raw = json.dumps(wire(provider)).encode()
    result = import_trends(
        raw, provider=provider, chain_id=8453, window_seconds=300, observed_at=100, retrieved_at=101
    )
    assert len(result) == 1 and result[0].rank == 1 and result[0].chain_id == 8453
    assert len(result[0].raw_sha256) == 64
    wrong = wire(provider)
    wrong["code"] = "429"
    with pytest.raises(ValueError):
        import_trends(
            json.dumps(wrong).encode(),
            provider=provider,
            chain_id=8453,
            window_seconds=300,
            observed_at=100,
            retrieved_at=101,
        )
    with pytest.raises(ValueError, match="chain mismatch"):
        import_trends(
            raw,
            provider=provider,
            chain_id=56,
            window_seconds=300,
            observed_at=100,
            retrieved_at=101,
        )


def test_duplicate_and_boolean_rank_fail_closed():
    root = wire("gmgn")
    root["data"]["rank"] *= 2
    with pytest.raises(ValueError):
        import_trends(
            json.dumps(root).encode(),
            provider="gmgn",
            chain_id=8453,
            window_seconds=300,
            observed_at=100,
            retrieved_at=101,
        )
    root = wire("gmgn")
    root["data"]["rank"][0]["rank"] = True
    with pytest.raises(ValueError):
        import_trends(
            json.dumps(root).encode(),
            provider="gmgn",
            chain_id=8453,
            window_seconds=300,
            observed_at=100,
            retrieved_at=101,
        )
