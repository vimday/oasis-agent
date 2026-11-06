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
from tests.unit.test_lp_receipts import sessions


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
    assert len(values) == 14 and key.calls == 0


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
        "legacy_v2",
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
    elif fault in ("legacy", "legacy_v2"):
        payload["schema"] = (
            "oasis.live-authority-simulation.v1"
            if fault == "legacy"
            else "oasis.live-authority-simulation.v2"
        )
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


@pytest.mark.parametrize("fault", ["wrong_chain", "parent_hash", "pending_nonce"])
def test_simulation_opening_mismatch_stops_before_simulating(proof, fault):
    store, authority, _, _, _, refs, block, tx, scope = proof
    reader = authority.loop.receipt_costs

    class Poster:
        def __init__(self, ref, peer=False):
            record = store.get_record(ref)
            self.responses = iter(json.loads(record.raw_response_payload)["exchanges"])
            self.methods = []
            self.peer = peer

        def post(self, payload):
            request = json.loads(payload)
            index = len(self.methods)
            self.methods.append(request["method"])
            wire = next(self.responses)
            assert wire["method"] == request["method"]
            response = json.loads(bytes.fromhex(wire["payload"]))
            response["id"] = request["id"]
            if self.peer:
                if fault == "wrong_chain" and index == 0:
                    response["result"] = "0x1"
                elif fault == "parent_hash" and index == 1:
                    response["result"]["parentHash"] = "0x" + "aa" * 32
                elif fault == "pending_nonce" and index == 4:
                    response["result"] = hex(scope["nonce"] + 1)
            return canonical_json(response)

    primary, peer = Poster(refs[0]), Poster(refs[1], peer=True)
    reader.sessions = sessions(dict(zip(reader.sources, (primary, peer), strict=True)))
    with pytest.raises(ValueError):
        simulation.simulate(
            reader,
            authority.loop.c,
            block,
            tx,
            nonce=scope["nonce"],
            gas=scope["gas"],
            maximum=scope["maximum"],
        )
    assert (
        primary.methods
        == peer.methods
        == [
            "eth_chainId",
            "eth_getBlockByNumber",
            *(["eth_getTransactionCount"] * 3),
        ]
    )


def test_single_simulation_replay_rejects_changed_closing_parent(proof):
    store, authority, _, _, _, refs, block, tx, scope = proof
    record = store.get_record(refs[0])
    payload = json.loads(record.payload)
    response = json.loads(record.raw_response_payload)
    closing = response["exchanges"][-1]
    raw = json.loads(bytes.fromhex(closing["payload"]))
    raw["result"]["parentHash"] = "0x" + "aa" * 32
    closing["payload"] = canonical_json(raw).hex()
    payload["results"][-1] = raw["result"]
    ref = store.put(
        refs[0].kind,
        canonical_json(payload),
        block,
        refs[0].source,
        request=json.loads(record.request_payload),
        raw_response=response,
        interpretation={"execution_eligible": False},
    )
    with pytest.raises(ValueError, match="simulation chain, block or nonce changed"):
        simulation.replay_simulation(
            store,
            ref,
            authority.loop.c,
            block,
            tx,
            nonce=scope["nonce"],
            gas=scope["gas"],
            maximum=scope["maximum"],
        )
