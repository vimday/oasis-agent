"""Concurrent providers, sequential reads within each frozen provider budget."""

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor

from ..evidence.exact_capture import RecordedSession, validate_session_pair


def acquire_pair(
    pair: tuple[RecordedSession, RecordedSession],
    calls: Sequence[tuple[str, list[object]]],
) -> None:
    """Only transport/recorder work runs in threads; evidence writes stay with caller.

    Both workers are joined even if one fails. A caller may validate and persist
    only after both complete; failures never return a partial accepted quorum.
    """
    validate_session_pair(*pair)

    def acquire(session: RecordedSession) -> None:
        for method, params in calls:
            session[0].request(method, params)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="live-rpc") as executor:
        futures = [executor.submit(acquire, session) for session in pair]
        for future in futures:
            future.result()
