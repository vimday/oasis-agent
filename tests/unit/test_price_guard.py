"""Price guard: blocks new positions when external price diverges."""

from fractions import Fraction

import pytest

from oasis_agent.live.price_guard import ExternalPriceSource, check_price_deviation


class FakeExternalSource(ExternalPriceSource):
    def __init__(self, prices: dict[tuple[int, str], Fraction | None]) -> None:
        self._prices = prices

    def fetch_usd_price(self, chain_id: int, currency: str) -> Fraction | None:
        return self._prices.get((chain_id, currency.lower()))


ETH = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"


def test_external_price_unavailable_fails_closed() -> None:
    source = FakeExternalSource({(8453, USDC): Fraction(1)})
    approved, reason = check_price_deviation(
        8453, ETH, USDC, Fraction(2500), Fraction(1), source, 100
    )
    assert not approved
    assert "unavailable" in reason


class SecretLeakingSource(ExternalPriceSource):
    def fetch_usd_price(self, chain_id: int, currency: str) -> Fraction | None:
        raise RuntimeError("OK-ACCESS-KEY=supersecret")


def test_source_failure_refuses_without_echoing_provider_error() -> None:
    approved, reason = check_price_deviation(
        8453, ETH, USDC, Fraction(2500), Fraction(1), SecretLeakingSource(), 100
    )
    assert not approved
    assert "supersecret" not in reason and "redacted" in reason


def test_external_price_within_threshold_approves() -> None:
    # Reference: 2500 USD/ETH, 1 USD/USDC → ratio 2500.
    # External: 2480 USD/ETH, 1 USD/USDC → ratio 2480.
    # Deviation: abs(2500/2480 - 1) = 0.0080645 = 80.6 bps < 100 bps.
    source = FakeExternalSource({(8453, ETH): Fraction(2480), (8453, USDC): Fraction(1)})
    approved, reason = check_price_deviation(
        8453, ETH, USDC, Fraction(2500), Fraction(1), source, 100
    )
    assert approved
    assert reason == ""


def test_external_price_exceeds_threshold_refuses() -> None:
    # Reference: 2500 USD/ETH, 1 USD/USDC → ratio 2500.
    # External: 2400 USD/ETH, 1 USD/USDC → ratio 2400.
    # Deviation: abs(2500/2400 - 1) = 416.67 bps > 100 bps, reported as the ceiling.
    source = FakeExternalSource({(8453, ETH): Fraction(2400), (8453, USDC): Fraction(1)})
    approved, reason = check_price_deviation(
        8453, ETH, USDC, Fraction(2500), Fraction(1), source, 100
    )
    assert not approved
    assert "deviation 417 bps exceeds 100 bps" in reason


def test_deviation_exactly_at_threshold_approves() -> None:
    # 2500 / 2475.2475... - 1 is exactly 100 bps.
    source = FakeExternalSource(
        {(8453, ETH): Fraction(2500 * 10_000, 10_100), (8453, USDC): Fraction(1)}
    )
    approved, _ = check_price_deviation(8453, ETH, USDC, Fraction(2500), Fraction(1), source, 100)
    assert approved


def test_external_price_zero_or_negative_refuses() -> None:
    # External returns zero.
    source = FakeExternalSource({(8453, ETH): Fraction(0), (8453, USDC): Fraction(1)})
    approved, reason = check_price_deviation(
        8453, ETH, USDC, Fraction(2500), Fraction(1), source, 100
    )
    assert not approved
    assert "not positive" in reason

    # External returns negative (should never happen, but guard against it).
    source = FakeExternalSource({(8453, ETH): Fraction(-1), (8453, USDC): Fraction(1)})
    approved, reason = check_price_deviation(
        8453, ETH, USDC, Fraction(2500), Fraction(1), source, 100
    )
    assert not approved
    assert "not positive" in reason


def test_reference_price_must_be_positive() -> None:
    source = FakeExternalSource({(8453, ETH): Fraction(2500), (8453, USDC): Fraction(1)})
    with pytest.raises(ValueError, match="reference prices must be positive"):
        check_price_deviation(8453, ETH, USDC, Fraction(0), Fraction(1), source, 100)
    with pytest.raises(ValueError, match="reference prices must be positive"):
        check_price_deviation(8453, ETH, USDC, Fraction(2500), Fraction(-1), source, 100)


def test_deviation_threshold_must_be_bounded() -> None:
    source = FakeExternalSource({(8453, ETH): Fraction(2500), (8453, USDC): Fraction(1)})
    with pytest.raises(ValueError, match="deviation threshold must be 0-10000 bps"):
        check_price_deviation(8453, ETH, USDC, Fraction(2500), Fraction(1), source, -1)
    with pytest.raises(ValueError, match="deviation threshold must be 0-10000 bps"):
        check_price_deviation(8453, ETH, USDC, Fraction(2500), Fraction(1), source, 10_001)


def test_deviation_is_symmetric() -> None:
    # External 10% above reference: 2750 vs 2500.
    high = FakeExternalSource({(8453, ETH): Fraction(2750), (8453, USDC): Fraction(1)})
    approved_high, _ = check_price_deviation(
        8453, ETH, USDC, Fraction(2500), Fraction(1), high, 500
    )
    # External 10% below reference: 2250 vs 2500.
    low = FakeExternalSource({(8453, ETH): Fraction(2250), (8453, USDC): Fraction(1)})
    approved_low, _ = check_price_deviation(8453, ETH, USDC, Fraction(2500), Fraction(1), low, 500)
    # Both should refuse if threshold is 500 bps (5%).
    assert not approved_high
    assert not approved_low


def test_zero_threshold_refuses_any_nonzero_deviation() -> None:
    # External differs by even 1 wei → refuse.
    source = FakeExternalSource(
        {(8453, ETH): Fraction(2500, 1), (8453, USDC): Fraction(1000001, 1000000)}
    )
    approved, reason = check_price_deviation(
        8453, ETH, USDC, Fraction(2500), Fraction(1), source, 0
    )
    assert not approved
    assert "exceeds 0 bps" in reason
