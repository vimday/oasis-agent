"""Fast append preserves full verification after any SQL writer changes data."""

import sqlite3
from unittest.mock import Mock

import pytest

from oasis_agent.sniping.journal import ExecutionJournal


def test_unchanged_connection_does_not_rescan_history_on_each_append(tmp_path):
    with ExecutionJournal(tmp_path / "j.db", {"test": True}) as journal:
        verify = Mock(wraps=journal._verify)
        journal._verify = verify
        for i in range(50):
            journal.append("event", {"index": i})
        assert verify.call_count == 1
        assert len(journal.read()) == 50
        assert verify.call_count == 2


@pytest.mark.parametrize("external", [False, True])
def test_old_record_tamper_invalidates_head_before_append(tmp_path, external):
    path = tmp_path / "j.db"
    with ExecutionJournal(path, {"test": True}) as journal:
        journal.append("event", {})
        journal.append("event", {})
        db = sqlite3.connect(path) if external else journal.db
        try:
            db.execute("UPDATE execution_events SET digest='tampered' WHERE seq=1")
            db.commit()
            with pytest.raises(ValueError, match="corrupted"):
                journal.append("event", {})
            assert journal.db.execute("SELECT count(*) FROM execution_events").fetchone()[0] == 2
        finally:
            if external:
                db.close()


def test_alternating_writers_revalidate_each_others_commits(tmp_path):
    path = tmp_path / "j.db"
    with (
        ExecutionJournal(path, {"test": True}) as first,
        ExecutionJournal(path, {"test": True}) as second,
    ):
        a = first.append("event", {})
        b = second.append("event", {})
        c = first.append("event", {})
        d = second.append("event", {})
        assert [r["seq"] for r in [a, b, c, d]] == [1, 2, 3, 4]
        assert b["previous"] == a["digest"] and c["previous"] == b["digest"]
        assert d["previous"] == c["digest"]
        assert len(first.read()) == 4


def test_changed_policy_blocks_cached_append(tmp_path):
    with ExecutionJournal(tmp_path / "j.db", {"test": True}) as journal:
        journal.append("event", {})
        journal.db.execute("UPDATE execution_policy SET payload=?", (b"{}",))
        journal.db.commit()
        with pytest.raises(ValueError, match="policy changed"):
            journal.append("event", {})


def test_trigger_cannot_hide_historical_rewrite_as_own_append(tmp_path):
    path = tmp_path / "j.db"
    with ExecutionJournal(path, {"test": True}) as journal:
        journal.append("event", {})
        journal.db.execute(
            "CREATE TRIGGER rewrite_old AFTER INSERT ON execution_events "
            "BEGIN UPDATE execution_events SET digest='bad' WHERE seq=1; END"
        )
        journal.db.commit()
        with pytest.raises(ValueError, match="additional journal writes"):
            journal.append("event", {})
        assert len(journal.read()) == 1
