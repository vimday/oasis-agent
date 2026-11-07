"""Fixed NAV refusal shared by price acquisition and chain-evidence replay."""


class NavRefusal(ValueError):
    """Non-sensitive valuation refusal; safe to report to the operator."""
