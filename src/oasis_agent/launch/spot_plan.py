"""Pre-registered acceptance of one shadow spot experiment: a plan frozen before capture.

A plan fixes the comparator, the UTC observation window [start, end) and the label
cutoff of `spot_experiment` (unix seconds, the same window contract as an analysis-time
window). The research runner binds the normalized plan and its digest into the capture's
`session_start`, the record that already carries the code identity, the strategy config,
the run policy digest and the initial state, before any provider call; it refuses to
start when it cannot write that record. Acceptance reads the window only from the plan
bound to the selected session, so the window cannot be chosen after the data is seen:

- the operator's frozen plan file must equal the plan bound to the selected session,
  and no other session of the archive may bind it;
- analysis-time window arguments are refused and a prefix is never accepted;
- acceptance depends on observation coverage alone, never on a label or a return: a
  complete session, the plan bound before the window start (host clock), a cycle at or
  before the window start, at least one cycle with a successful discovery inside the
  window, and a cycle that queried labels at or after the label cutoff. Anything else
  is `blocked`; the record still shows every cycle by phase and kind, the primary
  sample denominator and every label state, and the embedded fixed-baseline report.

Evidence. The binding is local: the archive's hash chain orders the plan before every
provider call of its session, and the times are the capturing host's clock. A consistent
rewrite of the whole archive cannot be ruled out from the archive alone; only a plan
digest held or timestamped independently before the window can show that the plan came
first (`independent_time_proof: not_established`). Nothing here is a return, a promotion
or an execution permit.
"""

import json
import sqlite3
from collections import Counter
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from ..models.common import canonical_json, sha256_hex
from .spot_agent import AgentConfig
from .spot_capture import CAPTURE_SCHEMA, replay_session
from .spot_experiment import (
    COMPARATOR,
    EvidenceSource,
    ExperimentRefused,
    check_window,
    replay_inputs,
    run_experiment,
)
from .spot_report import read_journal

Record = dict[str, Any]
PLAN_SCHEMA = "oasis.spot-experiment-plan.v1"
ACCEPTANCE_SCHEMA = "oasis.spot-plan-acceptance.v1"
# A plan names one comparator and three times; anything larger is not a plan.
PLAN_FILE_LIMIT = 1 << 16
# Plan refusals that keep their own category: the shared window contract, the comparator.
_CONTRACT = frozenset(
    {"empty_window", "label_cutoff_before_window_horizon", "comparator_not_fixed"}
)
# Acceptance is blocked for exactly these observation gaps, reported in this order.
BLOCKING = (
    "session_incomplete",
    "plan_bound_after_window_start",
    "window_start_not_observed",
    "no_discovery_cycle_in_window",
    "label_cutoff_not_reached",
)
# Kind of one replayed cycle: its tick_summary's coverage.discovery, or a failed tick.
CYCLE_KINDS = {
    "ok": "discovery",
    "failed": "discovery_failed",
    "skipped_held_position_unpriceable": "entries_blocked",
    "deferred_manage_only": "manage_only",
    "stopped": "stopped",
}
KINDS = (*CYCLE_KINDS.values(), "failed")
# Kinds whose tick queried due outcome labels. SpotAgent.tick labels before discovery
# (so also when discovery fails or entries are blocked); a manage-only or stopped tick
# defers labels, and a failed tick is rolled back with them.
LABELLING = frozenset({"discovery", "discovery_failed", "entries_blocked"})
PHASES = ("before_window", "window", "label_period", "after_label_cutoff")
EVIDENCE = {
    # The plan sits in the session's first record, ahead of every provider call in
    # the archive's hash chain: an ordering inside this archive, on the host clock.
    "plan_order": "local_hash_chain",
    "plan_time": "capturing_host_clock",
    "independent_time_proof": "not_established",
    "plan_uniqueness_scope": "this_archive",
}


class PlanRefused(Exception):
    """The plan or its binding cannot support acceptance; `category` is a fixed reason."""

    def __init__(self, category: str):
        super().__init__(f"spot experiment plan refused: {category}")
        self.category = category


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class PlanWindow(_Strict):
    """[start, end) and the label cutoff, plain integer unix seconds (UTC)."""

    start: int
    end: int
    label_cutoff: int


class ExperimentPlan(_Strict):
    schema_: Literal["oasis.spot-experiment-plan.v1"] = Field(alias="schema")
    comparator: str
    window: PlanWindow

    @model_validator(mode="after")
    def _contract(self) -> Self:
        if self.comparator != COMPARATOR:
            # spot_experiment has one fixed comparator; a plan cannot name another.
            raise PydanticCustomError("comparator_not_fixed", "plan names another comparator")
        w = self.window
        try:
            check_window(w.start, w.end, w.label_cutoff)  # the experiment's own contract
        except ExperimentRefused as refused:
            raise PydanticCustomError(refused.category, "plan window refused") from None
        return self


def canonical(plan: ExperimentPlan) -> Record:
    """The normalized plan: what is bound and digested, whatever the file's layout."""
    return plan.model_dump(mode="json", by_alias=True)


def plan_digest(plan: ExperimentPlan) -> str:
    return sha256_hex(canonical_json(canonical(plan)))


class _DuplicateKey(ValueError):
    pass


def _object(pairs: list[tuple[str, Any]]) -> Record:
    out = dict(pairs)
    if len(out) != len(pairs):
        # The parser would keep the last value; a reader of the file may see the first.
        raise _DuplicateKey
    return out


def parse_plan(raw: bytes) -> ExperimentPlan:
    """One plan from bytes: strict JSON object, strict fields, the window contract."""
    if len(raw) > PLAN_FILE_LIMIT:
        raise PlanRefused("plan_too_large")
    try:
        data = json.loads(raw, object_pairs_hook=_object)
    except _DuplicateKey:
        raise PlanRefused("plan_malformed") from None
    except ValueError as error:  # JSONDecodeError and UnicodeDecodeError
        raise PlanRefused("plan_not_json") from error
    return validate_plan(data)


def validate_plan(data: object) -> ExperimentPlan:
    try:
        return ExperimentPlan.model_validate(data)
    except ValidationError as error:
        kinds = {e["type"] for e in error.errors()}
        contract = sorted(kinds & _CONTRACT)
        raise PlanRefused(contract[0] if contract else "plan_malformed") from None


def read_plan(path: Path) -> ExperimentPlan:
    try:
        with path.open("rb") as handle:
            raw = handle.read(PLAN_FILE_LIMIT + 1)
    except OSError as error:
        raise PlanRefused("plan_unreadable") from error
    return parse_plan(raw)


def binding(plan: ExperimentPlan) -> Record:
    """What `session_start` carries: the normalized plan and its digest."""
    return {"plan": canonical(plan), "plan_sha256": plan_digest(plan)}


def bound_plan(start: Record) -> ExperimentPlan | None:
    """The plan bound to one session, or None for a session started without a plan.

    A session that names a plan must carry exactly the normalized plan and its digest;
    a partial, edited or re-digested binding refuses instead of reading as `no plan`.
    """
    if "plan" not in start and "plan_sha256" not in start:
        return None
    try:
        plan = validate_plan(start.get("plan"))
    except PlanRefused as refused:
        raise PlanRefused("bound_plan_malformed") from refused
    if start.get("plan") != canonical(plan) or start.get("plan_sha256") != plan_digest(plan):
        raise PlanRefused("bound_plan_digest_mismatch")
    return plan


def _session_starts(archive: Path) -> tuple[list[Record], str | None]:
    """Every session_start of a verified capture archive, and the archive head digest."""
    try:
        policy, records = read_journal(archive)
    except (ValueError, sqlite3.Error) as corrupt:
        raise PlanRefused("archive_corrupt") from corrupt
    if policy != {"schema": CAPTURE_SCHEMA}:
        raise PlanRefused("archive_unsupported")
    starts = [r for r in records if r.get("kind") == "session_start"]
    ids = [r.get("session_id") for r in starts]
    if len(ids) != len(set(ids)):
        raise PlanRefused("session_id_repeated")
    head = sha256_hex(canonical_json(records[-1])) if records else None
    return starts, head


def planned_session(
    archive: Path, plan: ExperimentPlan, session_id: str | None
) -> tuple[Record, str]:
    """The one session of the archive bound to exactly this plan, and the archive head.

    With `session_id`, that session must bind this plan; without it, exactly one session
    may. Any other session binding the same plan refuses: a plan is one trial, and two
    sessions under it would let the better-looking one be chosen afterwards.
    """
    digest = plan_digest(plan)
    starts, head = _session_starts(archive)
    bound = {r.get("session_id"): bound_plan(r) for r in starts}
    matching = [sid for sid, p in bound.items() if p is not None and plan_digest(p) == digest]
    if session_id is not None:
        if session_id not in bound:
            raise PlanRefused("unknown_session")
        if bound[session_id] is None:
            raise PlanRefused("session_has_no_plan")
        if session_id not in matching:
            raise PlanRefused("plan_mismatch")
    if not matching:
        raise PlanRefused(
            "plan_mismatch" if any(p is not None for p in bound.values()) else "no_planned_session"
        )
    if len(matching) > 1:
        raise PlanRefused("plan_bound_to_several_sessions")
    if head is None:  # pragma: no cover - a matching session means a non-empty archive
        raise PlanRefused("no_planned_session")
    start = next(r for r in starts if r.get("session_id") == matching[0])
    return start, head


def plan_bound_in(archive: Path, plan: ExperimentPlan) -> bool:
    """Whether a session of an existing capture archive already binds this plan."""
    digest = plan_digest(plan)
    starts, _ = _session_starts(archive)
    for r in starts:
        bound = bound_plan(r)
        if bound is not None and plan_digest(bound) == digest:
            return True
    return False


def require_unplanned(replay: Record) -> None:
    """Analysis-time windows are for sessions started without a plan: a planned session
    is read only through its own plan, never re-windowed after capture."""
    if bound_plan(replay["plan_binding"]) is not None:
        raise PlanRefused("session_has_bound_plan")


def _cycles(replay: Record) -> list[list[Record]]:
    """The replay's records split back into its cycles, in order.

    Replay appends each completed cycle's records exactly as the tick returned them, and a
    tick always ends with its `tick_summary`; a rolled-back tick leaves one `cycle_failed`
    marker instead. Every record of a cycle carries that cycle's `now` as `at`.
    """
    groups: list[list[Record]] = []
    current: list[Record] = []
    for r in replay["records"]:
        current.append(r)
        if r.get("kind") in ("tick_summary", "cycle_failed"):
            groups.append(current)
            current = []
    if current or len(groups) != len(replay["cycles"]):
        raise PlanRefused("cycle_records_unaligned")
    for group, cycle in zip(groups, replay["cycles"], strict=True):
        if any(r.get("at") != cycle["now"] for r in group):
            raise PlanRefused("cycle_records_unaligned")
    return groups


def _cycle_kind(group: list[Record]) -> str:
    last = group[-1]
    if last.get("kind") == "cycle_failed":
        if len(group) != 1:
            raise PlanRefused("cycle_records_unaligned")
        return "failed"
    coverage = last.get("coverage")
    discovery = coverage.get("discovery") if isinstance(coverage, dict) else None
    if not isinstance(discovery, str) or discovery not in CYCLE_KINDS:
        raise PlanRefused("cycle_summary_unknown")
    return CYCLE_KINDS[discovery]


def _phase(now: int, window: PlanWindow) -> str:
    if now < window.start:
        return "before_window"
    if now < window.end:
        return "window"
    if now < window.label_cutoff:
        return "label_period"
    return "after_label_cutoff"


def observation(replay: Record, plan: ExperimentPlan, bound_at_ms: int) -> tuple[Record, list[str]]:
    """What the replayed session observed against the plan, and what blocks acceptance.

    Coverage only, never a label value or a return: every completed cycle is counted by
    phase and kind, so failed, manage-only and discovery-failed cycles stay visible.
    `bound_at_ms` is the session_start time (host clock) that carries the plan.
    """
    window = plan.window
    cycles = [
        {"now": cycle["now"], "kind": _cycle_kind(group), "phase": _phase(cycle["now"], window)}
        for group, cycle in zip(_cycles(replay), replay["cycles"], strict=True)
    ]
    counts: dict[str, Counter[str]] = {p: Counter() for p in PHASES}
    for c in cycles:
        counts[c["phase"]][c["kind"]] += 1
    times = [c["now"] for c in cycles]
    first = times[0] if times else None
    labelled = [c["now"] for c in cycles if c["kind"] in LABELLING]
    gaps = []  # appended in BLOCKING order
    if not replay["session_complete"]:
        gaps.append("session_incomplete")
    if bound_at_ms > window.start * 1000:
        gaps.append("plan_bound_after_window_start")
    if first is None or first > window.start:
        gaps.append("window_start_not_observed")
    if not counts["window"]["discovery"]:
        gaps.append("no_discovery_cycle_in_window")
    if not labelled or max(labelled) < window.label_cutoff:
        gaps.append("label_cutoff_not_reached")
    observed = {
        "plan_bound_at_ms": bound_at_ms,
        "cycles_total": len(cycles),
        "first_cycle_at": first,
        "last_cycle_at": times[-1] if times else None,
        # Sparse coverage stays visible; the plan sets no gap threshold, so none blocks.
        "max_cycle_gap_seconds": max((b - a for a, b in pairwise(times)), default=None),
        "last_label_query_at": max(labelled) if labelled else None,
        "cycles_by_phase": {p: {k: counts[p][k] for k in KINDS} for p in PHASES},
        "incomplete_cycles": replay["incomplete_cycles"],
        "termination": replay["termination"],
    }
    return observed, gaps


def accept_plan(
    archive: Path,
    raw_dir: Path,
    plan: ExperimentPlan,
    *,
    session_id: str | None,
    evidence: EvidenceSource,
) -> Record:
    """Strict acceptance of the one session bound to `plan`, with the fixed experiment
    run on exactly the plan's window. Reads the archive; never writes it."""
    start, head = planned_session(archive, plan, session_id)
    replay = replay_session(archive, raw_dir, start["session_id"])
    if replay["archive_head_digest"] != head:
        raise PlanRefused("archive_changed_during_read")
    bound = bound_plan(replay["plan_binding"])
    digest = plan_digest(plan)
    if bound is None or plan_digest(bound) != digest:  # the same record, read twice
        raise PlanRefused("plan_mismatch")
    wall_ms = start.get("wall_ms")
    if type(wall_ms) is not int:
        raise PlanRefused("session_start_malformed")
    observed, blocking = observation(replay, plan, wall_ms)
    try:
        config = AgentConfig.model_validate(replay["config"])
    except ValidationError as error:
        raise PlanRefused("session_config_invalid") from error
    w = plan.window
    report = run_experiment(
        replay["records"],
        config,
        start=w.start,
        end=w.end,
        label_cutoff=w.label_cutoff,
        evidence=evidence,
        inputs=replay_inputs(replay),
        window_basis="pre_registered_plan",
        plan_sha256=digest,
    )
    document: Record = {
        "schema": ACCEPTANCE_SCHEMA,
        "execution_eligible": False,
        "status": "blocked" if blocking else "accepted",
        "blocking": blocking,
        "plan": canonical(plan),
        "plan_sha256": digest,
        "session": {
            "session_id": replay["session_id"],
            "archive_head_digest": replay["archive_head_digest"],
            "session_complete": replay["session_complete"],
            "run_policy_sha256": replay["run_policy_sha256"],
            "config_sha256": sha256_hex(canonical_json(replay["config"])),
            "decision_policy_sha256": report["manifest"]["policy_sha256"],
            "code": replay["code"],
        },
        "observation": observed,
        # The denominator and every label state of the primary samples; no return here.
        "labels": {
            "primary_samples": report["population"]["primary_samples"],
            "states": report["groups"]["all_primary"]["labels"],
        },
        "binding_evidence": EVIDENCE,
        "limits": {
            "acceptance_basis": "observation_coverage_only_not_labels_or_returns",
            "net_return": "not_measured",
            "promotion": "not_claimed",
        },
        "experiment": report,
    }
    document["acceptance_sha256"] = sha256_hex(canonical_json(document))
    return document
