import json

import pytest
from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.transport import JsonRpcTransport
from oasis_agent.shadow.pool_state_capture import (
    collect_slot0_window_quorum,
    persist_slot0_window_evidence,
    replay_slot0_window_evidence,
)

CHAIN_ID = 4663
STATE_VIEW = "0x" + "11" * 20
POOL_MANAGER = "0x" + "22" * 20
POOL_ID = "0x" + "33" * 32
BLOCK_HASH = "0x" + "aa" * 32
PARENT_HASH = "0x" + "bb" * 32
STATE_CODE = "6000"
MANAGER_CODE = "6001"
STATE_HASH = "0x" + keccak(bytes.fromhex(STATE_CODE)).hex()
MANAGER_HASH = "0x" + keccak(bytes.fromhex(MANAGER_CODE)).hex()


class Poster:
    def __init__(self, *, sqrt_price_x96=1 << 96, bad_getter=False):
        self.sqrt_price_x96 = sqrt_price_x96
        self.bad_getter = bad_getter

    def post(self, payload):
        request = json.loads(payload)
        method, params = request["method"], request["params"]
        if method == "eth_chainId":
            result = hex(CHAIN_ID)
        elif method == "eth_getBlockByNumber":
            result = {
                "number": "0x2a",
                "hash": BLOCK_HASH,
                "parentHash": PARENT_HASH,
                "timestamp": "0x3e8",
            }
        elif method == "eth_getCode":
            assert params[1] == {"blockHash": BLOCK_HASH, "requireCanonical": True}
            result = "0x" + (STATE_CODE if params[0] == STATE_VIEW else MANAGER_CODE)
        elif method == "eth_call":
            assert params[1] == {"blockHash": BLOCK_HASH, "requireCanonical": True}
            call = params[0]
            if call["data"] == "0x" + keccak(b"poolManager()")[:4].hex():
                address = STATE_VIEW if self.bad_getter else POOL_MANAGER
                result = "0x" + "00" * 12 + address[2:]
            else:
                assert call["to"] == STATE_VIEW
                assert call["data"].startswith("0x" + keccak(b"getSlot0(bytes32)")[:4].hex())
                result = (
                    "0x"
                    + encode(
                        ["uint160", "int24", "uint24", "uint24"],
                        [self.sqrt_price_x96, 0, 0, 3_000],
                    ).hex()
                )
        else:
            raise AssertionError(f"unexpected RPC method: {method}")
        return canonical_json({"jsonrpc": "2.0", "id": request["id"], "result": result})


def session(source, **kwargs):
    recorder = BoundedExchangeRecorder()
    return (
        JsonRpcTransport("https://fixture.invalid", http=Poster(**kwargs), recorder=recorder),
        recorder,
        source,
    )


def test_slot0_window_quorum_persists_and_replays_exact_wire(tmp_path):
    quorum = collect_slot0_window_quorum(
        session("rpc-a"),
        session("rpc-b"),
        chain_id=CHAIN_ID,
        state_view=STATE_VIEW,
        pool_manager=POOL_MANAGER,
        pool_id=POOL_ID,
        block_numbers=(42,),
        state_view_runtime_keccak=STATE_HASH,
        pool_manager_runtime_keccak=MANAGER_HASH,
    )
    assert quorum.left.states == quorum.right.states
    assert quorum.left.states[0].sqrt_price_x96 == 1 << 96
    store = EvidenceStore(tmp_path / "slot0.db")
    try:
        refs = persist_slot0_window_evidence(store, quorum)
        assert replay_slot0_window_evidence(store, refs[0]) == quorum.left
        assert replay_slot0_window_evidence(store, refs[1]) == quorum.right
    finally:
        store.close()


@pytest.mark.parametrize(
    "left,right,match",
    [
        (session("same"), session("same"), "distinct"),
        (session("a"), session("b", sqrt_price_x96=(1 << 96) + 1), "disagree"),
        (session("a"), session("b", bad_getter=True), "manager"),
    ],
)
def test_slot0_window_fails_closed(left, right, match):
    with pytest.raises(ValueError, match=match):
        collect_slot0_window_quorum(
            left,
            right,
            chain_id=CHAIN_ID,
            state_view=STATE_VIEW,
            pool_manager=POOL_MANAGER,
            pool_id=POOL_ID,
            block_numbers=(42,),
            state_view_runtime_keccak=STATE_HASH,
            pool_manager_runtime_keccak=MANAGER_HASH,
        )
