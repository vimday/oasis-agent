"""Bound the pending same-head shadow work and reuse verified parent simulations."""

from collections import OrderedDict
from threading import Condition, Lock

from .base_policy import BasePolicySimulation, _direct
from .batch_shadow import BatchShadowHead

_MAX_CACHED_RESULTS = 256


class BaseHeadQueue[T]:
    """Single-consumer queue with one pending head. Running RPC finishes normally."""

    def __init__(self) -> None:
        self._ready = Condition()
        self._last: BatchShadowHead | None = None
        self._pending: tuple[BatchShadowHead, T] | None = None
        self._closed = False

    def offer(self, head: BatchShadowHead, work: T) -> tuple[BatchShadowHead, T] | None:
        """Publish new work and return any pending work it superseded."""
        with self._ready:
            if self._closed:
                raise RuntimeError("Base head queue is closed")
            last = self._last
            if last is not None and (
                head.height <= last.height
                or head.observed_ns <= last.observed_ns
                or head.block_hash == last.block_hash
                or (head.height == last.height + 1 and not _direct(last, head))
            ):
                raise ValueError("Base realtime head order or parent hash conflicts")
            replaced = self._pending
            self._last = head
            self._pending = (head, work)
            self._ready.notify()
            return replaced

    def take(self) -> tuple[BatchShadowHead, T] | None:
        with self._ready:
            self._ready.wait_for(lambda: self._pending is not None or self._closed)
            ready = self._pending
            self._pending = None
            return ready

    def close(self, *, discard_pending: bool = False) -> tuple[BatchShadowHead, T] | None:
        """Shut down the consumer after draining the latest head or explicitly abandon it."""
        with self._ready:
            self._closed = True
            abandoned = self._pending if discard_pending else None
            if discard_pending:
                self._pending = None
            self._ready.notify_all()
            return abandoned


class BasePriorCache:
    """In-memory result cache for the exact envelope on the observed direct parent."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._results: OrderedDict[tuple[str, str], BasePolicySimulation] = OrderedDict()

    def record(self, result: BasePolicySimulation) -> None:
        key = (result.head.block_hash, result.envelope_sha256)
        with self._lock:
            if (existing := self._results.get(key)) is not None:
                if existing != result:
                    raise ValueError("conflicting Base simulation in parent cache")
                return
            self._results[key] = result
            if len(self._results) > _MAX_CACHED_RESULTS:
                self._results.popitem(last=False)

    def lookup(
        self, head: BatchShadowHead, envelope_sha256: str, *, as_of_ns: int
    ) -> BasePolicySimulation | None:
        if type(as_of_ns) is not int or not head.observed_ns <= as_of_ns < 1 << 64:
            raise ValueError("parent cache lookup precedes the observed head")
        with self._lock:
            result = self._results.get((head.parent_hash, envelope_sha256))
        if result is None or not _direct(result.head, head) or result.available_ns > as_of_ns:
            return None
        return result
