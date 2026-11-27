from threading import Thread

import pytest

from oasis_agent.fastpath.base_policy import BasePolicySimulation
from oasis_agent.fastpath.base_realtime import BaseHeadQueue, BasePriorCache
from oasis_agent.fastpath.batch_shadow import BatchShadowHead


def _head(height: int, tag: int, parent: int, observed: int) -> BatchShadowHead:
    return BatchShadowHead(
        height=height,
        block_hash=f"0x{tag:064x}",
        parent_hash=f"0x{parent:064x}",
        observed_ns=observed,
    )


P = _head(20, 1, 0, 100)
H = _head(21, 2, 1, 200)
C = _head(22, 3, 2, 300)
ENVELOPE = "a" * 64


def test_only_pending_head_is_superseded_while_a_request_is_running() -> None:
    queue: BaseHeadQueue[str] = BaseHeadQueue()
    assert queue.offer(P, "running") is None
    assert queue.take() == (P, "running")
    assert queue.offer(H, "expired") is None
    assert queue.offer(C, "latest") == (H, "expired")
    assert queue.close() is None
    assert queue.take() == (C, "latest")
    assert queue.take() is None


def test_close_releases_consumer_and_can_discard_unstarted_work() -> None:
    queue: BaseHeadQueue[str] = BaseHeadQueue()
    observed: list[tuple[BatchShadowHead, str] | None] = []
    worker = Thread(target=lambda: observed.append(queue.take()), daemon=True)
    worker.start()
    queue.close()
    worker.join(timeout=1)
    assert not worker.is_alive() and observed == [None]
    with pytest.raises(RuntimeError, match="queue is closed"):
        queue.offer(P, "closed")
    other: BaseHeadQueue[str] = BaseHeadQueue()
    other.offer(P, "pending")
    assert other.close(discard_pending=True) == (P, "pending")
    assert other.take() is None


@pytest.mark.parametrize("bad", [_head(20, 4, 0, 200), _head(21, 5, 6, 300), _head(21, 5, 1, 99)])
def test_bad_head_order_cannot_remove_pending_work(bad: BatchShadowHead) -> None:
    queue: BaseHeadQueue[str] = BaseHeadQueue()
    queue.offer(P, "pending")
    with pytest.raises(ValueError, match="conflicts"):
        queue.offer(bad, "bad")
    assert queue.take() == (P, "pending")


def test_cache_only_reuses_direct_parent_and_exact_envelope_when_available() -> None:
    cache = BasePriorCache()
    simulation = BasePolicySimulation(
        head=P,
        envelope_sha256=ENVELOPE,
        available_ns=250,
        disposition="settled",
        settlement_delta_wei=21,
        modeled_network_fee_wei=20,
    )
    cache.record(simulation)
    cache.record(simulation)
    assert cache.lookup(H, ENVELOPE, as_of_ns=200) is None
    assert cache.lookup(H, ENVELOPE, as_of_ns=249) is None
    assert cache.lookup(H, ENVELOPE, as_of_ns=250) == simulation
    assert cache.lookup(H, "b" * 64, as_of_ns=250) is None
    assert cache.lookup(C, ENVELOPE, as_of_ns=300) is None
    assert cache.lookup(_head(21, 5, 4, 200), ENVELOPE, as_of_ns=250) is None
    with pytest.raises(ValueError, match="precedes"):
        cache.lookup(H, ENVELOPE, as_of_ns=199)
    with pytest.raises(ValueError, match="conflicting"):
        cache.record(simulation.model_copy(update={"settlement_delta_wei": 22}))
