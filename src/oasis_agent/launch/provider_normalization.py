"""Version-two provider claims; no conversion into verified contract or holder safety."""

from typing import Literal

from ..models.common import sha256_hex
from .models import Address, StrictModel, UInt
from .provider_observation import Kind, ProviderName, ProviderObservation, claim, observation
from .provider_observation import response_object as decode

FLAG_FIELDS = {
    "honeypot": ("is_honeypot", "honeypot"),
    "source_verified": ("is_open_source", "open_source"),
    "ownership_renounced": ("owner_renounced", "is_renounced", "renounced"),
    "blacklist": ("is_blacklist", "blacklist"),
}


class HolderClaim(StrictModel):
    address: Address
    reported_amount: str | None
    reported_share: str | None
    share_unit: Literal["ratio", "percent"]


class NormalizedObservation(ProviderObservation):
    decoder_version: Literal[2] = 2
    normalized_flags: dict[str, bool | None]
    flag_fields: dict[str, tuple[str, ...]]
    conflicting_flags: tuple[str, ...]
    invalid_flags: tuple[str, ...]
    holder_rows: tuple[HolderClaim, ...]
    missing_holder_values: UInt
    holder_census_complete: Literal[False] = False


def flag(value: object) -> bool | None:
    """Explicit API representations only: strings such as 'false' do not become truthy."""
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value in ("yes", "no"):
        return value == "yes"
    return None


def normalize(
    raw: bytes,
    *,
    provider: ProviderName,
    kind: Kind,
    chain_id: int,
    token: str,
    retrieved_at: int,
    row: dict[str, object] | None = None,
    cli_unwrapped: bool = False,
) -> NormalizedObservation:
    root = decode(raw, cli_unwrapped=cli_unwrapped)
    data = root if cli_unwrapped else root.get("data")
    holders: list[HolderClaim] = []
    missing = 0
    if kind == "holders":
        if cli_unwrapped != (provider == "gmgn"):
            raise ValueError("holder transport mismatch")
        rows = data.get("list") if provider == "gmgn" and isinstance(data, dict) else data
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("bounded holder list required")
        seen = set()
        for item in rows:
            if not isinstance(item, dict):
                raise ValueError("holder object required")
            address = item.get("address" if provider == "gmgn" else "holderWalletAddress")
            if not isinstance(address, str) or address.lower() in seen:
                raise ValueError("unique holder address required")
            seen.add(address.lower())
            unit: Literal["ratio", "percent"] = "ratio" if provider == "gmgn" else "percent"
            amount = claim(item.get("balance" if provider == "gmgn" else "holdAmount"), "amount")
            share = claim(
                item.get("amount_percentage" if provider == "gmgn" else "holdPercent"), unit
            )
            assert not isinstance(amount, bool) and not isinstance(share, bool)
            holders.append(
                HolderClaim(
                    address=address.lower(),
                    reported_amount=amount,
                    reported_share=share,
                    share_unit=unit,
                )
            )
            missing += (amount is None) + (share is None)
        # Holder rows identify wallets, not the requested token. Bind only to the request.
        base = ProviderObservation(
            provider=provider,
            kind=kind,
            chain_id=chain_id,
            token=token,
            retrieved_at=retrieved_at,
            raw_sha256=sha256_hex(raw),
            claims={"sample_holder_count": str(len(holders))},
            missing_fields=(),
            identity_binding="request_only",
        )
    else:
        base = observation(
            raw,
            provider=provider,
            kind=kind,
            chain_id=chain_id,
            token=token,
            retrieved_at=retrieved_at,
            row=row,
            cli_unwrapped=cli_unwrapped,
        )
    if isinstance(data, dict):
        address_key = "address" if provider == "gmgn" else "tokenContractAddress"
        if data.get(address_key) is None and base.identity_binding == "response_chain_token":
            base = base.model_copy(update={"identity_binding": "request_only"})
    flags: dict[str, bool | None] = {}
    fields: dict[str, tuple[str, ...]] = {}
    conflicts, invalid = [], []
    if provider == "gmgn" and kind == "security" and isinstance(data, dict):
        for name, aliases in FLAG_FIELDS.items():
            fields[name] = tuple(k for k in aliases if k in data)
            provided = [
                data[k] for k in aliases if k in data and data[k] not in (None, "", "unknown")
            ]
            parsed = [flag(v) for v in provided]
            values = {v for v in parsed if v is not None}
            if len(values) > 1:
                conflicts.append(name)
            if any(v is None for v in parsed):
                invalid.append(name)
            flags[name] = next(iter(values)) if len(values) == 1 and None not in parsed else None
    return NormalizedObservation(
        **base.model_dump(),
        normalized_flags=flags,
        flag_fields=fields,
        conflicting_flags=tuple(conflicts),
        invalid_flags=tuple(invalid),
        holder_rows=tuple(holders),
        missing_holder_values=missing,
    )
