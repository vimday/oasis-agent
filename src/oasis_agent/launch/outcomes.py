"""Forward outcome labels for every assessed token, traded or not.

Judging a selector only by its own fills is survivorship- and selection-biased and has
too few samples. Every assessment therefore registers its reference price; at each
horizon the tracker re-reads one batch spot price and labels the forward return. A
missing or stale observation is labelled `unobserved`, never zero. Prices are provider
spot claims (mid), not executable fills, so labels measure signal quality, not PnL.
"""

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from ..models.common import normalize_address
from .opportunity import count, positive_price

HORIZONS = (900, 3_600, 14_400)
# A horizon observation older than this past its due time is too late to label.
MAX_LATENESS_SECONDS = 600
MAX_PENDING = 2_000
REGISTERED = "registered"
# Every outcome of one registration attempt; mutually exclusive.
REGISTRATION = (REGISTERED, "no_reference_price", "tracker_full", "overlaps_recent_sample")


@dataclass(frozen=True, slots=True)
class Pending:
    token: str
    symbol: str
    action: str
    score_bps: int
    at: int
    price: Fraction
    remaining: tuple[int, ...]


@dataclass
class OutcomeTracker:
    pending: dict[tuple[str, int], Pending] = field(default_factory=dict)

    def register(
        self, token: str, symbol: str, action: str, score_bps: int, at: int, price: Fraction | None
    ) -> bool:
        """Track one assessment; at most one new sample per token per 5 minutes."""
        return self.register_reason(token, symbol, action, score_bps, at, price) == REGISTERED

    def register_reason(
        self, token: str, symbol: str, action: str, score_bps: int, at: int, price: Fraction | None
    ) -> str:
        """`register`, saying why a sample was or was not tracked (one of REGISTRATION)."""
        if price is None or price <= 0:
            return "no_reference_price"
        if len(self.pending) >= MAX_PENDING:
            return "tracker_full"
        key = (token, at // 60)
        if any(p.token == token and at - p.at < 300 for p in self.pending.values()):
            return "overlaps_recent_sample"  # overlapping samples would overweight a token
        self.pending[key] = Pending(token, symbol, action, score_bps, at, price, HORIZONS)
        return REGISTERED

    def due(self, now: int) -> list[str]:
        return sorted({p.token for p in self.pending.values() if now >= p.at + p.remaining[0]})

    def label(self, now: int, prices: dict[str, tuple[Fraction, int]]) -> list[dict[str, Any]]:
        """Emit labels for every due horizon. `prices` maps token -> (price, observed_at)."""
        out: list[dict[str, Any]] = []
        for key, item in list(self.pending.items()):
            remaining = list(item.remaining)
            while remaining and now >= item.at + remaining[0]:
                horizon = remaining.pop(0)
                due_at = item.at + horizon
                observed = prices.get(item.token)
                record: dict[str, Any] = {
                    "token": item.token,
                    "symbol": item.symbol,
                    "action": item.action,
                    "score_bps": item.score_bps,
                    "assessed_at": item.at,
                    "horizon_seconds": horizon,
                }
                if observed is None or not due_at <= observed[1] <= due_at + MAX_LATENESS_SECONDS:
                    out.append(record | {"status": "unobserved"})
                    continue
                change = (observed[0] - item.price) * 10_000 / item.price
                out.append(
                    record
                    | {
                        "status": "observed",
                        "observed_at": observed[1],
                        # Truncated toward zero; a -100% move is the floor of a spot claim.
                        "forward_return_bps": int(change),
                    }
                )
            if remaining:
                self.pending[key] = Pending(
                    item.token,
                    item.symbol,
                    item.action,
                    item.score_bps,
                    item.at,
                    item.price,
                    tuple(remaining),
                )
            else:
                del self.pending[key]
        return out


def parse_prices(data: Any, chain: int) -> dict[str, tuple[Fraction, int]]:
    """Batch price rows -> token -> (price, observed_at seconds); malformed rows are dropped."""
    out: dict[str, tuple[Fraction, int]] = {}
    if not isinstance(data, list):
        return out
    for row in data:
        if not isinstance(row, dict) or row.get("chainIndex") != str(chain):
            continue
        try:
            token = normalize_address(str(row.get("tokenContractAddress", "")))
        except ValueError:
            continue
        price, at = positive_price(row.get("price")), count(row.get("time"))
        if price is not None and at is not None and token not in out:
            out[token] = (price, at // 1000)
    return out
