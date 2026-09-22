"""Replay provider claims without upgrading them to chain safety or settlement evidence."""

import json
import re
from decimal import Decimal
from typing import Literal

from ..models.common import sha256_hex
from .models import Address, Digest, Positive, StrictModel, UInt
from .trend_import import GMGN_EVM_CHAINS

ProviderName = Literal["gmgn", "okx"]
Kind = Literal["trending", "info", "security", "pool", "holders", "advanced", "clusters"]


class ProviderObservation(StrictModel):
    provider: ProviderName
    kind: Kind
    chain_id: Positive
    token: Address
    retrieved_at: UInt
    raw_sha256: Digest
    # Exact decimal strings / categorical labels; names remain provider-specific.
    claims: dict[str, str | bool | None]
    missing_fields: tuple[str, ...]
    source_event_time: None = None
    block_identity: None = None
    identity_binding: Literal["request_only", "response_chain_token"]
    execution_eligible: Literal[False] = False


# Field units are part of the decoder contract, not inferred from observed magnitude.
FIELDS: dict[tuple[str, str], dict[str, str]] = {
    ("gmgn", "trending"): {
        "holder_count": "count",
        "top_10_holder_rate": "ratio",
        "rug_ratio": "ratio",
        "bundler_rate": "ratio",
        "rat_trader_amount_rate": "ratio",
        "is_wash_trading": "bool",
        "liquidity": "usd",
        "market_cap": "usd",
        "volume": "usd",
    },
    ("okx", "trending"): {
        "holders": "count",
        "top10HoldPercent": "percent",
        "bundleHoldPercent": "percent",
        "insiderHoldPercent": "percent",
        "riskLevelControl": "risk_level",
        "liquidity": "usd",
        "marketCap": "usd",
        "volume": "usd",
    },
    ("gmgn", "security"): {
        "is_honeypot": "yes_no",
        "open_source": "yes_no",
        "owner_renounced": "yes_no",
        "buy_tax": "ratio",
        "sell_tax": "ratio",
        "top_10_holder_rate": "ratio",
        "rug_ratio": "ratio",
        "dev_team_hold_rate": "ratio",
        "suspected_insider_hold_rate": "ratio",
        "bundler_trader_amount_rate": "ratio",
        "is_wash_trading": "bool",
    },
    ("okx", "advanced"): {
        "top10HoldPercent": "percent",
        "bundleHoldingPercent": "percent",
        "suspiciousHoldingPercent": "percent",
        "devHoldingPercent": "percent",
        "sniperHoldingPercent": "percent",
        "riskControlLevel": "risk_level",
        "devRugPullTokenCount": "count",
    },
    ("okx", "clusters"): {
        "top100HoldingsPercent": "percent",
        "rugPullPercent": "percent",
        "holderNewAddressPercent": "percent",
        "holderSameFundSourcePercent": "percent",
        "holderSameCreationTimePercent": "percent",
    },
}


def response_object(raw: bytes, *, cli_unwrapped: bool = False) -> dict[str, object]:
    """Retain decimal lexemes without binary-float rounding; reject ambiguous envelopes."""
    if len(raw) > 5_000_000:
        raise ValueError("provider response exceeds limit")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for k, v in pairs:
            if k in result:
                raise ValueError("duplicate provider field")
            result[k] = v
        return result

    def reject(value: str) -> object:
        raise ValueError("non-finite provider number")

    root = json.loads(raw, object_pairs_hook=unique, parse_float=str, parse_constant=reject)
    if not isinstance(root, dict):
        raise ValueError("provider object required")
    code = root.get("code")
    if not (cli_unwrapped and "code" not in root) and (
        type(code) not in (str, int) or code not in (0, "0")
    ):
        raise ValueError("provider unsuccessful")
    return root


def claim(value: object, unit: str) -> str | bool | None:
    if value is None or value in ("", "--", "unknown"):
        return None
    if unit == "bool":
        return value if type(value) is bool else None
    if unit == "yes_no":
        return value if isinstance(value, str) and value in ("yes", "no") else None
    if type(value) not in (str, int):
        return None
    s = str(value)
    if len(s) > 100 or not re.fullmatch(r"\d+(?:\.\d+)?(?:[eE][+-]?\d{1,3})?", s):
        return None
    number = Decimal(s)
    maximum = {"ratio": 1, "percent": 100, "risk_level": 5}.get(unit)
    if not number.is_finite() or (maximum is not None and number > maximum):
        return None
    if unit in ("count", "risk_level") and number != number.to_integral_value():
        return None
    return s


def observation(
    raw: bytes,
    *,
    provider: ProviderName,
    kind: Kind,
    chain_id: int,
    token: str,
    retrieved_at: int,
    row: dict[str, object] | None = None,
    cli_unwrapped: bool = False,
) -> ProviderObservation:
    """Token-detail responses can be request-bound only; missing safety never passes."""
    if cli_unwrapped and (provider != "gmgn" or kind == "trending"):
        raise ValueError("unwrapped output only for successful GMGN detail CLI")
    root = response_object(raw, cli_unwrapped=cli_unwrapped)
    data = root if cli_unwrapped else root.get("data")
    if row is not None:
        rows = data.get("rank") if provider == "gmgn" and isinstance(data, dict) else data
        if kind != "trending" or not isinstance(rows, list) or row not in rows:
            raise ValueError("ranking row is not bound to raw response")
        data = row
    if not isinstance(data, dict):
        raise ValueError("provider detail object required")
    binding: Literal["request_only", "response_chain_token"] = "request_only"
    address_key, chain_key = (
        ("address", "chain") if provider == "gmgn" else ("tokenContractAddress", "chainIndex")
    )
    expected_chain = GMGN_EVM_CHAINS.get(chain_id) if provider == "gmgn" else str(chain_id)
    if expected_chain is None:
        raise ValueError("unsupported chain")
    if address_key in data and data[address_key] is not None:
        address = data[address_key]
        # Pool.address is a pool identity, not a token identity.
        if kind != "pool" and (not isinstance(address, str) or address.lower() != token):
            raise ValueError("provider token mismatch")
    if chain_key in data and data[chain_key] != expected_chain:
        raise ValueError("provider chain mismatch")
    if kind != "pool" and address_key in data and chain_key in data:
        binding = "response_chain_token"
    fields = FIELDS.get((provider, kind), {})
    claims = {key: claim(data.get(key), unit) for key, unit in fields.items()}
    return ProviderObservation(
        provider=provider,
        kind=kind,
        chain_id=chain_id,
        token=token,
        retrieved_at=retrieved_at,
        raw_sha256=sha256_hex(raw),
        claims=claims,
        missing_fields=tuple(k for k, v in claims.items() if v is None),
        identity_binding=binding,
    )
