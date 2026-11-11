"""Replay a sealed shadow window from exact block-range evidence."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from ..evidence.ingestion import replay_block_range_evidence
from ..evidence.rpc_validation import restore_exchanges
from ..evidence.store import EvidenceStore
from ..models.chain import BlockSnapshot
from ..models.common import EvidenceRef, canonical_json, sha256_hex
from ..uniswap_v4.events import (
    MODIFY_LIQUIDITY_TOPIC,
    POOL_INITIALIZED_TOPIC,
    SWAP_TOPIC,
    decode_pool_initialized,
)
from ..uniswap_v4.replay import bind_pool_initialization
from .cycle_case import SOLIDLY_SYNC, V3_SWAP
from .cycle_evidence import Export
from .cycle_plan import BlockNumber, Frozen, Hash, Label, Plan, utc
from .cycle_protocols import topic
from .cycle_research import QuoteScope, evaluate_windows, routes

V3_MINT = topic("Mint(address,address,int24,int24,uint128,uint256,uint256)")
V3_BURN = topic("Burn(address,int24,int24,uint128,uint256,uint256)")
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _floor_epoch_second(value: datetime) -> int:
    elapsed = value - EPOCH
    return elapsed.days * 86_400 + elapsed.seconds


class ShadowWindow(Frozen):
    schema_version: Literal["oasis-cycle-shadow-window-v1"] = Field(alias="schema")
    cycle_config_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    source: Label
    start_block: BlockNumber
    end_block: BlockNumber
    parent_hash: Hash
    locked_utc: str
    deadline_utc: str
    max_requests: Annotated[int, Field(ge=1, le=512)]
    discover_v4_pools: bool = False
    quote_checkpoint_at_end: bool = False

    @model_validator(mode="after")
    def check_window(self) -> Self:
        if not 1 <= self.end_block - self.start_block + 1 <= 16 or utc(self.locked_utc) >= utc(
            self.deadline_utc
        ):
            raise ValueError("invalid shadow window")
        return self


@dataclass(frozen=True, slots=True)
class Filter:
    pool: str | None
    event: str
    address: str
    topics: tuple[str, ...]


def filters(plan: Plan, *, discover_v4_pools: bool = False) -> dict[str, Filter]:
    result = {}
    for pool in plan.pools:
        watches: dict[str, tuple[str, ...]]
        if pool.protocol == "v4":
            assert pool.pool_id is not None
            watches = {
                "swap": (SWAP_TOPIC, pool.pool_id),
                "liquidity": (MODIFY_LIQUIDITY_TOPIC, pool.pool_id),
            }
            if discover_v4_pools:
                result[f"v4_initialize.{pool.address}"] = Filter(
                    None,
                    "initialize",
                    pool.address,
                    (POOL_INITIALIZED_TOPIC,),
                )
        elif pool.protocol == "v3":
            watches = {"swap": (V3_SWAP,), "mint": (V3_MINT,), "burn": (V3_BURN,)}
        else:
            watches = {"sync": (SOLIDLY_SYNC,)}
        for event, topics in watches.items():
            result[f"{pool.name}.{event}"] = Filter(pool.name, event, pool.address, topics)
    return result


def shadow_config_sha256(plan: Plan) -> str:
    return sha256_hex(canonical_json(plan.model_dump(by_alias=True, mode="json")))


def replay_shadow(
    plan: Plan,
    window: ShadowWindow,
    store: EvidenceStore,
    refs: Mapping[str, EvidenceRef],
    *,
    quote_records: tuple[Export, ...] | None = None,
) -> dict[str, Any]:
    if (
        window.cycle_config_sha256 != shadow_config_sha256(plan)
        or window.source not in plan.sources
    ):
        raise ValueError("shadow plan binding mismatch")
    watches = filters(plan, discover_v4_pools=window.discover_v4_pools)
    if refs.keys() - watches.keys():
        raise ValueError("unexpected shadow filter")
    max_calls = len(watches) * (window.end_block - window.start_block + 4)
    if max_calls > window.max_requests:
        raise ValueError("frozen shadow window exceeds request allowance")
    locked, deadline = utc(window.locked_utc), utc(window.deadline_utc)
    missing = sorted(watches.keys() - refs.keys())
    quote_filters = {name for name, watch in watches.items() if watch.pool is not None}
    quote_missing = sorted(quote_filters - refs.keys())
    discovery_missing = sorted(set(missing) - quote_filters)
    observed: tuple[BlockSnapshot, ...] = ()
    wakeups: dict[int, dict[str, Any]] = {}
    discoveries: dict[tuple[str, str], dict[str, Any]] = {}
    discovery_event_count = 0
    currencies = {asset.currency for asset in plan.assets}
    coverage: dict[str, str] = dict.fromkeys(missing, "missing")
    seen = set()
    used_calls = 0
    for name, ref in sorted(refs.items()):
        watch = watches[name]
        dataset = replay_block_range_evidence(store, ref)
        if (
            (dataset.chain_id, dataset.source, dataset.start_block, dataset.end_block)
            != (plan.chain_id, window.source, window.start_block, window.end_block)
            or (dataset.address, dataset.topics) != (watch.address, watch.topics)
            or dataset.blocks[0].parent_hash != window.parent_hash
            or (observed and dataset.blocks != observed)
        ):
            raise ValueError("shadow evidence differs from frozen scope or observed ancestry")
        first, last = dataset.blocks[0].ref.timestamp, dataset.blocks[-1].ref.timestamp
        assert first is not None and last is not None
        retrieved = utc(ref.retrieved_at)
        if not (
            _floor_epoch_second(locked) < first <= last <= _floor_epoch_second(retrieved)
            and retrieved <= deadline
        ):
            raise ValueError("shadow capture lies outside the declared time window")
        wire = store.get_record(ref)
        assert wire.request_payload is not None
        used_calls += len(restore_exchanges(wire.request_payload, wire.raw_response_payload))
        if used_calls > window.max_requests:
            raise ValueError("shadow evidence exceeds total request allowance")
        observed = dataset.blocks
        by_height = {block.ref.number: block for block in observed}
        coverage[name] = "observed_events" if dataset.logs else "observed_empty"
        for log in dataset.logs:
            event_id = (log.block_hash, log.transaction_hash, log.log_index)
            if event_id in seen:
                raise ValueError("duplicate shadow event across filters")
            seen.add(event_id)
            if watch.event == "initialize":
                discovery_event_count += 1
                initialization = decode_pool_initialized(log)
                event = bind_pool_initialization(
                    by_height[log.block_number].ref,
                    log,
                    initialization,
                )
                key = initialization.key
                if {key.currency0, key.currency1} <= currencies:
                    identity = (watch.address, initialization.pool_id)
                    if identity in discoveries or len(discoveries) >= 64:
                        raise ValueError("duplicate or excessive LP discoveries")
                    discoveries[identity] = {
                        "pool_manager": watch.address,
                        "event": asdict(event),
                        "status": "pool_initialization_observed",
                        "evidence_ref": asdict(ref),
                        "lp_access": None,
                        "active_liquidity": None,
                        "deployment_identity": "unverified",
                    }
                continue
            wakeup = wakeups.setdefault(
                log.block_number,
                {
                    "block": log.block_number,
                    "block_hash": log.block_hash,
                    "filters": set(),
                    "log_count": 0,
                },
            )
            wakeup["filters"].add(name)
            wakeup["log_count"] += 1
    scopes: dict[int, QuoteScope] = {}
    dispatches = []
    paths = tuple(route.names for route in routes(plan))
    selected_points = 0
    if not quote_missing:
        ready = max(utc(refs[name].retrieved_at) for name in quote_filters)
        cutoffs = {item.block: item.cutoff_utc for item in plan.windows}
        for block in observed:
            block_wakeup = wakeups.get(block.ref.number)
            checkpoint = window.quote_checkpoint_at_end and block.ref.number == window.end_block
            if block_wakeup is None and not checkpoint:
                continue
            changed = (
                {watches[name].pool for name in block_wakeup["filters"]} if block_wakeup else set()
            )
            affected = (
                frozenset(paths)
                if checkpoint
                else frozenset(path for path in paths if changed.intersection(path))
            )
            if not affected:
                continue
            point_count = len(affected) * len(plan.sizes_raw)
            selected_points += point_count
            cutoff = cutoffs.get(block.ref.number)
            eligible = cutoff is not None and ready <= utc(cutoff) <= deadline
            dispatches.append(
                {
                    "block": asdict(block),
                    "ready_utc": ready.isoformat(),
                    "paths": sorted(affected),
                    "quote_points": point_count,
                    "reason": "closing_checkpoint" if checkpoint else "pool_events",
                    "status": "ready_for_replay" if eligible else "missing_usable_quote_cutoff",
                }
            )
            if eligible:
                scopes[block.ref.number] = QuoteScope(block, affected)
    quote_windows = (
        evaluate_windows(plan, quote_records, window.cycle_config_sha256, scopes=scopes)
        if quote_records is not None and scopes
        else []
    )
    return {
        "schema": "oasis-cycle-shadow-review-v1",
        "scenario": plan.scenario,
        "window": window.model_dump(by_alias=True, mode="json"),
        "window_denominator": 1,
        "source_trust": "single_provider_research",
        "time_basis": "declared_lock_and_capture_clocks",
        "requests_in_retained_exchanges": used_calls,
        "coverage": coverage,
        "quote_missing_filters": quote_missing,
        "lp_missing_filters": discovery_missing,
        "evidence_refs": {name: asdict(ref) for name, ref in sorted(refs.items())},
        "status": "coverage_incomplete" if missing else "observed",
        "quote_wakeups": [
            {**item, "filters": sorted(item["filters"])} for _, item in sorted(wakeups.items())
        ]
        if not quote_missing
        else [],
        "quote_status": (
            "scoped_windows_replayed" if quote_windows else "separate_hash_pinned_quote_required"
        ),
        "lp_discoveries": list(discoveries.values()) if not discovery_missing else [],
        "initialize_events_observed": discovery_event_count,
        "quote_dispatches": dispatches,
        "quote_windows": quote_windows,
        "point_comparison": {
            "full_scan_points": len(paths)
            * len(plan.sizes_raw)
            * (window.end_block - window.start_block + 1),
            "affected_path_points": selected_points if not quote_missing else None,
            "basis": "planned_route_size_points" if not quote_missing else "coverage_incomplete",
        },
        "execution_eligible": False,
    }
