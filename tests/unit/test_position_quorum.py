"""Recorded canonical RPC exchanges exercised through both collector sessions."""

import json

import pytest

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.farming.position_quorum import collect_position_quorum
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.transport import JsonRpcTransport

from .test_position_capture import snapshot_capture


@pytest.mark.parametrize("wrong_chain", [False, True])
def test_collect_reconstructs_both_providers_and_rejects_wrong_chain(wrong_chain):
    sample = snapshot_capture()

    class Poster:
        def __init__(self, wrong=False):
            self.exchanges = iter(sample.exchanges)
            self.wrong = wrong

        def post(self, payload):
            request = json.loads(payload)
            expected = next(self.exchanges)
            assert request == json.loads(expected.request_payload)
            response = json.loads(expected.response_payload)
            if self.wrong and request["method"] == "eth_chainId":
                response["result"] = "0x1"
            return canonical_json(response)

    def session(source, wrong=False):
        recorder = BoundedExchangeRecorder()
        rpc = JsonRpcTransport("https://fixture.invalid", http=Poster(wrong), recorder=recorder)
        return rpc, recorder, source

    args = (session("a"), session("b", wrong_chain))
    kwargs = {
        "chain_id": sample.snapshot.block.chain_id,
        "position_manager": sample.snapshot.position_manager,
        "state_view": sample.state_view,
        "token_id": sample.snapshot.token_id,
        "block_number": sample.snapshot.block.number,
    }
    if wrong_chain:
        with pytest.raises(ValueError, match="chain"):
            collect_position_quorum(*args, **kwargs)
    else:
        result = collect_position_quorum(*args, **kwargs)
        assert result.left.snapshot == result.right.snapshot == sample.snapshot
        assert result.left.exchanges and result.right.exchanges
