"""Pinned Metric/V3 single-hop quote composition, without execution authority."""

from dataclasses import asdict
from typing import Annotated, Any, Literal, Self

from eth_hash.auto import keccak
from pydantic import Field, StringConstraints, model_validator

from ..models.common import ZERO_ADDRESS, canonical_json, sha256_hex
from .cycle_evidence import Export, Reads, Unavailable, header
from .cycle_plan import Address, Asset, Frozen, Hash, Label, Pool, Raw, Size, Window, utc
from .cycle_protocols import (
    INT128,
    abi,
    call,
    calldata,
    quote,
    verify_asset,
    verify_code,
    verify_pool,
    verify_quoter,
)
from .metric import COST_KINDS, IncrementalCost

LIVE_SIGNATURE = "quoteLiveExactInSingle(address,address,address,bool,uint128,uint128,bytes)"
LIVE_TYPES = ("address", "address", "address", "bool", "uint128", "uint128", "bytes")
IMMUTABLE_TYPES = (
    ("address",) * 3
    + ("uint256",) * 5
    + ("address", "int256", "int256")
    + ("address",) * 7
    + ("uint256",) * 6
)


class MetricDeployment(Frozen):
    pool: Address
    pool_hash: Hash
    factory: Address
    factory_hash: Hash
    quoter: Address
    quoter_hash: Hash
    token0: Address
    token1: Address
    immutables_hash: Hash
    sender: Address
    sender_hash: Hash
    recipient: Address
    extension_data: Annotated[str, StringConstraints(pattern=r"^0x(?:[0-9a-f]{2}){0,1024}$")]


class Start(Frozen):
    token: Address
    sizes_raw: Annotated[tuple[Size, ...], Field(min_length=1, max_length=16)]
    capital_raw: Raw | None
    commitments_raw: Raw | None
    reserve_raw: Raw | None
    costs: tuple[IncrementalCost, ...]

    @model_validator(mode="after")
    def scope(self) -> Self:
        if len(set(self.sizes_raw)) != len(self.sizes_raw):
            raise ValueError("duplicate_sizes")
        if len(self.costs) != len(COST_KINDS) or {c.kind for c in self.costs} != COST_KINDS:
            raise ValueError("each_incremental_cost_required_once")
        return self


class CrossVenuePlan(Frozen):
    schema_version: Literal["oasis-metric-v3-plan-v1"] = Field(alias="schema")
    scenario: Literal["synthetic", "historical"]
    chain_id: Annotated[int, Field(gt=0, lt=1 << 64)]
    sources: Annotated[tuple[Label, ...], Field(min_length=1, max_length=2)]
    assets: tuple[Asset, Asset]
    metric: MetricDeployment
    v3: Pool
    starts: tuple[Start, Start]
    window: Window
    max_queries: Annotated[int, Field(ge=1, le=256)]

    @model_validator(mode="after")
    def scope(self) -> Self:
        currencies = {a.address for a in self.assets}
        if len(currencies) != 2 or any(
            a.address in (None, ZERO_ADDRESS) or a.decimals > 36 or a.runtime_hash is None
            for a in self.assets
        ):
            raise ValueError("two_pinned_erc20_assets_required")
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("duplicate_sources")
        if {s.token for s in self.starts} != currencies:
            raise ValueError("both_start_assets_required")
        m, v = self.metric, self.v3
        if {m.token0, m.token1} != currencies or {v.token0, v.token1} != currencies:
            raise ValueError("venue_asset_pair_mismatch")
        if v.protocol != "v3" or any(
            value is None
            for value in (v.runtime_hash, v.factory_hash, v.quoter_hash, v.factory, v.quoter, v.fee)
        ):
            raise ValueError("pinned_v3_deployment_required")
        if m.pool == v.address or {m.pool, v.address} & currencies:
            raise ValueError("distinct_swap_targets_required")
        if ZERO_ADDRESS in (
            m.pool,
            m.factory,
            m.quoter,
            m.sender,
            m.recipient,
            v.factory,
            v.quoter,
        ):
            raise ValueError("nonzero_contract_context_required")
        if self.window.expected_hash is None or self.window.cutoff_utc is None:
            raise ValueError("explicit_block_hash_and_cutoff_required")
        utc(self.window.cutoff_utc)
        return self


def metric_calldata(deployment: MetricDeployment, token_in: str, amount: int) -> str:
    if token_in not in (deployment.token0, deployment.token1) or not 0 < amount < INT128:
        raise Unavailable("metric_input_outside_scope")
    return calldata(
        LIVE_SIGNATURE,
        LIVE_TYPES,
        (
            deployment.pool,
            deployment.sender,
            deployment.recipient,
            token_in == deployment.token0,
            amount,
            0,
            bytes.fromhex(deployment.extension_data[2:]),
        ),
    )


def verify_metric(reads: Reads, plan: CrossVenuePlan) -> tuple[str, ...]:
    m = plan.metric
    refs: tuple[str, ...] = ()
    for target, expected in (
        (m.pool, m.pool_hash),
        (m.factory, m.factory_hash),
        (m.quoter, m.quoter_hash),
        (m.sender, m.sender_hash),
    ):
        refs += verify_code(reads, target, expected)
    raw, current = call(reads, m.pool, calldata("getImmutables()"))
    values = abi(IMMUTABLE_TYPES, raw)
    decimals = {a.address: a.decimals for a in plan.assets}
    precision = max(18, *decimals.values())
    if (
        "0x" + keccak(raw).hex() != m.immutables_hash
        or values[:3] != (m.factory, m.token0, m.token1)
        or values[3:5]
        != (10 ** (precision - decimals[m.token0]), 10 ** (precision - decimals[m.token1]))
    ):
        raise Unavailable("metric_immutable_identity_mismatch")
    return refs + current


def metric_quote(
    reads: Reads, plan: CrossVenuePlan, token: str, amount: int
) -> tuple[int, tuple[str, ...]]:
    raw, refs = call(reads, plan.metric.quoter, metric_calldata(plan.metric, token, amount))
    consumed, output = abi(("uint256", "uint256"), raw)
    if consumed != amount:
        raise Unavailable("metric_partial_or_excess_input")
    # Pool deltas are signed int128, including the negative output delta.
    if not 0 < output <= INT128:
        raise Unavailable("metric_output_outside_delta_bounds")
    return output, refs


def _reason(exc: ValueError | TypeError) -> str:
    return exc.reason if isinstance(exc, Unavailable) else "malformed_retained_evidence"


def _point(
    plan: CrossVenuePlan,
    reads: Reads,
    start: Start,
    size: int,
    order: tuple[str, str],
    identity: tuple[str, ...],
    blocked: str | None,
) -> dict[str, Any]:
    assets = {a.currency: a.model_dump() for a in plan.assets}
    point: dict[str, Any] = {
        "start_asset": assets[start.token],
        "principal_raw": size,
        "order": order,
        "legs": [],
        "output_raw": None,
        "gross_raw": None,
        "net_estimate_raw": None,
        "quote_complete": False,
        "reasons": [],
        "identity_evidence": identity,
    }
    reasons = point["reasons"]
    if blocked:
        reasons.append(blocked)
        return point
    values = [c.raw for c in start.costs]
    cost = sum(n for n in values if n is not None)
    if None in values:
        reasons.append("incremental_cost_unknown")
    if None in (start.capital_raw, start.commitments_raw, start.reserve_raw):
        reasons.append("funding_or_reservations_unknown")
    else:
        assert start.capital_raw is not None and start.commitments_raw is not None
        assert start.reserve_raw is not None
        # Reserve is additional untouched inventory; every candidate is an alternative.
        if size + cost + start.commitments_raw + start.reserve_raw > start.capital_raw:
            reasons.append("insufficient_uncommitted_inventory")
            return point
    amount, token = size, start.token
    for venue in order:
        out_token = next(a for a in assets if a != token)
        try:
            output, refs = (
                metric_quote(reads, plan, token, amount)
                if venue == "metric"
                else quote(reads, plan.v3, token, amount, None)
            )
        except (ValueError, TypeError) as exc:
            reasons.append(_reason(exc))
            return point
        point["legs"].append(
            {
                "venue": venue,
                "pool": plan.metric.pool if venue == "metric" else plan.v3.address,
                "asset_in": assets[token],
                "asset_out": assets[out_token],
                "requested_input_raw": amount,
                "consumed_input_raw": amount,
                "input_completeness": "returned_exact_amount"
                if venue == "metric"
                else "canonical_v3_open_limit_not_reached",
                "output_raw": output,
                "evidence": refs,
            }
        )
        amount, token = output, out_token
    point.update(output_raw=amount, gross_raw=amount - size, quote_complete=True)
    if not reasons:
        point["net_estimate_raw"] = amount - size - cost
    return point


def evaluate(plan: CrossVenuePlan, records: tuple[Export, ...]) -> dict[str, Any]:
    endpoints: dict[str, set[str]] = {}
    for record in records:
        if record.source not in plan.sources:
            raise ValueError("record_outside_source_scope")
        endpoints.setdefault(record.source, set()).add(record.endpoint_sha256)
    if any(len(v) != 1 for v in endpoints.values()) or len(
        {next(iter(v)) for v in endpoints.values()}
    ) != len(endpoints):
        raise ValueError("source_aliases_must_have_distinct_endpoints")
    assert plan.window.cutoff_utc is not None
    cutoff = utc(plan.window.cutoff_utc)
    reads = Reads(records, plan.sources, plan.max_queries, cutoff)
    blocked = None
    identity: tuple[str, ...] = ()
    block = None
    try:
        reads.chain(plan.chain_id)
        raw, _ = reads.get("eth_getBlockByNumber", hex(plan.window.block), False)
        snapshot = header(raw, plan.chain_id, plan.window.block)
        if snapshot.ref.block_hash != plan.window.expected_hash:
            raise Unavailable("planned_block_hash_mismatch")
        if (snapshot.ref.timestamp or 0) > int(cutoff.timestamp()):
            raise Unavailable("block_timestamp_after_cutoff")
        reads.bind(snapshot.ref)
        block = asdict(snapshot)
        for asset in plan.assets:
            identity += verify_asset(reads, asset)
        identity += verify_metric(reads, plan)
        identity += verify_pool(reads, plan.v3) + verify_quoter(reads, plan.v3)
    except (ValueError, TypeError) as exc:
        blocked = _reason(exc)
    points = [
        _point(plan, reads, start, size, order, identity, blocked)
        for start in plan.starts
        for size in sorted(start.sizes_raw)
        for order in (("metric", "v3"), ("v3", "metric"))
    ]
    used = {ref["id"] for query in reads.queries for ref in query["evidence"]}
    if reads.block is not None and any(
        r.identity in used
        and r.method != "eth_chainId"
        and r.fetched is not None
        and int(r.fetched.timestamp()) < (reads.block.timestamp or 0)
        for r in records
    ):
        # A retained observation cannot predate the block whose state it claims to read.
        # Clear all derived quotes after a context-integrity failure, keeping raw provenance.
        block = None
        points = [
            _point(plan, reads, start, size, order, (), "retrieval_precedes_block_timestamp")
            for start in plan.starts
            for size in sorted(start.sizes_raw)
            for order in (("metric", "v3"), ("v3", "metric"))
        ]
    complete = [p for p in points if p["net_estimate_raw"] is not None]
    return {
        "schema": "oasis-metric-v3-report-v1",
        "evidence_class": "synthetic" if plan.scenario == "synthetic" else "parsed_retained_rpc",
        "plan_sha256": sha256_hex(canonical_json(plan.model_dump(by_alias=True, mode="json"))),
        "chain_id": plan.chain_id,
        "block": block,
        "cutoff_utc": plan.window.cutoff_utc,
        "sources": plan.sources,
        "independent_providers_verified": False,
        "original_http_bytes_verified": False,
        "first_observed_utc": None,
        "metric_context": plan.metric.model_dump(),
        "points": points,
        "planned_points": len(points),
        "quote_complete_points": sum(p["quote_complete"] for p in points),
        "cost_complete_points": len(complete),
        "positive_estimates": sum(p["net_estimate_raw"] > 0 for p in complete),
        "decision": "positive_conditional_quote"
        if any(p["net_estimate_raw"] > 0 for p in complete)
        else "no_positive_quote"
        if len(complete) == len(points)
        else "incomplete_evidence",
        "queries": reads.queries,
        "query_count": len(reads.queries),
        "max_queries": plan.max_queries,
        "evidence": [r.manifest() for r in records],
        "provider_revert_selectors": sorted(
            {
                r.error_selector
                for r in records
                if r.error_selector is not None and r.identity in used
            }
        ),
        "points_are_mutually_exclusive": True,
        "execution_eligible": False,
        "atomic_simulation": "unavailable",
        "realized_pnl_raw": None,
        "limitations": [
            "independent_hops_at_block_end",
            "no_cross_hop_state_effects",
            "no_builder_update_or_ordering_proof",
            "no_payment_or_settlement_proof",
            "runtime_pins_do_not_prove_source_equivalence_or_proxy_implementation",
        ],
    }
