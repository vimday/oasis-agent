"""Bounded, fair candidate batches; events prioritize fresh validation only."""

from collections import deque


class PoolSchedule:
    def __init__(self, batch_size: int = 8):
        if type(batch_size) is not int or batch_size < 2:
            raise ValueError("batch size must be at least two")
        self.batch_size = batch_size
        self._queue: deque[str] = deque()
        self._urgent: set[str] = set()

    def select(self, pools: list[str], signals: set[str]) -> list[str]:
        ordered = list(dict.fromkeys(pool.lower() for pool in pools))
        active = set(ordered)
        self._queue = deque(pool for pool in self._queue if pool in active)
        known = set(self._queue)
        self._queue.extend(pool for pool in ordered if pool not in known)
        self._urgent = (self._urgent | {pool.lower() for pool in signals}) & active
        # Reserve at least half each batch for FIFO progress. An event flood
        # must not permanently starve quiet pools, and signals never imply entry.
        chosen = [pool for pool in self._queue if pool in self._urgent][: self.batch_size // 2]
        selected = set(chosen)
        for pool in self._queue:
            if len(chosen) >= self.batch_size:
                break
            if pool not in selected:
                chosen.append(pool)
                selected.add(pool)
        self._queue = deque(pool for pool in self._queue if pool not in selected)
        self._queue.extend(chosen)
        self._urgent.difference_update(selected)
        return chosen
