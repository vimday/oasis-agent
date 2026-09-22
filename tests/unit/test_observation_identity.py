import json

import pytest
from eth_hash.auto import keccak

from oasis_agent.evidence.observation_identity import (
    RuntimeIdentityPin,
    V4ObservationPolicy,
    collect_observation_identity_quorum,
    persist_observation_identity_evidence,
    replay_observation_identity_evidence,
)
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.models.common import ZERO_ADDRESS, canonical_json
from oasis_agent.models.v4 import DYNAMIC_FEE_FLAG, PoolKey
from oasis_agent.rpc.transport import JsonRpcTransport

CHAIN_ID = 4663
POOL_MANAGER = "0x" + "11" * 20
POSITION_MANAGER = "0x" + "22" * 20
STATE_VIEW = "0x" + "33" * 20
HOOK = "0x" + "44" * 20
TOKEN = "0x" + "55" * 20
BLOCK_HASH = "0x" + "aa" * 32
PARENT_HASH = "0x" + "bb" * 32
CODES = {
    POOL_MANAGER: "6000",
    POSITION_MANAGER: "6001",
    STATE_VIEW: "6002",
    HOOK: "6003",
}


def pin(address):
    code = CODES[address]
    return RuntimeIdentityPin(address, "0x" + keccak(bytes.fromhex(code)).hex())


def policy(hook=HOOK):
    key = PoolKey(ZERO_ADDRESS, TOKEN, DYNAMIC_FEE_FLAG, 60, hook)
    return V4ObservationPolicy(
        chain_id=CHAIN_ID,
        pool_key=key,
        pool_manager=pin(POOL_MANAGER),
        position_manager=pin(POSITION_MANAGER),
        state_view=pin(STATE_VIEW),
        hook=None if hook == ZERO_ADDRESS else pin(HOOK),
    )


class Poster:
    def __init__(self, *, wrong_state_manager=False):
        self.wrong_state_manager = wrong_state_manager
        self.code_addresses = []

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
            self.code_addresses.append(params[0])
            result = "0x" + CODES[params[0]]
        elif method == "eth_call":
            call = params[0]
            assert call["data"] == "0x" + keccak(b"poolManager()")[:4].hex()
            address = (
                STATE_VIEW
                if self.wrong_state_manager and call["to"] == STATE_VIEW
                else POOL_MANAGER
            )
            result = "0x" + "00" * 12 + address[2:]
        else:
            raise AssertionError(method)
        return canonical_json({"jsonrpc": "2.0", "id": request["id"], "result": result})


def session(source, poster=None):
    poster = poster or Poster()
    recorder = BoundedExchangeRecorder()
    return (
        JsonRpcTransport("https://fixture.invalid", http=poster, recorder=recorder),
        recorder,
        source,
    )


def test_observation_identity_binds_runtimes_getters_and_replays(tmp_path):
    quorum = collect_observation_identity_quorum(
        session("rpc-a"), session("rpc-b"), policy=policy(), block_number=42
    )
    assert quorum.left.identity == quorum.right.identity
    store = EvidenceStore(tmp_path / "identity.db")
    try:
        refs = persist_observation_identity_evidence(store, quorum)
        assert replay_observation_identity_evidence(store, refs[0]) == quorum.left
        assert replay_observation_identity_evidence(store, refs[1]) == quorum.right
    finally:
        store.close()


def test_hookless_identity_never_reads_zero_address_code():
    left, right = Poster(), Poster()
    quorum = collect_observation_identity_quorum(
        session("rpc-a", left),
        session("rpc-b", right),
        policy=policy(ZERO_ADDRESS),
        block_number=42,
    )
    assert quorum.left.identity.hook is None
    assert all(ZERO_ADDRESS not in item.code_addresses for item in (left, right))


def test_wrong_dependency_getter_and_shared_alias_fail_closed():
    with pytest.raises(ValueError, match="manager"):
        collect_observation_identity_quorum(
            session("rpc-a"),
            session("rpc-b", Poster(wrong_state_manager=True)),
            policy=policy(),
            block_number=42,
        )
    with pytest.raises(ValueError, match="distinct"):
        collect_observation_identity_quorum(
            session("same"), session("same"), policy=policy(), block_number=42
        )
