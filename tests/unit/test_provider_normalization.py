import json

import pytest

from oasis_agent.launch.provider_normalization import normalize

TOKEN = "0x" + "12" * 20
WALLET = "0x" + "34" * 20


def parse(data, provider="gmgn", kind="security"):
    return normalize(
        json.dumps(data if provider == "gmgn" else {"code": "0", "data": data}).encode(),
        provider=provider,
        kind=kind,
        chain_id=8453,
        token=TOKEN,
        retrieved_at=100,
        cli_unwrapped=provider == "gmgn",
    )


@pytest.mark.parametrize("value", [False, 0, "no"])
def test_explicit_negative_flag_is_not_unknown(value):
    result = parse({"is_honeypot": value, "honeypot": 0})
    assert result.normalized_flags["honeypot"] is False
    assert result.execution_eligible is False
    assert result.flag_fields["honeypot"] == ("is_honeypot", "honeypot")


def test_conflicts_and_invalid_aliases_never_pass():
    result = parse(
        {"is_honeypot": False, "honeypot": 1, "is_open_source": True, "open_source": "true"}
    )
    assert result.normalized_flags["honeypot"] is None
    assert result.normalized_flags["source_verified"] is None
    assert result.conflicting_flags == ("honeypot",)
    assert result.invalid_flags == ("source_verified",)
    assert result.normalized_flags["ownership_renounced"] is None


def test_holder_units_zero_and_unknown_remain_distinct():
    gmgn = parse(
        {"list": [{"address": WALLET, "balance": "0", "amount_percentage": "0.75"}]}, kind="holders"
    )
    okx = parse(
        [{"holderWalletAddress": WALLET, "holdPercent": "75"}], provider="okx", kind="holders"
    )
    assert gmgn.holder_rows[0].reported_amount == "0"
    assert gmgn.holder_rows[0].reported_share == "0.75"
    assert gmgn.holder_rows[0].share_unit == "ratio"
    assert okx.holder_rows[0].reported_share == "75"
    assert okx.holder_rows[0].share_unit == "percent"
    assert okx.missing_holder_values == 1
    assert gmgn.missing_holder_values == 0
    assert not gmgn.holder_census_complete
    assert okx.identity_binding == "request_only"


def test_duplicate_holder_is_rejected():
    with pytest.raises(ValueError, match="unique holder"):
        parse({"list": [{"address": WALLET}, {"address": WALLET}]}, kind="holders")


@pytest.mark.parametrize("value", ["101", "-1", True, "NaN"])
def test_invalid_holder_share_is_unknown(value):
    result = parse(
        [{"holderWalletAddress": WALLET, "holdPercent": value}], provider="okx", kind="holders"
    )
    assert result.holder_rows[0].reported_share is None
    assert result.missing_holder_values == 2


def test_null_token_identity_is_not_response_binding():
    result = parse({"address": None, "chain": "base"})
    assert result.identity_binding == "request_only"
