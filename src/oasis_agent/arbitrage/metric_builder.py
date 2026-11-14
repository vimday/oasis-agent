"""Interpret Metric's declared oracle diffs; never apply them or authorize a read.

Layout: PythOracle 1.1.0, solc 0.8.33, OracleData mapping at slot 1.
See docs/verification/METRIC_BUILDER_FEEDS_20261114.md for source/build provenance.
This is a source-specific interpretation, not proof of code at the builder's target block.
"""

import re
from typing import Annotated, Any, Literal, Self

from eth_hash.auto import keccak
from pydantic import Field, StringConstraints, model_validator

from ..evidence.rpc_validation import strict_json_object, strict_list, strict_object
from ..models.common import ZERO_ADDRESS, canonical_json, sha256_hex
from .cycle_plan import Address, Frozen
from .titan_state import address, decode_snapshot, integer, storage

LAYOUT_RUNTIME = "0xd7169b763899b0e9a19b2b26ed2b566375440905f62964bd08817f9f1cb37ca2"
LAYOUT_SOURCE_SHA256 = "a1fd6c7afda75402ec2f15063bb41df683ae1a8d7740dbb98c808c5dec09aba8"
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Symbol = Annotated[str, StringConstraints(pattern=r"^[\x20-\x7e]{1,128}$")]
FeedId = Annotated[int, Field(gt=0, lt=1 << 32)]


class FeedBinding(Frozen):
    feed_id: FeedId
    symbol: Symbol
    token: Address
    max_age_seconds: Annotated[int, Field(gt=0, le=604800)]


class BuilderFeedPlan(Frozen):
    schema_version: Literal["oasis-metric-builder-feed-plan-v1"] = Field(alias="schema")
    layout: Literal["metric-pyth-lazer-1.1.0-solc-0.8.33"]
    chain_id: Annotated[int, Field(ge=1, le=1)]
    oracle: Address
    pool: Address
    catalog_sha256: Digest
    feeds: tuple[FeedBinding, FeedBinding]

    @model_validator(mode="after")
    def unique(self) -> Self:
        addresses = (self.oracle, self.pool, *(f.token for f in self.feeds))
        if ZERO_ADDRESS in addresses or len(set(addresses)) != 4:
            raise ValueError("distinct_nonzero_contracts_required")
        if self.feeds[0].feed_id == self.feeds[1].feed_id:
            raise ValueError("distinct_feed_bindings_required")
        return self


def feed_slot(feed_id: int) -> str:
    integer(feed_id, 32, "feed_id")
    if not feed_id:
        raise ValueError("zero_feed_id")
    return "0x" + keccak(feed_id.to_bytes(32, "big") + (1).to_bytes(32, "big")).hex()


def catalog(plan: BuilderFeedPlan, payload: bytes) -> dict[str, dict[str, Any]]:
    """Hash-bind full public metadata. Symbol labels remain provider claims, not chain proof."""
    if len(payload) > 16 * 1024 * 1024 or sha256_hex(payload) != plan.catalog_sha256:
        raise ValueError("feed_catalog_digest_or_size")
    rows = strict_list(strict_json_object(b'{"feeds":' + payload + b"}")["feeds"], "feeds")
    if not 1 <= len(rows) <= 16384:
        raise ValueError("feed_catalog_count")
    result = {}
    for item in rows:
        row = strict_object(item, "feed")
        feed_id = integer(row.get("pyth_lazer_id"), 32, "feed_id")
        symbol = row.get("symbol")
        exponent = row.get("exponent")
        if (
            not isinstance(symbol, str)
            or re.fullmatch(r"[\x20-\x7e]{1,128}", symbol) is None
            or type(exponent) is not int
            or not -128 <= exponent <= 127
        ):
            raise ValueError("invalid_feed_metadata")
        slot = feed_slot(feed_id)
        if slot in result:
            raise ValueError("duplicate_feed_id")
        result[slot] = {"feed_id": feed_id, "symbol": symbol, "catalog_exponent": exponent}
    for feed in plan.feeds:
        if result.get(feed_slot(feed.feed_id), {}).get("symbol") != feed.symbol:
            raise ValueError("feed_binding_catalog_mismatch")
    return result


def decode_word(word: str) -> dict[str, int]:
    # Reuse the exact bytes32 validator, including leading zeroes and case normalization.
    word = storage({"0x" + "00" * 32: word})["0x" + "00" * 32]
    n = int(word, 16)
    if n >> 152:
        raise ValueError("unsupported_oracle_word_padding")
    return {
        "mid_e8": n & ((1 << 64) - 1),
        "spread_0_01_bps": (n >> 64) & 65535,
        "spread1": (n >> 80) & 65535,
        "reference_time_ms": (n >> 96) & ((1 << 56) - 1),
    }


def interpret(
    plan: BuilderFeedPlan, value: object, received_ns: int, symbols: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Conditional feed diagnostics. Omitted diffs never imply absent canonical storage."""
    integer(received_ns, 64, "received_ns")
    rows = decode_snapshot(value)
    selected = next((r for r in rows if r["quote_address"] == plan.oracle), None)
    reasons = []
    feeds: list[dict[str, Any]] = []
    accounts: list[dict[str, Any]] = []
    unknown: list[str] = []
    if selected is None:
        reasons.append("missing_oracle_declaration")
    else:
        pair = {f.token for f in plan.feeds}
        if selected["maker"] != "Metric" or plan.pool not in selected["priced_addresses"]:
            reasons.append("metric_pool_declaration_mismatch")
        if not any({p["token_1"], p["token_2"]} == pair for p in selected["pairs"]):
            reasons.append("target_pair_not_declared")
        raw = strict_object(value, "snapshot")
        venue = next(
            strict_object(v, "venue")
            for v in strict_list(raw["venues"], "venues")
            if address(strict_object(v, "venue")["quoteAddress"]) == plan.oracle
        )
        diff: dict[str, str] = {}
        for key, item in strict_object(venue["stateOverride"], "overrides").items():
            target = address(key)
            fields = strict_object(item, "account")
            accounts.append(
                {
                    "address": target,
                    "fields": sorted(fields),
                    "sha256": sha256_hex(canonical_json(fields)),
                }
            )
            if "code" in fields or "state" in fields:
                reasons.append("code_or_full_state_override_unsupported")
            if "stateDiff" in fields:
                if target != plan.oracle:
                    reasons.append("non_oracle_storage_override_unsupported")
                else:
                    diff = storage(fields["stateDiff"])
        unknown = sorted(diff.keys() - symbols.keys())
        if unknown:
            reasons.append("unmapped_oracle_storage_slots")
        if any(feed_slot(f.feed_id) not in diff for f in plan.feeds):
            reasons.append("target_feed_diff_missing_canonical_value_unknown")
        # Never interpret a subset as usable if another part changes code/control storage.
        if not reasons:
            bounds = {f.feed_id: f for f in plan.feeds}
            for slot, word in sorted(diff.items()):
                info = symbols[slot]
                try:
                    decoded = decode_word(word)
                except ValueError:
                    reasons.append("unsupported_oracle_word_padding")
                    feeds = []
                    break
                ref_ms = decoded["reference_time_ms"]
                ref_sec = ref_ms // 1000
                bound = bounds.get(info["feed_id"])
                problems = []
                if not ref_ms:
                    problems.append("missing_reference_time")
                if ref_ms * 1_000_000 > min(selected["snapshot_timestamp_ns"], received_ns):
                    problems.append("reference_after_snapshot_or_retrieval")
                age = selected["target_timestamp_seconds"] - ref_sec
                if age < 0:
                    problems.append("reference_after_target_block")
                elif bound is not None and age > bound.max_age_seconds:
                    problems.append("reference_too_old_at_target")
                if decoded["mid_e8"] == 0:
                    problems.append("zero_reference_price")
                if decoded["spread_0_01_bps"] >= 65534:
                    problems.append("saturated_or_stalled_spread")
                # spread1=65535 is LazerConsumer's normal unused field, NOT the stall flag.
                feeds.append(
                    {
                        **info,
                        **decoded,
                        "slot": slot,
                        "word_sha256": sha256_hex(bytes.fromhex(word[2:])),
                        "reference_time_seconds": ref_sec,
                        "target_age_seconds": age,
                        "bound_token": bound.token if bound else None,
                        "max_age_seconds": bound.max_age_seconds if bound else None,
                        "reasons": problems,
                    }
                )
            if any(f["reasons"] for f in feeds):
                reasons.append("declared_feed_checks_failed")
    return {
        "schema": "oasis-metric-builder-feed-report-v1",
        "status": "rejected_or_incomplete" if reasons else "decoded_builder_claims",
        "reasons": sorted(set(reasons)),
        "layout_reference_runtime": LAYOUT_RUNTIME,
        "layout_source_bundle_sha256": LAYOUT_SOURCE_SHA256,
        "catalog_sha256": plan.catalog_sha256,
        "feed_plan_sha256": sha256_hex(canonical_json(plan.model_dump(by_alias=True, mode="json"))),
        "feeds": sorted(feeds, key=lambda f: f["feed_id"]),
        "override_accounts": sorted(accounts, key=lambda a: a["address"]),
        "unmapped_slots": unknown,
        "required_canonical_parent_number": selected["target_block"] - 1 if selected else None,
        "canonical_base_block_hash": None,
        "target_code_identity_verified": False,
        "target_provider_configuration_verified": False,
        "symbol_metadata_point_in_time_verified": False,
        "signed_price_payload_verified": False,
        "price_guards_and_source_shaping_verified": False,
        "caller_registration_and_blacklist_verified": False,
        "auxiliary_account_changes_verified": False,
        "state_overrides_applied": False,
        "execution_eligible": False,
        "net_profit": None,
    }


def compare(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    """Detect inconsistent per-feed declarations without assuming stream completeness."""
    old = {f["feed_id"]: f for f in previous["feeds"]}
    changes = []
    for new in current["feeds"]:
        prior = old.get(new["feed_id"])
        if prior is None:
            continue
        status = "advanced"
        if new["reference_time_ms"] < prior["reference_time_ms"]:
            status = "reference_time_regression"
        elif new["reference_time_ms"] == prior["reference_time_ms"]:
            status = (
                "duplicate" if new["word_sha256"] == prior["word_sha256"] else "same_time_conflict"
            )
        changes.append({"feed_id": new["feed_id"], "status": status})
    return changes
