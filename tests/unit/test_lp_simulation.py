"""Exact simulation envelopes, pinned runtime identity and dual-source failures."""

import json
from dataclasses import replace

import pytest
from eth_hash.auto import keccak

from oasis_agent.errors import RpcError
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.execution.lp_preparation import build_lp_simulation_plan
from oasis_agent.execution.lp_simulation import (
    POOL_MANAGER_SELECTOR,
    POSITION_MANAGER_SELECTOR,
    simulate_lp_plan,
)
from oasis_agent.execution.lp_state_machine import RuntimePin, RuntimeRole
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.transport import JsonRpcTransport

from .test_lp_preparation import preparation


@pytest.mark.parametrize(
    "failure", [None, "runtime", "misbound", "revert", "reorg", "gas", "parent", "result"]
)
def test_prepare_then_simulate_without_any_mutating_rpc(tmp_path, failure):
    store, journal, cycle, positions, policy, bounds = preparation(tmp_path)
    runtime_codes = ("6000", "6002", "6004")
    runtime_hashes = tuple("0x" + keccak(bytes.fromhex(code)).hex() for code in runtime_codes)
    executor, position_manager, pool_manager = tuple(pin.address for pin in policy.runtime_pins)
    runtime_pins = (
        RuntimePin(RuntimeRole.EXECUTOR, executor, runtime_hashes[0]),
        RuntimePin(RuntimeRole.POSITION_MANAGER, position_manager, runtime_hashes[1]),
        RuntimePin(RuntimeRole.POOL_MANAGER, pool_manager, runtime_hashes[2]),
    )
    policy = replace(policy, runtime_pins=runtime_pins)
    plan = build_lp_simulation_plan(store, journal, cycle, positions, policy, bounds, now=1001)
    block = positions.left.header
    runtime_by_address = {
        pin.address: runtime_codes[index] for index, pin in enumerate(runtime_pins)
    }

    class Poster:
        def __init__(self, broken=False):
            self.headers = 0
            self.broken = broken

        def post(self, payload):
            request = json.loads(payload)
            method, params = request["method"], request["params"]
            if method == "eth_chainId":
                result = hex(block.ref.chain_id)
            elif method == "eth_getBlockByNumber":
                self.headers += 1
                result = {
                    "number": hex(block.ref.number),
                    "hash": block.ref.block_hash,
                    "timestamp": hex(block.ref.timestamp),
                    "parentHash": block.parent_hash,
                }
                if self.broken and failure == "parent":
                    result["parentHash"] = "0x" + "dd" * 32
                if self.broken and failure == "reorg" and self.headers == 2:
                    result["hash"] = "0x" + "dd" * 32
            elif method == "eth_getCode":
                assert params[0] in runtime_by_address
                assert params[1] == {
                    "blockHash": block.ref.block_hash,
                    "requireCanonical": True,
                }
                result = (
                    "0x6001"
                    if self.broken and failure == "runtime"
                    else ("0x" + runtime_by_address[params[0]])
                )
            elif method == "eth_call":
                call = params[0]
                assert params[1] == {
                    "blockHash": block.ref.block_hash,
                    "requireCanonical": True,
                }
                if call == {
                    "to": policy.executor,
                    "data": "0x" + POSITION_MANAGER_SELECTOR.hex(),
                }:
                    address = pool_manager if failure == "misbound" else position_manager
                    result = "0x" + "00" * 12 + address[2:]
                elif call == {
                    "to": policy.executor,
                    "data": "0x" + POOL_MANAGER_SELECTOR.hex(),
                }:
                    result = "0x" + "00" * 12 + pool_manager[2:]
                else:
                    assert call == {
                        "from": policy.account,
                        "to": policy.executor,
                        "value": "0x0",
                        "gas": hex(bounds.gas_limit),
                        "data": "0x" + plan.calldata.hex(),
                    }
                    if self.broken and failure == "revert":
                        return canonical_json(
                            {
                                "jsonrpc": "2.0",
                                "id": request["id"],
                                "error": {"code": 3, "message": "execution reverted"},
                            }
                        )
                    result = "0x"
            else:
                raise AssertionError("unexpected or mutating RPC method")
            return canonical_json({"jsonrpc": "2.0", "id": request["id"], "result": result})

    class Recorder(BoundedExchangeRecorder):
        broken = False

        def record(self, exchange):
            if (
                self.broken
                and failure == "gas"
                and exchange.method == "eth_call"
                and json.loads(exchange.request_payload)["params"][0].get("gas") is not None
            ):
                parsed = json.loads(exchange.request_payload)
                parsed["params"][0]["gas"] = "0x1"
                exchange = replace(exchange, request_payload=canonical_json(parsed))
            if (
                self.broken
                and failure == "result"
                and exchange.method == "eth_call"
                and json.loads(exchange.request_payload)["params"][0].get("gas") is not None
            ):
                response = json.loads(exchange.response_payload)
                response["result"] = "0xdead"
                exchange = replace(exchange, response_payload=canonical_json(response))
            super().record(exchange)

    def session(source, broken):
        recorder = Recorder()
        recorder.broken = broken
        return (
            JsonRpcTransport("https://fixture.invalid", http=Poster(broken), recorder=recorder),
            recorder,
            source,
        )

    left, right = session("a", False), session("b", True)
    try:
        if failure:
            with pytest.raises((ValueError, RpcError)):
                simulate_lp_plan(store, plan, left, right)
        else:
            result = simulate_lp_plan(store, plan, left, right)
            assert result.execution_eligible is False
            assert result.evidence[0].source != result.evidence[1].source
            assert result.evidence[0].content_hash == result.evidence[1].content_hash
            payload = json.loads(store.get_record(result.evidence[0]).payload)
            assert payload["schema"] == "oasis.lp-pinned-simulation.v3"
            assert payload["runtime_pins"][1]["address"] == position_manager
            assert payload["gas_limit"] == bounds.gas_limit and payload["account"] == policy.account
    finally:
        journal.close()
        store.close()
