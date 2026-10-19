"""Durable observation quotas count failed requests and serialize competing workers."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from oasis_agent.rpc.budget import BudgetedPoster, BudgetPolicy, RpcBudget


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
