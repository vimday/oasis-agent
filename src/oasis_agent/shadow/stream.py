"""Bounded append-only JSONL consumption; incomplete writes are never decisions."""

import os
import time
from collections.abc import Iterator
from pathlib import Path


def cycle_lines(
    path: Path,
    *,
    follow: bool = False,
    poll_ms: int = 500,
    idle_seconds: int = 60,
    stop_file: Path | None = None,
) -> Iterator[bytes]:
    if type(poll_ms) is not int or not 10 <= poll_ms <= 60_000:
        raise ValueError("poll interval must be 10..60000 ms")
    if type(idle_seconds) is not int or not 1 <= idle_seconds <= 86_400:
        raise ValueError("idle deadline must be 1..86400 seconds")
    with path.open("rb") as source:
        identity = os.fstat(source.fileno())
        last_size = identity.st_size
        idle_since = time.monotonic()
        while True:
            if stop_file is not None and stop_file.exists():
                return
            current = path.stat()
            if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                raise ValueError("input rotated; start a new reviewed stream")
            offset = source.tell()
            if current.st_size < max(offset, last_size):
                raise ValueError("input truncated")
            last_size = current.st_size
            line = source.readline(1_000_001)
            if len(line) > 1_000_000:
                raise ValueError("cycle exceeds size bound")
            if line and (line.endswith(b"\n") or not follow):
                idle_since = time.monotonic()
                yield line
                continue
            if not follow:
                return
            # A producer may be between writes. Retry the same offset until a
            # newline closes the record; partial data does not reset the deadline.
            source.seek(offset)
            if time.monotonic() - idle_since >= idle_seconds:
                raise ValueError("LP input idle deadline exceeded")
            time.sleep(poll_ms / 1000)
