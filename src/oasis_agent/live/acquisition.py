"""Concurrent providers, sequential reads within each frozen provider budget."""

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from ..evidence.exact_capture import RecordedSession, validate_session_pair


def acquire_pair(
    pair: tuple[RecordedSession, RecordedSession],
    calls: Sequence[tuple[str, list[object]]],
    *,
    checkpoint: tuple[int, Callable[[], None]] | None = None,
) -> None:
    """Only transport/recorder work runs in threads; evidence writes stay with caller.

    A worker stops before its next read once it observes a peer's failure. A read
    already past that check may still complete; cancellation does not interrupt
    transport. Both workers are joined, and failures never accept a partial quorum.
    """
    validate_session_pair(*pair)
    cancelled = Event()

    if checkpoint is not None and not 0 < checkpoint[0] < len(calls):
        raise ValueError("checkpoint must split two nonempty phases")

    def acquire(session: RecordedSession, phase: Sequence[tuple[str, list[object]]]) -> None:
        for method, params in phase:
            if cancelled.is_set():
                return
            try:
                session[0].request(method, params)
            except BaseException:
                cancelled.set()
                raise

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="live-rpc") as executor:

        def phase(part: Sequence[tuple[str, list[object]]]) -> None:
            futures = [executor.submit(acquire, session, part) for session in pair]
            for future in futures:
                future.result()

        if checkpoint is None:
            phase(calls)
        else:
            boundary, verify = checkpoint
            phase(calls[:boundary])
            verify()
            phase(calls[boundary:])
