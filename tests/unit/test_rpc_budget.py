"""Durable observation quotas count failed requests and serialize competing workers."""

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
