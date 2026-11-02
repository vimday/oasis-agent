"""External price guard: refuses new capital allocation when an independent price diverges.

The guard applies only to swap/mint (new capital allocation); burn/exit never consults it.
It compares the pool-currency price ratio implied by the loop's own two-provider Chainlink
reference with the same ratio from one independent external source, refusing when the
deviation exceeds a threshold. Missing, non-positive or failed external data refuses too
(fail closed): the caller waits rather than deploying capital on unchecked prices.

This is a pre-decision check, not a settlement or execution guard, and the external price
is an independent sanity signal, not an attested settlement price.
"""

from abc import ABC, abstractmethod
from fractions import Fraction


class ExternalPriceSource(ABC):
    """USD price per whole token, independent of the loop's own RPC pair."""

    @abstractmethod
    def fetch_usd_price(self, chain_id: int, currency: str) -> Fraction | None:
        """USD per whole `currency` token on `chain_id`; None if unavailable or stale."""


def check_price_deviation(
    chain_id: int,
    currency0: str,
    currency1: str,
    reference0_usd: Fraction,
    reference1_usd: Fraction,
    external: ExternalPriceSource,
    max_deviation_bps: int,
) -> tuple[bool, str]:
    """Return (approved, reason); refuse unless the external ratio is within the threshold.

    `reference0_usd`/`reference1_usd` are the loop's own USD prices per whole token.
    """
    if type(max_deviation_bps) is not int or not 0 <= max_deviation_bps <= 10_000:
        raise ValueError("deviation threshold must be 0-10000 bps")
    if reference0_usd <= 0 or reference1_usd <= 0:
        raise ValueError("reference prices must be positive")
    try:
        external0 = external.fetch_usd_price(chain_id, currency0)
        external1 = external.fetch_usd_price(chain_id, currency1)
    except Exception:  # noqa: BLE001 - provider errors may contain credentials
        return False, "external price guard: source failed (details redacted)"
    if external0 is None or external1 is None:
        return False, "external price guard: price unavailable"
    if external0 <= 0 or external1 <= 0:
        return False, "external price guard: fetched price is not positive"
    reference_ratio = reference0_usd / reference1_usd
    external_ratio = external0 / external1
    deviation_bps = abs(reference_ratio / external_ratio - 1) * 10_000
    if deviation_bps > max_deviation_bps:
        shown = -(-deviation_bps.numerator // deviation_bps.denominator)  # ceiling
        return (
            False,
            f"external price guard: deviation {shown} bps exceeds {max_deviation_bps} bps",
        )
    return True, ""
