"""A real second connection flips the peer to WAL after the unlocked probe."""

import sqlite3
from contextlib import ExitStack, closing
from functools import partial

import pytest

from oasis_agent.live.feed_transition import apply_pair as apply_feed
from tests.unit.test_live_feed_pair_atomic import pair
from tests.unit.test_live_feed_transition import CONFIG, SOURCES
from tests.unit.test_live_runtime_epoch import apply, cli, prepare
from tests.unit.test_live_valuation import synthetic_feed_pin  # noqa: F401


@pytest.mark.parametrize("kind", ["runtime", "feed"])
def test_locked_mode_probe_refuses_peer_flip_before_any_epoch_write(tmp_path, monkeypatch, kind):
    with ExitStack() as resources:
        if kind == "runtime":
            data, report = prepare(tmp_path, monkeypatch)
            stores = {}
            for role, root in zip(cli.ROLES, data[0], strict=True):
                stores[role] = cli.StateStore(cli.paths(root, role)[0], require_existing=True)
            operation = partial(apply, data, report)
        else:
            stores, evidence, fresh = pair(tmp_path, monkeypatch, resources)
            operation = partial(
                apply_feed,
                stores["strategy"],
                stores["authority"],
                CONFIG,
                evidence,
                fresh,
                sources=SOURCES,
            )
        projections = {role: store.path.read_bytes() for role, store in stores.items()}
        before = {}
        for role, store in stores.items():
            with closing(sqlite3.connect(store.journal_path)) as db:
                before[role] = db.execute("SELECT * FROM events ORDER BY seq").fetchall()
        original = sqlite3.connect
        observed = []

        class FlipBeforeBegin(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql == "BEGIN IMMEDIATE":
                    databases = super().execute("PRAGMA database_list").fetchall()
                    peer = next((row[2] for row in databases if row[1] == "peer"), None)
                    if peer is not None and not observed:
                        with closing(original(peer, timeout=1)) as other:
                            observed.append(other.execute("PRAGMA journal_mode=WAL").fetchone()[0])
                return super().execute(sql, parameters)

        monkeypatch.setattr(
            sqlite3, "connect", lambda *a, **k: original(*a, **k, factory=FlipBeforeBegin)
        )
        with pytest.raises(ValueError, match="locked paired DELETE/FULL"):
            operation()
        monkeypatch.setattr(sqlite3, "connect", original)
        assert observed == ["wal"]
        for role, store in stores.items():
            assert store.path.read_bytes() == projections[role]
            with closing(original(store.journal_path)) as db:
                assert db.execute("SELECT * FROM events ORDER BY seq").fetchall() == before[role]
                assert not db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='config_transitions'"
                ).fetchone()
                assert db.execute("PRAGMA journal_mode").fetchone() == (
                    "wal" if role == "authority" else "delete",
                )
