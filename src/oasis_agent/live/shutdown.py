"""Explicit SIGTERM handling for the live container's Python PID 1."""

import signal
from collections.abc import Callable


class _Terminated(BaseException):
    """Unwind past per-request error handling without continuing an action."""


def _terminate(_signum: int, _frame: object) -> None:
    raise _Terminated


def run_with_term_handler(run: Callable[[], None]) -> bool:
    """Return false on operator TERM after context managers have unwound."""
    # Default SIGTERM is ignored for Linux PID 1. A handler also interrupts
    # blocking I/O so the service lock and durable journal can unwind.
    previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        run()
    except _Terminated:
        return False
    finally:
        signal.signal(signal.SIGTERM, previous)
    return True
