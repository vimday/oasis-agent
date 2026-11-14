"""No live network: frozen reads, exact bytes, durable budgets and failure recovery."""

import json
import shutil
from datetime import UTC, datetime

import httpx
import pytest

from oasis_agent.evidence.public_rpc_plan import CHAIN_SOURCES, SOURCES, PublicReadPlan
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.budget import RpcBudget
from scripts import capture_public_rpc as cli

HASH = "0x" + "12" * 32
TARGET = "0x" + "34" * 20


def setup(tmp_path, source="publicnode_ethereum", cap=24):
    scope = {
        "schema": "oasis-public-rpc-scope-v1",
        "purpose": "synthetic_capture_test",
        "expires_utc": "2100-01-01T00:00:00Z",
        "sources": [{"source": source, "max_requests": cap}],
        "max_capture_seconds": 60,
    }
    plan = {
        "schema": "oasis-public-rpc-read-plan-v1",
        "source": source,
        "chain_id": SOURCES[source][0],
        "block_number": 100,
        "block_hash": HASH,
        "reads": [
            {"method": "eth_getCode", "target": TARGET},
            {"method": "eth_call", "target": TARGET, "data": "0x313ce567"},
            {"method": "eth_getStorageAt", "target": TARGET, "slot": "0x" + "00" * 32},
        ],
    }
    scope_path, plan_path = tmp_path / "scope.json", tmp_path / "plan.json"
    scope_path.write_bytes(canonical_json(scope))
    plan_path.write_bytes(canonical_json(plan))
    root = tmp_path / "campaign"
    cli.initialize(scope_path, root)
    return root, plan_path, plan


def counts(root):
    scope, _ = cli.load(root / "scope.json", cli.PublicScope)
    with RpcBudget(root / "original-budget.sqlite3", cli.policies(scope), read_only=True) as b:
        return b.counts()


def mock(monkeypatch, root, plan, mutation=None):
    calls = []
    original = httpx.Client

    def handler(req):
        request = json.loads(req.content)
        calls.append(request)
        assert counts(root)[plan["source"]] >= len(calls)  # spent before I/O
        method, number = request["method"], request["id"]
        result = (
            hex(plan["chain_id"])
            if method == "eth_chainId"
            else {
                "number": "0x64",
                "hash": HASH,
                "parentHash": "0x" + "11" * 32,
                "timestamp": "0x6955b900",
            }
            if method == "eth_getBlockByNumber"
            else "0x6000"
            if method == "eth_getCode"
            else "0x" + "00" * 32
        )
        response = {"jsonrpc": "2.0", "id": number, "result": result}
        if mutation:
            changed = mutation(number, response)
            if changed is not None:
                return changed
        return httpx.Response(200, json=response)

    def factory(**kwargs):
        assert kwargs == {"timeout": 10, "follow_redirects": False, "trust_env": False}
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(cli.httpx, "Client", factory)
    return calls


@pytest.mark.parametrize("source", list(CHAIN_SOURCES))
def test_success_is_pinned_replayable_and_cumulative(tmp_path, monkeypatch, source):
    root, path, plan = setup(tmp_path, source)
    calls = mock(monkeypatch, root, plan)
    assert cli.capture(root, path, "first") == 0
    r = cli.replay(root / "first")
    assert r["status"] == "complete" and r["original_http_bytes_verified"]
    assert r["planned_requests"] == r["attempted_requests"] == r["reserved_requests"] == 6
    assert not r["execution_eligible"] and not r["independent_providers_verified"]
    assert len(r["exports"]["records"]) == 6
    assert all(c["params"][-1] == {"blockHash": HASH, "requireCanonical": True} for c in calls[2:5])
    assert not (root / "ACTIVE.json").exists()
    assert canonical_json(cli.replay(root / "first")) == canonical_json(r)
    assert cli.capture(root, path, "second") == 0
    assert counts(root)[source] == 12
    with pytest.raises(FileExistsError):
        cli.capture(root, path, "first")
    assert counts(root)[source] == 12


@pytest.mark.parametrize(
    "kind", ["429", "redirect", "rpc", "id", "json", "hex", "oversize", "timeout"]
)
def test_first_failure_spends_one_and_stops_persistently(tmp_path, monkeypatch, kind):
    root, path, plan = setup(tmp_path)

    def mutate(n, response):
        if n != 3:
            return None
        if kind == "429":
            return httpx.Response(429, text="limited")
        if kind == "redirect":
            return httpx.Response(302, headers={"Location": "https://not-allowed.invalid"})
        if kind == "rpc":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": n,
                    "error": {"code": 3, "message": "reverted", "data": "0x9a0423af"},
                },
            )
        if kind == "id":
            response["id"] = True
        if kind == "json":
            return httpx.Response(200, content=b'{"jsonrpc":"2.0","id":3,"id":3,"result":"0x"}')
        if kind == "hex":
            response["result"] = "0xz"
        if kind == "oversize":
            return httpx.Response(200, content=b"x" * (cli.MAX_BODY + 1))
        if kind == "timeout":
            raise httpx.ReadTimeout("untrusted endpoint failure")
        return None

    calls = mock(monkeypatch, root, plan, mutate)
    assert cli.capture(root, path, "failed") == 3
    assert len(calls) == counts(root)[plan["source"]] == 3
    assert (root / "STOP").exists()
    r = cli.replay(root / "failed")
    assert r["status"] == "incomplete"
    assert len(r["exports"]["records"]) == (3 if kind == "rpc" else 2)
    if kind in {"oversize", "timeout"}:
        assert not r["original_http_bytes_verified"]
    with pytest.raises(ValueError, match="stopped"):
        cli.capture(root, path, "next")
    assert len(calls) == 3


@pytest.mark.parametrize(
    "index,field,value",
    [
        (1, "result", "0x2"),
        (2, "hash", "0x" + "99" * 32),
        (6, "parentHash", "0x" + "77" * 32),
        (2, "timestamp", "0xffffffff"),
    ],
)
def test_chain_header_reorg_and_future_block_refuse(tmp_path, monkeypatch, index, field, value):
    root, path, plan = setup(tmp_path)

    def mutate(n, response):
        if n == index:
            if field == "result":
                response[field] = value
            else:
                response["result"][field] = value

    calls = mock(monkeypatch, root, plan, mutate)
    assert cli.capture(root, path, "bad_context") == 3
    r = cli.replay(root / "bad_context")
    assert len(calls) == index
    assert len(r["exports"]["records"]) == index - 1


def test_crash_marker_blocks_restart_with_spent_reservation(tmp_path, monkeypatch):
    root, path, plan = setup(tmp_path)

    def crash(n, response):
        if n == 3:
            raise KeyboardInterrupt

    calls = mock(monkeypatch, root, plan, crash)
    with pytest.raises(KeyboardInterrupt):
        cli.capture(root, path, "crashed")
    assert (root / "ACTIVE.json").exists()
    assert counts(root)[plan["source"]] == 3
    with pytest.raises(ValueError, match="ambiguous"):
        cli.capture(root, path, "again")
    assert len(calls) == 3


def test_competing_campaign_writer_cannot_reserve(tmp_path, monkeypatch):
    root, path, plan = setup(tmp_path)
    calls = mock(monkeypatch, root, plan)
    with cli.locked(root), pytest.raises(BlockingIOError):
        cli.capture(root, path, "race")
    assert not calls and counts(root)[plan["source"]] == 0


def test_whole_run_budget_preflight_prevents_partial_capture(tmp_path, monkeypatch):
    root, path, plan = setup(tmp_path, cap=9)
    calls = mock(monkeypatch, root, plan)
    assert cli.capture(root, path, "first") == 0
    with pytest.raises(ValueError, match="whole_capture"):
        cli.capture(root, path, "second")
    assert len(calls) == 6 and counts(root)[plan["source"]] == 6
    assert not (root / "second").exists()


@pytest.mark.parametrize("kind", ["scope", "ledger", "missing_ledger", "symlink_ledger"])
def test_scope_and_original_ledger_cannot_be_replaced(tmp_path, monkeypatch, kind):
    root, path, plan = setup(tmp_path)
    calls = mock(monkeypatch, root, plan)
    ledger = root / "original-budget.sqlite3"
    if kind == "scope":
        v = json.loads((root / "scope.json").read_bytes())
        v["sources"][0]["max_requests"] += 1
        (root / "scope.json").write_bytes(canonical_json(v))
    elif kind == "ledger":
        shutil.copyfile(ledger, root / "replacement.sqlite3")
        (root / "replacement.sqlite3").replace(ledger)
    elif kind == "missing_ledger":
        ledger.unlink()
    else:
        ledger.rename(root / "renamed.sqlite3")
        ledger.symlink_to(root / "renamed.sqlite3")
    with pytest.raises((ValueError, OSError)):
        cli.capture(root, path, "bad")
    assert not calls


@pytest.mark.parametrize(
    "mutation", ["wire", "meta", "missing", "budget", "hidden_failure", "clock"]
)
def test_replay_rejects_tampering(tmp_path, monkeypatch, mutation):
    root, path, plan = setup(tmp_path)
    mock(monkeypatch, root, plan)
    assert cli.capture(root, path, "capture") == 0
    out = root / "capture"
    m = json.loads((out / "capture.json").read_bytes())
    if mutation == "wire":
        (out / "2.response.json").write_bytes(b"{}")
    elif mutation == "meta":
        (out / "2.meta.json").write_bytes(b"{}")
    elif mutation == "missing":
        (out / "4.response.json").unlink()
    elif mutation == "budget":
        m["budget_after"][plan["source"]] -= 1
    else:
        row = m["exchanges"][2]
        row["error" if mutation == "hidden_failure" else "received_ms"] = (
            "rpc_error" if mutation == "hidden_failure" else 0
        )
        (out / "2.meta.json").write_bytes(canonical_json(row))
    (out / "capture.json").write_bytes(canonical_json(m))
    with pytest.raises((ValueError, OSError)):
        cli.replay(out)


@pytest.mark.parametrize(
    "changes",
    [
        {"method": "eth_sendRawTransaction"},
        {"target": "0x" + "00" * 20},
        {"sender": TARGET},
        {"data": "0x1234"},
        {"slot": HASH},
    ],
)
def test_plan_cannot_inject_methods_or_ambiguous_fields(tmp_path, changes):
    _, _, p = setup(tmp_path)
    p["reads"][0].update(changes)
    with pytest.raises(ValueError):
        PublicReadPlan.model_validate_json(json.dumps(p))


def test_cli_invalid_scope_no_network_and_no_overwrite(tmp_path, monkeypatch):
    root, path, plan = setup(tmp_path)
    mock(monkeypatch, root, plan)
    assert cli.capture(root, path, "ok") == 0
    output = tmp_path / "report.json"
    args = ["capture_public_rpc", "replay", "--capture", str(root / "ok"), "--output", str(output)]
    monkeypatch.setattr("sys.argv", args)
    assert cli.main() == 0
    original = output.read_bytes()
    assert cli.main() == 2 and output.read_bytes() == original


@pytest.mark.parametrize("tag", ["latest", "safe", "finalized"])
def test_head_observation_is_separate_from_pinned_reads(tmp_path, monkeypatch, tag):
    root, path, plan = setup(tmp_path)
    head = {k: plan[k] for k in ("source", "chain_id")}
    head.update(schema="oasis-public-rpc-head-plan-v1", block_tag=tag)
    path.write_bytes(canonical_json(head))
    calls = mock(monkeypatch, root, plan)
    assert cli.capture(root, path, "head") == 0
    r = cli.replay(root / "head")
    assert len(calls) == 2 and calls[1]["params"] == [tag, False]
    assert r["plan_kind"] == "head_observation"
    assert r["block"]["ref"]["number"] == 100
    assert not r["finality_independently_verified"]


def test_expired_scope_refuses_before_network(tmp_path, monkeypatch):
    root, path, plan = setup(tmp_path)
    calls = mock(monkeypatch, root, plan)

    class FutureDate(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2100, 1, 2, tzinfo=UTC)

    monkeypatch.setattr(cli, "datetime", FutureDate)
    with pytest.raises(ValueError, match="expired_scope"):
        cli.capture(root, path, "expired")
    assert not calls


def test_wall_clock_regression_cannot_export_failed_result(tmp_path, monkeypatch):
    root, path, plan = setup(tmp_path)
    mock(monkeypatch, root, plan)
    start = cli.time.time_ns()
    moments = iter([start, start + 1_000_000, start - 1_000_000])
    monkeypatch.setattr(cli.time, "time_ns", lambda: next(moments))
    assert cli.capture(root, path, "backward") == 3
    r = cli.replay(root / "backward")
    assert not r["exports"]["records"]
    assert r["reserved_requests"] == 1


def test_deadline_before_reservation_preserves_zero_usage(tmp_path, monkeypatch):
    root, path, plan = setup(tmp_path)
    calls = mock(monkeypatch, root, plan)
    moments = iter([0, 61])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(moments))
    assert cli.capture(root, path, "deadline") == 3
    r = cli.replay(root / "deadline")
    assert r["reserved_requests"] == 0 and not calls


def test_new_head_created_during_request_is_valid_at_receive_time(tmp_path, monkeypatch):
    root, path, plan = setup(tmp_path)
    path.write_bytes(
        canonical_json(
            {
                "schema": "oasis-public-rpc-head-plan-v1",
                "source": plan["source"],
                "chain_id": 1,
                "block_tag": "latest",
            }
        )
    )
    mock(monkeypatch, root, plan)
    block_ns = int("0x6955b900", 16) * 1_000_000_000
    offsets = iter([-100, -90, -60, -40, 150])
    monkeypatch.setattr(cli.time, "time_ns", lambda: block_ns + next(offsets) * 1_000_000)
    assert cli.capture(root, path, "new_head") == 0
    r = cli.replay(root / "new_head")
    assert r["status"] == "complete" and r["attempted_requests"] == 2


def test_external_stop_during_io_is_preserved_and_prevents_more_requests(tmp_path, monkeypatch):
    root, path, plan = setup(tmp_path)

    def stop(n, response):
        if n == 3:
            (root / "STOP").write_bytes(b"operator stop")

    calls = mock(monkeypatch, root, plan, stop)
    assert cli.capture(root, path, "stopped") == 3
    assert len(calls) == counts(root)[plan["source"]] == 3
    assert (root / "STOP").read_bytes() == b"operator stop"
    r = cli.replay(root / "stopped")
    assert r["status"] == "incomplete" and len(r["exports"]["records"]) == 2


@pytest.mark.parametrize("name", ["STOP", "ACTIVE.json"])
def test_broken_control_symlink_is_not_treated_as_absent(tmp_path, monkeypatch, name):
    root, path, plan = setup(tmp_path)
    calls = mock(monkeypatch, root, plan)
    (root / name).symlink_to(root / "absent")
    with pytest.raises(ValueError, match="stopped_or_ambiguous"):
        cli.capture(root, path, "never")
    assert not calls
