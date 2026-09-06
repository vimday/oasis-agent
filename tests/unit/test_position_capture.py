"""Synthetic full-wire fixtures, never evidence of live chain profitability."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.position_capture import (
    PositionSnapshotCapture,
    collect_position_snapshot,
    persist_position_snapshot_evidence,
)
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.client import RpcClient
from oasis_agent.rpc.transport import JsonRpcTransport
from tests.unit.test_position_dataset import scenario

VIEW = "0x" + "99" * 20


def snapshot_capture(*, closing: bool = False, reorg: bool = False) -> PositionSnapshotCapture:
    opening, ending, core, _ = scenario()
    snapshot = ending if closing else opening
    header = core.blocks[-1 if closing else 0]
    key = snapshot.pool_key
    packed = (
        (int(snapshot.pool_id[:52], 16) << 56)
        | ((snapshot.tick_lower & 0xFFFFFF) << 8)
        | ((snapshot.tick_upper & 0xFFFFFF) << 32)
    )
    values = {
        "ownerOf(uint256)": (["address"], [snapshot.nft_owner]),
        "getPoolAndPositionInfo(uint256)": (
            ["(address,address,uint24,int24,address)", "uint256"],
            [(key.currency0, key.currency1, key.fee, key.tick_spacing, key.hooks), packed],
        ),
        "getPositionLiquidity(uint256)": (["uint128"], [snapshot.liquidity]),
        "getPositionInfo(bytes32,bytes32)": (
            ["uint128", "uint256", "uint256"],
            [snapshot.liquidity, 0, 0],
        ),
        "getFeeGrowthInside(bytes32,int24,int24)": (["uint256", "uint256"], [0, 0]),
    }
    results = {
        "0x" + keccak(sig.encode())[:4].hex(): "0x" + encode(*value).hex()
        for sig, value in values.items()
    }

    class Poster:
        headers = 0

        def post(self, payload: bytes) -> bytes:
            request = json.loads(payload)
            method = request["method"]
            if method == "eth_chainId":
                result = hex(snapshot.block.chain_id)
            elif method == "eth_getBlockByNumber":
                self.headers += 1
                result = {
                    "number": hex(snapshot.block.number),
                    "hash": snapshot.block.block_hash,
                    "timestamp": hex(snapshot.block.timestamp),
                    "parentHash": header.parent_hash,
                }
                if reorg and self.headers == 2:
                    result["hash"] = "0x" + "ff" * 32
            elif method == "eth_call":
                result = results[request["params"][0]["data"][:10]]
            else:
                raise AssertionError("unexpected request")
            return canonical_json({"jsonrpc": "2.0", "id": request["id"], "result": result})

    recorder = BoundedExchangeRecorder()
    rpc = RpcClient(JsonRpcTransport("https://fixture.invalid", http=Poster(), recorder=recorder))
    collected_header, collected = collect_position_snapshot(
        rpc,
        position_manager=snapshot.position_manager,
        state_view=VIEW,
        token_id=snapshot.token_id,
        block_number=snapshot.block.number,
    )
    assert collected == snapshot
    return PositionSnapshotCapture("rpc-a", collected_header, VIEW, collected, recorder.exchanges)


def test_snapshot_reconstructs_and_retains_exact_envelopes(tmp_path: Path) -> None:
    capture = snapshot_capture()
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        ref = persist_position_snapshot_evidence(store, capture)
        record = store.get_record(ref)
        assert record.payload == capture.payload
        requests = json.loads(record.request_payload)["exchanges"]
        responses = json.loads(record.raw_response_payload)["exchanges"]
        assert [bytes.fromhex(item["payload"]) for item in requests] == [
            item.request_payload for item in capture.exchanges
        ]
        assert [bytes.fromhex(item["payload"]) for item in responses] == [
            item.response_payload for item in capture.exchanges
        ]
        assert json.loads(record.interpretation_payload)["execution_eligible"] is False
    finally:
        store.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("nft_owner", "0x" + "88" * 20),
        ("liquidity", 999),
        ("uncollected_fee0", 1),
        ("fee_growth_inside1_x128", 1),
        ("token_id", 8),
    ],
)
def test_rejects_changed_snapshot_with_original_evidence(field: str, value: object) -> None:
    capture = snapshot_capture()
    with pytest.raises(ValueError):
        replace(capture, snapshot=replace(capture.snapshot, **{field: value}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("liquidity", 100.0),
        ("token_id", 7.0),
        ("fee_growth_inside0_x128", False),
        ("has_subscriber", 0),
    ],
)
def test_equal_numeric_values_cannot_bypass_snapshot_types(field: str, value: object) -> None:
    capture = snapshot_capture()
    with pytest.raises((TypeError, ValueError)):
        replace(capture, snapshot=replace(capture.snapshot, **{field: value}))


@pytest.mark.parametrize(
    "mutation",
    [
        "latest",
        "canonical_integer",
        "wrong_address",
        "wrong_calldata",
        "wrong_result",
        "missing_call",
        "missing_chain",
        "missing_close",
        "duplicate_id",
        "failed_call",
        "extra_context",
        "wrong_header",
    ],
)
def test_rejects_unbound_or_incomplete_capture(mutation: str) -> None:
    capture = snapshot_capture()
    items = list(capture.exchanges)
    exchange = items[2]
    request = json.loads(exchange.request_payload)
    response = json.loads(exchange.response_payload)
    if mutation == "latest":
        request["params"][1] = "latest"
    elif mutation == "canonical_integer":
        request["params"][1]["requireCanonical"] = 1
    elif mutation == "wrong_address":
        request["params"][0]["to"] = VIEW
    elif mutation == "wrong_calldata":
        request["params"][0]["data"] = request["params"][0]["data"][:-2] + "08"
    elif mutation == "wrong_result":
        response["result"] = "0x" + encode(["address"], [VIEW]).hex()
    elif mutation == "extra_context":
        request["params"][0]["from"] = VIEW
    elif mutation == "failed_call":
        response.pop("result")
        response["error"] = {"code": -32000, "message": "fixture failure"}
    items[2] = replace(
        exchange, request_payload=canonical_json(request), response_payload=canonical_json(response)
    )
    if mutation == "missing_call":
        items.pop(2)
    elif mutation == "missing_chain":
        items.pop(0)
    elif mutation == "missing_close":
        items.pop()
    elif mutation == "duplicate_id":
        items.insert(3, exchange)
    elif mutation == "wrong_header":
        response = json.loads(items[-1].response_payload)
        response["result"]["hash"] = "0x" + "ff" * 32
        items[-1] = replace(items[-1], response_payload=canonical_json(response))
    with pytest.raises(ValueError):
        replace(capture, exchanges=tuple(items))


def test_collector_rejects_block_reorg() -> None:
    with pytest.raises(ValueError, match="block changed"):
        snapshot_capture(reorg=True)


def test_snapshot_and_event_evidence_feed_one_position_replay(tmp_path: Path) -> None:
    from oasis_agent.evidence.ingestion import IngestionTrust, persist_and_verify_block_range
    from oasis_agent.farming.position_pipeline import replay_captured_position_datasets
    from tests.rpc_evidence_helpers import range_exchanges
    from tests.unit.test_position_dataset import CORE

    store = EvidenceStore(tmp_path / "pipeline.db")
    try:
        _, _, core, nft = scenario()
        verified = tuple(
            persist_and_verify_block_range(
                store, dataset, range_exchanges(dataset), research_single_provider=True
            )
            for dataset in (core, nft)
        )
        report = replay_captured_position_datasets(
            store,
            opening=snapshot_capture(),
            closing=snapshot_capture(closing=True),
            pool_manager=CORE,
            core=verified[0],
            ownership=verified[1],
        )
        assert report.trust is IngestionTrust.SINGLE_PROVIDER_RESEARCH
        assert report.position.ledger.closing.liquidity == 120
        assert (
            report.position.ledger.ownership_replay.final_owner
            == "0x" + nft.logs[0].topics[2][-40:]
        )
        payload = json.loads(report.payload)
        assert payload["unresolved_settlements"] == 1
        assert payload["cumulative_owner_amount0"] is None
        assert payload["cumulative_owner_amount1"] is None
        assert payload["execution_eligible"] is False
        assert payload["net_pnl"] is None
        assert (
            payload["opening_snapshot_evidence"]["raw_response_hash"]
            == report.opening_evidence.raw_response_hash
        )
        assert store.get(report.opening_evidence) == snapshot_capture().payload
        assert store.get(report.closing_evidence) == snapshot_capture(closing=True).payload
    finally:
        store.close()


def test_pipeline_rejects_fabricated_stored_event_envelopes(tmp_path: Path) -> None:
    from oasis_agent.evidence.ingestion import persist_and_verify_block_range
    from oasis_agent.farming.position_pipeline import replay_captured_position_datasets
    from tests.rpc_evidence_helpers import range_exchanges
    from tests.unit.test_position_dataset import CORE

    store = EvidenceStore(tmp_path / "bad-pipeline.db")
    try:
        _, _, core, nft = scenario()
        verified = tuple(
            persist_and_verify_block_range(
                store, dataset, range_exchanges(dataset), research_single_provider=True
            )
            for dataset in (core, nft)
        )
        forged_ref = store.put(
            "canonical_block_range",
            core.payload,
            core.blocks[-1].ref,
            core.source,
            request={"fake": True},
            raw_response={"fake": True},
            interpretation={"fake": True},
            schema_version=2,
        )
        forged = replace(verified[0], evidence=forged_ref)
        with pytest.raises(ValueError, match="retained RPC envelope"):
            replay_captured_position_datasets(
                store,
                opening=snapshot_capture(),
                closing=snapshot_capture(closing=True),
                pool_manager=CORE,
                core=forged,
                ownership=verified[1],
            )
    finally:
        store.close()


@pytest.mark.parametrize("mutation", ["length", "metadata", "boolean_id", "hex", "duplicate_json"])
def test_restored_envelopes_are_strict(tmp_path: Path, mutation: str) -> None:
    from oasis_agent.evidence.rpc_validation import restore_exchanges

    store = EvidenceStore(tmp_path / "restore.db")
    try:
        capture = snapshot_capture()
        record = store.get_record(persist_position_snapshot_evidence(store, capture))
        assert (
            restore_exchanges(record.request_payload, record.raw_response_payload)
            == capture.exchanges
        )
        request = json.loads(record.request_payload)
        response = json.loads(record.raw_response_payload)
        if mutation == "length":
            response["exchanges"].pop()
        elif mutation == "metadata":
            response["exchanges"][0]["attempt"] = 2
        elif mutation == "boolean_id":
            response["exchanges"][0]["request_id"] = True
        elif mutation == "hex":
            response["exchanges"][0]["payload"] += " "
        raw_request = canonical_json(request)
        if mutation == "duplicate_json":
            raw_request = b'{"encoding":"hex_exact_bytes",' + raw_request[1:]
        with pytest.raises(ValueError):
            restore_exchanges(raw_request, canonical_json(response))
    finally:
        store.close()
