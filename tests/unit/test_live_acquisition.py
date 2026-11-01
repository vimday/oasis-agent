"""Provider concurrency preserves per-source ordering and durable budget limits."""

import json
import threading
from dataclasses import asdict
from pathlib import Path

import pytest

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.live.acquisition import acquire_pair
from oasis_agent.live.chain import ChainError
from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.live.runner import MAX_OBSERVATION_FAILURES
from oasis_agent.live.state import StateStore
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import RpcBudget
from oasis_agent.rpc.transport import JsonRpcTransport
from tests.unit.test_live_lp import ScriptedRpc, live_loop, market


class Rendezvous:
    def __init__(self, barrier, *, fail=False):
        self.barrier, self.fail = barrier, fail
        self.methods = []
        self.done = threading.Event()

    def post(self, payload):
        req = json.loads(payload)
        self.methods.append(req["method"])
        if len(self.methods) == 1:
            self.barrier.wait(timeout=3)
            if self.fail:
                raise ValueError("synthetic provider refusal")
        self.done.set()
        return canonical_json({"jsonrpc": "2.0", "id": req["id"], "result": "0x1"})


def session(http, alias):
    recorder = BoundedExchangeRecorder()
    return (
        JsonRpcTransport("https://unused.invalid", http=http, recorder=recorder, retries=0),
        recorder,
        alias,
    )


@pytest.mark.parametrize("fail", [False, True])
def test_sources_run_concurrently_but_each_call_sequence_stays_ordered(fail):
    barrier = threading.Barrier(2)
    first, second = Rendezvous(barrier, fail=fail), Rendezvous(barrier)
    pair = session(first, "a"), session(second, "b")
    calls = [("eth_chainId", []), ("eth_getBlockByNumber", ["0x1", False])]
    if fail:
        with pytest.raises(Exception, match="transport failure"):
            acquire_pair(pair, calls)
    else:
        acquire_pair(pair, calls)
        assert first.methods == second.methods == [c[0] for c in calls]
        assert len(pair[0][1].exchanges) == len(pair[1][1].exchanges) == 2
    assert second.done.is_set()


@pytest.mark.parametrize("value", [0, True, 4097, -1, 2.0])
def test_profile_cannot_expand_quota_or_coerce_values(value):
    with pytest.raises(ValueError):
        LiveRpcProfile(max_requests=value)


def test_pacing_profile_cannot_reset_an_existing_budget(tmp_path: Path):
    path = tmp_path / "rpc.db"
    sources = ("a", "b")
    old = LiveRpcProfile()
    with RpcBudget(path, old.policies(sources)) as budget:
        budget.reserve("a", now_ms=1000)
    with pytest.raises(ValueError, match="policy changed"):
        RpcBudget(path, LiveRpcProfile(min_interval_ms=250).policies(sources))
    with RpcBudget(path, old.policies(sources)) as budget:
        assert budget.counts() == {"a": 1, "b": 0}
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(asdict(LiveRpcProfile(min_interval_ms=250))))
    assert LiveRpcProfile.load(profile).min_interval_ms == 250
    profile.write_text('{"max_requests":1,"max_requests":2,"min_interval_ms":250}')
    with pytest.raises(ValueError, match="duplicate"):
        LiveRpcProfile.load(profile)


def test_live_observation_failure_stops_rpc_polling_across_restart(tmp_path, monkeypatch):
    loop = live_loop(tmp_path, ScriptedRpc())
    loop.store = StateStore(tmp_path / "live.json")
    loop.store.bind(sha256_hex(canonical_json(asdict(loop.c))))
    loop.state = loop.store.load("live")
    calls = []

    def unavailable():
        calls.append(1)
        raise ChainError("account NAV refused: stale feed")

    monkeypatch.setattr(loop, "observe", unavailable)
    assert "stale feed" in loop.tick()
    assert loop.state.halted == "live observation refused; operator review required before polling"
    loop.state = StateStore(loop.store.path).load("live")
    assert loop.tick().startswith("halted:")
    assert calls == [1]


def test_transient_observation_failures_retry_then_halt(tmp_path, monkeypatch):
    loop = live_loop(tmp_path, ScriptedRpc())
    calls = []

    def dropped():
        calls.append(1)
        raise ChainError("complete account NAV unresolved (details redacted)")

    monkeypatch.setattr(loop, "observe", dropped)
    for _ in range(MAX_OBSERVATION_FAILURES - 1):
        assert loop.tick().startswith("skip:")
        assert loop.state.halted is None
    loop.tick()
    assert loop.state.halted == "live observation refused; operator review required before polling"
    assert loop.tick().startswith("halted:")
    assert len(calls) == MAX_OBSERVATION_FAILURES


def test_valuation_refusal_halts_immediately(tmp_path, monkeypatch):
    loop = live_loop(tmp_path, ScriptedRpc())

    def refused():
        raise ChainError("account NAV refused: NAV ETH / USD feed incomplete, stale or future")

    monkeypatch.setattr(loop, "observe", refused)
    loop.tick()
    assert loop.state.halted == "live observation refused; operator review required before polling"


def test_successful_observation_resets_the_transient_failure_count(tmp_path, monkeypatch):
    loop = live_loop(tmp_path, ScriptedRpc())
    outcomes = iter(["drop", "drop", "ok", "drop", "drop"])

    def observe():
        if next(outcomes) == "drop":
            raise ChainError("complete account NAV unresolved (details redacted)")
        return market()

    def after_observation(_m):
        raise ChainError("stop after a successful observation")

    monkeypatch.setattr(loop, "observe", observe)
    monkeypatch.setattr(loop, "nav_usd", after_observation)
    for _ in range(5):
        loop.tick()
    # Two drops, a success that resets the count, then two more drops: never three in a row.
    assert loop.state.halted is None
    assert loop.observation_failures == 2
