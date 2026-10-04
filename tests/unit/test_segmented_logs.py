"""Segment coverage must be witnessed by original requests, not merged declarations."""

import json
from dataclasses import replace

import pytest

from oasis_agent.evidence.exact_capture import wire_envelope
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder, persist_block_range_evidence
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.segmented_logs import (
    KIND,
    SegmentedLogPlan,
    collect_segmented_logs,
    minimum_requests,
    replay_segmented_logs,
)
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.collector import BlockRangeDataset, require_provider_agreement
from oasis_agent.rpc.transport import JsonRpcTransport
from tests.rpc_evidence_helpers import range_exchanges
from tests.unit.test_market_window import MANAGER, POOL_ID, SWAP_TOPIC, blocks


class Poster:
    def __init__(self, headers, fault=None):
        self.headers = headers
        self.fault = fault
        self.calls = []

    def post(self, raw):
        r = json.loads(raw)
        method, params = r["method"], r["params"]
        self.calls.append((method, params))
        if method == "eth_chainId":
            result = hex(self.headers[0].ref.chain_id + (self.fault == "chain"))
        elif method == "eth_getBlockByNumber":
            b = next(b for b in self.headers if b.ref.number == int(params[0], 16))
            result = {
                "number": hex(b.ref.number),
                "hash": b.ref.block_hash,
                "parentHash": b.parent_hash,
                "timestamp": hex(b.ref.timestamp),
            }
            if self.fault == "reorg" and b == self.headers[-1]:
                result["hash"] = "0x" + "ff" * 32
        else:
            f = params[0]
            number = int(f["fromBlock"], 16)
            b = next(b for b in self.headers if b.ref.number == number)
            result = [
                {
                    "address": MANAGER,
                    "topics": [SWAP_TOPIC, POOL_ID],
                    "data": "0x",
                    "blockNumber": hex(number),
                    "blockHash": b.ref.block_hash,
                    "transactionHash": "0x" + "aa" * 32,
                    "transactionIndex": "0x0",
                    "logIndex": "0x0",
                    "removed": False,
                }
            ]
            if self.fault == "wrong_segment" and number == 100:
                result[0]["blockNumber"] = "0x66"
                result[0]["blockHash"] = self.headers[2].ref.block_hash
            if self.fault == "removed":
                result[0]["removed"] = True
            if self.fault == "missing_removed":
                del result[0]["removed"]
            if self.fault == "duplicate":
                result.append(result[0])
            if self.fault == "wrong_filter":
                result[0]["address"] = "0x" + "ff" * 20
        return canonical_json({"jsonrpc": "2.0", "id": r["id"], "result": result})


@pytest.fixture
def inputs(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    headers = blocks(100, 5)
    plans = {}
    for source in ("a", "b"):
        refs = []
        for start, end in ((0, 2), (2, 5)):
            d = BlockRangeDataset(source, headers[start:end], ())
            refs.append(persist_block_range_evidence(store, d, range_exchanges(d)))
        plans[source] = SegmentedLogPlan(tuple(refs), MANAGER, (SWAP_TOPIC, POOL_ID), 2)
    yield store, headers, plans
    store.close()


def collect(store, headers, plan, source, fault=None):
    poster = Poster(headers, fault)
    recorder = BoundedExchangeRecorder()
    rpc = JsonRpcTransport("https://fixture.invalid", http=poster, recorder=recorder, retries=0)
    return collect_segmented_logs(store, plan, (rpc, recorder, source)), poster, recorder


def test_exact_coverage_quorum_and_offline_replay(inputs):
    store, headers, plans = inputs
    datasets = []
    for source, plan in plans.items():
        assert minimum_requests(store, plan, source) == 6
        ref, poster, recorder = collect(store, headers, plan, source)
        before = len(poster.calls)
        dataset = replay_segmented_logs(store, ref)
        assert len(poster.calls) == before
        assert [log.block_number for log in dataset.logs] == [100, 102, 104]
        assert [q[1][0]["toBlock"] for q in poster.calls if q[0] == "eth_getLogs"] == [
            "0x65",
            "0x67",
            "0x68",
        ]
        assert len(recorder.exchanges) == 6
        datasets.append(dataset)
    require_provider_agreement(tuple(datasets))


@pytest.mark.parametrize(
    "fault",
    [
        "chain",
        "reorg",
        "wrong_segment",
        "removed",
        "missing_removed",
        "duplicate",
        "wrong_filter",
    ],
)
def test_refuses_hostile_results(inputs, fault):
    store, headers, plans = inputs
    with pytest.raises((ValueError, TypeError)):
        collect(store, headers, plans["a"], "a", fault)


def test_header_gap_overlap_or_foreign_provider_refused_before_network(inputs):
    store, headers, plans = inputs
    for refs in (
        plans["a"].header_evidence[::-1],
        plans["a"].header_evidence[:1] * 2,
        plans["b"].header_evidence,
    ):
        plan = replace(plans["a"], header_evidence=refs)
        poster, recorder = Poster(headers), BoundedExchangeRecorder()
        rpc = JsonRpcTransport("https://fixture.invalid", http=poster, recorder=recorder)
        with pytest.raises(ValueError):
            collect_segmented_logs(store, plan, (rpc, recorder, "a"))
        assert poster.calls == []


def test_replay_rejects_declaration_tampering_and_missing_segment(inputs):
    store, headers, plans = inputs
    ref, _, recorder = collect(store, headers, plans["a"], "a")
    record = store.get_record(ref)
    payload = json.loads(record.payload)
    payload["event_count"] = 0
    forged = store.put(
        KIND,
        canonical_json(payload),
        ref.block,
        "a",
        request=record.request_payload,
        raw_response=record.raw_response_payload,
    )
    with pytest.raises(ValueError):
        replay_segmented_logs(store, forged)
    exchanges = (*recorder.exchanges[:2], *recorder.exchanges[3:])
    forged = store.put(
        KIND,
        record.payload,
        ref.block,
        "a",
        request=wire_envelope(exchanges, response=False),
        raw_response=wire_envelope(exchanges, response=True),
    )
    with pytest.raises(ValueError):
        replay_segmented_logs(store, forged)


@pytest.mark.parametrize("size", [0, 101, True, 1.5])
def test_segment_bound_is_exact(inputs, size):
    _, _, plans = inputs
    with pytest.raises(ValueError):
        replace(plans["a"], segment_blocks=size)
