import pytest

from oasis_agent.evidence.base_denim import (
    DENIM_ACTIVATION_TIMESTAMP,
    parse_base_time,
    parse_header_timestamp_ms,
    validate_header_timestamp_ms,
)

DENIM_HEADER = {
    "number": "0x371de8f",
    "hash": "0x" + "ab" * 32,
    "parentHash": "0x" + "cd" * 32,
    "timestamp": hex(DENIM_ACTIVATION_TIMESTAMP + 1),
    "timestampMs": hex((DENIM_ACTIVATION_TIMESTAMP + 1) * 1000 + 800),
}


def test_denim_header_requires_a_consistent_millisecond_timestamp() -> None:
    assert parse_header_timestamp_ms(DENIM_HEADER, require_denim=True) == (
        (DENIM_ACTIVATION_TIMESTAMP + 1) * 1000 + 800
    )
    assert validate_header_timestamp_ms(DENIM_HEADER, chain_id=8453) == (
        (DENIM_ACTIVATION_TIMESTAMP + 1) * 1000 + 800
    )


def test_missing_millisecond_timestamp_is_refused_after_activation() -> None:
    header = {key: value for key, value in DENIM_HEADER.items() if key != "timestampMs"}
    with pytest.raises(ValueError, match="missing"):
        parse_header_timestamp_ms(header, require_denim=True)
    with pytest.raises(ValueError, match="missing"):
        validate_header_timestamp_ms(header, chain_id=8453)


def test_pre_denim_header_may_omit_millisecond_timestamp() -> None:
    header = {
        "timestamp": hex(DENIM_ACTIVATION_TIMESTAMP - 1),
        "timestampMs": None,
    }
    assert parse_header_timestamp_ms(header, require_denim=False) is None


def test_second_or_millisecond_mismatch_is_refused() -> None:
    with pytest.raises(ValueError, match="mismatch"):
        parse_header_timestamp_ms(
            DENIM_HEADER | {"timestampMs": hex((DENIM_ACTIVATION_TIMESTAMP + 2) * 1000)},
            require_denim=True,
        )
    with pytest.raises(ValueError, match="millis"):
        parse_base_time(
            timestamp_ms=(DENIM_ACTIVATION_TIMESTAMP + 1) * 1000 + 800,
            millis_part=799,
            header_timestamp=DENIM_ACTIVATION_TIMESTAMP + 1,
        )


def test_overflow_and_noncanonical_values_are_refused() -> None:
    with pytest.raises(ValueError):
        parse_header_timestamp_ms(DENIM_HEADER | {"timestampMs": hex(1 << 64)}, require_denim=True)
    with pytest.raises(ValueError):
        parse_header_timestamp_ms(DENIM_HEADER | {"timestampMs": "1"}, require_denim=True)
    with pytest.raises(ValueError):
        parse_base_time(
            timestamp_ms=(DENIM_ACTIVATION_TIMESTAMP + 1) * 1000,
            millis_part=1000,
            header_timestamp=DENIM_ACTIVATION_TIMESTAMP + 1,
        )


def test_non_base_chain_is_not_checked() -> None:
    assert validate_header_timestamp_ms({"timestamp": "0x1"}, chain_id=4663) is None
