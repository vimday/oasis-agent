import json

import pytest
from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.token_identity import (
    TokenIdentityPin,
    collect_token_identity_quorum,
    persist_token_identity_evidence,
    replay_token_identity_evidence,
)
from oasis_agent.models.common import ZERO_ADDRESS, canonical_json
from oasis_agent.rpc.transport import JsonRpcTransport

CHAIN_ID = 4663
TOKEN = "0x" + "11" * 20
CODE = "6000"
CODE_HASH = "0x" + keccak(bytes.fromhex(CODE)).hex()
BLOCK_HASH = "0x" + "aa" * 32
PARENT_HASH = "0x" + "bb" * 32


class Poster:
    def __init__(self, decimals=6):
        self.decimals = decimals
        self.methods = []

    def post(self, payload):
        request = json.loads(payload)
        method, params = request["method"], request["params"]
        self.methods.append(method)
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
            result = "0x" + CODE
        elif method == "eth_call":
            assert params[0]["data"] == "0x" + keccak(b"decimals()")[:4].hex()
            result = "0x" + encode(["uint8"], [self.decimals]).hex()
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


def test_erc20_identity_quorum_persists_and_replays(tmp_path):
    pin = TokenIdentityPin(TOKEN, 6, CODE_HASH)
    quorum = collect_token_identity_quorum(
        session("rpc-a"), session("rpc-b"), chain_id=CHAIN_ID, block_number=42, pin=pin
    )
    assert quorum.left.identity == quorum.right.identity == pin
    store = EvidenceStore(tmp_path / "token.db")
    try:
        refs = persist_token_identity_evidence(store, quorum)
        assert replay_token_identity_evidence(store, refs[0]) == quorum.left
        assert replay_token_identity_evidence(store, refs[1]) == quorum.right
    finally:
        store.close()


def test_native_identity_is_explicit_18_decimals_without_code_or_call():
    left_poster, right_poster = Poster(), Poster()
    quorum = collect_token_identity_quorum(
        session("rpc-a", left_poster),
        session("rpc-b", right_poster),
        chain_id=CHAIN_ID,
        block_number=42,
        pin=TokenIdentityPin(ZERO_ADDRESS, 18, None),
    )
    assert quorum.left.identity.decimals == 18
    assert all("eth_getCode" not in item.methods for item in (left_poster, right_poster))
    assert all("eth_call" not in item.methods for item in (left_poster, right_poster))


@pytest.mark.parametrize("decimals", [7, 78])
def test_token_decimals_disagreement_or_unsafe_width_fails(decimals):
    with pytest.raises(ValueError):
        collect_token_identity_quorum(
            session("rpc-a"),
            session("rpc-b", Poster(decimals)),
            chain_id=CHAIN_ID,
            block_number=42,
            pin=TokenIdentityPin(TOKEN, 6, CODE_HASH),
        )


def test_replay_validates_raw_decimals_even_when_payload_hash_is_recomputed(tmp_path):
    pin = TokenIdentityPin(TOKEN, 6, CODE_HASH)
    quorum = collect_token_identity_quorum(
        session("rpc-a"), session("rpc-b"), chain_id=CHAIN_ID, block_number=42, pin=pin
    )
    store = EvidenceStore(tmp_path / "forged.db")
    try:
        ref = persist_token_identity_evidence(store, quorum)[0]
        record = store.get_record(ref)
        data = json.loads(record.payload)
        data["pin"]["decimals"] = 7
        forged = store.put(
            ref.kind,
            canonical_json(data),
            ref.block,
            ref.source,
            request=record.request_payload,
            raw_response=record.raw_response_payload,
            retrieved_at=ref.retrieved_at,
        )
        with pytest.raises(ValueError, match="decimals mismatch"):
            replay_token_identity_evidence(store, forged)
    finally:
        store.close()
