"""Bridge verified EQN catalyst intelligence into SpotAgent's DiscoveryProvider slot.

This module NEVER converts arbitrary ticker symbols into contract addresses and NEVER
grants trading or execution authority. Only symbols pre-pinned in
``VerifiedSymbolRegistry`` (with checksummed address and ``code_sha256``) whose active
catalyst passes freshness and is not ``risk_freeze`` may be nominated for downstream
enrichment and risk gating.
"""

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from eth_utils import to_checksum_address  # type: ignore[attr-defined]

from ..launch.discovery_slot import MAX_NOMINATIONS, DiscoveryProvider
from ..launch.onchainos_source import SourceFailure
from ..models.common import normalize_address
from .catalyst_scorer import CatalystSignal, classify_catalyst
from .eqn_catalog import EqnNewsItem
from .eqn_client import EqnClient, EqnFetchError

DEFAULT_MAX_CATALYST_AGE_MS: int = 300_000


def _validate_sha256(digest: str) -> str:
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(ch not in "0123456789abcdef" for ch in digest)
    ):
        raise ValueError("code_sha256 must be a lowercase 64-char SHA-256 hex digest")
    return digest


@dataclass(frozen=True, slots=True)
class VerifiedSymbolEntry:
    """Pinned `(chain_id, symbol)` mapping to a checksummed token and code hash."""

    chain_id: int
    symbol: str
    token_address: str
    code_sha256: str

    def __post_init__(self) -> None:
        if type(self.chain_id) is not int or self.chain_id <= 0:
            raise ValueError("chain_id must be a positive integer")
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        normalized_sym = self.symbol.strip().upper()
        normalized_addr = normalize_address(self.token_address)
        if int(normalized_addr, 16) == 0:
            raise ValueError("verified token_address must be non-zero")
        checksummed = str(to_checksum_address(normalized_addr))
        object.__setattr__(self, "symbol", normalized_sym)
        object.__setattr__(self, "token_address", checksummed)
        object.__setattr__(self, "code_sha256", _validate_sha256(self.code_sha256))


class VerifiedSymbolRegistry:
    """Immutable lookup table mapping `(chain_id, symbol)` to verified contracts."""

    def __init__(self, entries: Iterable[VerifiedSymbolEntry] = ()) -> None:
        table: dict[tuple[int, str], VerifiedSymbolEntry] = {}
        for entry in entries:
            if not isinstance(entry, VerifiedSymbolEntry):
                raise TypeError("entries must be VerifiedSymbolEntry instances")
            key = (entry.chain_id, entry.symbol)
            if key in table:
                raise ValueError(f"duplicate verified symbol mapping for {key}")
            table[key] = entry
        self._entries = table

    def lookup(self, chain_id: int, symbol: str) -> VerifiedSymbolEntry | None:
        if type(chain_id) is not int or chain_id <= 0 or not isinstance(symbol, str):
            return None
        normalized = symbol.strip().upper()
        if not normalized:
            return None
        return self._entries.get((chain_id, normalized))

    def contains(self, chain_id: int, symbol: str) -> bool:
        return self.lookup(chain_id, symbol) is not None

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterable[VerifiedSymbolEntry]:
        return iter(self._entries.values())


class EqnDiscoveryProvider:
    """`DiscoveryProvider` implementation backed by EQN catalysts and pinned symbols."""

    name: str = "eqn_news"

    def __init__(
        self,
        registry: VerifiedSymbolRegistry,
        *,
        client: EqnClient | None = None,
        items: Sequence[EqnNewsItem] = (),
        channel: str = "all",
        limit: int = 50,
        max_age_ms: int = DEFAULT_MAX_CATALYST_AGE_MS,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if not isinstance(registry, VerifiedSymbolRegistry):
            raise TypeError("registry must be a VerifiedSymbolRegistry")
        if client is not None and not isinstance(client, EqnClient):
            raise TypeError("client must be an EqnClient or None")
        if type(max_age_ms) is not int or max_age_ms <= 0:
            raise ValueError("max_age_ms must be a positive integer")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer in [1, 100]")

        self.registry = registry
        self.client = client
        self._items = tuple(items)
        self.channel = channel
        self.limit = limit
        self.max_age_ms = max_age_ms
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.calls: int = 0
        self.unmapped_symbols: tuple[str, ...] = ()
        self.frozen_symbols: tuple[str, ...] = ()
        self.stale_item_ids: tuple[str, ...] = ()
        self.last_signals: tuple[CatalystSignal, ...] = ()

    def nominate(self, chain_id: int) -> list[str]:
        """Return verified checksummed token addresses on ``chain_id``."""
        self.calls += 1
        if type(chain_id) is not int or chain_id <= 0:
            raise SourceFailure("invalid_chain_id")

        now_ms = self._clock_ms()
        if type(now_ms) is not int or now_ms <= 0:
            raise SourceFailure("invalid_clock")

        if self.client is not None:
            try:
                batch = self.client.fetch_news(
                    channel=self.channel,
                    limit=self.limit,
                    received_ts_ms=now_ms,
                )
            except EqnFetchError as exc:
                raise SourceFailure("eqn_fetch_failed") from exc
            items: tuple[EqnNewsItem, ...] = batch.items
        else:
            items = self._items

        signals: list[CatalystSignal] = []
        fresh_signals: list[CatalystSignal] = []
        stale_ids: list[str] = []
        frozen: set[str] = set()
        unmapped: set[str] = set()

        for item in items:
            signal = classify_catalyst(item)
            signals.append(signal)
            age_ms = now_ms - item.published_ts_ms
            future_skew_ms = item.published_ts_ms - now_ms
            if age_ms > self.max_age_ms or future_skew_ms > item.max_future_skew_ms:
                stale_ids.append(item.item_id)
                continue
            fresh_signals.append(signal)
            if signal.risk_freeze:
                frozen.update(sym.strip().upper() for sym in signal.symbols if sym.strip())

        nominated: dict[str, None] = {}
        for signal in fresh_signals:
            if signal.risk_freeze:
                continue
            for raw_sym in signal.symbols:
                sym = raw_sym.strip().upper()
                if not sym or sym in frozen:
                    continue
                entry = self.registry.lookup(chain_id, sym)
                if entry is None:
                    unmapped.add(sym)
                    continue
                nominated.setdefault(entry.token_address, None)
                if len(nominated) >= MAX_NOMINATIONS:
                    break
            if len(nominated) >= MAX_NOMINATIONS:
                break

        self.last_signals = tuple(signals)
        self.stale_item_ids = tuple(stale_ids)
        self.frozen_symbols = tuple(sorted(frozen))
        self.unmapped_symbols = tuple(sorted(unmapped))
        return list(nominated)


def _protocol_check(provider: EqnDiscoveryProvider) -> DiscoveryProvider:
    return provider
