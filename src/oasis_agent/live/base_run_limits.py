"""Timing and tick limits shared by the Base supervisor and its read-only dashboard."""

MAX_TICKS = 3
POLL_SECONDS = 300


def wait_seconds(ticks: int) -> int:
    if type(ticks) is not int or not 1 <= ticks <= MAX_TICKS:
        raise ValueError("Base runs require one to three ticks")
    return 210 * ticks + POLL_SECONDS * (ticks - 1)


def authority_ttl(ticks: int) -> int:
    return wait_seconds(ticks) - 30
