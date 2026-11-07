"""RPC allowance for the original Base acceptance ledgers."""

from typing import TypedDict
from urllib.parse import urlsplit

from ..rpc.budget import UsageLimit
from .base_run_limits import wait_seconds
from .cash_interval import MAX_CASH_BLOCKS

SOURCES = ("live_primary", "live_secondary")
DOMAINS = dict(zip(SOURCES, ("alchemy.com", "drpc.org"), strict=True))
CEILING_MICROS = 3_000_000
PREVIOUS_COST_UPPER_MICROS = 20_155
PER_REQUEST_UPPER_MICROS = 50
CAPTURE_PER_SOURCE = 80

# One cash reconciliation can read a header and two log filters per block,
# plus opening/closing headers, wallet, cost, position, trace and receipt reads.
_SETTLEMENT_PER_SOURCE = 3 * MAX_CASH_BLOCKS + 64
# NAV, runtime pins, fees/nonce, independent simulation and one broadcast.
_DECISION_PER_SOURCE = 96
# Strategy primary can poll the just-submitted transaction for 120 seconds at
# one-second intervals. Authority and secondary have no equivalent poll.
_SUBMISSION_POLL = 128

# Original volume identities and prefix digests read with the verified image.
# See the Base bounded runbook for the retained acceptance evidence.
LIMITS = {
    "strategy": UsageLimit(
        2259,
        "7d2e494070526e38ad56dfd2a6d103505cb810d6753d227684f70a69cefce32f",
        (65025, 215311),
        4600,
    ),
    "authority": UsageLimit(
        2011,
        "59137cb05866bbf08456c9770f3b613e94f36cbf06048aeac7d104ff1889a836",
        (65025, 211407),
        3600,
    ),
}
COST_UPPER_MICROS = PREVIOUS_COST_UPPER_MICROS + PER_REQUEST_UPPER_MICROS * sum(
    limit.additional_requests for limit in LIMITS.values()
)


class LedgerBudget(TypedDict):
    identity: list[int]
    counts: dict[str, int]
    remaining: int


def run_requests(
    ticks: int, opening_settlements: int, *, include_capture: bool
) -> dict[str, dict[str, int]]:
    """Reserve enough for every tick to settle one action on each service."""
    wait_seconds(ticks)  # Validate the same tick bound as the supervisor.
    if type(opening_settlements) is not int or opening_settlements < 0:
        raise ValueError("verified opening settlement count required")
    # Each source may reread a finalized head and both the block and receipt
    # of every anchor. Each preceding tick could have added one more anchor.
    ancestry = sum(1 + 2 * (opening_settlements + tick) for tick in range(ticks))
    per_source = ticks * (_SETTLEMENT_PER_SOURCE + _DECISION_PER_SOURCE) + ancestry
    capture = CAPTURE_PER_SOURCE if include_capture else 0
    return {
        "strategy": {
            "live_primary": per_source + ticks * _SUBMISSION_POLL + capture,
            "live_secondary": per_source + capture,
        },
        "authority": dict.fromkeys(SOURCES, per_source),
    }


def covers_requests(
    ledgers: dict[str, LedgerBudget], required: dict[str, dict[str, int]], max_requests: int
) -> bool:
    return all(
        ledgers[role]["remaining"] >= sum(requests.values())
        and all(
            max_requests - ledgers[role]["counts"][source] >= count
            for source, count in requests.items()
        )
        for role, requests in required.items()
    )


def require_endpoint(source: str, url: str) -> None:
    parsed = urlsplit(url)
    host, domain = parsed.hostname, DOMAINS[source]
    if (
        parsed.scheme != "https"
        or not host
        or not (host == domain or host.endswith("." + domain))
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.port not in (None, 443)
        or any(char.isspace() or ord(char) < 32 for char in url)
    ):
        raise ValueError("priced HTTPS RPC providers required")
