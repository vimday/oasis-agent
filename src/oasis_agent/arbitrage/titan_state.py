"""Bounded Titan builder declarations, never mined state or executable overrides."""

import re
from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator

from ..evidence.rpc_validation import strict_integer, strict_list, strict_object, strict_string
from ..models.common import ZERO_ADDRESS, canonical_json, sha256_hex
from .aave_wallet import decimal_quantity
from .cycle_plan import Address, Frozen, hash32

BEACON_GENESIS = 1606824023


def integer(value: object, bits: int, name: str) -> int:
    n = strict_integer(value, name)
    if not 0 <= n < 1 << bits:
        raise ValueError("integer_out_of_bounds")
    return n


def address(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"0x[0-9a-fA-F]{40}", value) is None:
        raise ValueError("invalid_address")
    result = value.lower()
    if result == ZERO_ADDRESS:
        raise ValueError("zero_address")
    return result


def bounded_list(value: object, limit: int, name: str) -> list[object]:
    items = strict_list(value, name)
    if len(items) > limit:
        raise ValueError("list_limit")
    return items


def storage(value: object) -> dict[str, str]:
    obj = strict_object(value, "storage")
    if len(obj) > 4096:
        raise ValueError("storage_limit")
    result = {}
    for key, item in obj.items():
        if any(
            not isinstance(v, str) or re.fullmatch(r"0x[0-9a-fA-F]{64}", v) is None
            for v in (key, item)
        ):
            raise ValueError("invalid_storage_word")
        assert isinstance(item, str)
        if key.lower() in result:
            raise ValueError("aliased_storage_slot")
        result[key.lower()] = item.lower()
    return result


def overrides(value: object) -> dict[str, Any]:
    """Validate opaque input and return hashes only. Never construct an eth_call."""
    accounts = strict_object(value, "stateOverride")
    if not 1 <= len(accounts) <= 128:
        raise ValueError("override_account_limit")
    normalized = {}
    slots = 0
    code_addresses = []
    for raw_address, item in accounts.items():
        target = address(raw_address)
        if target in normalized:
            raise ValueError("aliased_override_address")
        fields = strict_object(item, "account_override")
        if not fields or fields.keys() - {"balance", "nonce", "code", "state", "stateDiff"}:
            raise ValueError("unsupported_override_field")
        if "state" in fields and "stateDiff" in fields:
            raise ValueError("ambiguous_override_storage")
        clean: dict[str, Any] = {}
        for key, raw in fields.items():
            if key in {"state", "stateDiff"}:
                clean[key] = storage(raw)
                slots += len(clean[key])
            elif key in {"balance", "nonce"}:
                if (
                    not isinstance(raw, str)
                    or re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,63})", raw) is None
                ):
                    raise ValueError("invalid_override_quantity")
                if key == "nonce" and int(raw, 16) >= 1 << 64:
                    raise ValueError("override_nonce_overflow")
                clean[key] = raw.lower()
            else:
                if (
                    not isinstance(raw, str)
                    or re.fullmatch(r"0x(?:[0-9a-fA-F]{2}){0,32768}", raw) is None
                ):
                    raise ValueError("invalid_override_code")
                clean[key] = raw.lower()
                code_addresses.append(target)
        normalized[target] = clean
    if slots > 16384:
        raise ValueError("total_storage_limit")
    return {
        "sha256": sha256_hex(canonical_json(normalized)),
        "addresses": sorted(normalized),
        "storage_slots": slots,
        "code_override_addresses": sorted(code_addresses),
    }


def decode_snapshot(value: object) -> list[dict[str, Any]]:
    obj = strict_object(value, "snapshot")
    if set(obj) != {"venues"}:
        raise ValueError("snapshot_schema")
    rows = []
    seen = set()
    for item in bounded_list(obj["venues"], 512, "venues"):
        row = strict_object(item, "venue")
        if set(row) != {
            "maker",
            "quoteAddress",
            "pricedAddresses",
            "assetPairs",
            "pairSequences",
            "slot",
            "blockNumber",
            "timestamp",
            "stateOverride",
        }:
            raise ValueError("venue_schema")
        maker = strict_string(row["maker"], "maker")
        if not 1 <= len(maker) <= 64 or any(ord(c) < 32 for c in maker):
            raise ValueError("maker_label")
        quote = address(row["quoteAddress"])
        if quote in seen:
            raise ValueError("duplicate_quote_address")
        seen.add(quote)
        priced = [address(a) for a in bounded_list(row["pricedAddresses"], 128, "priced")]
        if quote not in priced or len(priced) != len(set(priced)):
            raise ValueError("priced_address_identity")
        pairs = []
        pair_seen = set()
        for pair in bounded_list(row["assetPairs"], 512, "pairs"):
            fields = strict_object(pair, "pair")
            if set(fields) != {"token_1", "token_2"}:
                raise ValueError("pair_schema")
            tokens = (address(fields["token_1"]), address(fields["token_2"]))
            identity = tuple(sorted(tokens))
            if tokens[0] == tokens[1] or identity in pair_seen:
                raise ValueError("ambiguous_asset_pair")
            pair_seen.add(identity)
            pairs.append(tokens)
        sequences = [
            integer(v, 256, "sequence")
            for v in bounded_list(row["pairSequences"], 512, "sequences")
        ]
        if len(sequences) != len(pairs) or not pairs:
            raise ValueError("pair_sequence_alignment")
        slot, block, timestamp = (
            integer(row[k], bits, k)
            for k, bits in (("slot", 64), ("blockNumber", 64), ("timestamp", 64))
        )
        if not slot or not block or not timestamp:
            raise ValueError("missing_build_context")
        state = overrides(row["stateOverride"])
        rows.append(
            {
                "maker": maker,
                "quote_address": quote,
                "priced_addresses": sorted(priced),
                "pairs": [
                    {"token_1": p[0], "token_2": p[1], "sequence": seq}
                    for p, seq in zip(pairs, sequences, strict=True)
                ],
                "target_slot": slot,
                "target_block": block,
                "target_timestamp_seconds": BEACON_GENESIS + 12 * slot,
                "snapshot_timestamp_ns": timestamp,
                "override_summary": state,
            }
        )
    return rows


class TitanAnalysisPlan(Frozen):
    schema_version: Literal["oasis-titan-state-analysis-v1"] = Field(alias="schema")
    chain_id: Annotated[int, Field(ge=1, le=1)]
    metric_quote_addresses: Annotated[tuple[Address, ...], Field(min_length=1, max_length=8)]
    max_snapshot_age_ms: Annotated[int, Field(ge=1, le=60000)]
    max_future_ms: Annotated[int, Field(ge=0, le=1000)]
    max_slot_age_seconds: Annotated[int, Field(ge=1, le=60)]

    @model_validator(mode="after")
    def unique(self) -> Self:
        if ZERO_ADDRESS in self.metric_quote_addresses or len(
            set(self.metric_quote_addresses)
        ) != len(self.metric_quote_addresses):
            raise ValueError("distinct_nonzero_metric_addresses")
        return self


def inspect_snapshot(plan: TitanAnalysisPlan, value: object, received_ns: int) -> dict[str, Any]:
    integer(received_ns, 64, "received_ns")
    rows = decode_snapshot(value)
    selected = []
    unknown = []
    for row in rows:
        if row["quote_address"] not in plan.metric_quote_addresses:
            unknown.append(row["quote_address"])
            continue
        reasons = []
        age = received_ns - row["snapshot_timestamp_ns"]
        if row["maker"] != "Metric":
            reasons.append("maker_identity_mismatch")
        if age > plan.max_snapshot_age_ms * 1_000_000:
            reasons.append("stale_snapshot")
        elif age < -plan.max_future_ms * 1_000_000:
            reasons.append("future_snapshot")
        slot_ns = row["target_timestamp_seconds"] * 1_000_000_000
        if slot_ns > received_ns + 12_000_000_000:
            reasons.append("future_target_slot")
        if received_ns - slot_ns > plan.max_slot_age_seconds * 1_000_000_000:
            reasons.append("stale_target_slot")
        selected.append(
            {
                **row,
                "snapshot_age_ns": age,
                "reasons": reasons,
                "status": "rejected_declaration" if reasons else "observed_declaration",
            }
        )
    missing = sorted(set(plan.metric_quote_addresses) - {r["quote_address"] for r in selected})
    return {
        "venues_observed": len(rows),
        "selected": selected,
        "missing": missing,
        "unselected_quote_addresses": sorted(unknown),
    }


def compare(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    """Snapshot-to-snapshot diagnostics; jumps never establish event completeness."""
    prior = {r["quote_address"]: r for r in previous["selected"]}
    now = {r["quote_address"]: r for r in current["selected"]}
    changes = []
    for quote in sorted(prior.keys() | now.keys()):
        old, new = prior.get(quote), now.get(quote)
        if old is None:
            changes.append({"quote_address": quote, "status": "appeared"})
            continue
        if new is None:
            changes.append({"quote_address": quote, "status": "missing_in_next_snapshot"})
            continue
        status = "observed_update"
        old_pairs = {(p["token_1"], p["token_2"]): p["sequence"] for p in old["pairs"]}
        new_pairs = {(p["token_1"], p["token_2"]): p["sequence"] for p in new["pairs"]}
        if any(new[k] < old[k] for k in ("target_slot", "target_block", "snapshot_timestamp_ns")):
            status = "context_regression"
        elif old_pairs.keys() != new_pairs.keys():
            status = "pair_coverage_changed"
        elif any(new_pairs[k] < old_pairs[k] for k in old_pairs):
            status = "sequence_regression"
        elif (new["target_slot"], new["target_block"], new_pairs) == (
            old["target_slot"],
            old["target_block"],
            old_pairs,
        ):
            status = (
                "duplicate_state"
                if new["override_summary"]["sha256"] == old["override_summary"]["sha256"]
                and new["priced_addresses"] == old["priced_addresses"]
                else "same_version_conflict"
            )
        changes.append({"quote_address": quote, "status": status})
    return changes


def attribution(
    data: object, slot: int, header: dict[str, Any], keys: set[str]
) -> tuple[str, str | None]:
    """Match a delivered payload to a pinned header and the published builder keys."""
    known = set()
    for value in bounded_list(data, 64, "relay rows"):
        bid = strict_object(value, "relay bid")
        if decimal_quantity(bid.get("slot"), 64) != slot:
            raise ValueError("relay changed slot")
        if hash32(bid.get("block_hash")) != header["hash"]:
            continue
        if (
            hash32(bid.get("parent_hash")) != header["parent_hash"]
            or decimal_quantity(bid.get("block_number"), 64) != header["block"]
        ):
            raise ValueError("relay changed canonical header context")
        key = strict_string(bid.get("builder_pubkey"), "relay builder pubkey")
        if re.fullmatch(r"0x[0-9a-f]{96}", key) is None:
            raise ValueError("invalid relay builder pubkey")
        known.add(key)
    if len(known) > 1:
        raise ValueError("conflicting relay builder identities")
    if not known:
        return "no_matching_delivery", None
    (key,) = known
    return ("titan_confirmed" if key in keys else "unlisted_builder_key"), key
