"""Synthetic wire/real SQLite recovery captures; no external endpoint is contacted."""

import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import closing, suppress
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from oasis_agent.errors import RpcError
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.live.state import LoopState, Position, StateStore, day_of
from oasis_agent.live.valuation import capture_nav, replay_nav_quorum
from oasis_agent.models.common import BlockRef, canonical_json, evidence_ref_from_json, sha256_hex
from oasis_agent.rpc.budget import RpcBudget
from oasis_agent.rpc.transport import _NoRedirect
from scripts import capture_live_recovery as cli
from tests.unit.test_live_receipt_costs import position_config
from tests.unit.test_live_valuation import NavFixture, synthetic_feed_pin  # noqa: F401
from tests.unit.test_lp_receipts import sessions

CONFIG = position_config()
PROFILE = LiveRpcProfile(4096, 250)
SOURCES = cli.SOURCES
URLS = dict(zip(SOURCES, ("https://primary.invalid/a", "https://secondary.invalid/b"), strict=True))


class Clock:
    def __init__(self, now):
        self.now, self.mono, self.lock = float(now), 0.0, threading.Lock()

    def wall(self):
        with self.lock:
            return self.now

    def monotonic(self):
        with self.lock:
            return self.mono

    def sleep(self, seconds):
        with self.lock:
            self.now += seconds
            self.mono += seconds


class FixturePoster:
    def __init__(self, current, old):
        self.current, self.old, self.requests = current, old, []

    def post(self, payload):
        req = json.loads(payload)
        self.requests.append(req)
        if req["method"] == "eth_getBlockByNumber":
            fixture = self.old if req["params"][0] == hex(self.old.block_number) else self.current
            result = fixture._result("eth_getBlockByNumber")
            return canonical_json({"id": req["id"], "jsonrpc": "2.0", "result": result})
        if req["method"] == "eth_getTransactionReceipt":
            result = self.old._receipt() | {"transactionHash": req["params"][0]}
            return canonical_json({"id": req["id"], "jsonrpc": "2.0", "result": result})
        return self.current.post(payload)


def setup(tmp_path, monkeypatch, *, legacy=False):
    old = NavFixture(usdc_age=5000 if legacy else 10)
    epoch = replace(CONFIG, usdc_oracle_max_age_seconds=90000) if legacy else CONFIG
    current = replace(
        old,
        usdc_age=10,
        block_number=old.block_number + 30,
        block_hash="0x" + "44" * 32,
        timestamp=old.timestamp + 60,
    )
    roots = [tmp_path / role for role in cli.ROLES]
    for role, root in zip(cli.ROLES, roots, strict=True):
        root.mkdir(mode=0o700)
        state_path, evidence_path, lock = cli.paths(root, role)
        with closing(EvidenceStore(evidence_path)) as evidence:
            value, refs = capture_nav(
                evidence,
                epoch,
                BlockRef(epoch.chain_id, old.block_number, old.block_hash, old.timestamp),
                Position(123, -100, 100, 77, 0),
                sources=SOURCES,
                sessions=sessions(dict.fromkeys(SOURCES, old)),
            )
        state = LoopState("live", position=Position(123, -100, 100, 77, 0))
        state.integer_risk().observe(
            day=day_of(old.timestamp), at=old.timestamp, nav=value.nav_micros
        )
        state.last_units, state.nav_evidence = [value.wallet.native, value.wallet.token, 77], refs
        state.halted = "live observation refused; operator review required before polling"
        if legacy and role == "authority":
            state.halted = None
        state.settled_blocks = [
            {
                "number": old.block_number,
                "hash": old.block_hash,
                "transaction_hash": "0x" + f"{n:064x}",
            }
            for n in range(1, 6)
        ]
        store = StateStore(state_path)
        store.bind(sha256_hex(canonical_json(asdict(epoch))))
        store.save(state)
        lock.touch(mode=0o600)
        (root / "STOP").touch(mode=0o600)
        with RpcBudget(root / "live.receipt-budget.db", PROFILE.policies(SOURCES)) as budget:
            for source in SOURCES:
                assert budget.reserve(source, now_ms=old.timestamp * 1000) == 0
    with cli.prepared(*roots, CONFIG, PROFILE) as prepared:
        plan = prepared[0]
    scope = tmp_path / "external-scope.json"
    scope.write_bytes(
        canonical_json(
            {
                "schema": "oasis.live-recovery-zero-cost-scope.v1",
                "declared_cost_usd": "0",
                "authorization_evidence_hash": "a" * 64,
                "pricing_evidence_hash": "b" * 64,
                "endpoint_hashes": {
                    source: sha256_hex(url.encode()) for source, url in URLS.items()
                },
                "valid_until_ms": (current.timestamp + 120) * 1000,
            }
        )
    )
    for env, url in zip(cli.RPC_ENV, URLS.values(), strict=True):
        monkeypatch.setenv(env, url)
    posters = {url: FixturePoster(replace(current), old) for url in URLS.values()}
    clock = Clock(current.timestamp)
    return roots, plan, scope, posters, clock


def run(tmp_path, setup_result, *, transport=None, name="attempt"):
    roots, plan, scope, posters, clock = setup_result
    factory = transport or (lambda url, timeout: posters[url])
    return cli.capture(
        *roots,
        CONFIG,
        PROFILE,
        tmp_path / name,
        plan["plan_hash"],
        scope,
        transport=factory,
        clock=clock.wall,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


def unchanged_files(roots):
    return {
        path: path.read_bytes()
        for root in roots
        for path in root.iterdir()
        if path.name not in {"evidence.db", "live.receipts.db", "live.receipt-budget.db"}
    }


@pytest.mark.parametrize("legacy", [False, True])
def test_complete_same_head_capture_uses_original_sqlite_and_preserves_stop_and_risk(
    tmp_path, monkeypatch, legacy
):
    data = setup(tmp_path, monkeypatch, legacy=legacy)
    roots, plan, _, posters, _ = data
    before = unchanged_files(roots)
    result = run(tmp_path, data)
    assert result["capture_passed"] and result["exit_code"] == 0, result
    assert result["status"] == "raw_capture_requires_offline_rearm_review"
    assert result["request_counts"] == plan["request_limits"]
    assert plan["total_requests"] == 197
    assert sum(len(p.requests) for p in posters.values()) == 197
    assert not (tmp_path / "attempt/references.json").exists()  # supervisor alone publishes
    assert unchanged_files(roots) == before
    references = json.loads((tmp_path / "attempt/candidate-references.json").read_bytes())
    for role, root in zip(cli.ROLES, roots, strict=True):
        with RpcBudget(
            root / "live.receipt-budget.db", PROFILE.policies(SOURCES), read_only=True
        ) as budget:
            assert budget.counts() == {
                source: 1 + plan["request_limits"][role][source] for source in SOURCES
            }
        with closing(EvidenceStore(cli.paths(root, role)[1], require_existing=True)) as store:
            refs = tuple(evidence_ref_from_json(ref) for ref in references[role]["nav"])
            assert (
                replay_nav_quorum(store, refs, CONFIG, sources=SOURCES).nav_micros
                == result["nav_micros"]
            )
            assert all(ref["block"] == result["head"] for ref in references[role]["settlements"])
    wire = [
        json.loads(line) for line in (tmp_path / "attempt/wire.jsonl").read_bytes().splitlines()
    ]
    assert len(wire) == 394 and all(item["budget_spent"] for item in wire)
    assert sum(item["phase"] == "request_started" for item in wire) == 197


def test_plan_is_offline_and_leaves_all_original_bytes_unchanged(tmp_path, monkeypatch):
    roots, plan, *_ = setup(tmp_path, monkeypatch)
    before = {p: p.read_bytes() for root in roots for p in root.iterdir()}
    for name in cli.RPC_ENV:
        monkeypatch.delenv(name)
    with cli.prepared(*roots, CONFIG, PROFILE) as prepared:
        assert prepared[0] == plan
    assert before == {p: p.read_bytes() for root in roots for p in root.iterdir()}


def test_first_failure_spends_once_retains_wire_and_prevents_same_plan_reuse(tmp_path, monkeypatch):
    data = setup(tmp_path, monkeypatch)
    calls = []

    class Failure:
        def post(self, payload):
            calls.append(payload)
            raise OSError("private provider error")

    result = run(tmp_path, data, transport=lambda *_: Failure())
    assert not result["capture_passed"] and len(calls) == 1
    assert "private provider" not in json.dumps(result)
    assert result["request_counts"]["strategy"][SOURCES[0]] == 1
    for name in cli.RPC_ENV:
        monkeypatch.delenv(name)
    second = run(
        tmp_path, data, transport=lambda *_: pytest.fail("old plan reused"), name="new-attempt"
    )
    assert not second["capture_passed"] and second["request_counts"] is None
    assert result["plan"]["plan_hash"] != second["plan"]["plan_hash"]
    assert not (tmp_path / "attempt/candidate-references.json").exists()


@pytest.mark.parametrize("fault", ["stale", "nonce", "balance", "head", "receipt", "deadline"])
def test_raw_capture_refuses_incomplete_or_changed_proofs(tmp_path, monkeypatch, fault):
    data = setup(tmp_path, monkeypatch)
    _, _, _, posters, clock = data
    for poster in posters.values():
        if fault == "stale":
            poster.current.usdc_age = 1801
        if fault == "nonce":
            poster.current.nonce_after += 1
        if fault == "balance":
            poster.current.native_after += 1
        if fault == "head":
            poster.current.timestamp -= 121
    first = next(iter(posters.values()))
    original = first.post

    def post(payload):
        result = original(payload)
        if fault == "deadline":
            clock.sleep(121)
        if fault == "receipt" and json.loads(payload)["method"] == "eth_getTransactionReceipt":
            value = json.loads(result)
            value["result"]["blockHash"] = "0x" + "99" * 32
            return canonical_json(value)
        return result

    first.post = post
    result = run(tmp_path, data)
    assert not result["capture_passed"] and result["exit_code"] == 3
    assert not (tmp_path / "attempt/candidate-references.json").exists()


@pytest.mark.parametrize("fault", ["same_host", "userinfo", "fragment", "unpriced", "bad_hash"])
def test_endpoint_and_cost_scope_refuse_before_budget_or_http(tmp_path, monkeypatch, fault):
    data = setup(tmp_path, monkeypatch)
    roots, plan, scope, _, _ = data
    body = json.loads(scope.read_bytes())
    url = {
        "same_host": "https://primary.invalid/other",
        "userinfo": "https://secret@secondary.invalid",
        "fragment": "https://secondary.invalid/#secret",
    }.get(fault)
    if url:
        monkeypatch.setenv(cli.RPC_ENV[1], url)
        body["endpoint_hashes"][SOURCES[1]] = sha256_hex(url.encode())
    if fault == "unpriced":
        body["declared_cost_usd"] = "unknown"
    if fault == "bad_hash":
        plan["plan_hash"] = "f" * 64
    scope.write_bytes(canonical_json(body))
    before = [(root / "live.receipt-budget.db").read_bytes() for root in roots]
    result = run(tmp_path, data, transport=lambda *_: pytest.fail("scope checked too late"))
    assert not result["capture_passed"] and result["request_counts"] is None
    assert before == [(root / "live.receipt-budget.db").read_bytes() for root in roots]


def test_request_cap_does_not_spend_an_extra_reservation(tmp_path):
    budget = tmp_path / "budget.db"
    with RpcBudget(budget, PROFILE.policies(SOURCES)):
        pass
    plan = {"request_limits": {role: dict.fromkeys(SOURCES, 1) for role in cli.ROLES}}
    control = cli.CaptureControl(plan, dict.fromkeys(cli.ROLES, budget), PROFILE, tmp_path)

    class Reply:
        def post(self, payload):
            return canonical_json({"jsonrpc": "2.0", "id": 1, "result": "0x2105"})

    try:
        payload = canonical_json({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []})
        control.post("strategy", SOURCES[0], payload, "https://fixture.invalid", lambda *_: Reply())
        with pytest.raises(ValueError, match="allowance"):
            control.post(
                "strategy",
                SOURCES[0],
                b"{}",
                "https://fixture.invalid",
                lambda *_: pytest.fail("extra HTTP"),
            )
        with RpcBudget(budget, PROFILE.policies(SOURCES), read_only=True) as quota:
            assert quota.counts()[SOURCES[0]] == 1
    finally:
        control.wire.close()


def test_shared_poster_caps_one_read_and_keeps_redirect_guard(monkeypatch):
    calls = []
    bodies = []

    class Response(io.BytesIO):
        def read(self, size=-1):
            calls.append(size)
            return super().read(size)

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "https://declared.invalid"
            assert timeout == 0.25
            body = Response(b"x" * (cli.MAX_RESPONSE + 2))
            bodies.append(body)
            return body

    def build(*handlers):
        assert any(isinstance(handler, _NoRedirect) for handler in handlers)
        assert any(isinstance(handler, urllib.request.HTTPSHandler) for handler in handlers)
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build)
    with pytest.raises(RpcError, match="byte limit"):
        cli.BoundedPoster("https://declared.invalid", 0.25).post(b"{}")
    assert calls == [cli.MAX_RESPONSE + 1] and len(bodies) == 1 and bodies[0].closed


def test_supervisor_kills_stuck_worker_and_descendant_no_late_publication(tmp_path):
    out, late = tmp_path / "attempt", tmp_path / "late"

    def operation():
        out.mkdir()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = os.fork()
        if child == 0:
            time.sleep(0.7)
            late.touch()
            os._exit(0)
        (out / "report.json").write_bytes(canonical_json({"capture_passed": True, "exit_code": 0}))
        (out / "candidate-references.json").write_bytes(
            canonical_json({role: {"nav": [], "settlements": []} for role in cli.ROLES})
        )
        time.sleep(10)
        return {"capture_passed": True, "exit_code": 0}

    started = time.monotonic()
    result = cli.supervise(operation, out, seconds=0.2)
    assert time.monotonic() - started < 1
    assert not result["capture_passed"] and result["status"] == "hard_deadline_refused"
    assert not (out / "references.json").exists() and not (out / "fresh-nav.json").exists()
    time.sleep(0.75)
    assert not late.exists()


def test_supervisor_cleans_environment_and_publishes_only_timely_complete_worker(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PRIVATE_KEY", "must-not-be-inherited")
    monkeypatch.setenv(cli.RPC_ENV[0], "https://allowed.invalid")
    out = tmp_path / "attempt"

    def operation():
        assert "PRIVATE_KEY" not in os.environ
        assert os.environ[cli.RPC_ENV[0]] == "https://allowed.invalid"
        out.mkdir()
        (out / "report.json").write_bytes(canonical_json({"capture_passed": True, "exit_code": 0}))
        (out / "candidate-references.json").write_bytes(
            canonical_json({role: {"nav": [], "settlements": []} for role in cli.ROLES})
        )
        return {"capture_passed": True, "exit_code": 0}

    result = cli.supervise(operation, out, seconds=1)
    assert result["capture_passed"] and result["exit_code"] == 0
    assert (out / "references.json").exists() and (out / "fresh-nav.json").exists()
    with pytest.raises(ValueError):
        cli.supervise(operation, out, seconds=1)


def test_supervisor_refuses_late_artifact_validation_and_kills_exited_leaders_child(
    tmp_path, monkeypatch
):
    out, late = tmp_path / "attempt", tmp_path / "late"

    def operation():
        out.mkdir()
        if os.fork() == 0:
            time.sleep(0.5)
            late.touch()
            os._exit(0)
        (out / "report.json").write_bytes(canonical_json({"capture_passed": True, "exit_code": 0}))
        (out / "candidate-references.json").write_bytes(
            canonical_json({role: {"nav": [], "settlements": []} for role in cli.ROLES})
        )
        return {"capture_passed": True, "exit_code": 0}

    elapsed = 0.0
    original = cli.strict_json_object
    killpg = os.killpg
    cleanup_signals = []

    def record_cleanup(group, sig):
        cleanup_signals.append(sig)
        killpg(group, sig)

    def cross_deadline(raw):
        nonlocal elapsed
        elapsed = 1.1
        return original(raw)

    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=lambda: elapsed, sleep=time.sleep))
    monkeypatch.setattr(os, "killpg", record_cleanup)
    monkeypatch.setattr(cli, "strict_json_object", cross_deadline)
    report = cli.supervise(operation, out, seconds=1)
    assert report["worker_exit_code"] == 0 and report["worker_group_ready"], report
    assert cleanup_signals == [signal.SIGKILL]
    assert not report["capture_passed"] and report["status"] == "hard_deadline_refused", report
    assert not (out / "references.json").exists()
    assert not (out / "fresh-nav.json").exists()
    time.sleep(0.55)
    assert not late.exists()


def test_supervisor_handles_deadline_crossing_at_poll_boundary(tmp_path, monkeypatch):
    out = tmp_path / "attempt"
    supervisor_pid = os.getpid()
    samples = iter((0.0, 0.49))

    def monotonic():
        return next(samples, 0.51) if os.getpid() == supervisor_pid else 0.0

    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=monotonic, sleep=time.sleep))
    report = cli.supervise(lambda: time.sleep(2), out, seconds=0.5)
    assert report["status"] == "hard_deadline_refused" and not report["capture_passed"], report
    assert not (out / "references.json").exists() and not (out / "fresh-nav.json").exists()


@pytest.mark.parametrize("status", [403, 429])
def test_http_failure_status_is_retained_without_endpoint_or_error_text(
    tmp_path, monkeypatch, status
):
    data = setup(tmp_path, monkeypatch)

    class Failure:
        def post(self, payload):
            raise HTTPError("https://secret.invalid/token", status, "secret credential", {}, None)

    report = run(tmp_path, data, transport=lambda *_: Failure())
    assert not report["capture_passed"]
    wire = (tmp_path / "attempt/wire.jsonl").read_text()
    assert "secret" not in wire and "credential" not in wire
    records = [json.loads(line) for line in wire.splitlines()]
    assert records[0]["phase"] == "request_started"
    assert records[-1]["http_status"] == status
    assert records[-1]["failure_category"] == "http_error"


@pytest.mark.parametrize("fault", ["rpc_error", "invalid_json", "wrong_id", "ambiguous"])
def test_error_bodies_cannot_persist_reflected_rpc_credentials(tmp_path, monkeypatch, fault):
    data = setup(tmp_path, monkeypatch)
    private_text = "https://synthetic.invalid/?key=synthetic-rpc-credential"
    responses = []

    class Reply:
        def post(self, payload):
            request = json.loads(payload)
            body = {"jsonrpc": "2.0", "id": request["id"]}
            if fault == "invalid_json":
                raw = private_text.encode()
            elif fault == "wrong_id":
                raw = canonical_json(body | {"id": request["id"] + 1, "result": private_text})
            else:
                body["error"] = {"code": -32005, "message": private_text}
                if fault == "ambiguous":
                    body["result"] = "0x2105"
                raw = canonical_json(body)
            responses.append(raw)
            return raw

    report = run(tmp_path, data, transport=lambda *_: Reply())
    assert not report["capture_passed"] and len(responses) == 1
    records = [
        json.loads(line) for line in (tmp_path / "attempt/wire.jsonl").read_bytes().splitlines()
    ]
    finished = records[-1]
    assert finished["response_hex"] is None
    assert private_text not in json.dumps(records)
    assert finished["response_hash"] == sha256_hex(responses[0])
    assert finished["response_size"] == len(responses[0])
    assert finished["failure_category"] == (
        "rpc_error" if fault == "rpc_error" else "rpc_protocol_error"
    )
    assert finished["rpc_error_code"] == (-32005 if fault == "rpc_error" else None)
    assert report["request_counts"]["strategy"][SOURCES[0]] == 1
    assert not (tmp_path / "attempt/candidate-references.json").exists()


@pytest.mark.parametrize("reflection", ["url", "query_value", "path_value", "host_value"])
def test_success_envelope_cannot_retain_its_own_endpoint_credentials(
    tmp_path, monkeypatch, reflection
):
    data = setup(tmp_path, monkeypatch)
    private = "synthetic-rpc-credential"
    url = (
        "https://primary.invalid/" + private
        if reflection == "path_value"
        else ("https://primary.invalid/?key=" + private)
    )
    if reflection == "host_value":
        private += ".invalid"
        url = "https://" + private
    monkeypatch.setenv(cli.RPC_ENV[0], url)
    scope = data[2]
    body = json.loads(scope.read_bytes())
    body["endpoint_hashes"][SOURCES[0]] = sha256_hex(url.encode())
    scope.write_bytes(canonical_json(body))
    responses = []

    class Reply:
        def post(self, payload):
            request = json.loads(payload)
            # Even an otherwise successful envelope can contain an invalid result
            # or extra result metadata that reflects a private endpoint component.
            value = url if reflection == "url" else {"provider_note": private}
            raw = canonical_json({"jsonrpc": "2.0", "id": request["id"], "result": value})
            responses.append(raw)
            return raw

    report = run(tmp_path, data, transport=lambda *_: Reply())
    assert not report["capture_passed"] and len(responses) == 1
    wire = (tmp_path / "attempt/wire.jsonl").read_bytes()
    records = [json.loads(line) for line in wire.splitlines()]
    assert records[-1]["response_hex"] is None
    assert private not in wire.decode() and private.encode().hex() not in wire.decode()
    assert records[-1]["response_hash"] == sha256_hex(responses[0])
    assert records[-1]["failure_category"] == "rpc_response_reflected_endpoint"
    assert not (tmp_path / "attempt/candidate-references.json").exists()


def test_successful_wire_bytes_keep_spacing_and_short_routing_values_do_not_match_quantities():
    request = canonical_json({"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": []})
    raw = b'{ "jsonrpc" : "2.0", "id" : 1, "result" : "0x1" }\n'
    assert cli.successful_response(request, raw, "https://fixture.invalid/rpc?chain=1") is raw


@pytest.mark.parametrize("inherited_alarm_blocked", [False, True])
def test_worker_deadline_survives_supervisor_sigkill(tmp_path, inherited_alarm_blocked):
    out, marker, late = tmp_path / "attempt", tmp_path / "worker.pid", tmp_path / "late"
    code = """
import os, signal, sys, time
from pathlib import Path
from scripts.capture_live_recovery import supervise
out, marker, late = map(Path, sys.argv[1:4])
if sys.argv[4] == '1':
    signal.signal(signal.SIGALRM, signal.SIG_IGN)
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
def blocked_request():
    out.mkdir()
    marker.write_text(str(os.getpid()))
    time.sleep(0.9)
    late.touch()
    time.sleep(10)
supervise(blocked_request, out, seconds=0.3)
"""
    root = Path(cli.__file__).resolve().parents[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            code,
            str(out),
            str(marker),
            str(late),
            str(int(inherited_alarm_blocked)),
        ],
        cwd=root,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": f"{root / 'src'}{os.pathsep}{root}"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    worker = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                worker = int(marker.read_text())
                break
            except (FileNotFoundError, ValueError):
                if process.poll() is not None:
                    break
                time.sleep(0.01)
        assert worker is not None and process.poll() is None
        process.kill()
        assert process.wait(timeout=3) == -signal.SIGKILL
        time.sleep(1.1)
        assert not late.exists(), (
            "worker continued after its authorized window with supervisor gone"
        )
        assert not (out / "references.json").exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        if worker is not None:
            with suppress(ProcessLookupError):
                os.killpg(worker, signal.SIGKILL)
