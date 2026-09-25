"""Replay agreed v4 activity into lagged prices and matured core-flow markouts.

No inventory, independent oracle, owner settlement or profitability is inferred.
PoolManager Swap deltas are negated exactly once to obtain pool-side flows.
"""

from bisect import bisect_left
from dataclasses import asdict, dataclass

from ..evidence.ingestion import replay_block_range_evidence
from ..evidence.store import EvidenceStore
from ..models.common import BlockRef, EvidenceRef, canonical_json, normalize_address
from ..models.v4 import PoolKey, SwapDirection
from ..rpc.collector import require_provider_agreement
from ..uniswap_v4.events import SWAP_TOPIC, decode_swap
from ..uniswap_v4.pool_id import pool_id_from_key
from .features import AgreedInteger, LpFeatureWindow, SwapMarkout, agreed_value_payload
from .pool_state_capture import Slot0WindowQuorum, replay_slot0_window_evidence


@dataclass(frozen=True, slots=True)
class MaturedMarkout:
    transaction_hash: str
    log_index: int
    event_block: BlockRef
    value: SwapMarkout


@dataclass(frozen=True, slots=True)
class MarketWindow:
    chain_id: int
    pool_id: str
    decision_block: BlockRef
    prices: tuple[AgreedInteger, ...]
    matured: tuple[MaturedMarkout, ...]
    pending_count: int
    swap_count: int
    horizon_seconds: int
    max_lateness_seconds: int
    input_references: tuple[EvidenceRef, ...]
    event_cutoff_timestamp: int | None = None
    deferred_count: int = 0

    @property
    def markouts_complete(self) -> bool:
        return self.pending_count == 0

    def feature_window(
        self,
        *,
        inventory0: AgreedInteger,
        inventory1: AgreedInteger,
        reference_price_x192: AgreedInteger,
    ) -> LpFeatureWindow:
        """Compose acquired market inputs with separately verified account/oracle inputs.

        The caller still owns the inventory and independent-price acquisition proofs;
        names and block binding cannot substitute for those verifiers. Pending markouts
        block composition rather than biasing toxicity by dropping the newest swaps.
        """
        if not self.markouts_complete:
            raise ValueError("unmatured or late markouts block LP feature composition")
        for expected, value in (
            ("inventory0", inventory0),
            ("inventory1", inventory1),
            ("reference_price_x192", reference_price_x192),
        ):
            if value.name != expected:
                raise ValueError("LP account/reference input role mismatch")
        return LpFeatureWindow(
            self.decision_block,
            self.prices,
            inventory0,
            inventory1,
            reference_price_x192,
            tuple(item.value for item in self.matured),
        )

    @property
    def payload(self) -> bytes:
        return canonical_json(
            {
                "schema": "oasis.lp-market-window.v2",
                "execution_eligible": False,
                **asdict(self),
                "markouts_complete": self.markouts_complete,
                "missing_lp_inputs": [
                    "independent_reference_price",
                    "reconciled_inventory",
                    "route_depth",
                    "costs_and_economic_acceptance",
                ],
            }
        )


def validate_market_successor(previous: MarketWindow, current: MarketWindow) -> None:
    """Require coherent overlapping windows; a later head alone cannot heal a gap."""
    if (
        previous.chain_id != current.chain_id
        or previous.pool_id != current.pool_id
        or previous.horizon_seconds != current.horizon_seconds
        or previous.max_lateness_seconds != current.max_lateness_seconds
        or (previous.event_cutoff_timestamp is None) != (current.event_cutoff_timestamp is None)
    ):
        raise ValueError("market sequence scope or maturity policy changed")
    if current.decision_block.number <= previous.decision_block.number:
        raise ValueError("market sequence head did not advance")
    prior = {value.block.number: value for value in previous.prices}
    first = current.prices[0].block.number
    if first > previous.decision_block.number:
        raise ValueError("market sequence requires overlap to verify continuity")
    if previous.event_cutoff_timestamp is not None and (
        current.prices[0].block.timestamp is None
        or current.prices[0].block.timestamp > previous.event_cutoff_timestamp
    ):
        raise ValueError("market sequence lost coverage of the deferred prefix")
    for value in current.prices:
        old = prior.get(value.block.number)
        if old is not None and (old.block != value.block or old.value != value.value):
            raise ValueError("market sequence overlapping state changed")
    if (
        previous.event_cutoff_timestamp is not None
        and current.event_cutoff_timestamp is not None
        and current.event_cutoff_timestamp <= previous.event_cutoff_timestamp
    ):
        raise ValueError("market sequence maturity cutoff did not advance")


def build_market_window(
    store: EvidenceStore,
    *,
    swap_references: tuple[EvidenceRef, EvidenceRef],
    price_references: tuple[EvidenceRef, EvidenceRef],
    pool: PoolKey,
    pool_manager: str,
    horizon_seconds: int,
    max_lateness_seconds: int,
    mature_prefix: bool = False,
) -> MarketWindow:
    """Require complete agreeing block windows and use only prices available by close.

    A markout uses the first block timestamp at or after event time + horizon.
    Pending/late samples are retained as counts, never silently filled with zero.
    These are core pool deltas, not owner-cash attribution or a realized PnL ledger.
    """
    for value in (horizon_seconds, max_lateness_seconds):
        if type(value) is not int or not 1 <= value <= 86400:
            raise ValueError("explicit markout horizon/lateness must be 1..86400 seconds")
    if len(swap_references) != 2 or len(price_references) != 2:
        raise ValueError("exactly two sources for activity and prices required")
    if type(mature_prefix) is not bool:
        raise TypeError("mature-prefix mode must be a boolean")
    manager = normalize_address(pool_manager)
    pool_id = pool_id_from_key(pool)
    datasets = tuple(replay_block_range_evidence(store, ref) for ref in swap_references)
    require_provider_agreement(datasets)
    if any(ref.kind != "slot0_window" for ref in price_references):
        raise ValueError("slot0 price evidence kind required")
    captures = tuple(replay_slot0_window_evidence(store, ref) for ref in price_references)
    Slot0WindowQuorum(*captures)
    for ref, capture in zip(price_references, captures, strict=True):
        if ref.source != capture.source:
            raise ValueError("price evidence source mismatch")
    if {item.source for item in datasets} != {item.source for item in captures}:
        raise ValueError("market evidence requires the same two provider sources")
    dataset = datasets[0]
    for capture in captures:
        if (
            capture.pool_id != pool_id
            or capture.pool_manager != manager
            or capture.headers != dataset.blocks
        ):
            raise ValueError("price and activity scope/snapshot mismatch")
    if dataset.address != manager or dataset.topics != (SWAP_TOPIC, pool_id):
        raise ValueError("market activity must use exact manager and pool Swap filter")
    if len(dataset.blocks) < 2:
        raise ValueError("market feature window requires at least two contiguous blocks")
    timestamps = tuple(header.ref.timestamp for header in dataset.blocks)
    if any(value is None for value in timestamps):
        raise ValueError("market timestamps required")
    times = tuple(int(value) for value in timestamps if value is not None)
    cutoff = times[-1] - horizon_seconds - max_lateness_seconds if mature_prefix else None
    if cutoff is not None and cutoff < times[0]:
        raise ValueError("window does not cover the predeclared maturity tail")
    refs_by_source = {ref.source: ref for ref in price_references}
    price_cache: dict[tuple[str, int], AgreedInteger] = {}

    def agreed(name: str, index: int) -> AgreedInteger:
        key = (name, index)
        if key not in price_cache:
            block = dataset.blocks[index].ref
            sqrt = captures[0].slot0[index].sqrt_price_x96
            if not 0 < sqrt < 2**160:
                raise ValueError("positive bounded market price required")
            value = sqrt if name == "sqrt_price_x96" else sqrt * sqrt
            payload = agreed_value_payload(name, value, block)
            interpretation = {
                "schema": "oasis.derived-slot0-integer.v1",
                "pool_id": pool_id,
                "name": name,
                "value": value,
                "parents": [
                    asdict(ref) for ref in sorted(price_references, key=lambda r: r.source)
                ],
            }
            references = []
            for capture in captures:
                parent = refs_by_source[capture.source]
                record = store.get_record(parent)
                references.append(
                    store.put(
                        "derived_slot0_integer",
                        payload,
                        block,
                        capture.source,
                        request=record.request_payload,
                        raw_response=record.raw_response_payload,
                        interpretation=interpretation,
                        retrieved_at=parent.retrieved_at,
                    )
                )
            price_cache[key] = AgreedInteger(name, value, block, tuple(references))
        return price_cache[key]

    # Validate all events before retaining any derived interpretation.
    swaps = tuple(decode_swap(log) for log in dataset.logs)
    if any(swap.amount0 * swap.amount1 >= 0 for swap in swaps):
        raise ValueError("market markout requires opposite nonzero caller deltas")
    prices = tuple(agreed("sqrt_price_x96", index) for index in range(len(times)))
    matured: list[MaturedMarkout] = []
    pending = deferred = 0
    for log, swap in zip(dataset.logs, swaps, strict=True):
        event_index = swap.block_number - dataset.start_block
        if cutoff is not None and times[event_index] > cutoff:
            deferred += 1
            continue
        target_time = times[event_index] + horizon_seconds
        price_index = bisect_left(times, target_time)
        if price_index == len(times) or times[price_index] - target_time > max_lateness_seconds:
            pending += 1
            continue
        markout = SwapMarkout(
            SwapDirection.ZERO_FOR_ONE if swap.amount0 < 0 else SwapDirection.ONE_FOR_ZERO,
            -swap.amount0,
            -swap.amount1,
            agreed("future_price_x192", price_index),
        )
        matured.append(
            MaturedMarkout(
                log.transaction_hash, log.log_index, dataset.blocks[event_index].ref, markout
            )
        )
    return MarketWindow(
        dataset.chain_id,
        pool_id,
        dataset.blocks[-1].ref,
        prices,
        tuple(matured),
        pending,
        len(swaps),
        horizon_seconds,
        max_lateness_seconds,
        (*swap_references, *price_references),
        cutoff,
        deferred,
    )
