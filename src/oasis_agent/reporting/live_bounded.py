"""Read one local bounded run declaration without Docker, keys or RPC."""

import os
import stat
import time
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from ..evidence.rpc_validation import strict_json_object
from ..models.common import canonical_json, sha256_hex

MAX_BYTES = 65_536
Role = Literal["strategy", "authority"]
RunResult = Literal["completed", "failed", "timed_out", "interrupted"]
Status = Literal[
    "unconfigured", "missing", "invalid", "launch_requested", "cleanup_unverified", "stopped"
]


class BoundedRunStatus(BaseModel):
    status: Status
    execution_eligible: Literal[False] = False
    attempt_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")] | None = None
    as_of: int | None = None
    result: RunResult | None = None
    stop_present: dict[Role, bool] | None = None


def _read(path: Path) -> dict[str, object]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("bounded local run declaration required")
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("run declaration exceeds bound")
    report = strict_json_object(raw)
    if (
        report.pop("evidence_hash", None) != sha256_hex(canonical_json(report))
        or report.get("execution_eligible") is not False
    ):
        raise ValueError("bounded run declaration digest or flags differ")
    return report


def read_bounded_run(path: Path | None, *, now: int | None = None) -> BoundedRunStatus:
    if path is None:
        return BoundedRunStatus(status="unconfigured")
    clock = int(time.time()) if now is None else now
    try:
        try:
            raw = _read(path / "report.json")
        except FileNotFoundError:
            attempt = _read(path / "attempt.json")
            started = attempt["started_at"]
            if (
                attempt["schema"] != "oasis.base-bounded-attempt.v1"
                or type(started) is not int
                or not 0 <= started <= clock
                or type(attempt["wait_seconds"]) is not int
                or attempt["wait_seconds"] != 210
            ):
                raise ValueError("invalid start declaration") from None
            return BoundedRunStatus.model_validate(
                {
                    "status": "launch_requested"
                    if clock - started <= 240
                    else "cleanup_unverified",
                    "attempt_id": attempt["attempt_id"],
                    "as_of": started,
                }
            )
        finished, cleanup = raw["finished_at"], raw["cleanup"]
        if (
            raw["schema"] != "oasis.base-bounded-run.v1"
            or type(finished) is not int
            or not 0 <= finished <= clock
            or not isinstance(cleanup, dict)
            or type(cleanup["verified"]) is not bool
            or type(cleanup["services_stopped"]) is not bool
        ):
            raise ValueError("invalid terminal declaration")
        stops = cleanup["stop_present"]
        if (
            not isinstance(stops, dict)
            or set(stops) != {"strategy", "authority"}
            or any(type(value) is not bool for value in stops.values())
            or (
                cleanup["verified"] and (not cleanup["services_stopped"] or not all(stops.values()))
            )
            or (
                raw["result"] == "completed"
                and (not cleanup["verified"] or raw.get("guard") != "released")
            )
        ):
            raise ValueError("incoherent terminal cleanup")
        return BoundedRunStatus.model_validate(
            {
                "status": "stopped" if cleanup["verified"] else "cleanup_unverified",
                "attempt_id": raw["attempt_id"],
                "as_of": finished,
                "result": raw["result"],
                "stop_present": stops,
            }
        )
    except FileNotFoundError:
        return BoundedRunStatus(status="missing")
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return BoundedRunStatus(status="invalid")
