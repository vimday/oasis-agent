"""Raw cash reconstruction rejects self-consistent forged interpretations."""

import json
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.native_payments import persist_native_owner_payment
from oasis_agent.farming.owner_payments import persist_owner_payment
from oasis_agent.farming.owner_quorum import replay_owner_cash_quorum
from oasis_agent.models.common import canonical_json
from scripts.verify_owner_cash import verify
from tests.unit.test_native_payments import native_fixture
from tests.unit.test_owner_payments import fixture


@pytest.mark.parametrize(
    "factory,persist",
    [(fixture, persist_owner_payment), (native_fixture, persist_native_owner_payment)],
)
def test_cash_quorum_replays_both_provider_witnesses_and_rejects_forgery(
    tmp_path, factory, persist
):
    capture = factory()
    other = replace(capture, core=replace(capture.core, source="second"), source="second")
    store = EvidenceStore(tmp_path / "cash.db")
    try:
        refs = tuple(persist(store, c).settlement_evidence for c in (capture, other))
        scope = {
            "header": capture.core.header,
            "pool_manager": capture.core.pool_manager,
            "pool_key": capture.core.pool_key,
            "entry": capture.core.entry,
            "owner": capture.owner,
        }
        result = replay_owner_cash_quorum(store, refs, **scope)
        assert (result.amounts.amount0, result.amounts.amount1) == (-100, -200)
        assert json.loads(result.payload)["execution_eligible"] is False
        request = tmp_path / "request.json"
        request.write_bytes(
            canonical_json(
                {
                    "references": [asdict(ref) for ref in refs],
                    **{
                        key: asdict(value) if key in {"header", "pool_key", "entry"} else value
                        for key, value in scope.items()
                    },
                }
            )
        )
        report = tmp_path / "report.json"
        verify(request, tmp_path / "cash.db", report)
        assert report.read_bytes() == result.payload + b"\n"
        with pytest.raises(ValueError, match="new output"):
            verify(request, tmp_path / "cash.db", report)
        with pytest.raises(ValueError, match="distinct"):
            replay_owner_cash_quorum(store, (refs[0], refs[0]), **scope)
        record = store.get_record(refs[1])
        data = json.loads(record.payload)
        data["amounts"]["amount0"] = 0
        forged = store.put(
            refs[1].kind,
            canonical_json(data),
            refs[1].block,
            refs[1].source,
            request=record.request_payload,
            raw_response=record.raw_response_payload,
            interpretation={"forged": True},
        )
        with pytest.raises(ValueError, match="interpretation"):
            replay_owner_cash_quorum(store, (refs[0], forged), **scope)
    finally:
        store.close()
