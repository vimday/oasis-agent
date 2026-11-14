"""Reference-feed diagnosis for a pinned immutable AnchoredPriceProvider.

This checks reference guards only, not custom-source shaping, swaps or access authority.
The public raw getters on provider oracles may always revert; use their documented price path.
"""

from dataclasses import asdict
from typing import Annotated, Any, Literal, Self

from eth_hash.auto import keccak
from pydantic import Field, model_validator

from ..models.common import ZERO_ADDRESS, canonical_json, sha256_hex
from .cycle_evidence import Export, Reads, Unavailable, header
from .cycle_plan import Address, Frozen, Hash, Label, Window, utc
from .cycle_protocols import abi, call, calldata, getter, verify_code
from .metric_v3 import IMMUTABLE_TYPES


class OraclePlan(Frozen):
    schema_version: Literal["oasis-metric-oracle-plan-v1"] = Field(alias="schema")
    chain_id: Annotated[int, Field(gt=0, lt=1 << 64)]
    sources: Annotated[tuple[Label, ...], Field(min_length=1, max_length=2)]
    window: Window
    pool: Address
    pool_hash: Hash
    immutables_hash: Hash
    provider: Address
    provider_hash: Hash
    oracle: Address
    oracle_hash: Hash
    oracle_kind: Literal["pyth-lazer", "chainlink"]
    reader: Address
    reader_hash: Hash
    base_feed: Hash
    quote_feed: Hash
    max_queries: Annotated[int, Field(ge=1, le=128)]

    @model_validator(mode="after")
    def scope(self) -> Self:
        if ZERO_ADDRESS in (self.pool, self.provider, self.oracle, self.reader):
            raise ValueError("nonzero_context_required")
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("duplicate_sources")
        if self.window.expected_hash is None or self.window.cutoff_utc is None:
            raise ValueError("pinned_block_and_cutoff_required")
        utc(self.window.cutoff_utc)
        if self.base_feed == self.quote_feed:
            raise ValueError("distinct_feed_legs_required")
        return self


def reference_guards(
    *, mid: int, spread: int, reference_time: int, now: int, max_age: int, lower: int, upper: int
) -> list[str]:
    """Canonical reference-leg checks in source order; no modelled execution price."""
    problems = []
    if reference_time == 0:
        problems.append("missing_reference_time")
    elif reference_time > now:
        problems.append("future_reference_time")
    elif now - reference_time > max_age:
        problems.append("reference_too_old")
    if mid == 0:
        problems.append("zero_reference_price")
    if spread >= 65534:
        problems.append("saturated_or_stalled_spread")
    if mid < lower or mid > (upper or (1 << 128) - 1):
        problems.append("price_guard_violation")
    return problems


def diagnose(plan: OraclePlan, records: tuple[Export, ...]) -> dict[str, Any]:
    endpoints: dict[str, set[str]] = {}
    for record in records:
        if record.source not in plan.sources:
            raise ValueError("record_outside_scope")
        endpoints.setdefault(record.source, set()).add(record.endpoint_sha256)
    if any(len(v) != 1 for v in endpoints.values()) or len(
        {next(iter(v)) for v in endpoints.values()}
    ) != len(endpoints):
        raise ValueError("source_endpoint_alias_conflict")
    assert plan.window.cutoff_utc is not None
    cutoff = utc(plan.window.cutoff_utc)
    reads = Reads(records, plan.sources, plan.max_queries, cutoff)
    block = None
    config: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        reads.chain(plan.chain_id)
        raw, _ = reads.get("eth_getBlockByNumber", hex(plan.window.block), False)
        snapshot = header(raw, plan.chain_id, plan.window.block)
        if (
            snapshot.ref.block_hash != plan.window.expected_hash
            or (snapshot.ref.timestamp or 0) > cutoff.timestamp()
        ):
            raise Unavailable("planned_block_context_mismatch")
        reads.bind(snapshot.ref)
        block = asdict(snapshot)
        for target, expected_digest in (
            (plan.pool, plan.pool_hash),
            (plan.provider, plan.provider_hash),
            (plan.oracle, plan.oracle_hash),
            (plan.reader, plan.reader_hash),
        ):
            verify_code(reads, target, expected_digest)
        raw, _ = call(reads, plan.pool, calldata("getImmutables()"))
        immutables = abi(IMMUTABLE_TYPES, raw)
        if "0x" + keccak(raw).hex() != plan.immutables_hash or immutables[8] != plan.provider:
            raise Unavailable("immutable_provider_mismatch_or_mutable_provider")
        for signature, kind, expected in (
            ("token0()", "address", immutables[1]),
            ("token1()", "address", immutables[2]),
            ("offchainOracle()", "address", plan.oracle),
            ("baseFeedId()", "bytes32", bytes.fromhex(plan.base_feed[2:])),
            ("quoteFeedId()", "bytes32", bytes.fromhex(plan.quote_feed[2:])),
        ):
            actual, _ = getter(reads, plan.provider, signature, kind)
            if actual != expected:
                raise Unavailable("provider_configuration_mismatch")
        kind, _ = getter(reads, plan.oracle, "kind()", "string")
        version, _ = getter(reads, plan.oracle, "version()", "string")
        if kind != plan.oracle_kind or version != "1.1.0":
            raise Unavailable("unsupported_oracle_model")
        for key in ("MAX_REF_STALENESS", "baseMaxStaleness", "quoteMaxStaleness"):
            config[key], _ = getter(reads, plan.provider, key + "()", "uint256")
        if (
            not max(config["baseMaxStaleness"], config["quoteMaxStaleness"])
            <= config["MAX_REF_STALENESS"]
            <= 604800
        ):
            raise Unavailable("invalid_reference_age_configuration")
        for role, feed_id in (("base", plan.base_feed), ("quote", plan.quote_feed)):
            if role == "quote" and int(feed_id, 16) == 0:
                continue
            row: dict[str, Any] = {
                "role": role,
                "feed_id": feed_id,
                "status": "unavailable",
                "reasons": [],
                "observation": None,
            }
            rows.append(row)
            try:
                feed = bytes.fromhex(feed_id[2:])
                raw, price_refs = call(
                    reads,
                    plan.oracle,
                    calldata("price(bytes32,address)", ("bytes32", "address"), (feed, plan.reader)),
                )
                mid, spread, _, ref_time = abi(("uint256", "uint256", "uint16", "uint256"), raw)
                raw, guard_refs = call(
                    reads, plan.oracle, calldata("priceGuard(bytes32)", ("bytes32",), (feed,))
                )
                lower, upper = abi(("uint128", "uint128"), raw)
                age = config[role + "MaxStaleness"]
                reasons = reference_guards(
                    mid=mid,
                    spread=spread,
                    reference_time=ref_time,
                    now=snapshot.ref.timestamp or 0,
                    max_age=age,
                    lower=lower,
                    upper=upper,
                )
                row.update(
                    status="reference_guard_reject" if reasons else "reference_guards_pass",
                    reasons=reasons,
                    observation={
                        "mid_e8": mid,
                        "spread_0_01_bps": spread,
                        "reference_time_seconds": ref_time,
                        "max_age_seconds": age,
                        "guard_min_e8": lower,
                        "guard_max_e8": upper,
                        "evidence": price_refs + guard_refs,
                    },
                )
            except (ValueError, TypeError) as exc:
                row["reasons"] = [
                    exc.reason if isinstance(exc, Unavailable) else "malformed_retained_evidence"
                ]
    except (ValueError, TypeError) as exc:
        errors.append(exc.reason if isinstance(exc, Unavailable) else "malformed_retained_evidence")
    used = {ref["id"] for q in reads.queries for ref in q["evidence"]}
    if reads.block and any(
        r.identity in used
        and r.method != "eth_chainId"
        and r.fetched
        and r.fetched.timestamp() < (reads.block.timestamp or 0)
        for r in records
    ):
        errors.append("retrieval_precedes_block_timestamp")
    if errors:
        block, config, rows = None, {}, []
    return {
        "schema": "oasis-metric-oracle-report-v1",
        "plan_sha256": sha256_hex(canonical_json(plan.model_dump(by_alias=True, mode="json"))),
        "status": "incomplete_evidence"
        if errors or not rows or any(r["status"] == "unavailable" for r in rows)
        else "reference_guard_reject"
        if any(r["status"] == "reference_guard_reject" for r in rows)
        else "reference_guards_pass",
        "block": block,
        "configuration": config,
        "feeds": rows,
        "errors": errors,
        "queries": reads.queries,
        "evidence": [r.manifest() for r in records],
        "evidence_class": "parsed_retained_rpc",
        "original_http_bytes_verified": False,
        "independent_providers_verified": False,
        "feed_economic_pair_verified": False,
        "source_shaping_checked": False,
        "onchain_read_access_proven": False,
        "execution_eligible": False,
        "call_context": {"reader_argument": plan.reader, "from": "omitted_zero_origin_simulation"},
    }
