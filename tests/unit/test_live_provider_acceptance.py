"""Nested raw trace/cost consensus and replay without signing capability."""

import json
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass, replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.provider_acceptance import collect_quorum, replay
from oasis_agent.models.common import BlockRef, canonical_json
from tests.unit.test_lp_receipts import PRIMARY, SECONDARY, TARGET, ReceiptFixture, sessions


@dataclass
class Fixture(ReceiptFixture):
    chain_id: int = 8453
    fault: str = ""

    @property
    def block(self):
        return BlockRef(self.chain_id, self.block_number, self.block_hash, self.timestamp)

    def _receipt(self):
        return super()._receipt() | {"to": TARGET}

    def _result(self, method):
        if method == "eth_getTransactionByHash":
            value = {
                "hash": self.transaction_hash,
                "blockHash": self.block_hash,
                "blockNumber": hex(self.block_number),
                "from": self.payer,
                "to": TARGET,
                "input": "0x1234",
                "value": "0x0",
            }
            if self.fault == "transaction":
                value["hash"] = "0x" + "ff" * 32
            if self.fault == "block":
                value["blockHash"] = "0x" + "ee" * 32
            return value
        if method == "debug_traceTransaction":
            value = {
                "type": "CALL",
                "from": self.payer,
                "to": TARGET,
                "input": "0x1234",
                "value": "0x0",
                "gas": "0x100",
                "gasUsed": "0x64",
                "calls": [
                    {
                        "type": "STATICCALL",
                        "from": TARGET,
                        "to": self.payer,
                        "input": "0x",
                        "gas": "0x20",
                        "gasUsed": "0x10",
                    }
                ],
            }
            if self.fault == "single":
                value.pop("calls")
            if self.fault == "input":
                value["input"] = "0x12"
            if self.fault == "value":
                value["value"] = "0x1"
            if self.fault == "revert":
                value["error"] = "execution reverted"
            if self.fault == "children":
                value["calls"] = None
            if self.fault == "trace_difference":
                value["calls"][0]["gasUsed"] = "0x11"
            if self.fault == "frame":
                value["calls"][0]["type"] = "UNKNOWN"
            return value
        return super()._result(method)


def run(store, a, b):
    build = sessions({PRIMARY: a, SECONDARY: b})
    return collect_quorum(store, build(PRIMARY), build(SECONDARY), a.block, a.transaction_hash)


def test_nested_trace_and_complete_op_cost_replay_from_raw(tmp_path):
    a = Fixture()
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        refs = run(store, a, replace(a, methods=[]))
        first, second = [replay(store, ref) for ref in refs]
        assert first == second and refs[0].source != refs[1].source
        assert first["cost"]["total_native"] == 218
        assert first["cost"]["operator_native"] == 11 and first["trace_frames"] == 2
        assert not first["execution_eligible"]
        assert len(a.methods) == 9


@pytest.mark.parametrize(
    "fault",
    [
        "transaction",
        "block",
        "single",
        "input",
        "value",
        "revert",
        "children",
        "trace_difference",
        "frame",
        "cost",
    ],
)
def test_disagreement_or_incomplete_trace_refuses(tmp_path, fault):
    a, b = Fixture(), Fixture(fault=fault)
    if fault == "cost":
        b.operator_fee += 1
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store, pytest.raises(ValueError):
        run(store, a, b)


def test_rehashed_summary_cannot_replace_wire_truth(tmp_path):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        ref = run(store, Fixture(), Fixture())[0]
        record = store.get_record(ref)
        summary = json.loads(record.payload)
        summary["cost"]["total_native"] = 0
        forged = store.put(
            ref.kind,
            canonical_json(summary),
            ref.block,
            ref.source,
            request=json.loads(record.request_payload),
            raw_response=json.loads(record.raw_response_payload),
            interpretation={"execution_eligible": False},
        )
        with pytest.raises(ValueError, match="interpretation"):
            replay(store, forged)


def test_rehashed_request_cannot_truncate_nested_trace(tmp_path):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        ref = run(store, Fixture(), Fixture())[0]
        record = store.get_record(ref)
        requests = json.loads(record.request_payload)
        entry = requests["exchanges"][7]
        body = json.loads(bytes.fromhex(entry["payload"]))
        body["params"][1]["tracerConfig"]["onlyTopCall"] = True
        entry["payload"] = canonical_json(body).hex()
        forged = store.put(
            ref.kind,
            record.payload,
            ref.block,
            ref.source,
            request=deepcopy(requests),
            raw_response=json.loads(record.raw_response_payload),
            interpretation={"execution_eligible": False},
        )
        with pytest.raises(ValueError, match="request/response"):
            replay(store, forged)
