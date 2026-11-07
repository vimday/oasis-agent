"""Read-only GMGN CLI adapter used as an independent second opinion on OKX claims.

Only `token info` and `market trending` can be built; swap, multi-swap, order, cooking
and config commands are refused before a process starts. GMGN responses are provider
claims. Cross-checking two providers detects one stale or wrong source; it does not
make either an on-chain attestation.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from glob import glob
from pathlib import Path
from typing import Any

from ..models.common import normalize_address, sha256_hex
from .onchainos_source import Capture, Runner, SourceFailure
from .opportunity import Market, count, micros

GMGN_CHAINS = {8453: "base"}
READ_ONLY = frozenset({("token", "info"), ("market", "trending")})
# Two providers' liquidity may differ by pool coverage; beyond this the token is unpriceable.
MAX_LIQUIDITY_RATIO = 3
MAX_HOLDER_RATIO = 2


def default_binary() -> Path:
    found = sorted(glob(str(Path.home() / ".nvm/versions/node/*/bin/gmgn-cli")))
    return Path(found[-1]) if found else Path("gmgn-cli")


def build_argv(group: str, command: str, chain: int, **options: str) -> tuple[str, ...]:
    if (group, command) not in READ_ONLY:
        raise SourceFailure("command_not_read_only")
    if chain not in GMGN_CHAINS:
        raise SourceFailure("chain_not_supported")
    argv = [group, command, "--chain", GMGN_CHAINS[chain]]
    for key, value in sorted(options.items()):
        if not key.isalpha() or not value or len(value) > 64 or value.startswith("-"):
            raise SourceFailure("invalid_option")
        if any(c in value for c in " \t\r\n;|&$`'\"\\"):
            raise SourceFailure("invalid_option")
        argv += ["--" + key, value]
    return (*argv, "--raw")


class GmgnSource:
    def __init__(self, runner: Runner, clock: Callable[[], float], retain: Path | None = None):
        self._run, self._clock, self._retain = runner, clock, retain
        self.calls = 0

    def _query(self, group: str, command: str, chain: int, **options: str) -> Capture:
        argv = build_argv(group, command, chain, **options)
        self.calls += 1
        raw = self._run(argv)
        digest = sha256_hex(raw)
        if self._retain is not None and not (path := self._retain / (digest + ".json")).exists():
            path.write_bytes(raw)
        try:
            root = json.loads(raw, parse_float=str, parse_int=str)
        except (ValueError, RecursionError) as error:
            raise SourceFailure("invalid_json") from error
        if not isinstance(root, dict):
            raise SourceFailure("provider_error")
        if "code" in root:  # list endpoints wrap {code, data}; detail endpoints are bare
            if root.get("code") != "0" or "data" not in root:
                raise SourceFailure("provider_error")
            root = root["data"]
        return Capture(argv, int(self._clock()), digest, root)

    def token_info(self, chain: int, token: str) -> Capture:
        return self._query("token", "info", chain, address=normalize_address(token))


@dataclass(frozen=True, slots=True)
class GmgnView:
    created_at: int | None
    liquidity_usd_micros: int | None
    holders: int | None


def parse_token_info(data: Any, token: str) -> GmgnView | None:
    if not isinstance(data, dict):
        return None
    try:
        if normalize_address(str(data.get("address", ""))) != token:
            return None
    except ValueError:
        return None
    created = count(data.get("creation_timestamp"))
    return GmgnView(
        created_at=created or None,  # GMGN reports 0 for unknown
        liquidity_usd_micros=micros(data.get("liquidity")),
        holders=count(data.get("holder_count")),
    )


def _disagree(a: int | None, b: int | None, ratio: int) -> bool:
    return a is not None and b is not None and (a > b * ratio or b > a * ratio)


def cross_check(market: Market, view: GmgnView | None) -> Market:
    """Fill a missing age from GMGN and flag material provider disagreement."""
    if view is None:
        return market
    issues = list(market.provider_disagreement)
    if _disagree(market.liquidity_usd_micros, view.liquidity_usd_micros, MAX_LIQUIDITY_RATIO):
        issues.append("liquidity")
    if _disagree(market.holders, view.holders, MAX_HOLDER_RATIO):
        issues.append("holders")
    first = market.first_trade_at
    if first is None:
        first = view.created_at
    elif view.created_at is not None:
        # The age gate guards against too-new tokens, so the younger claim (later
        # timestamp) is the conservative one: one provider cannot make a token look older.
        first = max(first, view.created_at)
    return replace(
        market,
        first_trade_at=first,
        gmgn_listed=True,
        provider_disagreement=tuple(sorted(set(issues))),
    )
