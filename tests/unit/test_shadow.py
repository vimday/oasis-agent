from dataclasses import replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import BlockRef
from oasis_agent.shadow.journal import ShadowJournal


def evidence(tmp_path, block):
    return EvidenceStore(tmp_path / "evidence.db").put(
        "shadow_header",
        b"fixture",
        block.ref,
        "fixture",
        request={"block": block.ref.number},
        raw_response={"hash": block.ref.block_hash},
        interpretation={"fixture": True},
    )


def test_halt_latches_across_restart(tmp_path):
    b = BlockSnapshot(BlockRef(1, 10, "0x" + "11" * 32, 100), "0x" + "00" * 32)
    path = tmp_path / "shadow.db"
    journal = ShadowJournal(path, chain_id=1, max_age_seconds=10)
    assert journal.observe(b, evidence(tmp_path, b), now=100)["decision"] == "HOLD"
    assert journal.observe(b, evidence(tmp_path, b), now=111)["decision"] == "HALT"
    journal.close()
    journal = ShadowJournal(path, chain_id=1, max_age_seconds=10)
    c = BlockSnapshot(BlockRef(1, 11, "0x" + "22" * 32, 112), b.ref.block_hash)
    result = journal.observe(c, evidence(tmp_path, c), now=112)
    assert result["decision"] == "HALT"
    assert "halt_latched" in result["reasons"]
    assert result["execution_eligible"] is False
    journal.close()


@pytest.mark.parametrize("kind", ["reorg", "gap", "chain", "future"])
def test_invalid_observation_halts(tmp_path, kind):
    b = BlockSnapshot(BlockRef(1, 10, "0x" + "11" * 32, 100), "0x" + "00" * 32)
    j = ShadowJournal(tmp_path / "shadow.db", chain_id=1, max_age_seconds=10)
    j.observe(b, evidence(tmp_path, b), now=100)
    changes = {
        "reorg": {"block_hash": "0x" + "22" * 32},
        "gap": {"number": 12},
        "chain": {"chain_id": 2},
        "future": {"timestamp": 102},
    }
    c = replace(b, ref=replace(b.ref, **changes[kind]))
    assert j.observe(c, evidence(tmp_path, c), now=101)["decision"] == "HALT"
    j.close()


def test_policy_change_and_tampering_fail(tmp_path):
    path = tmp_path / "shadow.db"
    j = ShadowJournal(path, chain_id=1, max_age_seconds=10)
    b = BlockSnapshot(BlockRef(1, 10, "0x" + "11" * 32, 100), "0x" + "00" * 32)
    j.observe(b, evidence(tmp_path, b), now=100)
    j.db.execute("UPDATE observations SET digest='broken'")
    j.db.commit()
    with pytest.raises(ValueError, match="corrupted"):
        j.observe(b, evidence(tmp_path, b), now=101)
    with pytest.raises(ValueError, match="policy"):
        ShadowJournal(path, chain_id=2, max_age_seconds=10)
    j.close()


def test_acquisition_failure_before_first_header_latches(tmp_path):
    path = tmp_path / "shadow.db"
    j = ShadowJournal(path, chain_id=1, max_age_seconds=10)
    assert j.fault(now=100)["decision"] == "HALT"
    j.close()
    j = ShadowJournal(path, chain_id=1, max_age_seconds=10)
    b = BlockSnapshot(BlockRef(1, 10, "0x" + "11" * 32, 100), "0x" + "00" * 32)
    assert j.observe(b, evidence(tmp_path, b), now=101)["decision"] == "HALT"
    j.close()


def test_halt_reset_is_operator_journal_rotation_only(tmp_path):
    b = BlockSnapshot(BlockRef(1, 10, "0x" + "11" * 32, 100), "0x" + "00" * 32)
    halted = ShadowJournal(tmp_path / "halted.db", chain_id=1, max_age_seconds=10)
    assert halted.observe(b, evidence(tmp_path, b), now=111)["decision"] == "HALT"
    halted.close()

    # The same file stays latched: there is no in-place reset path.
    reopened = ShadowJournal(tmp_path / "halted.db", chain_id=1, max_age_seconds=10)
    c = BlockSnapshot(BlockRef(1, 11, "0x" + "22" * 32, 112), b.ref.block_hash)
    result = reopened.observe(c, evidence(tmp_path, c), now=112)
    assert result["decision"] == "HALT"
    assert "halt_latched" in result["reasons"]
    reopened.close()

    # The documented recovery is operator rotation to a fresh journal file;
    # a fresh empty chain carries no latch by construction.
    fresh = ShadowJournal(tmp_path / "fresh.db", chain_id=1, max_age_seconds=10)
    assert fresh.observe(c, evidence(tmp_path, c), now=112)["decision"] == "HOLD"
    fresh.close()
