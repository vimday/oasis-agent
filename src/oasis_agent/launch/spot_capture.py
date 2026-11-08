"""Ordered input capture of shadow spot runs, and strict offline replay of the same agent.

Capture. The runner wraps each read-only provider runner with `CaptureRecorder.logical`
and journals, in a separate hash-linked archive next to the run journal:

- `session_start`: the agent's exact initial state, config and code identity;
- `cycle_start`: cycle number, the tick's `now` and whether it was `manage_only`;
- `call`: one logical provider query in global order: provider, exact read-only argv,
  start and response-available times (integer ms), transport attempts (retries inside
  `paced`, which the call budget does not count), and either the raw response hash (the
  bytes are kept content-addressed under `raw/`) or a fixed failure category;
- `cycle_end`: the digest of the records the tick returned (or its error type) and of
  the agent state after the cycle (after the runner's rollback on error).

One call is one record, so equal responses of different calls stay distinct and the
order across providers is kept. A source's `retrieved_at` is the call's recorded
response-available time, so replay sees the same clock. The archive never feeds the
financial journal and never raises into the agent: a failed archive write marks the
session incomplete, which replay detects; the shadow run itself behaves as before. Live
runs are never captured.

Replay. `replay_session` rebuilds the production `SpotAgent` (no executor) from the
recorded state, config and code identity, and serves each provider call from the
archive only: the next recorded call of the current cycle must have the same provider
and argv; its bytes must hash to the recorded digest; a recorded failure is raised as
the same `SourceFailure`. A missing, extra, reordered, foreign-cycle or tampered input
refuses (`ReplayRefused`), as does any cycle whose records or state digest differ.
There is no network, wallet or CLI fallback.
"""

import copy
import hashlib
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models.common import canonical_json, sha256_hex
from ..sniping.journal import ExecutionJournal
from .gmgn_source import GmgnSource
from .onchainos_source import OnchainosSource, Runner, SourceFailure
from .spot_agent import AgentConfig, SpotAgent, state_from_json, state_to_json
from .spot_report import read_journal  # the same chain verification as the report

Record = dict[str, Any]
CAPTURE_SCHEMA = "oasis.spot-capture.v1"
PROVIDERS = ("onchainos", "gmgn")
# Source modules whose bytes define the decision behavior being captured.
CODE_MODULES = (
    "spot_agent.py",
    "opportunity.py",
    "spot_book.py",
    "outcomes.py",
    "onchainos_source.py",
    "gmgn_source.py",
    "discovery_slot.py",
)
# Non-SourceFailure runner errors that can be replayed as the same builtin type.
REPLAYABLE_ERRORS: dict[str, type[Exception]] = {
    "OSError": OSError,
    "FileNotFoundError": FileNotFoundError,
    "PermissionError": PermissionError,
    "TimeoutError": TimeoutError,
}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
# Why a run loop returned; see scripts/run_spot_agent.py::run_loop.
STOP_REASONS = frozenset(
    {"max_ticks", "stop_requested", "budget_stop", "repeated_errors", "unresolved_live"}
)


def code_identity() -> dict[str, str]:
    here = Path(__file__).resolve().parent
    return {name: sha256_hex((here / name).read_bytes()) for name in CODE_MODULES}


def records_digest(records: list[Record] | None) -> str | None:
    return None if records is None else sha256_hex(canonical_json(records))


def state_digest(agent: SpotAgent) -> str:
    return sha256_hex(canonical_json(state_to_json(agent.state)))


def _ms(seconds: float) -> int:
    return int(seconds * 1000)


class CaptureRecorder:
    """Writes one shadow session's ordered inputs; never raises into the agent."""

    def __init__(
        self, archive: ExecutionJournal, raw_dir: Path, clock: Callable[[], float] = time.time
    ):
        self._archive, self._raw, self._clock = archive, raw_dir, clock
        self.session_id = uuid.uuid4().hex
        self.healthy = True
        self._seq = 0
        self._cycles = 0
        self._cycle: int | None = None
        self._cycle_calls = 0
        self._attempts = 0
        self._available_ms: int | None = None

    def _write(self, kind: str, body: Record) -> None:
        if not self.healthy:
            return
        try:
            payload = body | {"session_id": self.session_id, "execution_eligible": False}
            self._archive.append(kind, payload | {"kind": kind})
        except Exception:  # noqa: BLE001 - evidence loss must never alter the run itself
            # Every later write stops too: the session ends inside an open cycle, which
            # replay reports as incomplete instead of a silently shorter success.
            self.healthy = False

    def source_clock(self) -> float:
        """A source's `retrieved_at`: the recorded response-available time of the call
        just made, at the archive's ms precision, so replay derives the same second."""
        if self._available_ms is None:
            return self._clock()
        return self._available_ms // 1000

    def start_session(self, agent: SpotAgent, run_policy_sha256: str) -> None:
        try:
            self._start_session(agent, run_policy_sha256)
        except Exception:  # noqa: BLE001 - evidence loss must never alter the run itself
            self.healthy = False

    def _start_session(self, agent: SpotAgent, run_policy_sha256: str) -> None:
        reasons = []
        if agent.executor is not None:
            reasons.append("live_runs_are_not_captured")
        if agent.nominators:
            reasons.append("nominators_not_supported")
        state = state_to_json(agent.state)
        self._write(
            "session_start",
            {
                "schema": CAPTURE_SCHEMA,
                "replayable": not reasons,
                "unreplayable_reasons": reasons,
                "run_policy_sha256": run_policy_sha256,
                "code": code_identity(),
                "config": agent.config.model_dump(mode="json"),
                "gmgn": agent.gmgn is not None,
                "state": state,
                "state_sha256": sha256_hex(canonical_json(state)),
                "wall_ms": _ms(self._clock()),
            },
        )
        if reasons:
            self.healthy = False

    def attempt(self, runner: Runner) -> Runner:
        """Innermost wrapper: counts transport attempts (retries) of the current call."""

        def run(argv: Sequence[str]) -> bytes:
            self._attempts += 1
            return runner(argv)

        return run

    def logical(self, provider: str, runner: Runner) -> Runner:
        """Outermost wrapper: one record per logical query, success or failure."""
        if provider not in PROVIDERS:
            raise ValueError("unknown provider")

        def run(argv: Sequence[str]) -> bytes:
            command = [str(a) for a in argv]
            started, self._attempts, self._available_ms = self._clock(), 0, None
            try:
                raw = runner(argv)
            except SourceFailure as failure:
                self._call(provider, command, started, {"failure": failure.category})
                raise
            except Exception as error:
                # Type only: messages can embed process output or paths.
                self._call(provider, command, started, {"error_type": type(error).__name__})
                raise
            digest = sha256_hex(raw)
            self._store(digest, raw)
            self._call(provider, command, started, {"raw_sha256": digest, "bytes": len(raw)})
            return raw

        return run

    def _store(self, digest: str, raw: bytes) -> None:
        path = self._raw / f"{digest}.json"
        try:
            if not path.exists():
                temporary = path.with_name(f".{digest}.{uuid.uuid4().hex}.tmp")
                temporary.write_bytes(raw)
                temporary.replace(path)
        except OSError:
            self.healthy = False

    def _call(self, provider: str, command: list[str], started: float, outcome: Record) -> None:
        available = _ms(self._clock())
        self._available_ms = available
        self._seq += 1
        self._cycle_calls += 1
        self._write(
            "call",
            {
                "call_seq": self._seq,
                "cycle": self._cycle,
                "provider": provider,
                "argv": command,
                "started_ms": _ms(started),
                "available_ms": available,
                "attempts": self._attempts,
                **outcome,
            },
        )

    def cycle_start(self, now: int, manage_only: bool) -> None:
        self._cycles += 1
        self._cycle, self._cycle_calls = self._cycles, 0
        self._write(
            "cycle_start",
            {
                "cycle": self._cycle,
                "now": now,
                "manage_only": manage_only,
                "first_call_seq": self._seq + 1,
                "wall_ms": _ms(self._clock()),
            },
        )

    def session_end(self, reason: str) -> None:
        """The run's own end. Only a healthy archive can write it: after any lost write
        the session stays a prefix, never a healthy-looking complete run."""
        if reason not in STOP_REASONS:
            raise ValueError("unknown stop reason")
        if self._cycle is not None:
            self.healthy = False  # ending inside a cycle is not a clean end
        self._write("session_end", {"reason": reason, "cycles": self._cycles, "calls": self._seq})

    def cycle_end(self, records: list[Record] | None, error: str | None, agent: SpotAgent) -> None:
        try:
            self._cycle_end(records, error, agent)
        except Exception:  # noqa: BLE001 - evidence loss must never alter the run itself
            self.healthy = False
        self._cycle = None

    def _cycle_end(self, records: list[Record] | None, error: str | None, agent: SpotAgent) -> None:
        self._write(
            "cycle_end",
            {
                "cycle": self._cycle,
                "calls": self._cycle_calls,
                "records_sha256": records_digest(records),
                "error_type": error,
                "state_sha256": state_digest(agent),
                "wall_ms": _ms(self._clock()),
            },
        )


class ReplayRefused(Exception):
    """The archive cannot reproduce a decision; `category` is a fixed reason.

    Deliberately not a ValueError: SpotAgent treats ValueError/SourceFailure from a
    provider as a missed observation, which would turn a refusal into a decision.
    """

    def __init__(self, category: str, detail: str = ""):
        super().__init__(f"spot replay refused: {category}" + (f" ({detail})" if detail else ""))
        self.category = category


@dataclass
class _Cycle:
    start: Record
    calls: list[Record] = field(default_factory=list)
    end: Record | None = None


def _session(
    records: list[Record], session_id: str | None
) -> tuple[Record, list[_Cycle], Record | None]:
    """The session's start record, its cycles and its end record, structurally validated."""
    ids = list(dict.fromkeys(r.get("session_id") for r in records))
    if not ids:
        raise ReplayRefused("empty_archive")
    chosen = ids[-1] if session_id is None else session_id
    if chosen not in ids:
        raise ReplayRefused("unknown_session")
    rows = [r for r in records if r.get("session_id") == chosen]
    start, rest = rows[0], rows[1:]
    if start.get("kind") != "session_start" or start.get("schema") != CAPTURE_SCHEMA:
        raise ReplayRefused("unsupported_schema")
    if start.get("replayable") is not True:
        raise ReplayRefused("session_not_replayable", ",".join(start["unreplayable_reasons"]))
    cycles: list[_Cycle] = []
    expected_seq = 1
    end: Record | None = None
    for r in rest:
        kind = r.get("kind")
        if end is not None:
            # Nothing may follow the session's end: it would be input no cycle consumed.
            raise ReplayRefused("record_after_session_end", str(kind))
        current = cycles[-1] if cycles and cycles[-1].end is None else None
        if kind == "session_end":
            if current is not None:
                raise ReplayRefused("session_end_inside_cycle")
            if r.get("cycles") != len(cycles) or r.get("calls") != expected_seq - 1:
                raise ReplayRefused("session_end_count_mismatch")
            if r.get("reason") not in STOP_REASONS:
                raise ReplayRefused("session_end_malformed")
            end = r
        elif kind == "cycle_start":
            if current is not None or r.get("cycle") != len(cycles) + 1:
                raise ReplayRefused("cycle_sequence_broken")
            if r.get("first_call_seq") != expected_seq:
                raise ReplayRefused("call_sequence_broken")
            if type(r.get("now")) is not int or type(r.get("manage_only")) is not bool:
                raise ReplayRefused("cycle_malformed")
            if cycles and r["now"] * 1000 < cycles[-1].start["now"] * 1000:
                raise ReplayRefused("cycle_time_regressed")
            cycles.append(_Cycle(r))
        elif kind == "call":
            if current is None or r.get("cycle") != current.start["cycle"]:
                raise ReplayRefused("call_outside_its_cycle", str(r.get("call_seq")))
            if r.get("call_seq") != expected_seq:
                raise ReplayRefused("call_sequence_broken", str(r.get("call_seq")))
            expected_seq += 1
            _check_call(r, current)
            current.calls.append(r)
        elif kind == "cycle_end":
            if current is None or r.get("cycle") != current.start["cycle"]:
                raise ReplayRefused("cycle_sequence_broken")
            if r.get("calls") != len(current.calls):
                raise ReplayRefused("cycle_call_count_mismatch")
            if type(r.get("wall_ms")) is not int or any(
                c["available_ms"] > r["wall_ms"] for c in current.calls
            ):
                # A response that became available after its cycle ended cannot have
                # informed that cycle's decisions: it is another time's input.
                raise ReplayRefused("call_after_its_cycle")
            current.end = r
        else:
            raise ReplayRefused("unsupported_record", str(kind))
    return start, cycles, end


def _check_call(call: Record, cycle: _Cycle) -> None:
    """Shape and time: a response is only usable inside the cycle that requested it."""
    if call.get("provider") not in PROVIDERS or not isinstance(call.get("argv"), list):
        raise ReplayRefused("call_malformed", str(call.get("call_seq")))
    started, available = call.get("started_ms"), call.get("available_ms")
    if type(started) is not int or type(available) is not int or available < started:
        raise ReplayRefused("call_time_invalid", str(call.get("call_seq")))
    # A tick's `now` is the floor of its start time: no request precedes it.
    if started < cycle.start["now"] * 1000:
        raise ReplayRefused("call_before_its_cycle", str(call.get("call_seq")))
    if cycle.calls and available < cycle.calls[-1]["available_ms"]:
        raise ReplayRefused("call_time_out_of_order", str(call.get("call_seq")))
    outcomes = [k for k in ("raw_sha256", "failure", "error_type") if k in call]
    if len(outcomes) != 1:
        raise ReplayRefused("call_outcome_ambiguous", str(call.get("call_seq")))
    if "raw_sha256" in call and not _HEX64.fullmatch(str(call["raw_sha256"])):
        raise ReplayRefused("call_malformed", str(call.get("call_seq")))


class _Frozen:
    """Serves one cycle's recorded calls in order; anything else refuses."""

    def __init__(self, raw_dir: Path):
        self._raw = raw_dir
        self._calls: list[Record] = []
        self._next = 0
        self._served_ms: int | None = None
        self.served: list[int] = []
        self.refused: ReplayRefused | None = None

    def load(self, cycle: _Cycle) -> None:
        self._calls, self._next, self._served_ms = cycle.calls, 0, None

    def _refuse(self, category: str, detail: str = "") -> ReplayRefused:
        # Sticky: even if a caller swallowed it, the cycle cannot pass `finish`.
        self.refused = self.refused or ReplayRefused(category, detail)
        return self.refused

    def finish(self) -> None:
        if self.refused is not None:
            raise self.refused
        if self._next != len(self._calls):
            missing = self._calls[self._next]["call_seq"]
            raise ReplayRefused("recorded_call_not_consumed", str(missing))

    def clock(self) -> float:
        if self._served_ms is None:
            raise self._refuse("clock_read_before_any_call")
        return self._served_ms // 1000

    def runner(self, provider: str) -> Runner:
        def run(argv: Sequence[str]) -> bytes:
            if self._next >= len(self._calls):
                raise self._refuse("unrecorded_call", provider)
            call = self._calls[self._next]
            if call["provider"] != provider or call["argv"] != [str(a) for a in argv]:
                raise self._refuse("call_mismatch", str(call["call_seq"]))
            self._next += 1
            self._served_ms = call["available_ms"]
            self.served.append(call["call_seq"])
            if "failure" in call:
                raise SourceFailure(str(call["failure"]))
            if "error_type" in call:
                error = REPLAYABLE_ERRORS.get(str(call["error_type"]))
                if error is None:
                    raise self._refuse("unreplayable_error", str(call["error_type"]))
                raise error("replayed provider error")
            path = self._raw / f"{call['raw_sha256']}.json"
            try:
                raw = path.read_bytes()
            except OSError as missing:
                raise self._refuse("raw_missing", str(call["call_seq"])) from missing
            if hashlib.sha256(raw).hexdigest() != call["raw_sha256"]:
                raise self._refuse("raw_hash_mismatch", str(call["call_seq"]))
            return raw

        return run


def replay_session(
    archive_path: Path, raw_dir: Path, session_id: str | None = None
) -> dict[str, Any]:
    """Re-run one captured shadow session through the production SpotAgent, offline."""
    try:
        policy, records = read_journal(archive_path)
    except (ValueError, sqlite3.Error) as corrupt:
        raise ReplayRefused("archive_corrupt") from corrupt
    if policy != {"schema": CAPTURE_SCHEMA}:
        raise ReplayRefused("unsupported_schema")
    start, cycles, end = _session(records, session_id)
    if start["code"] != code_identity():
        # Different decision code would replay different behavior, not this session's.
        raise ReplayRefused("code_identity_changed")
    config = AgentConfig.model_validate(start["config"])
    if sha256_hex(canonical_json(start["state"])) != start["state_sha256"]:
        raise ReplayRefused("state_hash_mismatch")
    frozen = _Frozen(raw_dir)
    source = OnchainosSource(frozen.runner("onchainos"), clock=frozen.clock)
    gmgn = GmgnSource(frozen.runner("gmgn"), clock=frozen.clock) if start["gmgn"] else None
    agent = SpotAgent(source, config, state_from_json(start["state"]), gmgn=gmgn)
    if state_digest(agent) != start["state_sha256"]:
        raise ReplayRefused("state_not_reproducible")
    out: list[Record] = []
    cycle_results: list[Record] = []
    complete = [c for c in cycles if c.end is not None]
    for cycle in complete:
        assert cycle.end is not None  # nosec B101 - filtered above
        frozen.load(cycle)
        before = copy.deepcopy(agent.state)
        produced: list[Record] | None = None
        error: str | None = None
        try:
            produced = agent.tick(cycle.start["now"], manage_only=cycle.start["manage_only"])
        except ReplayRefused:
            raise
        except Exception as raised:  # noqa: BLE001 - mirror the runner's per-tick rollback
            error = type(raised).__name__
            agent.state = before
        frozen.finish()
        result = {
            "cycle": cycle.start["cycle"],
            "now": cycle.start["now"],
            "manage_only": cycle.start["manage_only"],
            "calls": len(cycle.calls),
            "records_sha256": records_digest(produced),
            "error_type": error,
            "state_sha256": state_digest(agent),
        }
        for key in ("records_sha256", "error_type", "state_sha256"):
            if result[key] != cycle.end[key]:
                raise ReplayRefused("decision_mismatch", f"cycle {result['cycle']} {key}")
        cycle_results.append(result)
        if produced is not None:
            out.extend(produced)
        else:
            # The agent returned nothing (the tick raised and the state was rolled back).
            # Keep the cycle in the output so no denominator can silently lose it; this
            # marker is replay bookkeeping, not an agent decision record.
            out.append(
                {
                    "kind": "cycle_failed",
                    "at": cycle.start["now"],
                    "cycle": cycle.start["cycle"],
                    "error_type": error,
                    "state_rolled_back": True,
                    "execution_eligible": False,
                }
            )
    return {
        "schema": "oasis.spot-replay.v1",
        "execution_eligible": False,
        "session_id": start["session_id"],
        # Stored digests are sha256 of each record's canonical bytes (read_journal checks).
        "archive_head_digest": sha256_hex(canonical_json(records[-1])),
        "run_policy_sha256": start["run_policy_sha256"],
        "code": start["code"],
        "config": start["config"],
        "initial_state_sha256": start["state_sha256"],
        "final_state_sha256": state_digest(agent),
        "cycles_replayed": len(cycle_results),
        # Complete only with the run's own end record: closed cycles alone prove a
        # prefix, never that the run did not continue past the last recorded cycle.
        "session_complete": end is not None,
        "termination": (
            {"status": "ended", "reason": end["reason"], "cycles": end["cycles"]}
            if end is not None
            else {"status": "unknown_prefix_only", "reason": None, "cycles": None}
        ),
        "incomplete_cycles": [c.start["cycle"] for c in cycles if c.end is None],
        "calls_served": len(frozen.served),
        "cycles": cycle_results,
        "records": out,
        "assumptions": {
            "inputs": "provider_claims_not_chain_attestations",
            "execution": "paper_only_no_executor",
            "costs": "declared_config_fees_gas_and_slippage",
            "labels": "provider_spot_mid_not_executable_fills",
        },
    }
