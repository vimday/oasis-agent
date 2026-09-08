import pytest
from eth_abi.abi import encode

from oasis_agent.uniswap_v4.quotes import (
    QuoteDirection,
    QuoteKind,
    QuotePoolKey,
    SinglePoolQuoteRequest,
    decode_single_pool_quote,
    encode_single_pool_quote,
)


def pool() -> QuotePoolKey:
    return QuotePoolKey(
        "0x0000000000000000000000000000000000000001",
        "0x0000000000000000000000000000000000000002",
        0x800000,
        60,
        "0x0000000000000000000000000000000000000080",
    )


@pytest.mark.parametrize(
    ("kind", "selector"),
    [
        (QuoteKind.EXACT_INPUT, "aa9d21cb"),
        (QuoteKind.EXACT_OUTPUT, "58733073"),
    ],
)
def test_single_quote_encoding_matches_canonical_interface_selector(
    kind: QuoteKind, selector: str
) -> None:
    calldata = encode_single_pool_quote(
        pool(), SinglePoolQuoteRequest(kind, QuoteDirection.ZERO_FOR_ONE, 123, "0x0102")
    )

    assert calldata[2:10] == selector
    assert len(bytes.fromhex(calldata[2:])) == 356


def test_single_quote_encoding_preserves_max_amount_and_false_direction() -> None:
    calldata = encode_single_pool_quote(
        pool(),
        SinglePoolQuoteRequest(QuoteKind.EXACT_INPUT, QuoteDirection.ONE_FOR_ZERO, 2**128 - 1),
    )

    assert "ff" * 16 in calldata
    # The bool ABI word is false.
    assert "00" * 32 in calldata


def test_quote_result_requires_two_canonical_abi_words() -> None:
    result = decode_single_pool_quote("0x" + encode(["uint256", "uint256"], [456, 78_901]).hex())

    assert result.quoted_amount == 456
    assert result.gas_estimate == 78_901
    with pytest.raises(ValueError, match="exactly two"):
        decode_single_pool_quote("0x00")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"currency0": "0x0000000000000000000000000000000000000002"},
        {"fee": 1_000_000},
        {"tick_spacing": 0},
    ],
)
def test_pool_key_rejects_noncanonical_or_protocol_invalid_values(
    kwargs: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "currency0": pool().currency0,
        "currency1": pool().currency1,
        "fee": pool().fee,
        "tick_spacing": pool().tick_spacing,
        "hooks": pool().hooks,
    }
    values.update(kwargs)

    with pytest.raises(ValueError):
        QuotePoolKey(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("amount", [0, -1, 2**128, True])
def test_quote_request_rejects_invalid_amount(amount: int) -> None:
    with pytest.raises(ValueError):
        SinglePoolQuoteRequest(QuoteKind.EXACT_INPUT, QuoteDirection.ZERO_FOR_ONE, amount)
