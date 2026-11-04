"""Concurrent providers, sequential reads within each frozen provider budget."""

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from ..evidence.exact_capture import RecordedSession, validate_session_pair


def acquire_pair(
    pair: tuple[RecordedSession, RecordedSession],
    calls: Sequence[tuple[str, list[object]]],
) -> None:
    """Only transport/recorder work runs in threads; evidence writes stay with caller.

    A worker stops before its next read once it observes a peer's failure. A read
    already past that check may still complete; cancellation does not interrupt
    transport. Both workers are joined, and failures never accept a partial quorum.
    """
    validate_session_pair(*pair)
    cancelled = Event()

    def acquire(session: RecordedSession) -> None:
        for method, params in calls:
            if cancelled.is_set():
                return
            try:
                session[0].request(method, params)
            except BaseException:
                cancelled.set()
                raise

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="live-rpc") as executor:
        futures = [executor.submit(acquire, session) for session in pair]
        for future in futures:
            future.result()
