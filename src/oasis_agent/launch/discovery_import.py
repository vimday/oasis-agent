"""Translate documented public profiles or retained ranking data into discovery only."""

from typing import Literal

from ..evidence.rpc_validation import strict_json_object
from ..models.common import sha256_hex
from .discovery import DiscoverySignal
from .trend_import import import_trends

DEX_CHAINS = {"ethereum": 1, "bsc": 56, "base": 8453, "arbitrum": 42161, "robinhood": 4663}


def import_profiles(raw: bytes, *, retrieved_at: int) -> tuple[DiscoverySignal, ...]:
    """Latest profile is neither token birth nor organic popularity; unsupported chains omitted."""
    if len(raw) > 5_000_000:
        raise ValueError("profile response too large")
    rows = strict_json_object(b'{"items":' + raw + b"}")["items"]
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("bounded profile list required")
    output = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("profile object required")
        chain = row.get("chainId")
        if not isinstance(chain, str) or chain not in DEX_CHAINS:
            continue
        token = row.get("tokenAddress")
        if not isinstance(token, str):
            raise ValueError("token address required")
        output.append(
            DiscoverySignal(
                provider="dexscreener",
                chain_id=DEX_CHAINS[chain],
                token=token.lower(),
                kind="profile",
                observed_at=retrieved_at,
                retrieved_at=retrieved_at,
                raw_sha256=sha256_hex(raw),
            )
        )
    return tuple(output)


def import_ranking_discovery(
    raw: bytes,
    *,
    provider: Literal["gmgn", "okx"],
    chain_id: int,
    window_seconds: int,
    observed_at: int,
    retrieved_at: int,
) -> tuple[DiscoverySignal, ...]:
    return tuple(
        DiscoverySignal(
            provider=signal.provider,
            chain_id=signal.chain_id,
            token=signal.token,
            kind="trending",
            observed_at=signal.observed_at,
            retrieved_at=signal.retrieved_at,
            window_seconds=signal.window_seconds,
            rank=signal.rank,
            raw_sha256=signal.raw_sha256,
        )
        for signal in import_trends(
            raw,
            provider=provider,
            chain_id=chain_id,
            window_seconds=window_seconds,
            observed_at=observed_at,
            retrieved_at=retrieved_at,
        )
    )
