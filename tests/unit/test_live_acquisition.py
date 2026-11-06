"""Provider concurrency preserves per-source ordering and durable budget limits."""

import json
import threading
from dataclasses import asdict
from pathlib import Path

import pytest

import oasis_agent.live.acquisition as acquisition
from oasis_agent.errors import RpcError
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.live.acquisition import acquire_pair
from oasis_agent.live.chain import ChainError
from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.live.runner import MAX_OBSERVATION_FAILURES, MAX_SETTLEMENT_FAILURES
from oasis_agent.live.state import Pending, StateStore
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import RpcBudget
from oasis_agent.rpc.transport import JsonRpcTransport
from tests.unit.test_live_lp import ScriptedRpc, bind_pending, live_loop, market


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


@pytest.mark.parametrize("failed_source", [0, 1])
@pytest.mark.parametrize("failed_call", [1, 2])
def test_provider_failure_cancels_later_reads_and_joins_inflight_peer(
    monkeypatch, failed_source, failed_call
):
    barrier = threading.Barrier(2)
    cancelled, peer_completed = threading.Event(), threading.Event()
    monkeypatch.setattr(acquisition, "Event", lambda: cancelled)

    class ControlledProvider:
        def __init__(self, index):
            self.index, self.methods = index, []

        def post(self, payload):
            req = json.loads(payload)
            self.methods.append(req["method"])
            barrier.wait(timeout=3)
            if len(self.methods) == failed_call:
                if self.index == failed_source:
                    raise ValueError("synthetic provider refusal")
                # Keep the peer request in flight until the failure has been
                # signalled, independent of which worker the caller joins first.
                assert cancelled.wait(timeout=3)
                peer_completed.set()
            return canonical_json({"jsonrpc": "2.0", "id": req["id"], "result": "0x1"})

    providers = ControlledProvider(0), ControlledProvider(1)
    pair = session(providers[0], "a"), session(providers[1], "b")
    calls = [
        ("eth_chainId", []),
        ("eth_getBlockByNumber", ["0x1", False]),
        ("eth_getCode", ["0x" + "11" * 20, "0x1"]),
    ]
    with pytest.raises(RpcError, match="transport failure"):
        acquire_pair(pair, calls)
    assert cancelled.is_set() and peer_completed.is_set()
    assert providers[0].methods == providers[1].methods == [c[0] for c in calls[:failed_call]]
    assert len(pair[failed_source][1].exchanges) == failed_call - 1
    assert len(pair[1 - failed_source][1].exchanges) == failed_call


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
    assert loop.state.observation_attempts == 2


def test_crash_during_each_observation_cannot_replenish_durable_retry_allowance(
    tmp_path, monkeypatch
):
    path = tmp_path / "live.json"
    initial = live_loop(tmp_path, ScriptedRpc())
    StateStore(path).bind(sha256_hex(canonical_json(asdict(initial.c))))
    calls = []

    class SimulatedCrash(BaseException):
        pass

    def reopen():
        loop = live_loop(tmp_path, ScriptedRpc())
        loop.store = StateStore(path, require_existing=True)
        loop.state = loop.store.load("live")
        return loop

    def interrupted():
        calls.append(1)
        raise SimulatedCrash

    for already_spent in range(MAX_OBSERVATION_FAILURES):
        loop = reopen()
        assert loop.state.observation_attempts == already_spent
        monkeypatch.setattr(loop, "observe", interrupted)
        with pytest.raises(SimulatedCrash):
            loop.tick()

    loop = reopen()
    monkeypatch.setattr(
        loop, "observe", lambda: pytest.fail("exhausted loop performed another read")
    )
    assert loop.tick().startswith("HALT live observation refused")
    assert len(calls) == MAX_OBSERVATION_FAILURES
    assert reopen().state.halted is not None


def test_pending_poll_crash_limit_survives_journal_reopen(tmp_path, monkeypatch):
    path = tmp_path / "live.json"
    loop = live_loop(tmp_path, ScriptedRpc())
    store = StateStore(path)
    store.bind(sha256_hex(canonical_json(asdict(loop.c))))
    original = store.load("live")
    pending = Pending("mint", "0x" + "ab" * 32, 3, 1_791_470_000)
    bind_pending(pending)
    original.pending = pending
    store.save(original)

    class SimulatedCrash(BaseException):
        pass

    def reopen():
        fresh = live_loop(tmp_path, ScriptedRpc())
        fresh.store = StateStore(path, require_existing=True)
        fresh.state = fresh.store.load("live")
        return fresh

    def interrupted():
        raise SimulatedCrash

    for spent in range(MAX_SETTLEMENT_FAILURES):
        loop = reopen()
        assert loop.state.settlement_attempts == spent
        monkeypatch.setattr(loop, "_resolve_pending", interrupted)
        with pytest.raises(SimulatedCrash):
            loop.tick()

    loop = reopen()
    monkeypatch.setattr(
        loop, "_resolve_pending", lambda: pytest.fail("exhausted loop polled again")
    )
    assert loop.tick().startswith("HALT pending mint settlement failed")
    retained = reopen().state
    assert retained.pending == pending
    assert retained.settlement_attempts == MAX_SETTLEMENT_FAILURES
    assert retained.halted is not None
