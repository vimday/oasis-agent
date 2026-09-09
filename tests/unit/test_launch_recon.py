"""Raw capture, canonical reads and provider disagreement in launch recon."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from eth_utils import keccak

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.launch.recon import acquire_identity, identity_report, retain_identity
from oasis_agent.rpc.transport import JsonRpcTransport

H = "0x" + "1" * 64


class Poster:
    def __init__(self, chain: str = "0x2105", reorg: bool = False):
        self.chain = chain
        self.reorg = reorg
        self.blocks = 0
        self.requests: list[dict[str, Any]] = []

    def post(self, payload: bytes) -> bytes:
        request = json.loads(payload)
        self.requests.append(request)
        method = request["method"]
        result: object
        if method == "eth_chainId":
            result = self.chain
        elif method == "eth_getBlockByNumber":
            self.blocks += 1
            result = {
                "number": "0x64",
                "hash": "0x" + "2" * 64 if self.reorg and self.blocks > 1 else H,
                "timestamp": "0x3e8",
                "parentHash": "0x" + "0" * 64,
            }
        elif method == "eth_getCode":
            result = "0x6000"
        elif method == "eth_call":
            selector = request["params"][0]["data"]
            number = 18 if selector == "0x" + keccak(text="decimals()")[:4].hex() else 10**27
            result = "0x" + number.to_bytes(32, "big").hex()
        else:
            raise AssertionError("unexpected or mutating method")
        return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()


def test_recon_retains_pinned_raw_evidence_and_never_promotes(tmp_path: Path) -> None:
    poster = Poster()
    recorder = BoundedExchangeRecorder()
    sample = acquire_identity(
        JsonRpcTransport("https://example.invalid", http=poster, recorder=recorder),
        recorder,
        source="base_primary",
        block_number=100,
    )
    for request in poster.requests:
        if request["method"] in {"eth_getCode", "eth_call"}:
            assert request["params"][-1] == {"blockHash": H, "requireCanonical": True}
    assert sample.decimals == 18
    assert sample.local_supply_base_units == 10**27
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        first = retain_identity(store, sample)
        second_sample = replace(sample, source="base_secondary")
        second = retain_identity(store, second_sample)
        assert first.source != second.source
        assert first.raw_response_hash
        assert json.loads(store.get(first))["reference_supply_tokens"] == 10**9
        report = identity_report((sample, second_sample), (first, second))
        assert report["identity_agreement"] is True
        assert report["execution_eligible"] is False
        report = identity_report((sample, replace(second_sample, decimals=6)), (first, second))
        assert report["decision"] == "stand_down"
    finally:
        store.close()


@pytest.mark.parametrize("poster", [Poster(chain="0x1"), Poster(reorg=True)])
def test_wrong_chain_or_reorg_rejected(poster: Poster) -> None:
    recorder = BoundedExchangeRecorder()
    with pytest.raises(ValueError):
        acquire_identity(
            JsonRpcTransport("https://example.invalid", http=poster, recorder=recorder),
            recorder,
            source="base_primary",
            block_number=100,
        )
