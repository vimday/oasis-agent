"""Offline regression over public Base Sepolia wire data, not a live-network test."""

import json
import shutil
from pathlib import Path

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.position_ledger import PositionLedgerEntry
from oasis_agent.farming.position_settlement import replay_position_settlement_evidence
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import BlockRef, EvidenceRef, canonical_json
from oasis_agent.models.v4 import ModifyLiquidityObservation, PoolKey

BASE = Path(__file__).resolve().parents[2] / (
    "reports/evidence/base-sepolia-2026-09-06-recorded/resumed-http1"
)


def test_real_multinft_proxy_trace_replays_but_never_becomes_owner_cash(tmp_path):
    payload = json.loads((BASE / "summary.json").read_bytes())
    raw_ref = payload["core_settlement_evidence"]
    raw_ref["block"] = BlockRef(**raw_ref["block"])
    evidence = EvidenceRef(**raw_ref)
    raw = payload["core_settlement"]
    header = BlockSnapshot(BlockRef(**raw["header"]["ref"]), raw["header"]["parent_hash"])
    entry = raw["entry"]
    entry["block"] = BlockRef(**entry["block"])
    entry["observation"] = ModifyLiquidityObservation(**entry["observation"])
    args = {
        "header": header,
        "pool_manager": raw["pool_manager"],
        "pool_key": PoolKey(**raw["pool_key"]),
        "entry": PositionLedgerEntry(**entry),
    }
    shutil.copyfile(BASE / "evidence.db", tmp_path / "evidence.db")
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        capture = replay_position_settlement_evidence(store, evidence, **args)
        result = capture.reconstruct()
        assert result.token_id == 27369
        assert result.caller_delta.amount0 == 0
        assert result.caller_delta.amount1 == -11641294639626340598824
        assert result.fees_accrued.amount0 == result.fees_accrued.amount1 == 0
        assert json.loads(capture.payload)["settlement_complete"] is False
        assert json.loads(capture.payload)["owner_amount1"] is None
        record = store.get_record(evidence)
        forged = json.loads(record.payload)
        forged["core_settlement"]["caller_delta"]["amount1"] = 1
        bad_ref = store.put(
            evidence.kind,
            canonical_json(forged),
            header.ref,
            evidence.source,
            request=json.loads(record.request_payload),
            raw_response=json.loads(record.raw_response_payload),
            interpretation=json.loads(record.interpretation_payload),
            schema_version=1,
        )
        with pytest.raises(ValueError, match="interpretation differs"):
            replay_position_settlement_evidence(store, bad_ref, **args)
    finally:
        store.close()
