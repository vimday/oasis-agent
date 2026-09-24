"""Shadow-window gate and observation loop: fail-closed, with no execution permit.

A window is only a precondition. These tests drive every refusal path of the
derived window record and of the canary admission decision, then drive the
observation loop with scripted observer exit codes so a flaky capture costs an
attempt, a ledger refusal stops the loop, and no scenario ends with
`execution_eligible` true.
"""

import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from oasis_agent.models.common import canonical_json
from oasis_agent.risk.shadow_acceptance import (
    CANARY_WINDOW_REQUIREMENTS,
    CanaryAdmission,
    ObservationRun,
    ShadowRequirements,
    ShadowWindow,
    build_shadow_window,
    canary_admission,
)
from scripts.check_canary_admission import main as admission_main
from scripts.run_account_risk_shadow import (
    ObservationPorts,
    ShadowOutcome,
    _observer_command,
    _retained_run,
    observe,
)
from tests.unit.test_account_risk import (
    ACCOUNT_A,
    CHAIN_A,
    LIMIT_CUMULATIVE,
    LIMIT_DAILY,
    LIMIT_TOTAL,
    make_policy,
)
from tests.unit.test_account_risk_reader import InventoryFixture, manifest

ACCOUNT_KEY = f"{CHAIN_A}:{ACCOUNT_A}"
OTHER_ACCOUNT = "0x" + "77" * 20
MANIFEST = "a" * 64
# The policy digest a real window would carry: the gate compares it byte for byte.
POLICY = make_policy().digest
BLOCK_HASH = "0x" + "cd" * 32
BASE_TIME = 1_800_000_000


def run(
    seq: int,
    *,
    block: int = 100,
    timestamp: int = BASE_TIME,
    state_digest: str | None = None,
    report_digest: str | None = None,
    cumulative_loss_usd_micros: int = 0,
    unresolved_operations: int = 0,
    halted: bool = False,
) -> ObservationRun:
    return ObservationRun(
        seq=seq,
        block_number=block,
        block_hash=BLOCK_HASH,
        timestamp=timestamp,
        state_digest=state_digest if state_digest is not None else f"{seq:064x}",
        report_digest=report_digest if report_digest is not None else f"{seq + 500:064x}",
        cumulative_loss_usd_micros=cumulative_loss_usd_micros,
        unresolved_operations=unresolved_operations,
        halted=halted,
    )


def sustained_runs(*, seconds: int = 60) -> tuple[ObservationRun, ...]:
    """Three accepted runs, 60 block and `seconds` apart, spanning the minimum."""
    return (
        run(1, block=100, timestamp=BASE_TIME),
        run(2, block=101, timestamp=BASE_TIME + seconds),
        run(3, block=102, timestamp=BASE_TIME + 2 * seconds),
    )


def with_third(third: ObservationRun) -> tuple[ObservationRun, ...]:
    """Two accepted runs followed by a third the caller controls."""
    return (*sustained_runs()[:2], third)


def window(
    runs: tuple[ObservationRun, ...] | None = None,
    *,
    account_key: str = ACCOUNT_KEY,
    requirements: ShadowRequirements = CANARY_WINDOW_REQUIREMENTS,
) -> ShadowWindow:
    return build_shadow_window(
        sustained_runs() if runs is None else runs,
        account_key=account_key,
        manifest_digest=MANIFEST,
        policy_digest=POLICY,
        requirements=requirements,
    )


def test_a_sustained_window_derives_every_summary_from_its_runs() -> None:
    accepted = window()
    assert accepted.first_block == 100
    assert accepted.last_block == 102
    assert accepted.last_timestamp == BASE_TIME + 120
    assert accepted.span_seconds == 120
    assert accepted.cumulative_loss_usd_micros == 0
    payload = json.loads(accepted.payload)
    assert payload["schema"] == "oasis.account-shadow-window.v1"
    assert payload["first_block"] == 100
    assert payload["last_block"] == 102
    assert payload["span_seconds"] == 120
    assert payload["account_key"] == ACCOUNT_KEY
    assert payload["manifest_digest"] == MANIFEST
    assert payload["execution_eligible"] is False
    assert [entry["seq"] for entry in payload["runs"]] == [1, 2, 3]
    assert accepted.digest == hashlib.sha256(accepted.payload).hexdigest()


def test_a_window_needs_the_required_number_of_accepted_runs() -> None:
    with pytest.raises(ValidationError, match="too few accepted runs"):
        window(sustained_runs()[:2])
    with pytest.raises(ValidationError, match="at least one accepted run"):
        ShadowWindow(
            account_key=ACCOUNT_KEY,
            manifest_digest=MANIFEST,
            policy_digest=POLICY,
            requirements=CANARY_WINDOW_REQUIREMENTS,
            runs=(),
        )


def test_run_sequence_must_be_contiguous_from_one() -> None:
    with pytest.raises(ValidationError, match="sequence must be contiguous"):
        window(
            (
                run(1, block=100, timestamp=BASE_TIME),
                run(3, block=101, timestamp=BASE_TIME + 60),
                run(4, block=102, timestamp=BASE_TIME + 120),
            )
        )


def test_a_halted_or_unresolved_run_is_refused() -> None:
    halted = with_third(run(3, block=102, timestamp=BASE_TIME + 120, halted=True))
    with pytest.raises(ValidationError, match="run is halted"):
        window(halted)
    unresolved = with_third(run(3, block=102, timestamp=BASE_TIME + 120, unresolved_operations=1))
    with pytest.raises(ValidationError, match="unresolved operations"):
        window(unresolved)


def test_block_and_time_must_strictly_advance() -> None:
    with pytest.raises(ValidationError, match="block regressed or repeated"):
        window(
            (
                run(1, block=100, timestamp=BASE_TIME),
                run(2, block=100, timestamp=BASE_TIME + 60),
                run(3, block=102, timestamp=BASE_TIME + 120),
            )
        )
    with pytest.raises(ValidationError, match="time regressed or repeated"):
        window(
            (
                run(1, block=100, timestamp=BASE_TIME),
                run(2, block=101, timestamp=BASE_TIME),
                run(3, block=102, timestamp=BASE_TIME + 120),
            )
        )


def test_a_gap_beyond_the_requirement_is_refused() -> None:
    with pytest.raises(ValidationError, match="gap beyond the requirement"):
        window(
            (
                run(1, block=100, timestamp=BASE_TIME),
                run(2, block=101, timestamp=BASE_TIME + 60),
                run(3, block=102, timestamp=BASE_TIME + 60 + 301),
            )
        )


def test_one_ledger_state_cannot_be_reported_twice() -> None:
    with pytest.raises(ValidationError, match="repeats one ledger state"):
        window(
            (
                run(1, block=100, timestamp=BASE_TIME, state_digest="c" * 64),
                run(2, block=101, timestamp=BASE_TIME + 60, state_digest="c" * 64),
                run(3, block=102, timestamp=BASE_TIME + 120),
            )
        )


def test_a_window_must_span_the_required_duration() -> None:
    with pytest.raises(ValidationError, match="does not span the required duration"):
        window(
            (
                run(1, block=100, timestamp=BASE_TIME),
                run(2, block=101, timestamp=BASE_TIME + 30),
                run(3, block=102, timestamp=BASE_TIME + 60),
            )
        )


def test_build_shadow_window_refuses_a_non_tuple_or_empty_run_set() -> None:
    for candidate in (
        (),
        [run(1)],
    ):
        with pytest.raises(ValueError, match="non-empty tuple of accepted runs"):
            build_shadow_window(
                candidate,  # type: ignore[arg-type]
                account_key=ACCOUNT_KEY,
                manifest_digest=MANIFEST,
                policy_digest=POLICY,
            )


def test_the_digest_binds_the_runs_and_the_requirements() -> None:
    baseline = window()
    staggered = (
        run(1, block=100, timestamp=BASE_TIME),
        run(2, block=101, timestamp=BASE_TIME + 60),
        run(3, block=102, timestamp=BASE_TIME + 180),
    )
    assert baseline.digest != window(staggered).digest
    relaxed = ShadowRequirements(
        min_runs=2,
        min_span_seconds=60,
        max_gap_seconds=300,
        max_staleness_seconds=900,
    )
    assert baseline.digest != window(requirements=relaxed).digest
    assert (
        baseline.digest
        != build_shadow_window(
            sustained_runs(),
            account_key=f"{CHAIN_A}:{OTHER_ACCOUNT}",
            manifest_digest=MANIFEST,
            policy_digest=POLICY,
        ).digest
    )


def test_a_canary_is_admitted_only_for_a_fresh_window_on_the_same_policy() -> None:
    accepted = window()
    policy = make_policy()
    decision = canary_admission(
        accepted,
        policy=policy,
        manifest_digest=MANIFEST,
        cap_usd_micros=50_000_000,
        now=accepted.last_timestamp + 10,
    )
    assert decision.admitted is True
    assert decision.reasons == ()
    assert decision.window_digest == accepted.digest
    assert decision.cap_usd_micros == 50_000_000


def test_canary_admission_refuses_another_manifest_or_policy() -> None:
    accepted = window()
    policy = make_policy()
    decision = canary_admission(
        accepted,
        policy=policy,
        manifest_digest="c" * 64,
        cap_usd_micros=10_000_000,
        now=accepted.last_timestamp + 10,
    )
    assert decision.admitted is False
    assert decision.reasons == ("shadow window was observed under a different manifest",)
    assert canary_admission(
        accepted,
        policy=make_policy(total=LIMIT_TOTAL - 1),
        manifest_digest=MANIFEST,
        cap_usd_micros=10_000_000,
        now=accepted.last_timestamp + 10,
    ).reasons == ("shadow window was observed under a different policy",)


def test_canary_admission_refuses_an_account_outside_the_policy() -> None:
    accepted = window(account_key=f"{CHAIN_A}:{OTHER_ACCOUNT}")
    decision = canary_admission(
        accepted,
        policy=make_policy(),
        manifest_digest=MANIFEST,
        cap_usd_micros=10_000_000,
        now=accepted.last_timestamp + 10,
    )
    assert decision.admitted is False
    assert decision.reasons == ("shadow window account is outside the policy inventory",)


def test_canary_admission_refuses_a_future_or_stale_window() -> None:
    accepted = window()
    future = canary_admission(
        accepted,
        policy=make_policy(),
        manifest_digest=MANIFEST,
        cap_usd_micros=10_000_000,
        now=accepted.last_timestamp - 1,
    )
    assert future.admitted is False
    assert future.reasons == ("shadow window is in the future",)
    stale = canary_admission(
        accepted,
        policy=make_policy(),
        manifest_digest=MANIFEST,
        cap_usd_micros=10_000_000,
        now=accepted.last_timestamp + CANARY_WINDOW_REQUIREMENTS.max_staleness_seconds + 1,
    )
    assert stale.admitted is False
    assert stale.reasons == ("shadow window is stale",)


def test_canary_admission_bounds_the_cap_by_capital_and_loss_budgets() -> None:
    accepted = window()
    policy = make_policy()
    daily = canary_admission(
        accepted,
        policy=policy,
        manifest_digest=MANIFEST,
        cap_usd_micros=LIMIT_DAILY + 1,
        now=accepted.last_timestamp + 10,
    )
    assert daily.admitted is False
    assert daily.reasons == ("canary cap exceeds the daily loss budget",)
    total = canary_admission(
        accepted,
        policy=policy,
        manifest_digest=MANIFEST,
        cap_usd_micros=LIMIT_TOTAL + 1,
        now=accepted.last_timestamp + 10,
    )
    assert total.reasons == (
        "canary cap exceeds the approved total capital",
        "canary cap exceeds the daily loss budget",
        "canary cap exceeds the remaining cumulative loss budget",
    )
    spent = window(
        with_third(
            run(3, block=102, timestamp=BASE_TIME + 120, cumulative_loss_usd_micros=400_000_000)
        )
    )
    exhausted = canary_admission(
        spent,
        policy=policy,
        manifest_digest=MANIFEST,
        cap_usd_micros=LIMIT_CUMULATIVE - 400_000_000 + 1,
        now=spent.last_timestamp + 10,
    )
    assert exhausted.reasons == ("canary cap exceeds the remaining cumulative loss budget",)


def test_canary_admission_enforces_types_and_outcome_invariants() -> None:
    accepted = window()
    policy = make_policy()
    now = accepted.last_timestamp + 10
    with pytest.raises(TypeError, match="typed shadow window required"):
        canary_admission(
            "window", policy=policy, manifest_digest=MANIFEST, cap_usd_micros=1, now=now
        )
    with pytest.raises(TypeError, match="typed global risk policy required"):
        canary_admission(
            accepted, policy="policy", manifest_digest=MANIFEST, cap_usd_micros=1, now=now
        )
    with pytest.raises(ValueError, match="integer wall clock required"):
        canary_admission(
            accepted, policy=policy, manifest_digest=MANIFEST, cap_usd_micros=1, now=-1
        )
    with pytest.raises(ValueError, match="positive integer of USD micros"):
        canary_admission(
            accepted, policy=policy, manifest_digest=MANIFEST, cap_usd_micros=0, now=now
        )
    with pytest.raises(ValueError, match="cannot carry reasons"):
        CanaryAdmission(admitted=True, reasons=("x",), window_digest="d" * 64, cap_usd_micros=1)
    with pytest.raises(ValueError, match="requires explicit reasons"):
        CanaryAdmission(admitted=False, reasons=(), window_digest="d" * 64, cap_usd_micros=1)
    with pytest.raises(ValueError, match="must be boolean"):
        CanaryAdmission(admitted=1, reasons=(), window_digest="d" * 64, cap_usd_micros=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive integer of USD micros"):
        CanaryAdmission(admitted=False, reasons=("x",), window_digest="d" * 64, cap_usd_micros=0)


@dataclass(frozen=True, slots=True)
class Loop:
    """One scripted run of the observation loop."""

    outcome: ShadowOutcome
    recorded: tuple[ObservationRun, ...]
    pinned: tuple[int, ...]
    sleeps: tuple[float, ...]
    unspent: tuple[int, ...]


def drive(codes: Sequence[int]) -> Loop:
    """Run `observe` with a scripted observer exit code per attempt."""
    remaining = list(codes)
    recorded: list[ObservationRun] = []
    pinned: list[int] = []
    sleeps: list[float] = []

    def tip() -> dict[int, int]:
        block = 100 + len(pinned)
        pinned.append(block)
        return {CHAIN_A: block}

    def attempt(directory: Path, blocks: Mapping[int, int]) -> int:
        if not remaining:
            raise AssertionError("the loop attempted more observers than scripted")
        return remaining.pop(0)

    def read(directory: Path, blocks: Mapping[int, int]) -> ObservationRun:
        index = len(recorded) + 1
        block = blocks[CHAIN_A]
        return run(index, block=block, timestamp=BASE_TIME + 60 * index)

    def record(observed: ObservationRun) -> None:
        recorded.append(observed)

    outcome = observe(
        runs_required=3,
        attempts_per_run=2,
        run_interval_seconds=60.0,
        ports=ObservationPorts(
            tip=tip,
            attempt=attempt,
            read=read,
            record=record,
            sleep=sleeps.append,
        ),
    )
    return Loop(outcome, tuple(recorded), tuple(pinned), tuple(sleeps), tuple(remaining))


def test_the_loop_retries_a_capture_failure_at_a_fresh_pinned_block() -> None:
    result = drive([2, 0, 0, 0])
    assert result.outcome.status == "complete"
    assert result.outcome.reason == "3 consecutive accepted runs"
    assert [r.seq for r in result.recorded] == [1, 2, 3]
    assert [r.block_number for r in result.recorded] == [101, 102, 103]
    assert result.pinned == (100, 101, 102, 103)
    assert result.sleeps == (60.0, 60.0)
    assert result.recorded == result.outcome.runs
    assert result.unspent == ()


def test_the_loop_halts_on_a_refused_capture_without_retrying(tmp_path, monkeypatch) -> None:
    refused = tmp_path / "attempt-002-01"
    refused.mkdir()
    (refused / "report.json").write_bytes(
        canonical_json({"rejected_reason": "global loss latch blocked admission"}) + b"\n"
    )
    monkeypatch.chdir(tmp_path)
    result = drive([0, 3, 0, 0])
    assert result.outcome.status == "halted"
    assert len(result.outcome.runs) == 1
    assert "ledger refused an accepted capture" in result.outcome.reason
    assert "global loss latch blocked admission" in result.outcome.reason
    assert result.recorded == result.outcome.runs
    assert result.sleeps == (60.0,)
    assert result.unspent == (0, 0)


def test_the_loop_stops_incomplete_when_capture_attempts_are_exhausted() -> None:
    result = drive([2, 2])
    assert result.outcome.status == "incomplete"
    assert result.outcome.runs == ()
    assert "attempts exhausted after 2 tries at run 1" in result.outcome.reason
    assert result.pinned == (100, 101)
    assert result.unspent == ()


def test_the_loop_aborts_on_an_invalid_observer_configuration() -> None:
    result = drive([0, 1])
    assert result.outcome.status == "incomplete"
    assert result.outcome.reason == "observer configuration is invalid"
    assert len(result.outcome.runs) == 1
    assert result.sleeps == (60.0,)


def test_an_incomplete_outcome_reports_its_own_status_only() -> None:
    with pytest.raises(ValueError, match="must be complete, halted or incomplete"):
        ShadowOutcome("done", "reason", ())  # type: ignore[arg-type]


def retained_attempt(
    root: Path,
    *,
    ledger_accepted: bool = True,
    halted: bool = False,
    rejected_reason: str = "",
    chain_id: int = CHAIN_A,
    block: int = 100,
    block_hash: str = BLOCK_HASH,
    timestamp: int = BASE_TIME,
    unresolved_operations: int = 0,
    cumulative_loss_usd_micros: int | None = 0,
    inventory_block: int | None = None,
    inventory_hash: str | None = None,
    accounts: int = 1,
) -> Path:
    """One retained observer attempt, as `run_account_risk_reconcile.py` writes it."""
    directory = root / "attempt"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "state.json").write_bytes(b'{"halted": false}\n')
    (directory / "report.json").write_bytes(
        canonical_json(
            {
                "ledger_accepted": ledger_accepted,
                "halted": halted,
                "rejected_reason": rejected_reason,
                "cumulative_loss_usd_micros": cumulative_loss_usd_micros,
                "inventory": [
                    {
                        "key": ACCOUNT_KEY,
                        "block": block if inventory_block is None else inventory_block,
                        "block_hash": BLOCK_HASH if inventory_hash is None else inventory_hash,
                    }
                ],
            }
        )
        + b"\n"
    )
    account = {
        "chain_id": chain_id,
        "account": ACCOUNT_A,
        "block_number": block,
        "block_hash": block_hash,
        "timestamp": timestamp,
        "unresolved_operations": unresolved_operations,
    }
    (directory / "frame.json").write_bytes(
        canonical_json({"accounts": [account] * accounts}) + b"\n"
    )
    return directory


def test_a_retained_run_binds_the_frame_to_the_pinned_block(tmp_path) -> None:
    directory = retained_attempt(tmp_path)
    observed = _retained_run(directory, {CHAIN_A: 100})
    assert observed.seq == 1
    assert observed.block_number == 100
    assert observed.block_hash == BLOCK_HASH
    assert observed.timestamp == BASE_TIME
    assert observed.halted is False
    assert observed.unresolved_operations == 0
    assert observed.cumulative_loss_usd_micros == 0
    assert (
        observed.state_digest == hashlib.sha256((directory / "state.json").read_bytes()).hexdigest()
    )
    assert (
        observed.report_digest
        == hashlib.sha256((directory / "report.json").read_bytes()).hexdigest()
    )


def test_a_retained_run_refuses_anything_it_cannot_bind(tmp_path) -> None:
    cases = [
        ({"ledger_accepted": False}, "accepted, unhalted ledger state"),
        ({"halted": True}, "accepted, unhalted ledger state"),
        ({"rejected_reason": "unreconciled per-asset residual"}, "rejected frame"),
        ({"block": 101}, "not at the pinned block"),
        ({"inventory_block": 101}, "disagree on the block"),
        ({"inventory_hash": "0x" + "ef" * 32}, "disagree on the block hash"),
        ({"chain_id": 8453}, "chain is not the observed chain"),
        ({"accounts": 2}, "exactly one reconciled account"),
        ({"cumulative_loss_usd_micros": None}, "no reconciled cumulative loss"),
    ]
    for index, (overrides, expected) in enumerate(cases):
        directory = retained_attempt(tmp_path / str(index), **overrides)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match=expected):
            _retained_run(directory, {CHAIN_A: 100})


def test_the_observer_command_matches_the_reconciliation_cli() -> None:
    command = _observer_command(
        Path("configs/manifest.json"),
        Path("out/attempts/attempt-001-01"),
        rpc=["8453:BASE_RPC_PRIMARY:BASE_RPC_SECONDARY"],
        chain_id=8453,
        block=51_740_095,
        env_file=Path(".env"),
        state=None,
        interval_ms=3000,
        max_requests=128,
    )
    assert command[0] == sys.executable
    assert command[1].endswith("run_account_risk_reconcile.py")
    assert command[2:] == [
        "--manifest",
        "configs/manifest.json",
        "--output",
        "out/attempts/attempt-001-01",
        "--block",
        "8453=51740095",
        "--env-file",
        ".env",
        "--interval-ms",
        "3000",
        "--max-requests",
        "128",
        "--rpc",
        "8453:BASE_RPC_PRIMARY:BASE_RPC_SECONDARY",
    ]
    chained = _observer_command(
        Path("configs/manifest.json"),
        Path("out/attempts/attempt-002-01"),
        rpc=["8453:BASE_RPC_PRIMARY:BASE_RPC_SECONDARY"],
        chain_id=8453,
        block=51_740_200,
        env_file=Path(".env"),
        state=Path("out/attempts/attempt-001-01/state.json"),
        interval_ms=3000,
        max_requests=128,
    )
    assert chained[-2:] == ["--state", "out/attempts/attempt-001-01/state.json"]


@dataclass(frozen=True, slots=True)
class Gate:
    """A real manifest plus the retained window that claims to describe it."""

    portfolio: object
    window: Path
    manifest: Path
    key: str


def gate(tmp_path: Path, *, mutate: str | None = None, manifest_digest: str | None = None) -> Gate:
    tmp_path.mkdir(parents=True, exist_ok=True)
    portfolio = manifest(InventoryFixture())
    key = portfolio.chains[0].key
    record = build_shadow_window(
        sustained_runs(),
        account_key=key,
        manifest_digest=portfolio.digest if manifest_digest is None else manifest_digest,
        policy_digest=portfolio.policy.digest,
        requirements=CANARY_WINDOW_REQUIREMENTS,
    )
    payload = json.loads(record.payload)
    if mutate is not None:
        payload[mutate] = "tampered"
    window_path = tmp_path / "window.json"
    window_path.write_bytes(canonical_json(payload) + b"\n")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(canonical_json(portfolio.model_dump(mode="json")) + b"\n")
    return Gate(portfolio, window_path, manifest_path, key)


def run_gate(
    monkeypatch,
    retained: Gate,
    *,
    cap: str = "50",
    now: int = BASE_TIME + 130,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_canary_admission",
            "--window",
            str(retained.window),
            "--manifest",
            str(retained.manifest),
            "--cap-usd",
            cap,
            "--now",
            str(now),
        ],
    )
    admission_main()


def test_the_gate_admits_a_fresh_window_and_still_permits_no_execution(
    tmp_path, monkeypatch, capsys
) -> None:
    retained = gate(tmp_path)
    run_gate(monkeypatch, retained)
    result = json.loads(capsys.readouterr().out)
    assert result["schema"] == "oasis.account-canary-admission.v1"
    assert result["admitted"] is True
    assert result["reasons"] == []
    assert result["execution_eligible"] is False
    assert result["account_key"] == retained.key
    assert result["runs"] == 3
    assert result["span_seconds"] == 120
    assert result["cap_usd_micros"] == 50_000_000


def test_the_gate_refuses_a_stale_or_foreign_window(tmp_path, monkeypatch, capsys) -> None:
    retained = gate(tmp_path)
    with pytest.raises(SystemExit) as stale:
        run_gate(monkeypatch, retained, now=BASE_TIME + 120 + 901)
    assert stale.value.code == 2
    assert json.loads(capsys.readouterr().out)["reasons"] == ["shadow window is stale"]
    foreign = gate(tmp_path / "foreign", manifest_digest="c" * 64)
    with pytest.raises(SystemExit) as other:
        run_gate(monkeypatch, foreign)
    assert other.value.code == 2
    assert json.loads(capsys.readouterr().out)["reasons"] == [
        "shadow window was observed under a different manifest"
    ]


def test_the_gate_rejects_an_altered_window_or_a_malformed_cap(tmp_path, monkeypatch) -> None:
    altered = gate(tmp_path / "altered", mutate="span_seconds")
    with pytest.raises(SystemExit) as tampered:
        run_gate(monkeypatch, altered)
    assert tampered.value.code == 1
    malformed = gate(tmp_path / "cap")
    with pytest.raises(SystemExit) as bad_cap:
        run_gate(monkeypatch, malformed, cap="fifty dollars")
    assert bad_cap.value.code == 1
