"""Capture an explicit historical market range in resumable, budgeted chunks.

Use the same output directory to resume. Retained chunks are replayed before any
network call; failures never reset the budget or skip an unverified range.
"""

import argparse
import fcntl
import json
import os
import signal
from dataclasses import asdict
from pathlib import Path

from pydantic import TypeAdapter

from oasis_agent.evidence.exact_capture import wire_envelope
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.lp_dataset_coverage import assess_dataset_coverage
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetedPoster, BudgetPolicy, RpcBudget
from oasis_agent.rpc.transport import HttpPoster, JsonRpcTransport
from oasis_agent.shadow.market_archive import (
    ArchiveConfig,
    MarketChunk,
    build_archive_window,
    capture_chunk,
    validate_archive,
)
from oasis_agent.shadow.market_window import InsufficientMaturityTail
from scripts.report_lp_market_economics import summarize
from scripts.run_pool_state_observer import Http
from scripts.run_v4_route_probe import CurlPoster


def _write(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(canonical_json(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def capture(
    config: ArchiveConfig,
    posters: dict[str, HttpPoster],
    output: Path,
    *,
    endpoint_hashes: dict[str, str],
    max_chunks: int = 1,
) -> dict[str, object]:
    if type(max_chunks) is not int or not 1 <= max_chunks <= 1000:
        raise ValueError("bounded chunk count required")
    if set(posters) != {"public", "paid"} or set(endpoint_hashes) != set(posters):
        raise ValueError("two bound providers required")
    if len(set(endpoint_hashes.values())) != 2:
        raise ValueError("distinct endpoint bindings required")
    output.mkdir(parents=True, exist_ok=True)
    with (output / "owner.lock").open("a+b") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _capture_locked(config, posters, output, endpoint_hashes, max_chunks)


def _capture_locked(config, posters, output, endpoint_hashes, max_chunks):
    binding = {
        "schema": "oasis.market-archive.v1",
        "config": config.model_dump(mode="json"),
        "endpoint_hashes": endpoint_hashes,
    }
    manifest = output / "manifest.json"
    if manifest.exists():
        if json.loads(manifest.read_bytes()) != binding:
            raise ValueError("archive binding changed")
        if not (output / "budget.db").is_file() or not (output / "evidence.db").is_file():
            raise ValueError("archive lost evidence or budget")
    else:
        if any(p.name != "owner.lock" for p in output.iterdir()):
            raise ValueError("unbound archive directory is not empty")
    observer = config.observer
    policies = tuple(
        BudgetPolicy(
            source,
            config.max_requests_per_provider,
            observer.public_interval_ms if source == "public" else observer.paid_interval_ms,
        )
        for source in ("public", "paid")
    )
    with RpcBudget(output / "budget.db", policies) as budget:
        budget.counts()
    store = EvidenceStore(output / "evidence.db")
    try:
        if not manifest.exists():
            _write(manifest, binding)
        paths = sorted(output.glob("chunk-*.json"))
        if [p.name for p in paths] != [f"chunk-{i:04d}.json" for i in range(len(paths))]:
            raise ValueError("archive chunk sequence missing")
        chunks = tuple(TypeAdapter(MarketChunk).validate_json(p.read_bytes()) for p in paths)
        validate_archive(store, chunks, config)
        completed = len(chunks)
        status_path = output / "status.json"
        if status_path.exists():
            previous = json.loads(status_path.read_bytes())
            acknowledged = previous.get("chunks_completed")
            if (
                previous.get("schema") != "oasis.market-archive-status.v1"
                or type(acknowledged) is not int
                or not 0 <= acknowledged <= completed
                or previous.get("chunks_total") != len(config.ranges)
            ):
                raise ValueError("archive checkpoint contradicts retained chunks")
        elif completed:
            raise ValueError("archive lost its progress checkpoint")
        status = {
            "schema": "oasis.market-archive-status.v1",
            "execution_eligible": False,
            "historical_only": True,
            "chunks_completed": completed,
            "chunks_total": len(config.ranges),
            "stage": "acquiring",
        }

        def save_status():
            with RpcBudget(output / "budget.db", policies) as ledger:
                spent = ledger.counts()
            remaining = {
                source: config.max_requests_per_provider - count for source, count in spent.items()
            }
            required_to_finish = config.requests_to_finish(status["chunks_completed"])
            status.update(
                request_counts=spent,
                remaining_requests=remaining,
                minimum_requests_to_finish_per_provider=required_to_finish,
                budget_can_finish_without_failures=all(
                    available >= required_to_finish for available in remaining.values()
                ),
            )
            _write(output / "status.json", status)

        save_status()
        for index in range(completed, min(len(config.ranges), completed + max_chunks)):
            start, end = config.ranges[index]
            required = config.chunk_requests(start, end)
            with RpcBudget(output / "budget.db", policies) as budget:
                counts = budget.counts()
                if any(
                    counts[source] + required > config.max_requests_per_provider
                    for source in posters
                ):
                    status.update(stage="halted", reason="insufficient_remaining_budget")
                    save_status()
                    return status
            sessions = []

            def session(source, records=sessions):
                recorder = BoundedExchangeRecorder()
                rpc = JsonRpcTransport(
                    "https://archive.invalid",
                    retries=0,
                    recorder=recorder,
                    http=BudgetedPoster(posters[source], output / "budget.db", policies, source),
                )
                pair = (rpc, recorder, source)
                records.append(pair)
                return pair

            try:
                chunk = capture_chunk(
                    store,
                    config,
                    start,
                    end,
                    (session("public"), session("paid")),
                    (session("public"), session("paid")),
                )
                validate_archive(store, (*chunks, chunk), config)
                _write(output / f"chunk-{index:04d}.json", asdict(chunk))
                chunks = (*chunks, chunk)
                status.update(chunks_completed=len(chunks))
            except KeyboardInterrupt:
                status.update(stage="stopped", reason="operator_stop")
                return status
            except Exception:  # noqa: BLE001 - RPC errors may contain credentials
                status.update(stage="halted", reason="chunk_capture_or_replay_refused")
                return status
            finally:
                for _, recorder, source in sessions:
                    if recorder.exchanges:
                        store.put(
                            "market_archive_wire",
                            canonical_json({"execution_eligible": False}),
                            None,
                            source,
                            request=wire_envelope(recorder.exchanges, response=False),
                            raw_response=wire_envelope(recorder.exchanges, response=True),
                        )
                save_status()
        status.update(stage="completed" if len(chunks) == len(config.ranges) else "partial")
        if status["stage"] == "completed":
            try:
                window = build_archive_window(store, chunks, config)
                _write(output / "market-window.json", json.loads(window.payload))
                _write(output / "economics.json", summarize(window))
                coverage = assess_dataset_coverage((window,))
                _write(output / "coverage.json", json.loads(coverage.payload))
                status.update(window_ready=True, coverage_passed=coverage.coverage_passed)
            except InsufficientMaturityTail as exc:
                status.update(
                    window_ready=False,
                    reason="market_analysis_refused",
                    analysis_detail={
                        "code": "insufficient_maturity_tail",
                        "observed_span_seconds": exc.observed_seconds,
                        "required_tail_seconds": exc.required_seconds,
                    },
                )
            except Exception:  # noqa: BLE001 - acquisition success is not analysis acceptance
                status.update(
                    window_ready=False,
                    reason="market_analysis_refused",
                    analysis_detail={"code": "analysis_or_output_failure"},
                )
        save_status()
        return status
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-chunks", type=int, default=1)
    parser.add_argument("--transport", choices=("httpx", "curl"), default="httpx")
    args = parser.parse_args()

    def stop_requested(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_requested)
    posters: dict[str, HttpPoster] = {}
    try:
        config = ArchiveConfig.from_json(args.config.read_bytes())
        urls = {"public": os.environ["BASE_RPC_PRIMARY"], "paid": os.environ["BASE_RPC_SECONDARY"]}
        factory = CurlPoster if args.transport == "curl" else Http
        posters = {source: factory(url) for source, url in urls.items()}
        status = capture(
            config,
            posters,
            args.output,
            endpoint_hashes={source: sha256_hex(url.encode()) for source, url in urls.items()},
            max_chunks=args.max_chunks,
        )
    except Exception:  # noqa: BLE001 - never expose endpoints or raw input
        parser.exit(2, "Archive binding or replay refused; inspect retained inputs.\n")
    finally:
        for poster in posters.values():
            if isinstance(poster, Http):
                poster.client.close()
    print(json.dumps(status), flush=True)
    if status["stage"] not in {"partial", "completed"}:
        parser.exit(3, "Archive stopped without skipping the incomplete chunk.\n")


if __name__ == "__main__":
    main()
