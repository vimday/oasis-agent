"""Synthetic event-to-quote pipeline, durable delivery and fail-closed recovery."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from threading import Barrier

import pytest

from oasis_agent.arbitrage import cycle_queue
from oasis_agent.arbitrage.cycle_evidence import load_exports
from oasis_agent.arbitrage.cycle_plan import Plan
from oasis_agent.arbitrage.cycle_queue import (
    CycleShadowQueue,
    LeaseRefused,
    QueueInvalidated,
    ReplayLease,
)
from oasis_agent.arbitrage.cycle_shadow import ShadowWindow, filters, shadow_config_sha256
from oasis_agent.evidence.chain_store import CanonicalChainStore
from oasis_agent.evidence.rpc_capture import persist_block_range_evidence
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.models.chain import BlockSnapshot, LogRecord
from oasis_agent.models.common import BlockRef, canonical_json
from oasis_agent.rpc.collector import BlockRangeDataset
from scripts import process_cycle_shadow_queue, replay_cycle_shadow
from tests.fixtures import cycle_research as fixture
from tests.rpc_evidence_helpers import range_exchanges


@pytest.fixture
def case(tmp_path):
    config = fixture.plan()
    config["windows"][1]["cutoff_utc"] = fixture.DAYS[2]
    plan = Plan.model_validate_json(canonical_json(config))
    window = ShadowWindow.model_validate_json(
        canonical_json(
            {
                "schema": "oasis-cycle-shadow-window-v1",
                "cycle_config_sha256": shadow_config_sha256(plan),
                "source": "synthetic",
                "start_block": 100,
                "end_block": 101,
                "parent_hash": fixture.block_hash(99),
                "locked_utc": "2025-12-31T23:59:59Z",
                "deadline_utc": "2026-01-01T00:00:04Z",
                "max_requests": 30,
            }
        )
    )
    blocks = tuple(
        BlockSnapshot(
            BlockRef(fixture.CHAIN, n, fixture.block_hash(n), fixture.TIMESTAMP),
            fixture.block_hash(n - 1),
        )
        for n in (100, 101)
    )
    store_path, queue_path = tmp_path / "evidence.db", tmp_path / "queue.db"
    store = EvidenceStore(store_path)
    refs, datasets = {}, {}
    for name, watch in filters(plan).items():
        logs = (
            ()
            if name != "third.sync"
            else (
                LogRecord(
                    watch.address,
                    watch.topics,
                    "0x",
                    101,
                    fixture.block_hash(900),
                    0,
                    blocks[-1].ref.block_hash,
                    0,
                ),
            )
        )
        dataset = BlockRangeDataset("synthetic", blocks, logs, watch.address, watch.topics)
        datasets[name] = dataset
        refs[name] = persist_block_range_evidence(
            store, dataset, range_exchanges(dataset), retrieved_at=fixture.DAYS[1]
        )
    now = [1000]
    queue = CycleShadowQueue(queue_path, clock_ms=lambda: now[0])
    context = {
        "plan": plan,
        "window": window,
        "refs": refs,
        "datasets": datasets,
        "store": store,
        "queue": queue,
        "now": now,
        "store_path": store_path,
        "queue_path": queue_path,
        "tmp": tmp_path,
    }
    yield context
    queue.close()
    context["store"].close()


def publish(c, refs=None, session="window"):
    return c["queue"].publish(
        session, c["plan"], c["window"], c["store"], c["refs"] if refs is None else refs
    )


def test_atomic_publication_dedup_restart_and_offline_result(case):
    c = case
    report = publish(c)
    jobs = report["outbox"]["jobs"]
    assert len(jobs) == 1 and jobs[0]["state"] == "pending"
    assert publish(c)["outbox"]["jobs"] == jobs
    lease = c["queue"].claim("window", c["store"], "worker")
    assert lease.job_id == jobs[0]["job_id"]
    assert c["queue"].claim("window", c["store"], "other") is None
    # Empty retained quote input is an unavailable research result, never a fabricated profit.
    result = c["queue"].complete("window", c["store"], lease, ())
    assert result["windows"][0]["decision"] == "unavailable"
    assert result["execution_eligible"] is False
    assert c["queue"].complete("window", c["store"], lease, ()) == result
    db = CycleShadowQueue(c["queue_path"], clock_ms=lambda: 1001)
    try:
        assert db.inspect("window", c["store"])["jobs"][0]["result"] == result
        assert (
            db.publish("window", c["plan"], c["window"], c["store"], c["refs"])["outbox"]["jobs"][
                0
            ]["state"]
            == "completed"
        )
        assert db.claim("window", c["store"], "after-restart") is None
    finally:
        db.close()


def test_partial_inputs_wait_then_complete_but_regression_latches(case):
    c = case
    partial = {k: v for k, v in c["refs"].items() if k != "first.swap"}
    assert publish(c, partial)["outbox"]["state"] == "collecting"
    assert c["queue"].claim("window", c["store"], "worker") is None
    assert publish(c)["outbox"]["jobs"]
    with pytest.raises(QueueInvalidated):
        publish(c, partial)
    assert c["queue"].inspect("window", c["store"])["state"] == "invalidated"
    with pytest.raises(QueueInvalidated):
        publish(c)


def test_expired_delivery_reuses_job_id_and_fences_old_worker(case):
    c = case
    publish(c)
    old = c["queue"].claim("window", c["store"], "old", lease_ms=10)
    c["now"][0] = 1010
    new = c["queue"].claim("window", c["store"], "new", lease_ms=10)
    assert new.job_id == old.job_id and new.attempt == 2
    with pytest.raises(LeaseRefused):
        c["queue"].complete("window", c["store"], old, ())
    assert c["queue"].complete("window", c["store"], new, ())["execution_eligible"] is False


def test_expired_completion_does_not_claim_delivery(case):
    c = case
    publish(c)
    lease = c["queue"].claim("window", c["store"], "worker", lease_ms=5)
    c["now"][0] = 1005
    with pytest.raises(LeaseRefused):
        c["queue"].complete("window", c["store"], lease, ())
    assert c["queue"].inspect("window", c["store"])["jobs"][0]["state"] == "leased"


def test_independent_chain_writer_reorg_invalidates_completed_results(case):
    c = case
    publish(c)
    lease = c["queue"].claim("window", c["store"], "worker")
    c["queue"].complete("window", c["store"], lease, ())
    dataset = c["datasets"]["first.swap"]
    first, last = dataset.blocks
    alternative = replace(last, ref=replace(last.ref, block_hash=fixture.block_hash(999)))
    writer = CanonicalChainStore(c["queue_path"])
    writer.apply("external", replace(dataset, blocks=(first, alternative), logs=()))
    writer.close()
    view = c["queue"].inspect("window", c["store"])
    assert view["state"] == "invalidated" and view["reason"] == "canonical_reorg"
    assert all(job["result"] is None for job in view["jobs"])
    with pytest.raises(QueueInvalidated):
        c["queue"].complete("window", c["store"], lease, ())
    assert c["queue"]._connection.execute("SELECT result_json FROM cycle_queue_jobs").fetchone()[0]


def test_reorg_in_publisher_latches_all_existing_chain_sessions(case):
    c = case
    publish(c)
    publish(c, session="second")
    refs = {}
    for name, dataset in c["datasets"].items():
        first, last = dataset.blocks
        alternative = replace(last, ref=replace(last.ref, block_hash=fixture.block_hash(999)))
        changed = replace(
            dataset,
            blocks=(first, alternative),
            logs=tuple(replace(log, block_hash=alternative.ref.block_hash) for log in dataset.logs),
        )
        refs[name] = persist_block_range_evidence(
            c["store"], changed, range_exchanges(changed), retrieved_at=fixture.DAYS[1]
        )
    with pytest.raises(QueueInvalidated):
        publish(c, refs, session="third")
    for name in ("window", "second", "third"):
        assert c["queue"].inspect(name, c["store"])["state"] == "invalidated"


@pytest.mark.parametrize("kind", ["report", "task", "coverage", "evidence", "binding"])
def test_corruption_clears_candidates_and_latches(case, kind):
    c = case
    publish(c)
    if kind == "report":
        c["queue"]._connection.execute("UPDATE cycle_queue_sessions SET report_json = X'7b7d'")
    elif kind == "task":
        c["queue"]._connection.execute("UPDATE cycle_queue_jobs SET task_json = X'7b7d'")
    elif kind == "coverage":
        c["queue"]._connection.execute("DELETE FROM canonical_coverage")
    elif kind == "binding":
        c["queue"]._connection.execute("UPDATE cycle_queue_sessions SET binding_sha = 'invalid'")
    else:
        c["store"].close()
        c["store"] = EvidenceStore(c["tmp"] / "empty-evidence.db")
    c["queue"]._connection.commit()
    view = c["queue"].inspect("window", c["store"])
    assert view["state"] == "invalidated" and not any(job.get("result") for job in view["jobs"])
    with pytest.raises(QueueInvalidated):
        c["queue"].claim("window", c["store"], "worker")


def test_config_change_invalidates_original_session(case):
    c = case
    publish(c)
    plan = c["plan"].model_copy(update={"scenario": "changed"})
    with pytest.raises(QueueInvalidated):
        c["queue"].publish("window", plan, c["window"], c["store"], c["refs"])
    assert c["queue"].inspect("window", c["store"])["reason"] == "frozen_queue_binding_changed"


def test_clock_regression_and_attempt_bound_are_persistent(case):
    c = case
    publish(c)
    for i in range(8):
        c["now"][0] = 1000 + i
        assert c["queue"].claim("window", c["store"], "worker", lease_ms=1).attempt == i + 1
    c["now"][0] = 1008
    with pytest.raises(QueueInvalidated, match="attempt_limit"):
        c["queue"].claim("window", c["store"], "worker")
    assert c["queue"].inspect("window", c["store"])["state"] == "invalidated"
    publish(c, session="clock")
    c["now"][0] = 1007
    with pytest.raises(QueueInvalidated, match="clock_regressed"):
        c["queue"].claim("clock", c["store"], "worker")


def test_competing_workers_only_one_claims(case):
    c = case
    publish(c)
    barrier = Barrier(2)

    def take(worker):
        db = CycleShadowQueue(c["queue_path"], clock_ms=lambda: 1000)
        evidence = EvidenceStore(c["store_path"], require_existing=True)
        barrier.wait(timeout=5)
        try:
            return db.claim("window", evidence, worker)
        finally:
            db.close()
            evidence.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        leases = list(pool.map(take, ["left", "right"]))
    assert sum(lease is not None for lease in leases) == 1


def test_rollback_covers_progress_and_jobs_when_publication_process_exits(case):
    c = case
    # Existing immutable evidence survives; the child exits inside the publication transaction.
    (c["tmp"] / "plan.json").write_bytes(canonical_json(c["plan"].model_dump(by_alias=True)))
    (c["tmp"] / "window.json").write_bytes(canonical_json(c["window"].model_dump(by_alias=True)))
    (c["tmp"] / "refs.json").write_bytes(
        canonical_json({k: asdict(v) for k, v in c["refs"].items()})
    )
    code = """
import os,sys,json
from pathlib import Path
from oasis_agent.arbitrage.cycle_queue import CycleShadowQueue
from oasis_agent.arbitrage import cycle_queue
from oasis_agent.arbitrage.cycle_plan import Plan
from oasis_agent.arbitrage.cycle_evidence import load_exports
from oasis_agent.arbitrage.cycle_shadow import ShadowWindow
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.models.common import evidence_ref_from_json
p=Path(sys.argv[1]);q=CycleShadowQueue(p/'queue.db',clock_ms=lambda:1000)
s=EvidenceStore(p/'evidence.db',require_existing=True)
original=q._event
def crash(session,event,details):
    original(session,event,details)
    if event=='published': os._exit(9)
q._event=crash
q.publish(
    'window', Plan.model_validate_json((p/'plan.json').read_bytes()),
    ShadowWindow.model_validate_json((p/'window.json').read_bytes()), s,
    {k:evidence_ref_from_json(v) for k,v in json.loads((p/'refs.json').read_bytes()).items()},
)
"""
    result = subprocess.run([sys.executable, "-c", code, str(c["tmp"])], check=False, timeout=10)
    assert result.returncode == 9
    assert c["queue"].tip(fixture.CHAIN) is None
    assert (
        c["queue"]._connection.execute("SELECT COUNT(*) FROM cycle_queue_jobs").fetchone()[0] == 0
    )
    assert publish(c)["outbox"]["jobs"][0]["state"] == "pending"


def test_normal_producer_and_worker_cli_replays_actual_quote_fixture(case, monkeypatch):
    c = case
    (c["tmp"] / "cycle.json").write_bytes(canonical_json(c["plan"].model_dump(by_alias=True)))
    (c["tmp"] / "window.json").write_bytes(canonical_json(c["window"].model_dump(by_alias=True)))
    (c["tmp"] / "refs.json").write_bytes(
        canonical_json({k: asdict(v) for k, v in c["refs"].items()})
    )
    fixture.write_bundle(c["tmp"] / "quotes.json", fixture.records())
    producer = [
        "producer",
        "--cycle-plan",
        str(c["tmp"] / "cycle.json"),
        "--window",
        str(c["tmp"] / "window.json"),
        "--store",
        str(c["store_path"]),
        "--refs",
        str(c["tmp"] / "refs.json"),
        "--chain-store",
        str(c["queue_path"]),
        "--queue-session",
        "cli",
        "--output",
        str(c["tmp"] / "producer.json"),
    ]
    monkeypatch.setattr(sys, "argv", producer)
    assert replay_cycle_shadow.main() == 0
    consumer = [
        "worker",
        "run-one",
        "--chain-store",
        str(c["queue_path"]),
        "--store",
        str(c["store_path"]),
        "--session",
        "cli",
        "--export",
        f"synthetic={c['tmp'] / 'quotes.json'}",
        "--output",
        str(c["tmp"] / "worker.json"),
    ]
    monkeypatch.setattr(sys, "argv", consumer)
    assert process_cycle_shadow_queue.main() == 0
    view = json.loads((c["tmp"] / "worker.json").read_bytes())
    result = view["outbox"]["jobs"][0]["result"]
    assert result["windows"][0]["requested_block"] == 101
    assert result["windows"][0]["decision"] == "positive_conditional_quote"
    assert all(point["execution_eligible"] is False for point in result["windows"][0]["points"])
    assert not result["execution_eligible"]
    # Producer report write failure does not undo committed work; retry cannot republish jobs.
    monkeypatch.setattr(sys, "argv", producer)
    assert replay_cycle_shadow.main() == 2
    consumer[-1] = str(c["tmp"] / "restart.json")
    monkeypatch.setattr(sys, "argv", consumer)
    assert process_cycle_shadow_queue.main() == 0
    assert json.loads((c["tmp"] / "restart.json").read_bytes())["lease"] is None
    # A failed integrity check at the producer's normal entry point clears durable candidates.
    (c["tmp"] / "refs.json").write_bytes(b"{invalid")
    monkeypatch.setattr(sys, "argv", producer)
    assert replay_cycle_shadow.main() == 2
    view = c["queue"].inspect("cli", c["store"])
    assert view["state"] == "invalidated" and all(job["result"] is None for job in view["jobs"])


def test_crashed_consumer_is_redelivered_after_restart(case):
    c = case
    publish(c)
    script = """
import os,sys
from pathlib import Path
from oasis_agent.arbitrage.cycle_queue import CycleShadowQueue
from oasis_agent.evidence.store import EvidenceStore
p=Path(sys.argv[1])
q=CycleShadowQueue(p/'queue.db',clock_ms=lambda:1000)
s=EvidenceStore(p/'evidence.db',require_existing=True)
assert q.claim('window',s,'crashed',lease_ms=5) is not None
os._exit(9)
"""
    run = subprocess.run([sys.executable, "-c", script, str(c["tmp"])], check=False, timeout=10)
    assert run.returncode == 9
    c["now"][0] = 1005
    lease = c["queue"].claim("window", c["store"], "recovery")
    assert lease.attempt == 2
    assert c["queue"].complete("window", c["store"], lease, ())["execution_eligible"] is False


def test_quiet_complete_window_does_not_create_jobs(case):
    c = case
    refs = {}
    for name, dataset in c["datasets"].items():
        quiet = replace(dataset, logs=())
        refs[name] = persist_block_range_evidence(
            c["store"], quiet, range_exchanges(quiet), retrieved_at=fixture.DAYS[1]
        )
    assert publish(c, refs)["outbox"]["jobs"] == []
    assert c["queue"].inspect("window", c["store"])["state"] == "observed"


def test_changed_quote_inputs_cannot_overwrite_completed_result(case):
    c = case
    publish(c)
    lease = c["queue"].claim("window", c["store"], "worker")
    first = c["queue"].complete("window", c["store"], lease, ())
    path = c["tmp"] / "quotes.json"
    fixture.write_bundle(path, fixture.records())
    records = load_exports([("synthetic", path)], c["plan"].sources)
    with pytest.raises(LeaseRefused, match="inputs_changed"):
        c["queue"].complete("window", c["store"], lease, records)
    assert c["queue"].inspect("window", c["store"])["jobs"][0]["result"] == first


def test_processing_expiry_during_quote_calculation_refuses_completion(case, monkeypatch):
    c = case
    publish(c)
    lease = c["queue"].claim("window", c["store"], "worker", lease_ms=5)
    original = cycle_queue.evaluate_windows

    def late(*args, **kwargs):
        result = original(*args, **kwargs)
        c["now"][0] = 1005
        return result

    monkeypatch.setattr(cycle_queue, "evaluate_windows", late)
    with pytest.raises(LeaseRefused, match="during_evaluation"):
        c["queue"].complete("window", c["store"], lease, ())
    assert c["queue"].inspect("window", c["store"])["jobs"][0]["result"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("attempt", True),
        ("expires_ms", True),
        ("attempt", 9),
        ("worker", "bad label"),
        ("job_id", "bad"),
    ],
)
def test_invalid_lease_fields_are_refused(field, value):
    fields = {"job_id": "1" * 64, "worker": "worker", "attempt": 1, "expires_ms": 1000}
    fields[field] = value
    with pytest.raises(ValueError):
        ReplayLease(**fields)


def test_missing_usable_cutoff_keeps_event_without_dispatch(case):
    c = case
    config = c["plan"].model_dump(by_alias=True, mode="json")
    for item in config["windows"]:
        item["cutoff_utc"] = None
    plan = Plan.model_validate_json(canonical_json(config))
    window = c["window"].model_copy(update={"cycle_config_sha256": shadow_config_sha256(plan)})
    report = c["queue"].publish("unready", plan, window, c["store"], c["refs"])
    assert report["quote_wakeups"]
    assert report["quote_dispatches"][0]["status"] == "missing_usable_quote_cutoff"
    assert report["outbox"]["jobs"] == []


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE cycle_queue_jobs SET state = 'unknown'",
        "UPDATE cycle_queue_jobs SET attempt = 1",
        "UPDATE cycle_queue_sessions SET state = 'unknown'",
        "UPDATE cycle_queue_sessions SET ready_seen = 0",
    ],
)
def test_corrupt_delivery_projection_cannot_be_claimed(case, mutation):
    c = case
    publish(c)
    c["queue"]._connection.execute(mutation)
    c["queue"]._connection.commit()
    with pytest.raises(QueueInvalidated):
        c["queue"].claim("window", c["store"], "worker")
    assert c["queue"].inspect("window", c["store"])["state"] == "invalidated"


def test_source_change_latches_existing_session(case, monkeypatch):
    c = case
    publish(c)
    monkeypatch.setattr(cycle_queue, "_source_fingerprint", lambda: "0" * 64)
    with pytest.raises(QueueInvalidated, match="source_changed"):
        c["queue"].claim("window", c["store"], "worker")
    assert c["queue"].inspect("window", c["store"])["state"] == "invalidated"


def test_new_process_implementation_cannot_resume_old_binding(case, monkeypatch):
    c = case
    publish(c)
    monkeypatch.setattr(cycle_queue, "_source_fingerprint", lambda: "0" * 64)
    monkeypatch.setattr(cycle_queue, "LOADED_SOURCE_SHA256", "0" * 64)
    with pytest.raises(QueueInvalidated, match="binding_corrupted"):
        c["queue"].claim("window", c["store"], "worker")
    assert c["queue"].inspect("window", c["store"])["state"] == "invalidated"


def test_unreadable_source_latches_session(case, monkeypatch):
    c = case
    publish(c)

    def unavailable():
        raise OSError("synthetic missing source")

    monkeypatch.setattr(cycle_queue, "_source_fingerprint", unavailable)
    with pytest.raises(QueueInvalidated, match="source_unavailable"):
        c["queue"].claim("window", c["store"], "worker")
    assert c["queue"].inspect("window", c["store"])["state"] == "invalidated"
