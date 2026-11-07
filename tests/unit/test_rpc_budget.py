"""Durable observation quotas count failed requests and serialize competing workers."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest

from oasis_agent.rpc.budget import BudgetedPoster, BudgetPolicy, RpcBudget, UsageLimit


def policies():
    return (BudgetPolicy("public", 4, 1000), BudgetPolicy("paid", 2, 3000))


def test_restart_spacing_and_exhaustion(tmp_path):
    path = tmp_path / "budget.db"
    with RpcBudget(path, policies()) as budget:
        assert budget.reserve("paid", now_ms=10000) == 0
        assert budget.reserve("paid", now_ms=11000) == 2000
        assert budget.counts() == {"public": 0, "paid": 1}
    with RpcBudget(path, policies()) as budget:
        assert budget.reserve("paid", now_ms=13000) == 0
        with pytest.raises(ValueError, match="exhausted"):
            budget.reserve("paid", now_ms=20000)
        assert budget.counts()["paid"] == 2


@pytest.mark.parametrize("kind", ["missing", "empty", "wrong_policy", "missing_policy"])
def test_required_existing_budget_never_initializes_or_repairs(tmp_path, kind):
    path = tmp_path / "budget.db"
    if kind == "empty":
        path.touch()
    elif kind in {"wrong_policy", "missing_policy"}:
        with RpcBudget(path, policies()) as budget:
            if kind == "missing_policy":
                budget.db.execute("DELETE FROM rpc_budget_policy")
    before = path.read_bytes() if path.exists() else None
    requested = (BudgetPolicy("different", 1, 0),) if kind == "wrong_policy" else policies()
    with pytest.raises((ValueError, sqlite3.Error)):
        RpcBudget(path, requested, require_existing=True)
    assert (path.read_bytes() if path.exists() else None) == before


def test_required_poster_never_recreates_a_disappeared_budget(tmp_path):
    class Forbidden:
        def post(self, payload):
            raise AssertionError("no network without retained quota")

    path = tmp_path / "budget.db"
    with RpcBudget(path, policies()):
        pass
    poster = BudgetedPoster(Forbidden(), path, policies(), "paid", require_existing=True)
    path.unlink()
    with pytest.raises(sqlite3.Error):
        poster.post(b'{"method":"eth_chainId"}')
    assert not path.exists()


def test_changed_policy_or_backwards_clock_fails(tmp_path):
    path = tmp_path / "budget.db"
    with RpcBudget(path, policies()) as budget:
        budget.reserve("paid", now_ms=10000)
        with pytest.raises(ValueError, match="clock"):
            budget.reserve("paid", now_ms=9999)
        with pytest.raises(ValueError):
            budget.reserve("unknown", now_ms=12000)
    with pytest.raises(ValueError, match="policy"):
        RpcBudget(path, (BudgetPolicy("paid", 200, 0),))


def test_two_connections_cannot_overspend(tmp_path):
    path = tmp_path / "budget.db"
    frozen = (BudgetPolicy("paid", 1, 0),)
    with RpcBudget(path, frozen):
        pass

    def attempt():
        with RpcBudget(path, frozen) as budget:
            try:
                budget.reserve("paid", now_ms=1000)
                return True
            except ValueError:
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == [False, True]


@pytest.mark.parametrize("cap,interval", [(True, 0), (0, 1), (-1, 1), (1, -1), (1, True)])
def test_invalid_policy(cap, interval):
    with pytest.raises(ValueError):
        BudgetPolicy("paid", cap, interval)


def test_event_tampering_fails_closed(tmp_path):
    path = tmp_path / "budget.db"
    with RpcBudget(path, policies()) as budget:
        budget.reserve("paid", now_ms=1000)
        budget.db.execute("UPDATE rpc_usage SET payload=?", (b"{}",))
        with pytest.raises(ValueError):
            budget.reserve("paid", now_ms=10000)


def test_failed_network_spends_quota_and_write_methods_are_rejected(tmp_path):

    class Failing:
        def post(self, payload):
            raise OSError("offline")

    path = tmp_path / "quota.db"
    policy = (BudgetPolicy("paid", 1, 0),)
    poster = BudgetedPoster(Failing(), path, policy, "paid", clock=lambda: 1000)
    with pytest.raises(ValueError, match="not allowed"):
        poster.post(b'{"method":"eth_sendRawTransaction"}')
    with pytest.raises(OSError):
        poster.post(b'{"method":"eth_chainId"}')
    with pytest.raises(ValueError, match="exhausted"):
        poster.post(b'{"method":"eth_chainId"}')
    with RpcBudget(path, policy) as budget:
        assert budget.counts() == {"paid": 1}


@pytest.mark.parametrize(
    "method,params",
    [
        ("eth_getTransactionByHash", ["0x" + "22" * 32]),
        ("eth_getTransactionCount", ["0x" + "11" * 20, {"blockHash": "0x" + "22" * 32}]),
        ("eth_getStorageAt", ["0x" + "11" * 20, "0x0", {"blockHash": "0x" + "22" * 32}]),
        (
            "debug_traceTransaction",
            [
                "0x" + "22" * 32,
                {"tracer": "callTracer", "timeout": "5s", "tracerConfig": {"onlyTopCall": False}},
            ],
        ),
    ],
)
def test_cash_proof_reads_are_budgeted_without_broadcast_capability(tmp_path, method, params):
    class Poster:
        def post(self, payload):
            return payload

    policy = (BudgetPolicy("proof", 1, 0),)
    path = tmp_path / "budget.db"
    poster = BudgetedPoster(Poster(), path, policy, "proof", clock=lambda: 1000)
    raw = json.dumps({"method": method, "params": params}).encode()
    assert poster.post(raw) == raw
    with RpcBudget(path, policy) as budget:
        assert budget.counts() == {"proof": 1}
    with pytest.raises(ValueError, match="not allowed"):
        poster.post(b'{"method":"eth_sendRawTransaction","params":[]}')


@pytest.mark.parametrize(
    "options",
    [
        {"tracer": "custom JavaScript"},
        {"tracer": "callTracer", "timeout": "1000s", "tracerConfig": {"onlyTopCall": False}},
        {"tracer": "callTracer", "timeout": "5s", "tracerConfig": {"onlyTopCall": True}},
    ],
)
def test_unbounded_or_incomplete_tracer_refused_before_spending_budget(tmp_path, options):
    class Forbidden:
        def post(self, payload):
            raise AssertionError("trace must be refused before network")

    path = tmp_path / "absent.db"
    poster = BudgetedPoster(Forbidden(), path, (BudgetPolicy("proof", 1, 0),), "proof")
    with pytest.raises(ValueError, match="bounded complete"):
        poster.post(
            json.dumps(
                {"method": "debug_traceTransaction", "params": ["0x" + "22" * 32, options]}
            ).encode()
        )
    assert not path.exists()


@pytest.mark.parametrize("params", [[], [None], ["latest"], ["0x12"], ["0x" + "22" * 32, False]])
def test_transaction_lookup_requires_one_hash_before_budget_or_network(tmp_path, params):
    class Forbidden:
        def post(self, payload):
            raise AssertionError("network must not be reached")

    path = tmp_path / "absent.db"
    poster = BudgetedPoster(Forbidden(), path, (BudgetPolicy("proof", 1, 0),), "proof")
    with pytest.raises(ValueError, match="canonical transaction"):
        poster.post(json.dumps({"method": "eth_getTransactionByHash", "params": params}).encode())
    assert not path.exists()


@pytest.mark.parametrize(
    "raw",
    [
        b'{"method":"eth_sendRawTransaction","method":"eth_chainId","params":[]}',
        b'{"method":"eth_sendRawTransaction","meth\\u006fd":"eth_chainId","params":[]}',
        b'{"method":"eth_chainId","params":["unexpected"],"params":[]}',
        b'{"method":"eth_call","params":[{"to":"first","to":"second"},"latest"]}',
        b'{"method":"eth_call","params":[{"value":NaN},"latest"]}',
        b'{"method":"eth_call","params":[{"value":Infinity},"latest"]}',
        b'{"method":"eth_call","params":[{"value":-Infinity},"latest"]}',
    ],
)
@pytest.mark.parametrize("existing", [False, True])
def test_ambiguous_observation_never_reaches_network_or_spends_quota(tmp_path, raw, existing):
    calls = []

    class Poster:
        def post(self, payload):
            calls.append(payload)
            return b"{}"

    path = tmp_path / "budget.db"
    policy = (BudgetPolicy("proof", 2, 0),)
    if existing:
        with RpcBudget(path, policy) as budget:
            budget.reserve("proof", now_ms=1)
    before = path.read_bytes() if existing else None
    poster = BudgetedPoster(Poster(), path, policy, "proof", clock=lambda: 2)
    with pytest.raises(ValueError):
        poster.post(raw)
    assert calls == []
    assert (path.read_bytes() if path.exists() else None) == before


@pytest.mark.parametrize("field", ["tracer", "timeout", "onlyTopCall"])
def test_duplicate_trace_options_cannot_override_bounded_trace_policy(tmp_path, field):
    options = json.dumps(
        {"tracer": "callTracer", "timeout": "5s", "tracerConfig": {"onlyTopCall": False}}
    )
    old, new = {
        "tracer": ('"tracer": "callTracer"', '"tracer": "custom", "tracer": "callTracer"'),
        "timeout": ('"timeout": "5s"', '"timeout": "1000s", "timeout": "5s"'),
        "onlyTopCall": ('"onlyTopCall": false', '"onlyTopCall": true, "onlyTopCall": false'),
    }[field]
    options = options.replace(old, new)
    raw = (
        '{"jsonrpc":"2.0","id":1,"method":"debug_traceTransaction","params":["0x'
        + "22" * 32
        + '",'
        + options
        + "]}"
    ).encode()

    class Forbidden:
        def post(self, payload):
            raise AssertionError("ambiguous trace must be refused before network")

    path = tmp_path / "budget.db"
    poster = BudgetedPoster(Forbidden(), path, (BudgetPolicy("proof", 1, 0),), "proof")
    with pytest.raises(ValueError, match="duplicate"):
        poster.post(raw)
    assert not path.exists()


@pytest.mark.parametrize("phase", ["after_lock", "before_commit"])
def test_admission_expiry_never_commits_reserved_quota(tmp_path, phase):
    path = tmp_path / "budget.db"
    with RpcBudget(path, policies()) as budget:
        budget.reserve("paid", now_ms=1000)
        before = path.read_bytes()
        checked = []

        def admit():
            assert budget.db.in_transaction
            checked.append(budget.counts()["paid"])
            if phase == "after_lock" or len(checked) == 2:
                raise TimeoutError("original capture deadline expired")

        with pytest.raises(TimeoutError, match="deadline"):
            budget.reserve("paid", now_ms=5000, admission=admit)
        assert checked == ([1] if phase == "after_lock" else [1, 2])
        assert not budget.db.in_transaction
        assert budget.counts()["paid"] == 1
        assert path.read_bytes() == before
    with RpcBudget(path, policies(), require_existing=True) as reopened:
        assert reopened.counts()["paid"] == 1


def test_admission_rechecks_after_real_sqlite_write_lock_wait(tmp_path):
    path = tmp_path / "budget.db"
    with RpcBudget(path, policies()) as initial:
        initial.reserve("paid", now_ms=1000)
    ready, start, waiting = Event(), Event(), Event()
    expired = Event()

    def worker():
        with RpcBudget(path, policies(), require_existing=True) as budget:
            budget.db.set_trace_callback(
                lambda sql: waiting.set() if sql == "BEGIN IMMEDIATE" else None
            )
            ready.set()
            assert start.wait(5)

            def admit():
                assert budget.db.in_transaction
                if expired.is_set():
                    raise TimeoutError("deadline elapsed during SQLite lock wait")

            with pytest.raises(TimeoutError, match="lock wait"):
                budget.reserve("paid", now_ms=5000, admission=admit)
            assert not budget.db.in_transaction
            return budget.counts()

    with (
        ThreadPoolExecutor(max_workers=1) as pool,
        sqlite3.connect(path, isolation_level=None) as holder,
    ):
        result = pool.submit(worker)
        assert ready.wait(5)
        holder.execute("BEGIN IMMEDIATE")
        start.set()
        assert waiting.wait(5)
        expired.set()
        holder.execute("COMMIT")
        assert result.result(timeout=5) == {"public": 0, "paid": 1}


@pytest.mark.parametrize("wait_expected", [0, 2000])
def test_optional_guard_preserves_frozen_policy_and_pacing(tmp_path, wait_expected):
    with RpcBudget(tmp_path / "budget.db", policies()) as budget:
        budget.reserve("paid", now_ms=1000)
        checks = []
        wait = budget.reserve(
            "paid",
            now_ms=2000 if wait_expected else 4000,
            admission=lambda: checks.append(budget.db.in_transaction),
        )
        assert wait == wait_expected and checks == [True, True]
        assert budget.counts()["paid"] == (1 if wait_expected else 2)


def test_guard_cancellation_rolls_back_even_base_exception(tmp_path):
    class Cancelled(BaseException):
        pass

    with RpcBudget(tmp_path / "budget.db", policies()) as budget:

        def cancel():
            if budget.counts()["paid"]:
                raise Cancelled()

        with pytest.raises(Cancelled):
            budget.reserve("paid", now_ms=1000, admission=cancel)
        assert not budget.db.in_transaction and budget.counts()["paid"] == 0


def test_cumulative_sublimit_survives_restart_and_covers_both_sources(tmp_path):
    path = tmp_path / "budget.db"
    with RpcBudget(path, policies()) as budget:
        budget.reserve("public", now_ms=1000)
        (digest,) = budget.db.execute("SELECT digest FROM rpc_usage WHERE seq=1").fetchone()
    info = path.stat()
    limit = UsageLimit(1, digest, (info.st_dev, info.st_ino), 2)
    with RpcBudget(path, policies(), require_existing=True, usage_limit=limit) as budget:
        assert budget.reserve("public", now_ms=2000) == 0
    with RpcBudget(path, policies(), require_existing=True, usage_limit=limit) as budget:
        assert budget.reserve("paid", now_ms=2000) == 0
    with RpcBudget(path, policies(), require_existing=True, usage_limit=limit) as budget:
        with pytest.raises(ValueError, match="cumulative RPC sublimit exhausted"):
            budget.reserve("public", now_ms=3000)
        assert budget.counts() == {"public": 2, "paid": 1}
        assert budget.remaining_under_limit() == 0


def test_cumulative_sublimit_requires_verified_prefix_and_original_file(tmp_path):
    path = tmp_path / "budget.db"
    with RpcBudget(path, policies()) as budget:
        budget.reserve("public", now_ms=1000)
        (digest,) = budget.db.execute("SELECT digest FROM rpc_usage WHERE seq=1").fetchone()
    info = path.stat()
    limit = UsageLimit(1, digest, (info.st_dev, info.st_ino), 2)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="checkpoint differs"):
        RpcBudget(path, policies(), read_only=True, usage_limit=replace(limit, digest="a" * 64))
    assert path.read_bytes() == before
    replacement = path.with_suffix(".replacement")
    with RpcBudget(replacement, policies()) as budget:
        budget.reserve("public", now_ms=1000)
        assert budget.db.execute("SELECT digest FROM rpc_usage WHERE seq=1").fetchone() == (digest,)
    replacement.replace(path)
    with pytest.raises(ValueError, match="checkpoint differs"):
        RpcBudget(path, policies(), read_only=True, usage_limit=limit)
    assert path.read_bytes() == before
