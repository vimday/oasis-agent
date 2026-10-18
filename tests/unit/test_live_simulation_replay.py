"""Simulation approval is reconstructed from wire bytes, not retained summaries."""

import json
from contextlib import closing
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live import authority_simulation as simulation
from oasis_agent.live.state import StateStore
from oasis_agent.models.common import BlockRef, canonical_json, evidence_ref_from_json
from tests.unit.test_live_authority import setup_authority
from tests.unit.test_live_valuation import synthetic_feed_pin  # noqa: F401


@pytest.fixture
def proof(tmp_path, monkeypatch):
    path = tmp_path / "evidence.db"
    with closing(EvidenceStore(path)) as store:
        authority, key, fields, context = setup_authority(tmp_path, store, monkeypatch)
        market = authority.loop.observe()
        authority._envelope(fields, market, context)
        assert key.calls == 0
        refs = authority.simulation_evidence
        assert refs is not None
        record = store.get_record(refs[0])
        tx = json.loads(record.payload)["transaction"]
        scope = {
            "sources": authority.loop.receipt_costs.sources,
            "nonce": fields[1],
            "gas": fields[4],
            "maximum": fields[3],
        }
        block = BlockRef(
            authority.loop.c.chain_id, market.block, market.block_hash, market.timestamp
        )
        yield store, authority, key, fields, context, refs, block, tx, scope


def test_reopens_and_replays_without_sessions_or_keys(proof, tmp_path):
    _, authority, key, _, _, refs, block, tx, scope = proof
    # Equal answers can legitimately hash identically; full references preserve provenance.
    assert refs[0].content_hash == refs[1].content_hash
    assert refs[0].source != refs[1].source
    with closing(EvidenceStore(tmp_path / "evidence.db")) as reopened:
        values = simulation.replay_simulation_quorum(
            reopened, refs, authority.loop.c, block, tx, **scope
        )
    assert len(values) == 12 and key.calls == 0


@pytest.mark.parametrize(
    "fault", ["nonce", "gas", "maximum", "data", "block", "source", "duplicate"]
)
def test_foreign_scope_or_repeated_source_is_not_a_quorum(proof, fault):
    store, authority, _, _, _, refs, block, tx, scope = proof
    if fault in ("nonce", "gas", "maximum"):
        scope[fault] += 1
    elif fault == "data":
        tx["data"] += "00"
    elif fault == "block":
        block = replace(block, number=block.number + 1)
    elif fault == "source":
        scope["sources"] = ("undeclared", refs[1].source)
    else:
        refs = (refs[0], refs[0])
    with pytest.raises(ValueError):
        simulation.replay_simulation_quorum(store, refs, authority.loop.c, block, tx, **scope)


@pytest.mark.parametrize(
    "fault",
    [
        "payload",
        "return",
        "fee",
        "disagreement",
        "closing_nonce",
        "request",
        "missing",
        "legacy",
        "eligible",
    ],
)
def test_rehashed_forgery_still_fails_raw_reconstruction(proof, fault):
    store, authority, _, _, _, refs, block, tx, scope = proof
    record = store.get_record(refs[0])
    payload = json.loads(record.payload)
    request = json.loads(record.request_payload)
    response = json.loads(record.raw_response_payload)
    interpretation = {"execution_eligible": False}
    if fault == "payload":
        payload["results"][6] = "0x1"
    elif fault in ("return", "fee", "disagreement", "closing_nonce"):
        index, value = {
            "return": (5, "0x"),
            "fee": (8, "0x" + f"{authority.loop.c.gas_reserve_wei + 1:064x}"),
            "disagreement": (8, "0x" + f"{1:064x}"),
            "closing_nonce": (10, hex(scope["nonce"] + 1)),
        }[fault]
        raw = json.loads(bytes.fromhex(response["exchanges"][index]["payload"]))
        raw["result"] = value
        response["exchanges"][index]["payload"] = canonical_json(raw).hex()
        payload["results"][index] = value
    elif fault == "request":
        item = request["exchanges"][5]
        raw = json.loads(bytes.fromhex(item["payload"]))
        raw["params"][-1] = "latest"
        item["payload"] = canonical_json(raw).hex()
    elif fault == "missing":
        request["exchanges"].pop()
        response["exchanges"].pop()
    elif fault == "legacy":
        payload["schema"] = "oasis.live-authority-simulation.v1"
    else:
        interpretation["execution_eligible"] = True
    forged = store.put(
        refs[0].kind,
        canonical_json(payload),
        block,
        refs[0].source,
        request=request,
        raw_response=response,
        interpretation=interpretation,
    )
    with pytest.raises(ValueError):
        simulation.replay_simulation_quorum(
            store, (forged, refs[1]), authority.loop.c, block, tx, **scope
        )


def test_failed_retained_replay_cannot_reach_key(proof, monkeypatch):
    _, authority, key, fields, context, _, _, _, _ = proof

    def corrupted(*args, **kwargs):
        raise ValueError("retained proof refused")

    monkeypatch.setattr(simulation, "replay_simulation_quorum", corrupted)
    with pytest.raises(ValueError, match="retained proof refused"):
        authority.sign(fields, context)
    saved = StateStore(authority.loop.store.path).load("live")
    assert key.calls == 0 and saved.pending is None and saved.signing_attempt is None


def test_unknown_signing_result_preserves_full_simulation_provenance(proof):
    store, authority, key, fields, context, _, block, tx, scope = proof
    key.fail = True
    with pytest.raises(RuntimeError, match="signing authority unavailable"):
        authority.sign(fields, context)
    attempt = StateStore(authority.loop.store.path).load("live").signing_attempt
    refs = tuple(evidence_ref_from_json(r) for r in attempt["simulation_evidence"])
    assert tuple(attempt["simulation_hashes"]) == tuple(r.content_hash for r in refs)
    assert [asdict(r) for r in refs] == attempt["simulation_evidence"]
    simulation.replay_simulation_quorum(store, refs, authority.loop.c, block, tx, **scope)
    assert key.calls == 1
