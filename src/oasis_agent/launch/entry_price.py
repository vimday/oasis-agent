"""Strict FDV ceiling using exact rational native-ETH valuation."""

UINT256_MAX = 2**256 - 1


def uint(value: int, *, positive: bool = False) -> int:
    if type(value) is not int or not (int(positive) <= value <= UINT256_MAX):
        raise ValueError("bounded integer base units required")
    return value


def minimum_received(
    *,
    native_wei: int,
    eth_usd_micros: int,
    token_decimals: int,
    quoted_base_units: int,
    slippage_bps: int,
    max_fdv_usd_micros: int = 200_000_000_000_000,
    reference_supply: int = 1_000_000_000,
) -> int:
    """Return minimum actual receipt, not router gross output.

    Strict FDV < F requires floor(numerator/denominator)+1, including exact
    divisibility. Keep ETH valuation rational until the final token rounding.
    This helper does not construct calldata or attest to router semantics.
    """
    for amount in (
        native_wei,
        eth_usd_micros,
        quoted_base_units,
        max_fdv_usd_micros,
        reference_supply,
    ):
        uint(amount, positive=True)
    uint(token_decimals)
    uint(slippage_bps)
    if token_decimals > 77 or slippage_bps >= 10_000:
        raise ValueError("invalid decimals or slippage")
    numerator = native_wei * eth_usd_micros * reference_supply * 10**token_decimals
    strict_floor = numerator // (10**18 * max_fdv_usd_micros) + 1
    slip_floor = quoted_base_units * (10_000 - slippage_bps) // 10_000
    return uint(max(strict_floor, slip_floor), positive=True)


def below_fdv(
    *,
    spend_usd_micros: int,
    received_base_units: int,
    token_decimals: int,
    max_fdv_usd_micros: int = 200_000_000_000_000,
    reference_supply: int = 1_000_000_000,
) -> bool:
    for value in (spend_usd_micros, received_base_units, max_fdv_usd_micros, reference_supply):
        uint(value, positive=True)
    uint(token_decimals)
    if token_decimals > 77:
        raise ValueError("invalid token decimals")
    return bool(
        spend_usd_micros * reference_supply * 10**token_decimals
        < max_fdv_usd_micros * received_base_units
    )
