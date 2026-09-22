import json

import pytest

from oasis_agent.launch.provider_observation import claim, observation, response_object

TOKEN = "0x" + "12" * 20


def security(data):
    return observation(
        json.dumps({"code": 0, "data": data}).encode(),
        provider="gmgn",
        kind="security",
        chain_id=8453,
        token=TOKEN,
        retrieved_at=100,
    )


def test_missing_is_unknown_not_safe():
    item = security({"is_honeypot": "", "open_source": "yes", "buy_tax": "0"})
    assert item.claims["is_honeypot"] is None
    assert item.claims["buy_tax"] == "0"
    assert "is_honeypot" in item.missing_fields
    assert item.identity_binding == "request_only"
    assert item.source_event_time is None and item.block_identity is None
    assert item.execution_eligible is False


@pytest.mark.parametrize(
    "raw",
    [
        b'{"code":0,"code":0}',
        b'{"code":false}',
        b'{"code":0,"data":{"x":NaN}}',
        b"[]",
        b'{"code":403}',
    ],
)
def test_reject_ambiguous_provider_envelopes(raw):
    with pytest.raises(ValueError):
        response_object(raw)


def test_decimal_precision_and_percent_ratio_are_distinct():
    root = response_object(b'{"code":0,"data":{"x":0.123456789012345678901}}')
    assert root["data"]["x"] == "0.123456789012345678901"
    assert claim("20.6599", "percent") == "20.6599"
    assert claim("20.6599", "ratio") is None
    assert claim("0.2", "ratio") == "0.2"


@pytest.mark.parametrize("value", [True, -1, "-1", "NaN", "Infinity", "1e9999", "1\nrun", {}, []])
def test_numeric_claims_fail_closed(value):
    assert claim(value, "ratio") is None


@pytest.mark.parametrize("data", [{"chain": "sol"}, {"address": "0x" + "34" * 20}])
def test_identity_mismatch(data):
    with pytest.raises(ValueError):
        security(data)


def test_rank_row_must_come_from_retained_response():
    with pytest.raises(ValueError):
        observation(
            b'{"code":0,"data":{"rank":[]}}',
            provider="gmgn",
            kind="trending",
            chain_id=8453,
            token=TOKEN,
            retrieved_at=100,
            row={"address": TOKEN},
        )


def test_count_boolean_and_categories():
    assert claim("1.2", "count") is None
    assert claim("2", "count") == "2"
    assert claim("yes", "bool") is None
    assert claim(False, "bool") is False
    assert claim("unknown", "yes_no") is None
    assert claim("injected text", "yes_no") is None
    assert claim("6", "risk_level") is None


def test_bounded_response():
    with pytest.raises(ValueError):
        response_object(b" " * 5_000_001)


def test_gmgn_detail_cli_has_no_api_envelope():
    raw = b'{"is_honeypot":"no","top_10_holder_rate":0.25}'
    with pytest.raises(ValueError):
        response_object(raw)
    item = observation(
        raw,
        provider="gmgn",
        kind="security",
        chain_id=8453,
        token=TOKEN,
        retrieved_at=100,
        cli_unwrapped=True,
    )
    assert item.claims["top_10_holder_rate"] == "0.25"
    assert item.claims["is_honeypot"] == "no"
    with pytest.raises(ValueError):
        response_object(b'{"code":403}', cli_unwrapped=True)
    with pytest.raises(ValueError):
        observation(
            raw,
            provider="okx",
            kind="advanced",
            chain_id=8453,
            token=TOKEN,
            retrieved_at=100,
            cli_unwrapped=True,
        )
