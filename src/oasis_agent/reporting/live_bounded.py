"""Read one local bounded run declaration without Docker, keys or RPC."""

import os
import stat
import time
from pathlib import Path
from typing import Annotated, Literal, cast

from pydantic import BaseModel, Field

from ..evidence.rpc_validation import strict_json_object
from ..live.base_run_limits import MAX_TICKS, wait_seconds
from ..models.common import canonical_json, sha256_hex

MAX_BYTES = 65_536
Role = Literal["strategy", "authority"]
RunResult = Literal["completed", "failed", "timed_out", "interrupted"]
RunBlocker = Literal[
    "rpc_budget_insufficient",
    "rpc_budget_unverified",
    "rpc_provider_rate_limited",
    "post_stop_reconciliation_required",
    "post_stop_replay_unverified",
]
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
    start_attempted: bool | None = None
    requested_ticks: int | None = None
    completed_ticks: int | None = None
    reconciled_transactions: int | None = None
    blocker: RunBlocker | None = None
    blocked_service: Role | None = None


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


def _rpc_budget_insufficient(raw: dict[str, object]) -> bool:
    budget = raw.get("rpc_budget")
    if not isinstance(budget, dict):
        return False
    # Earlier reports only admitted enough quota for a fresh capture.
    if budget.get("enough_for_run", budget.get("enough_for_capture")) is not False:
        return False
    if "enough_for_run" in budget and budget.get("requested_ticks") != raw.get("requested_ticks"):
        return False
    used = budget.get("cumulative_reserved_upper_micros")
    scope = budget.get("cumulative_scope_upper_micros")
    ceiling = budget.get("ceiling_micros")
    declared_ceiling = raw.get("rpc_ceiling_micros")
    return (
        type(used) is int
        and type(scope) is int
        and type(ceiling) is int
        and 0 <= used <= scope <= ceiling
        and ceiling > 0
        and type(declared_ceiling) is int
        and declared_ceiling == ceiling
    )


def _terminal_blocker(
    raw: dict[str, object], *, stopped: bool, start_attempted: bool | None
) -> tuple[RunBlocker | None, Role | None]:
    stage = raw.get("failure_stage")
    if stage == "cumulative_rpc_budget" and start_attempted is False:
        if _rpc_budget_insufficient(raw):
            return "rpc_budget_insufficient", None
        return "rpc_budget_unverified", None
    if stage == "fresh_evidence" and start_attempted is False and raw.get("result") == "failed":
        sources = raw.get("rpc_rate_limited_sources")
        digest = raw.get("fresh_evidence_hash")
        if (
            isinstance(sources, list)
            and 1 <= len(sources) <= 2
            and all(source in ("live_primary", "live_secondary") for source in sources)
            and len(set(sources)) == len(sources)
            and isinstance(digest, str)
            and len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)
        ):
            return "rpc_provider_rate_limited", None
    if not stopped or start_attempted is not True:
        return None, None
    if "post_stop_replay" not in raw and stage != "post_stop_replay":
        return None, None
    replay = raw.get("post_stop_replay")
    if (
        not isinstance(replay, dict)
        or replay.get("schema") != "oasis.live-startup-state.v1"
        or replay.get("execution_eligible") is not False
        or replay.get("historical_only") is not True
    ):
        return "post_stop_replay_unverified", None
    if (
        replay.get("startup_prerequisites_passed") is True
        and replay.get("stop_markers_present") is True
        and "blockers" not in replay
        and stage != "post_stop_replay"
    ):
        return None, None
    role = replay.get("blocked_service")
    if (
        replay.get("startup_prerequisites_passed") is False
        and replay.get("blockers") == ["halt_or_reconciliation_unresolved"]
        and role in ("strategy", "authority")
    ):
        return "post_stop_reconciliation_required", cast(Role, role)
    return "post_stop_replay_unverified", None


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
            ticks = attempt.get("requested_ticks", 1)
            if (
                attempt["schema"] != "oasis.base-bounded-attempt.v1"
                or type(started) is not int
                or not 0 <= started <= clock
                or type(attempt["wait_seconds"]) is not int
                or type(ticks) is not int
                or attempt["wait_seconds"] != wait_seconds(ticks)
            ):
                raise ValueError("invalid start declaration") from None
            return BoundedRunStatus.model_validate(
                {
                    "status": "launch_requested"
                    if clock - started <= wait_seconds(ticks) + 30
                    else "cleanup_unverified",
                    "attempt_id": attempt["attempt_id"],
                    "as_of": started,
                    "requested_ticks": ticks,
                }
            )
        finished, cleanup = raw["finished_at"], raw["cleanup"]
        start_attempted = raw.get("start_attempted")
        ticks = raw.get("requested_ticks", 1)
        completed = raw.get("completed_ticks")
        reconciled = raw.get("reconciled_transactions")
        if (
            raw["schema"] != "oasis.base-bounded-run.v1"
            or type(finished) is not int
            or not 0 <= finished <= clock
            or ("start_attempted" in raw and type(start_attempted) is not bool)
            or not isinstance(cleanup, dict)
            or type(cleanup["verified"]) is not bool
            or type(cleanup["services_stopped"]) is not bool
            or type(ticks) is not int
            or not 1 <= ticks <= MAX_TICKS
            or (
                "completed_ticks" in raw
                and (
                    type(completed) is not int
                    or not 0 <= completed <= ticks
                    or type(reconciled) is not int
                    or not 0 <= reconciled <= completed
                )
            )
            or ("completed_ticks" in raw) != ("reconciled_transactions" in raw)
            or (start_attempted is False and completed is not None)
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
                and (
                    not cleanup["verified"]
                    or raw.get("guard") != "released"
                    or start_attempted is False
                    or (completed != ticks and (ticks != 1 or completed is not None))
                )
            )
        ):
            raise ValueError("incoherent terminal cleanup")
        attempted = start_attempted if isinstance(start_attempted, bool) else None
        blocker, role = _terminal_blocker(
            raw, stopped=cleanup["verified"], start_attempted=attempted
        )
        if raw["result"] == "completed" and blocker is not None:
            raise ValueError("terminal success conflicts with an unresolved blocker")
        return BoundedRunStatus.model_validate(
            {
                "status": "stopped" if cleanup["verified"] else "cleanup_unverified",
                "attempt_id": raw["attempt_id"],
                "as_of": finished,
                "result": raw["result"],
                "stop_present": stops,
                "start_attempted": attempted,
                "requested_ticks": ticks,
                "completed_ticks": 1
                if completed is None and raw["result"] == "completed"
                else completed,
                "reconciled_transactions": reconciled,
                "blocker": blocker,
                "blocked_service": role,
            }
        )
    except FileNotFoundError:
        return BoundedRunStatus(status="missing")
    except OSError, ValueError, TypeError, KeyError, RecursionError:
        return BoundedRunStatus(status="invalid")
