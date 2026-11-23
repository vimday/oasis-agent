"""Exercise the first-receive path and its fail-closed controls without a remote node."""

import asyncio
import json
import time
from dataclasses import asdict

import httpx
import pytest
from eth_abi.abi import encode

from oasis_agent.arbitrage.cycle_protocols import calldata, quote_request
from oasis_agent.arbitrage.event_quote import (
    AERO,
    AERO_CL,
    BASE_AMOUNT,
    BYTES_MODEL,
    CHAINS,
    DEADLINE_NS,
    ORACLE,
    V3,
    V4,
    covered_logs,
    route_request,
    route_result,
    same_opportunity,
    snap,
    swap,
)
from oasis_agent.arbitrage.event_quote_capture import (
    CAPS,
    PLAN_SCHEMA,
    Capture,
    Journal,
    pin,
    summarize,
)
from oasis_agent.arbitrage.event_quote_replay import replay
from oasis_agent.arbitrage.event_quote_replay import replay_quote as verify_retained_quote
from oasis_agent.models.common import ZERO_ADDRESS, canonical_json
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from oasis_agent.uniswap_v4.liquidity_math import MAX_SQRT_PRICE, MIN_SQRT_PRICE

HASH = "0x" + "ab" * 32
PARENT = "0x" + "12" * 32
TX = "0x" + "ca" * 32
CHAIN = CHAINS["base"]


def event(height=10, *, block_hash=HASH, index=2):
    return {
        "removed": False,
        "blockNumber": hex(height),
        "blockHash": block_hash,
        "transactionHash": TX,
        "transactionIndex": "0x0",
        "logIndex": hex(index),
        "address": CHAIN.address,
        "topics": [CHAIN.topics[0], "0x" + "00" * 32, "0x" + "00" * 32],
        "data": "0x"
        + encode(("int256", "int256", "uint160", "uint128", "int24"), (25, -30, 2**96, 5, 1)).hex(),
    }


def block(height=10, block_hash=HASH):
    return {
        "number": hex(height),
        "hash": block_hash,
        "parentHash": PARENT,
        "timestamp": "0x100",
        "baseFeePerGas": "0x1000",
        "transactions": [TX],
    }


@pytest.mark.parametrize("false_receipt_claim", [False, True])
def test_event_quote_and_poll_pin_equal_four_reads_and_replay(tmp_path, false_receipt_claim):
    async def scenario():
        rpc_calls = []
        v4 = "0x" + encode(("uint256", "uint256"), (2_000_000, 80_000)).hex()
        weth = BASE_AMOUNT - 100
        v3 = (
            "0x"
            + encode(("uint256", "uint160", "uint32", "uint256"), (weth, 2**96, 0, 90_000)).hex()
        )
        fee = "0x" + encode(("uint256",), (10_000,)).hex()

        async def handler(request):
            msg = json.loads(request.content)
            method, params = msg["method"], msg["params"]
            rpc_calls.append((method, params))
            if method == "eth_call":
                assert params[1] == pin(HASH)
                value = {V4.quoter: v4, V3.quoter: v3, ORACLE: fee}[params[0]["to"]]
            else:
                value = block()
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": value})

        raw = Journal(tmp_path / "wire.sqlite3")
        policies = tuple(BudgetPolicy(lane, cap, 0) for lane, cap in CAPS.items())
        try:
            with RpcBudget(tmp_path / "original-budget.sqlite3", policies) as budget:
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                    capture = Capture(CHAIN, budget, raw, http)
                    now = time.monotonic_ns()
                    capture.on_log(event(), now, quote_allowed=True)
                    capture.on_log(event(index=3), now, quote_allowed=True)
                    capture.on_log(event(), now, quote_allowed=True)
                    await asyncio.gather(*capture.tasks)
                    first = capture.rows["event"][0]
                    assert len(capture.rows["event"]) == 1
                    assert first["start_ns"] == now
                    assert first["gross_raw"] == -100
                    assert first["net_model_raw"] < first["gross_raw"]
                    assert rpc_calls[0] == (
                        "eth_call",
                        [
                            {
                                "to": V4.quoter,
                                "data": quote_request(V4, ZERO_ADDRESS, BASE_AMOUNT, None)[1],
                            },
                            pin(HASH),
                        ],
                    )
                    assert len(rpc_calls) == 4
                    assert (
                        rpc_calls.count(
                            (
                                "eth_call",
                                [
                                    {
                                        "to": ORACLE,
                                        "data": calldata(
                                            "getL1FeeUpperBound(uint256)",
                                            ("uint256",),
                                            (BYTES_MODEL,),
                                        ),
                                    },
                                    pin(HASH),
                                ],
                            )
                        )
                        == 1
                    )
                    await capture.quote("poll", time.monotonic_ns())
                    assert budget.counts()["event"] == budget.counts()["poll"] == 4
                    assert capture.rows["poll"][0]["net_model_raw"] == first["net_model_raw"]
                    first["receipt_verified"] = false_receipt_claim
                    result = summarize(capture, budget.counts(), now, now, None, "interrupted")
                    assert result["decision"] == "incomplete"
                    (tmp_path / "plan.json").write_bytes(
                        canonical_json(
                            {
                                "schema": PLAN_SCHEMA,
                                "budget": CAPS,
                                "chain": asdict(CHAIN),
                            }
                        )
                    )
                    (tmp_path / "summary.json").write_bytes(canonical_json(result))
                    raw.put("local", "summary", canonical_json(result))
            reason = (
                "receipt_proof_does_not_match" if false_receipt_claim else "start_not_bound_to_swap"
            )
            with pytest.raises(ValueError, match=reason):
                replay(tmp_path)
        finally:
            raw.db.close()

    asyncio.run(scenario())


def test_base_quote_overlaps_fee_read_and_waits_for_bound_header(tmp_path, monkeypatch):
    async def scenario():
        now = 10**9
        started = now
        monkeypatch.setattr(time, "monotonic_ns", lambda: now)
        fee_ready = asyncio.Event()
        legs_ready = asyncio.Event()
        calls = []
        anchor_received = None

        async def handler(request):
            nonlocal now, anchor_received
            sent = now
            msg = json.loads(request.content)
            if msg["method"] == "eth_getBlockByHash":
                await legs_ready.wait()
                now = started + DEADLINE_NS + 1
                anchor_received = now
                value = block()
            else:
                params = msg["params"]
                target = params[0]["to"]
                assert params[1] == pin(HASH)
                if target == ORACLE:
                    value = encode(("uint256",), (1,))
                    fee_ready.set()
                elif target == V4.quoter:
                    await fee_ready.wait()
                    now += 1
                    value = encode(("uint256", "uint256"), (2_000_000, 80_000))
                else:
                    assert target == V3.quoter
                    value = encode(
                        ("uint256", "uint160", "uint32", "uint256"),
                        (BASE_AMOUNT * 2, 2**96, 0, 90_000),
                    )
                    legs_ready.set()
                value = "0x" + value.hex()
                calls.append((sent, now, params, value))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": value})

        raw = Journal(tmp_path / "wire.sqlite3")
        policies = tuple(BudgetPolicy(lane, cap, 0) for lane, cap in CAPS.items())
        log = swap(event(), CHAIN, stream=True)
        try:
            with RpcBudget(tmp_path / "original-budget.sqlite3", policies) as budget:
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                    capture = Capture(CHAIN, budget, raw, http)
                    await asyncio.wait_for(capture.quote("event", started, log), timeout=2)
                    row = capture.rows["event"][0]
                    assert budget.counts()["event"] == 4
                    assert row["net_model_raw"] > 0
                    assert row["on_time"] is True
                    assert row["net_on_time"] is False
                    result = verify_retained_quote(
                        row, calls, {log: started}, "event", {}, anchor_received=anchor_received
                    )
                    assert result["fee_done_ns"] == started + DEADLINE_NS + 1
                    assert result["net_on_time"] is False
                    for changed in ({"net_on_time": True}, {"fee_done_ns": started + 1}):
                        with pytest.raises(ValueError, match="fee_inclusive_quote_timing"):
                            verify_retained_quote(
                                row | changed,
                                calls,
                                {log: started},
                                "event",
                                {},
                                anchor_received=anchor_received,
                            )
                    early_second_leg = [
                        (started if params[0]["to"] == V3.quoter else sent, received, params, val)
                        for sent, received, params, val in calls
                    ]
                    with pytest.raises(ValueError, match="quote_call_missing_or_ambiguous"):
                        verify_retained_quote(
                            row,
                            early_second_leg,
                            {log: started},
                            "event",
                            {},
                            anchor_received=anchor_received,
                        )
                    row["receipt_verified"] = True
                    result = summarize(
                        capture, budget.counts(), started, now, {"internal_missing": 0}, None
                    )
                    assert result["decision"] == "model_positive_after_deadline"
        finally:
            raw.db.close()

    asyncio.run(scenario())


def test_429_consumes_one_request_and_stops_all_lanes(tmp_path):
    async def scenario():
        raw = Journal(tmp_path / "wire.sqlite3")
        policies = tuple(BudgetPolicy(lane, cap, 0) for lane, cap in CAPS.items())
        try:
            with RpcBudget(tmp_path / "original-budget.sqlite3", policies) as budget:
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda _: httpx.Response(429, text="slow down"))
                ) as http:
                    capture = Capture(CHAIN, budget, raw, http)
                    with pytest.raises(ValueError, match="http_429"):
                        await capture.latest("primary")
                    with pytest.raises(ValueError, match="prior_rpc_failure"):
                        await capture.latest("poll")
                    assert budget.counts()["primary"] == 1
                    assert budget.counts()["poll"] == 0
        finally:
            raw.db.close()

    asyncio.run(scenario())


def test_duplicate_height_reorg_even_after_quote_budget_is_spent():
    capture = Capture(CHAINS["base"], None, None, None)
    capture.events = dict.fromkeys(range(4))
    capture.on_log(event(), 1, quote_allowed=False)
    with pytest.raises(ValueError, match="stream_log_reorg"):
        capture.on_log(event(block_hash=PARENT, index=3), 2, quote_allowed=False)
    with pytest.raises(ValueError, match="stream_head_reorg"):
        capture.check_head(snap(block(block_hash=PARENT), CHAIN))
    with pytest.raises(ValueError, match="removed"):
        capture.on_log(event() | {"removed": True}, 3, quote_allowed=True)


def test_backfill_has_no_live_clock_or_candidate():
    archived = covered_logs([event(11)], CHAIN, 10, 12)
    assert archived[(11, 2)] == swap(event(11), CHAIN, stream=False)
    with pytest.raises(ValueError, match="backfill_position_conflict"):
        covered_logs([event(11), event(11)], CHAIN, 10, 12)
    with pytest.raises(ValueError, match="backfill_position_conflict"):
        covered_logs([event(9)], CHAIN, 10, 12)
    trigger = {"block": 11, "start_ns": 1}
    assert same_opportunity(trigger, {"block": 11, "dispatch_ns": DEADLINE_NS + 1})
    assert not same_opportunity(trigger, {"block": 10, "dispatch_ns": DEADLINE_NS + 1})
    assert not same_opportunity(trigger, {"block": 11, "dispatch_ns": DEADLINE_NS + 2})


def test_rejection_requires_full_sample_with_reconciled_costs():
    capture = Capture(CHAIN, None, None, None)
    result = summarize(capture, {}, 1, 2, {"internal_missing": 0}, None)
    assert result["decision"] == "incomplete"
    for height in range(4):
        capture.rows["event"].append(
            {
                "block": height,
                "start_ns": 1,
                "receipt_verified": True,
                "on_time": height == 1,
                "net_model_raw": -100,
            }
        )
    assert summarize(capture, {}, 1, 2, {"internal_missing": 0}, None)["decision"] == (
        "reject_frozen_route"
    )
    capture.stream_error = "disconnect"
    assert summarize(capture, {}, 1, 2, {"internal_missing": 0}, None)["decision"] == "incomplete"
    capture.stream_error = None
    assert summarize(capture, {}, 1, 2, {"internal_missing": 1}, None)["decision"] == "incomplete"
    capture.rows["event"][-1]["net_model_raw"] = None
    assert summarize(capture, {}, 1, 2, {"internal_missing": 0}, None)["decision"] == "incomplete"


def test_bad_second_leg_replay_cannot_make_result():
    log = swap(event(), CHAIN, stream=True)
    first_target, first_data = quote_request(V4, ZERO_ADDRESS, BASE_AMOUNT, None)
    first_result = "0x" + encode(("uint256", "uint256"), (2_000_000, 25000)).hex()
    with pytest.raises(ValueError, match="missing_or_ambiguous"):
        verify_retained_quote(
            {"start_ns": 1, "block_hash": HASH, "dispatch_ns": 2, "finish_ns": 8, "tx": TX},
            [(2, 4, [{"to": first_target, "data": first_data}, pin(HASH)], first_result)],
            {log: 1},
            "event",
            {},
        )


def test_elysium_aero_quote_is_by_pool_and_checks_partial_input():
    chain = CHAINS["elysium-live"]
    target, data = route_request(chain, 1, 12345)
    assert target == AERO
    assert data[10:74] == AERO_CL[2:].rjust(64, "0")
    assert route_result(chain, 1, encode(("uint256", "uint160", "uint32"), (9, 2**96, 0))) == (
        9,
        None,
    )
    for limit in (MIN_SQRT_PRICE + 1, MAX_SQRT_PRICE - 1):
        with pytest.raises(ValueError, match="partial_input"):
            route_result(chain, 1, encode(("uint256", "uint160", "uint32"), (9, limit, 0)))


def test_elysium_positive_gross_with_unknown_cost_keeps_decision_incomplete():
    capture = Capture(CHAINS["elysium-live"], None, None, None)
    for height in range(4):
        capture.rows["event"].append(
            {
                "block": height,
                "start_ns": 1,
                "receipt_verified": True,
                "on_time": True,
                "gross_raw": -100,
                "net_model_raw": None,
            }
        )
    evidence = {"internal_missing": 0}
    assert summarize(capture, {}, 1, 2, evidence, None)["decision"] == "reject_frozen_route"
    capture.rows["event"][-1]["gross_raw"] = 1
    assert summarize(capture, {}, 1, 2, evidence, None)["decision"] == "incomplete"
