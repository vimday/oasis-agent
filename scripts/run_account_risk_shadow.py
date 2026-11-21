"""Sustained, observe-only reconciliation loop that records a shadow window.

Run from the repository root as ``python -m scripts.run_account_risk_shadow``.

Each attempt runs the reconciliation observer once, in an isolated subprocess, at
a freshly pinned block. Only accepted runs enter the window: a capture failure
(exit 2) is retried at a new pinned block, an accepted capture the ledger refused
(exit 3) stops the loop immediately, and a configuration error (exit 1) aborts.

This tool opens no risk journal, holds no key, and offers no signing or broadcast
path; `execution_eligible` is always false. Attempt output is captured and
discarded rather than persisted, because a provider error can echo an endpoint
URL: run `run_account_risk_reconcile.py` directly to inspect a failing attempt,
and read that attempt's retained `report.json` for its refusal reason.

Exit codes: 0 = window recorded, 2 = ledger refused an accepted capture,
3 = window incomplete (attempts exhausted or an unhealable gap), 1 = invalid
configuration.
"""

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from oasis_agent.errors import RpcError
from oasis_agent.evidence.rpc_validation import rpc_quantity
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.risk.portfolio_manifest import ChainInventory, PortfolioManifest
from oasis_agent.risk.shadow_acceptance import (
    CANARY_WINDOW_REQUIREMENTS,
    ObservationRun,
    ShadowRequirements,
    ShadowWindow,
    build_shadow_window,
)
from oasis_agent.rpc.transport import JsonRpcTransport
from scripts.run_account_risk_reconcile import (
    _bind_endpoints,
    _environment,
    _rpc_map,
)

WINDOW_SCHEMA = "oasis.account-shadow-outcome.v1"
OBSERVER = Path(__file__).with_name("run_account_risk_reconcile.py")
_LEDGER_REFUSED = 3
_INVALID_CONFIGURATION = 1


@dataclass(frozen=True, slots=True)
class ShadowOutcome:
    status: Literal["complete", "halted", "incomplete"]
    reason: str
    runs: tuple[ObservationRun, ...]

    def __post_init__(self) -> None:
        if self.status not in {"complete", "halted", "incomplete"}:
            raise ValueError("shadow outcome status must be complete, halted or incomplete")
        if not isinstance(self.reason, str):
            raise TypeError("shadow outcome reason must be a string")


@dataclass(frozen=True, slots=True)
class ObservationPorts:
    """The loop's only side effects, injected so the sequence is unit-testable."""

    tip: Callable[[], dict[int, int]]
    attempt: Callable[[Path, Mapping[int, int]], int]
    read: Callable[[Path, Mapping[int, int]], ObservationRun]
    record: Callable[[ObservationRun], None]
    sleep: Callable[[float], None]


def _json(path: Path) -> dict:
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict):
        raise ValueError("retained reconciliation artifact must be a JSON object")
    return data


def _retained_run(directory: Path, blocks: Mapping[int, int]) -> ObservationRun:
    """Read one accepted attempt's retained artifacts into a window run.

    The frame is the authoritative observation, and it must sit at the block the
    loop pinned; the report supplies the ledger facts and its own digest.
    """
    report_path = directory / "report.json"
    state_path = directory / "state.json"
    frame_path = directory / "frame.json"
    report = _json(report_path)
    frame = _json(frame_path)
    if report.get("ledger_accepted") is not True or report.get("halted") is not False:
        raise ValueError("attempt did not retain an accepted, unhalted ledger state")
    if report.get("rejected_reason"):
        raise ValueError("attempt retained a rejected frame")
    accounts = frame.get("accounts")
    if not isinstance(accounts, list) or len(accounts) != 1:
        raise ValueError("attempt must retain exactly one reconciled account")
    account = accounts[0]
    chain_id = account.get("chain_id")
    if type(chain_id) is not int or chain_id not in blocks:
        raise ValueError("retained frame chain is not the observed chain")
    if account.get("block_number") != blocks[chain_id]:
        raise ValueError("retained frame is not at the pinned block")
    inventory = report.get("inventory")
    if not isinstance(inventory, list) or len(inventory) != 1:
        raise ValueError("attempt must retain exactly one inventory entry")
    if inventory[0].get("block") != account.get("block_number"):
        raise ValueError("retained inventory and frame disagree on the block")
    if inventory[0].get("block_hash") != account.get("block_hash"):
        raise ValueError("retained inventory and frame disagree on the block hash")
    cumulative_loss = report.get("cumulative_loss_usd_micros")
    if type(cumulative_loss) is not int or cumulative_loss < 0:
        raise ValueError("retained report has no reconciled cumulative loss")
    return ObservationRun(
        seq=1,
        block_number=account["block_number"],
        block_hash=account["block_hash"],
        timestamp=account["timestamp"],
        state_digest=sha256_hex(state_path.read_bytes()),
        report_digest=sha256_hex(report_path.read_bytes()),
        cumulative_loss_usd_micros=cumulative_loss,
        unresolved_operations=account["unresolved_operations"],
        halted=False,
    )


def _refusal_reason(directory: Path) -> str:
    """The observer's own bounded refusal text, never a provider message."""
    try:
        reason = _json(directory / "report.json").get("rejected_reason")
    except OSError, ValueError:
        return "reason not retained"
    return reason if isinstance(reason, str) and reason else "reason not retained"


def observe(
    *,
    runs_required: int,
    attempts_per_run: int,
    run_interval_seconds: float,
    max_gap_seconds: int,
    ports: ObservationPorts,
) -> ShadowOutcome:
    """Collect `runs_required` accepted observations, or stop fail-closed.

    A capture failure — including a provider read that fails while pinning the
    head — is retried at a fresh pinned block, so a flaky provider costs an
    attempt rather than the window. An accepted capture the ledger refused is
    never retried: it is a risk signal, not a transport failure. An accepted run
    that lands beyond the window's own maximum gap ends the loop immediately,
    because no later run can heal a sequence `ShadowWindow` will refuse.
    """
    runs: list[ObservationRun] = []
    while len(runs) < runs_required:
        accepted = False
        for number in range(1, attempts_per_run + 1):
            directory = Path(f"attempt-{len(runs) + 1:03d}-{number:02d}")
            try:
                blocks = ports.tip()
            except RpcError:
                # Pinning the head is itself a provider read, so a transport failure
                # here costs an attempt rather than the whole observed window.
                continue
            code = ports.attempt(directory, blocks)
            if code == _LEDGER_REFUSED:
                return ShadowOutcome(
                    "halted",
                    f"ledger refused an accepted capture: {_refusal_reason(directory)}",
                    tuple(runs),
                )
            if code == _INVALID_CONFIGURATION:
                return ShadowOutcome("incomplete", "observer configuration is invalid", tuple(runs))
            if code != 0:
                continue
            run = ports.read(directory, blocks).model_copy(update={"seq": len(runs) + 1})
            if runs and run.timestamp - runs[-1].timestamp > max_gap_seconds:
                return ShadowOutcome(
                    "incomplete",
                    f"observation gap exceeded the requirement at run {len(runs) + 1}",
                    tuple(runs),
                )
            runs.append(run)
            ports.record(run)
            accepted = True
            break
        if not accepted:
            return ShadowOutcome(
                "incomplete",
                f"capture attempts exhausted after {attempts_per_run} tries at run {len(runs) + 1}",
                tuple(runs),
            )
        if len(runs) < runs_required:
            ports.sleep(run_interval_seconds)
    return ShadowOutcome("complete", f"{len(runs)} consecutive accepted runs", tuple(runs))


def resolve_schedule(
    *,
    runs: int | None,
    attempts_per_run: int,
    run_interval_seconds: float,
    requirements: ShadowRequirements,
) -> int:
    """Return the accepted-run count, refusing a schedule that cannot satisfy it.

    The clock a window is measured on is the pinned block time, so a real gap is
    at least the sleep interval plus the observer's own duration. An interval that
    reaches the maximum gap therefore cannot produce an accepted window, and an
    interval that cannot reach the minimum span cannot either; both are refused
    here rather than after the whole schedule has been spent.
    """
    if not 1 <= attempts_per_run <= 10:
        raise ValueError("attempts per run must be 1..10")
    if not 0 < run_interval_seconds < requirements.max_gap_seconds:
        raise ValueError("run interval must leave headroom within the maximum gap")
    runs_required = requirements.min_runs if runs is None else runs
    if (runs_required - 1) * run_interval_seconds < requirements.min_span_seconds:
        raise ValueError("schedule cannot span the required window duration")
    return runs_required


def _tip_blocks(entry: ChainInventory, urls: Mapping[str, str], *, lag: int) -> dict[int, int]:
    """Pin a fresh block behind the primary source's tip, never at the tip itself."""
    transport = JsonRpcTransport(urls[entry.primary_source], timeout=20, retries=0)
    tip = rpc_quantity(transport.request("eth_blockNumber", []))
    if tip <= lag:
        raise ValueError("pinned block lag is beyond the observed tip")
    return {entry.chain_id: tip - lag}


def _observer_command(
    manifest: Path,
    output: Path,
    *,
    rpc: Sequence[str],
    chain_id: int,
    block: int,
    env_file: Path,
    state: Path | None,
    interval_ms: int,
    max_requests: int,
) -> list[str]:
    command = [
        sys.executable,
        str(OBSERVER),
        "--manifest",
        str(manifest),
        "--output",
        str(output),
        "--block",
        f"{chain_id}={block}",
        "--env-file",
        str(env_file),
        "--interval-ms",
        str(interval_ms),
        "--max-requests",
        str(max_requests),
    ]
    for entry in rpc:
        command.extend(["--rpc", entry])
    if state is not None:
        command.extend(["--state", str(state)])
    return command


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--rpc", action="append", default=[], metavar="CHAIN:PRIMARY:SECONDARY")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--runs", type=int, help="accepted runs required (default: the minimum)")
    parser.add_argument("--attempts-per-run", type=int, default=3)
    parser.add_argument("--run-interval-seconds", type=float, default=60.0)
    parser.add_argument("--tip-lag", type=int, default=5)
    parser.add_argument("--interval-ms", type=int, default=3000)
    parser.add_argument("--max-requests", type=int, default=256)
    parser.add_argument("--min-span-seconds", type=int, default=None)
    parser.add_argument("--max-gap-seconds", type=int, default=None)
    parser.add_argument("--max-staleness-seconds", type=int, default=None)
    args = parser.parse_args()

    requirements = ShadowRequirements(
        min_runs=args.runs or CANARY_WINDOW_REQUIREMENTS.min_runs,
        min_span_seconds=(
            CANARY_WINDOW_REQUIREMENTS.min_span_seconds
            if args.min_span_seconds is None
            else args.min_span_seconds
        ),
        max_gap_seconds=(
            CANARY_WINDOW_REQUIREMENTS.max_gap_seconds
            if args.max_gap_seconds is None
            else args.max_gap_seconds
        ),
        max_staleness_seconds=(
            CANARY_WINDOW_REQUIREMENTS.max_staleness_seconds
            if args.max_staleness_seconds is None
            else args.max_staleness_seconds
        ),
    )
    try:
        manifest = PortfolioManifest.model_validate_json(args.manifest.read_bytes())
        if len(manifest.keys) != 1 or len(manifest.chains) != 1:
            raise ValueError("a shadow window observes exactly one declared account")
        _bind_endpoints(manifest, _rpc_map(args.rpc))
        entry = manifest.chains[0]
        urls = _environment(args.env_file, {entry.primary_source, entry.secondary_source})
        runs_required = resolve_schedule(
            runs=args.runs,
            attempts_per_run=args.attempts_per_run,
            run_interval_seconds=args.run_interval_seconds,
            requirements=requirements,
        )
        args.out.mkdir(parents=True, exist_ok=False)
    except Exception:  # noqa: BLE001 - never print endpoint values
        parser.exit(1, "Invalid shadow-window configuration; no signing is available.\n")

    attempts = args.out / "attempts"
    attempts.mkdir()
    runs_path = args.out / "runs.jsonl"
    state: Path | None = None

    def tip() -> dict[int, int]:
        return _tip_blocks(entry, urls, lag=args.tip_lag)

    def attempt(directory: Path, blocks: Mapping[int, int]) -> int:
        nonlocal state
        target = attempts / directory
        command = _observer_command(
            args.manifest,
            target,
            rpc=args.rpc,
            chain_id=entry.chain_id,
            block=blocks[entry.chain_id],
            env_file=args.env_file,
            state=state,
            interval_ms=args.interval_ms,
            max_requests=args.max_requests,
        )
        try:
            completed = subprocess.run(command, capture_output=True, check=False)
        except OSError:
            return _INVALID_CONFIGURATION
        if completed.returncode == 0:
            state = target / "state.json"
        return completed.returncode

    def read(directory: Path, blocks: Mapping[int, int]) -> ObservationRun:
        return _retained_run(attempts / directory, blocks)

    def record(run: ObservationRun) -> None:
        with runs_path.open("ab") as stream:
            stream.write(canonical_json(run.model_dump(mode="json")) + b"\n")

    outcome = observe(
        runs_required=runs_required,
        attempts_per_run=args.attempts_per_run,
        run_interval_seconds=args.run_interval_seconds,
        max_gap_seconds=requirements.max_gap_seconds,
        ports=ObservationPorts(
            tip=tip,
            attempt=attempt,
            read=read,
            record=record,
            sleep=time.sleep,
        ),
    )
    window: ShadowWindow | None = None
    if outcome.status == "complete":
        try:
            window = build_shadow_window(
                outcome.runs,
                account_key=entry.key,
                manifest_digest=manifest.digest,
                policy_digest=manifest.policy.digest,
                requirements=requirements,
            )
        except ValueError as error:
            outcome = ShadowOutcome("incomplete", str(error), outcome.runs)
    if window is not None:
        (args.out / "window.json").write_bytes(window.payload + b"\n")
    report = {
        "schema": WINDOW_SCHEMA,
        "status": outcome.status,
        "reason": outcome.reason,
        "runs": len(outcome.runs),
        "window_digest": None if window is None else window.digest,
        "execution_eligible": False,
    }
    (args.out / "outcome.json").write_bytes(canonical_json(report) + b"\n")
    print(canonical_json({"status": outcome.status, "window": window is not None}).decode())
    if outcome.status == "halted":
        parser.exit(2, "Ledger refused an accepted capture.\n")
    if outcome.status == "incomplete":
        parser.exit(3, "Shadow window is incomplete.\n")


if __name__ == "__main__":
    main()
