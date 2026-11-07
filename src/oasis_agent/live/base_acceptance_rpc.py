"""RPC allowance for the original Base acceptance ledgers."""

from urllib.parse import urlsplit

from ..rpc.budget import UsageLimit

SOURCES = ("live_primary", "live_secondary")
DOMAINS = dict(zip(SOURCES, ("alchemy.com", "drpc.org"), strict=True))
CEILING_MICROS = 100_000
PREVIOUS_COST_UPPER_MICROS = 20_155
PER_REQUEST_UPPER_MICROS = 50

# Original volume identities and prefix digests read with the verified image.
# See the Base bounded runbook for the retained acceptance evidence.
LIMITS = {
    "strategy": UsageLimit(
        2259,
        "7d2e494070526e38ad56dfd2a6d103505cb810d6753d227684f70a69cefce32f",
        (65025, 215311),
        750,
    ),
    "authority": UsageLimit(
        2011,
        "59137cb05866bbf08456c9770f3b613e94f36cbf06048aeac7d104ff1889a836",
        (65025, 211407),
        750,
    ),
}
COST_UPPER_MICROS = PREVIOUS_COST_UPPER_MICROS + PER_REQUEST_UPPER_MICROS * sum(
    limit.additional_requests for limit in LIMITS.values()
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
