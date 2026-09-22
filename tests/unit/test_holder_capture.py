"""Exact RPC holder acquisition, replays and hostile-provider boundaries."""

import json
from dataclasses import replace

import pytest
from eth_abi.abi import encode

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.launch.holder_capture import (
    BALANCE_SELECTOR,
    DECIMALS_SELECTOR,
    SUPPLY_SELECTOR,
    bind_holder_evidence,
    collect_holder_quorum,
    persist_holder_quorum,
    replay_holder_evidence,
)
from oasis_agent.launch.models import HolderTarget
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.transport import JsonRpcTransport
from scripts import capture_launch_holders as command

from .test_launch import cycle
from .test_launch_holders import policy
from .test_token_identity import BLOCK_HASH, CHAIN_ID, CODE, CODE_HASH, PARENT_HASH, TOKEN

ADDRESSES = ("0x" + "01" * 20, "0x" + "02" * 20, "0x" + "03" * 20)


def target():
    return HolderTarget.model_validate(
        policy().model_dump()
        | {
            "chain_id": CHAIN_ID,
            "target_ca": TOKEN,
            "runtime_hash": CODE_HASH,
            "token_decimals": 6,
        }
    )


class Poster:
    def __init__(
        self,
        *,
        amounts=(60, 40, 0),
        supply=100,
        chain=CHAIN_ID,
        code=CODE,
        malformed=False,
        reorg=False,
    ):
        self.amounts = amounts
        self.supply = supply
        self.chain = chain
        self.code = code
        self.malformed = malformed
        self.reorg = reorg
        self.headers = 0
        self.requests = []

    def post(self, payload):
        request = json.loads(payload)
        self.requests.append(request)
        method, params = request["method"], request["params"]
        if method == "eth_chainId":
            result = hex(self.chain)
        elif method == "eth_getBlockByNumber":
            self.headers += 1
            result = {
                "number": "0x2a",
                "hash": BLOCK_HASH,
                "parentHash": PARENT_HASH,
                "timestamp": "0x3e8",
            }
            if self.reorg and self.headers > 1:
                result["hash"] = "0x" + "cc" * 32
        elif method == "eth_getCode":
            assert params == [TOKEN, {"blockHash": BLOCK_HASH, "requireCanonical": True}]
            result = "0x" + self.code
        elif method == "eth_call":
            assert params[1] == {"blockHash": BLOCK_HASH, "requireCanonical": True}
            call = params[0]
            assert call["to"] == TOKEN
            data = call["data"]
            if data == DECIMALS_SELECTOR:
                amount = 6
            elif data == SUPPLY_SELECTOR:
                amount = self.supply
            else:
                assert data.startswith(BALANCE_SELECTOR)
                address = "0x" + data[-40:]
                amount = self.amounts[ADDRESSES.index(address)]
            result = "0x" + encode(["uint256"], [amount]).hex()
            if self.malformed:
                result += "00"
        else:
            raise AssertionError(method)
        return canonical_json({"jsonrpc": "2.0", "id": request["id"], "result": result})


def session(source, poster=None):
    recorder = BoundedExchangeRecorder()
    return (
        JsonRpcTransport(
            "https://fixture.invalid", http=poster or Poster(), recorder=recorder, retries=0
        ),
        recorder,
        source,
    )


def collect(left=None, right=None, **kwargs):
    return collect_holder_quorum(
        session("primary", left),
        session("secondary", right),
        target=target(),
        block_number=42,
        candidates=ADDRESSES,
        **kwargs,
    )


def test_capture_retention_and_raw_replay(tmp_path):
    quorum = collect()
    assert quorum.left.observation.complete
    assert len(quorum.left.observation.balances) == 2
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        references = persist_holder_quorum(store, quorum)
        for capture, reference in zip((quorum.left, quorum.right), references, strict=True):
            assert replay_holder_evidence(store, reference) == capture
            assert capture.observation.raw_evidence_digest == reference.raw_response_hash
            assert reference.block.chain_id == CHAIN_ID
    finally:
        store.close()


def test_partial_list_is_retained_without_completeness_claim():
    quorum = collect(Poster(supply=101), Poster(supply=101))
    assert quorum.left.observation.complete is False


@pytest.mark.parametrize(
    "poster",
    [
        Poster(amounts=(59, 41, 0)),
        Poster(chain=1),
        Poster(code="6001"),
        Poster(malformed=True),
        Poster(reorg=True),
        Poster(supply=99),
        Poster(supply=0),
    ],
)
def test_hostile_or_disagreeing_rpc_rejected(poster):
    with pytest.raises(ValueError):
        collect(right=poster)


@pytest.mark.parametrize(
    "candidates",
    [
        (),
        ADDRESSES * 2,
        ("0x" + "00" * 20,),
        ("not-address",),
        tuple(ADDRESSES[0] for _ in range(513)),
    ],
)
def test_invalid_candidate_sets_rejected_before_rpc(candidates):
    poster = Poster()
    with pytest.raises(ValueError):
        collect_holder_quorum(
            session("primary", poster),
            session("secondary"),
            target=target(),
            block_number=42,
            candidates=candidates,
        )
    assert poster.requests == []


def test_deadline_exhaustion_rejects_before_request():
    ticks = iter(range(1000))
    poster = Poster()
    with pytest.raises(TimeoutError):
        collect_holder_quorum(
            session("primary", poster),
            session("secondary"),
            target=target(),
            block_number=42,
            candidates=ADDRESSES,
            deadline_seconds=1,
            clock=lambda: next(ticks),
        )


def test_replay_rejects_forged_interpretation_even_with_new_content_hash(tmp_path):
    quorum = collect()
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        reference = persist_holder_quorum(store, quorum)[0]
        record = store.get_record(reference)
        data = json.loads(record.payload)
        data["observation"]["balances"][0]["balance_base_units"] = 59
        data["observation"]["complete"] = False
        forged = store.put(
            reference.kind,
            canonical_json(data),
            reference.block,
            reference.source,
            request=record.request_payload,
            raw_response=record.raw_response_payload,
            retrieved_at=reference.retrieved_at,
        )
        with pytest.raises(ValueError):
            replay_holder_evidence(store, forged)
    finally:
        store.close()


def test_forged_wire_or_reference_binding_rejected(tmp_path):
    quorum = collect()
    capture = quorum.left
    exchange = capture.exchanges[-2]
    request = json.loads(exchange.request_payload)
    request["params"][1] = "latest"
    changed = replace(exchange, request_payload=canonical_json(request))
    with pytest.raises(ValueError):
        replace(capture, exchanges=(*capture.exchanges[:-2], changed, capture.exchanges[-1]))
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        reference = persist_holder_quorum(store, quorum)[0]
        record = store.get_record(reference)
        forged = store.put(
            reference.kind,
            record.payload,
            reference.block,
            "another-source",
            request=record.request_payload,
            raw_response=record.raw_response_payload,
            retrieved_at=reference.retrieved_at,
        )
        with pytest.raises(ValueError):
            replay_holder_evidence(store, forged)
    finally:
        store.close()


def test_binding_uses_replayed_raw_data_and_rejects_lookahead(tmp_path):

    quorum = collect()
    sample = quorum.left.observation
    market = cycle()
    market = market.model_copy(
        update={
            "observations": tuple(
                item.model_copy(
                    update={
                        "source": source,
                        "snapshot": sample.snapshot,
                        "target_ca": sample.target_ca,
                        "runtime_hash": sample.runtime_hash,
                        "token_decimals": sample.token_decimals,
                        "retrieved_at": sample.retrieved_at,
                    }
                )
                for item, source in zip(market.observations, ("primary", "secondary"), strict=True)
            ),
            "evaluated_at": max(
                quorum.left.observation.retrieved_at, quorum.right.observation.retrieved_at
            ),
            "current_block": 42,
        }
    )
    store = EvidenceStore(tmp_path / "binding.db")
    try:
        refs = persist_holder_quorum(store, quorum)
        attached = bind_holder_evidence(store, refs, market)
        assert attached.holder_observations == (quorum.left.observation, quorum.right.observation)
        assert attached.evaluated_at == market.evaluated_at
        with pytest.raises(ValueError):
            bind_holder_evidence(store, refs, market.model_copy(update={"evaluated_at": 1001}))
        with pytest.raises(ValueError):
            bind_holder_evidence(store, refs, attached)
    finally:
        store.close()


def test_capture_command_retains_data_without_overwriting(tmp_path, monkeypatch):

    def transport(url, **kwargs):
        return JsonRpcTransport(url, http=Poster(), **kwargs)

    monkeypatch.setattr(command, "JsonRpcTransport", transport)
    directory = tmp_path / "capture"
    report = command.capture(
        target=target(),
        candidates=ADDRESSES,
        block=42,
        output=directory,
        urls=("https://one.invalid", "https://two.invalid"),
    )
    assert report["raw_replay_verified"] is True
    assert report["execution_eligible"] is False
    before = (directory / "report.json").read_bytes()
    with pytest.raises(FileExistsError):
        command.capture(
            target=target(),
            candidates=ADDRESSES,
            block=42,
            output=directory,
            urls=("https://one.invalid", "https://two.invalid"),
        )
    assert (directory / "report.json").read_bytes() == before


def test_capture_command_failure_retains_sanitized_halt(tmp_path, monkeypatch):

    class FailedPoster:
        def post(self, payload):
            raise ValueError("sensitive-provider-detail")

    monkeypatch.setattr(
        command,
        "JsonRpcTransport",
        lambda url, **kwargs: JsonRpcTransport(url, http=FailedPoster(), **kwargs),
    )
    directory = tmp_path / "failed"
    with pytest.raises(ValueError, match="no eligibility granted"):
        command.capture(
            target=target(),
            candidates=ADDRESSES,
            block=42,
            output=directory,
            urls=("https://one.invalid", "https://two.invalid"),
        )
    assert "sensitive-provider-detail" not in (directory / "HALT.json").read_text()
    assert not (directory / "report.json").exists()


def test_capture_failure_keeps_partial_wire_without_success_report(tmp_path, monkeypatch):
    monkeypatch.setattr(
        command,
        "JsonRpcTransport",
        lambda url, **kwargs: JsonRpcTransport(url, http=Poster(reorg=True), **kwargs),
    )
    directory = tmp_path / "reorg"
    with pytest.raises(ValueError):
        command.capture(
            target=target(),
            candidates=ADDRESSES,
            block=42,
            output=directory,
            urls=("https://one.invalid", "https://two.invalid"),
        )
    halt = json.loads((directory / "HALT.json").read_bytes())
    assert len(halt["partial_evidence"]) == 2
    assert all(item["kind"] == "holder_capture_failure" for item in halt["partial_evidence"])
    assert not (directory / "report.json").exists()
