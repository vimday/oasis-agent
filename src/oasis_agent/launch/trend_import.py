"""Read-only import of retained official ranking responses, not live acquisition."""

from typing import Literal

from ..evidence.rpc_validation import strict_json_object
from ..models.common import sha256_hex
from .scoring import TrendSignal

GMGN_EVM_CHAINS = {1: "eth", 56: "bsc", 8453: "base", 4663: "robinhood", 42161: "arbitrum"}


def import_trends(
    raw: bytes,
    *,
    provider: Literal["gmgn", "okx"],
    chain_id: int,
    window_seconds: int,
    observed_at: int,
    retrieved_at: int,
) -> tuple[TrendSignal, ...]:
    """Only first-page chain-scoped ranking responses; timestamps supplied by retained capture.

    Unknown provider schemas fail closed. Rank is not a probability or investment edge.
    Source safety flags are deliberately not promoted into on-chain safety checks.
    """
    if len(raw) > 5_000_000 or provider not in ("gmgn", "okx"):
        raise ValueError("unsupported or oversized trend response")
    root = strict_json_object(raw)
    code = root.get("code")
    if type(code) not in (int, str) or code not in (0, "0"):
        raise ValueError("provider response unsuccessful")
    data = root.get("data")
    if provider == "gmgn":
        if chain_id not in GMGN_EVM_CHAINS or not isinstance(data, dict):
            raise ValueError("unsupported GMGN EVM scope")
        rows = data.get("rank")
    else:
        rows = data
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError("ranked first-page list of 1..100 tokens required")
    result = []
    seen = set()
    ranks = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError("invalid ranking item")
        if provider == "gmgn":
            if row.get("chain") != GMGN_EVM_CHAINS[chain_id]:
                raise ValueError("GMGN chain mismatch")
            token, rank = row.get("address"), row.get("rank")
        else:
            if row.get("chainIndex") != str(chain_id):
                raise ValueError("OKX chain mismatch")
            token, rank = row.get("tokenContractAddress"), index
        if type(rank) is not int:
            raise ValueError("integer rank required")
        if not isinstance(token, str):
            raise ValueError("token address required")
        signal = TrendSignal(
            provider=provider,
            chain_id=chain_id,
            token=token.lower(),
            rank=rank,
            window_seconds=window_seconds,
            observed_at=observed_at,
            retrieved_at=retrieved_at,
            raw_sha256=sha256_hex(raw),
        )
        if signal.token in seen or signal.rank in ranks or observed_at > retrieved_at:
            raise ValueError("duplicate ranking or incoherent time")
        seen.add(signal.token)
        ranks.add(signal.rank)
        result.append(signal)
    return tuple(result)
