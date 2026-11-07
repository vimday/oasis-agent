"""Pluggable discovery providers for the spot agent (e.g. FOMO once access exists).

A provider can only *nominate* token addresses. A nominated token enters the candidate
pool with every market field unknown, so it is admitted only if OKX enrichment fills the
fields and every gate passes; a provider can never admit, size or exit a trade.
"""

from typing import Protocol

from ..models.common import normalize_address
from .opportunity import Market

MAX_NOMINATIONS = 20


class DiscoveryProvider(Protocol):
    name: str
    calls: int

    def nominate(self, chain_id: int) -> list[str]:
        """Token addresses on `chain_id`; raises SourceFailure when unavailable."""
        ...


def nominated_markets(addresses: list[str], observed_at: int) -> list[Market]:
    """Unknown-everything markets for valid, distinct addresses, bounded in count."""
    seen: dict[str, Market] = {}
    for raw in addresses:
        try:
            token = normalize_address(raw)
        except ValueError:
            continue
        seen.setdefault(
            token,
            Market(
                token=token,
                symbol="",
                observed_at=observed_at,
                liquidity_usd_micros=None,
                market_cap_usd_micros=None,
                holders=None,
                top10_bps=None,
                unique_traders=None,
                buys=None,
                sells=None,
                first_trade_at=None,
            ),
        )
        if len(seen) >= MAX_NOMINATIONS:
            break
    return list(seen.values())
