"""Raw holder proof cannot promote imported safety or beneficial-owner assertions."""

from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.launch.holder_capture import HolderQuorum, persist_holder_quorum
from oasis_agent.launch.holder_scoring import HolderIdentity, score_with_holders
from oasis_agent.launch.scoring import Candidate, ScorePolicy
from oasis_agent.models.common import canonical_json
from scripts.score_holder_candidate import run

from .test_holder_capture import Poster, collect
from .test_token_identity import BLOCK_HASH, CHAIN_ID, CODE_HASH, TOKEN
from .test_token_scoring import candidate


@pytest.fixture
def retained(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")

    def save(supply=100):
        quorum = collect(Poster(supply=supply), Poster(supply=supply))
        captures = tuple(
            replace(c, observation=c.observation.model_copy(update={"retrieved_at": 1001}))
            for c in (quorum.left, quorum.right)
        )
        return persist_holder_quorum(store, HolderQuorum(*captures))

    yield store, save
    store.close()


def inputs():
    data = candidate().model_dump(mode="json")
    data.update(chain_id=CHAIN_ID, token=TOKEN, evaluated_at=1002)
    data["chain"].update(
        chain_id=CHAIN_ID, token=TOKEN, block_number=42, block_hash=BLOCK_HASH, observed_at=1000
    )
    for trend in data["trends"]:
        trend.update(chain_id=CHAIN_ID, token=TOKEN)
    identity = HolderIdentity(
        chain_id=CHAIN_ID, token=TOKEN, runtime_hash=CODE_HASH, token_decimals=6
    )
    return Candidate.model_validate(data), identity


def test_raw_balances_override_imported_metrics_without_common_control_claim(retained):
    store, save = retained
    c, identity = inputs()
    refs = save()
    r = score_with_holders(c, ScorePolicy(), store=store, references=refs, identity=identity)
    assert c.chain.holder_count == 1000
    assert r.candidate.chain.holder_count == 2
    assert r.candidate.chain.holder_top10_bps == 10000
    assert r.candidate.chain.holder_coverage == "pass"
    assert r.candidate.chain.evidence_sha256 == c.chain.evidence_sha256
    assert "common_control_analysis_unknown" in r.score.blockers
    assert r.score.action == "reject" and not r.execution_eligible
    reversed_result = score_with_holders(
        c, ScorePolicy(), store=store, references=refs[::-1], identity=identity
    )
    assert reversed_result == r


def test_incomplete_supply_cannot_keep_imported_pass(retained):
    store, save = retained
    c, identity = inputs()
    r = score_with_holders(c, ScorePolicy(), store=store, references=save(101), identity=identity)
    assert r.candidate.chain.holder_coverage == "fail"
    assert "holder_coverage_fail" in r.score.blockers


@pytest.mark.parametrize(
    "field,value",
    [("block_number", 43), ("block_hash", "0x" + "bb" * 32), ("observed_at", 1001)],
)
def test_mixed_snapshot_rejected(retained, field, value):
    store, save = retained
    c, identity = inputs()
    data = c.model_dump(mode="json")
    data["chain"][field] = value
    with pytest.raises(ValueError, match="snapshot"):
        score_with_holders(
            Candidate.model_validate(data),
            ScorePolicy(),
            store=store,
            references=save(),
            identity=identity,
        )


@pytest.mark.parametrize("evaluated_at", [1000, 1031])
def test_future_capture_or_stale_proof_rejected(retained, evaluated_at):
    store, save = retained
    c, identity = inputs()
    with pytest.raises(ValueError, match="stale or acquired"):
        score_with_holders(
            Candidate.model_validate(c.model_dump(mode="json") | {"evaluated_at": evaluated_at}),
            ScorePolicy(),
            store=store,
            references=save(),
            identity=identity,
        )


@pytest.mark.parametrize(
    "field,value", [("runtime_hash", "0x" + "cc" * 32), ("token_decimals", 18)]
)
def test_identity_pin_mismatch_rejected(retained, field, value):
    store, save = retained
    c, identity = inputs()
    with pytest.raises(ValueError, match="identity"):
        score_with_holders(
            c,
            ScorePolicy(),
            store=store,
            references=save(),
            identity=HolderIdentity.model_validate(identity.model_dump() | {field: value}),
        )


def test_duplicate_provider_and_tampered_reference_rejected(retained):
    store, save = retained
    c, identity = inputs()
    refs = save()
    for invalid in ((refs[0], refs[0]), (replace(refs[0], raw_response_hash="f" * 64), refs[1])):
        with pytest.raises(ValueError):
            score_with_holders(c, ScorePolicy(), store=store, references=invalid, identity=identity)


def test_cli_replays_and_refuses_overwrite(retained, tmp_path):
    _, save = retained
    c, identity = inputs()
    refs = save()
    request = tmp_path / "request.json"
    output = tmp_path / "score.json"
    request.write_bytes(
        canonical_json(
            {
                "candidate": c.model_dump(mode="json"),
                "policy": ScorePolicy().model_dump(mode="json"),
                "identity": identity.model_dump(mode="json"),
                "references": [asdict(r) for r in refs],
            }
        )
    )
    run(request, tmp_path / "evidence.db", output)
    original = output.read_bytes()
    assert b'"execution_eligible":false' in original
    with pytest.raises(ValueError):
        run(request, tmp_path / "evidence.db", output)
    assert output.read_bytes() == original
