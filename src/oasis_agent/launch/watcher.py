"""Bounded event reducer for adapters; pending is discovery-only.

This is not a network subscriber. Preserve every provider observation while
coalescing candidate triggers. Log index prevents create/mint/burn events in
the same transaction from overwriting each other.
"""

from collections import OrderedDict
from typing import Literal

from .models import Address, Hash, Observation, Source, StrictModel, UInt


class PoolEvent(StrictModel):
    chain_id: Literal[8453]
    source: Source
    factory: Address
    pool: Address
    block_hash: Hash
    transaction_hash: Hash
    log_index: UInt
    phase: Literal["pending", "final"]
    kind: Literal["created", "liquidity_added", "liquidity_removed", "transfer", "position"]
    removed: bool


class CandidateWatcher:
    def __init__(self, capacity: int = 4096):
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("positive capacity required")
        self.capacity = capacity
        self.events: OrderedDict[tuple[object, ...], dict[str, PoolEvent]] = OrderedDict()
        self.stand_down = False

    def ingest(self, event: PoolEvent) -> bool:
        key = (
            event.chain_id,
            event.factory,
            event.pool,
            event.block_hash,
            event.transaction_hash,
            event.log_index,
            event.phase,
        )
        prior = self.events.get(key)
        self.stand_down |= event.removed
        if prior and any(
            item.kind != event.kind or item.removed != event.removed for item in prior.values()
        ):
            self.stand_down = True
        fresh = prior is None
        if fresh:
            self.events[key] = {}
        self.events[key][event.source] = event
        self.events.move_to_end(key)
        if len(self.events) > self.capacity:
            self.events.popitem(last=False)
            # Eviction means the dedup/reconciliation window is incomplete.
            self.stand_down = True
        return fresh and not self.stand_down


def coherent(observations: tuple[Observation, ...]) -> bool:
    return (
        len(observations) == 2
        and observations[0].source != observations[1].source
        and observations[0].snapshot == observations[1].snapshot
    )
