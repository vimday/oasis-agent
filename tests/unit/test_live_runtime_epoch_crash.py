"""SIGKILL at real paired runtime transaction boundaries; no production files."""

import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import asdict

import pytest

from oasis_agent.live.state import StateStore
from oasis_agent.models.common import canonical_json
from tests.unit.test_live_runtime_epoch import apply, cli, events, prepare
from tests.unit.test_live_valuation import synthetic_feed_pin  # noqa: F401


@pytest.mark.parametrize("phase", ["first_staged", "both_staged", "committed"])
def test_process_death_preserves_both_original_chains_and_requires_inspection(
    tmp_path, monkeypatch, phase
):
    data, report = prepare(tmp_path, monkeypatch)
    request, marker = tmp_path / "synthetic-request.json", tmp_path / "paused"
    request.write_bytes(
        canonical_json(
            {
                "target": asdict(data[1]),
                "references": data[2],
                "review": report,
                "gas": str(data[3]),
                "roots": [str(root) for root in data[0]],
                "phase": phase,
            }
        )
    )
    before = {role: events(root, role) for role, root in zip(cli.ROLES, data[0], strict=True)}
    projections = {
        root: cli.paths(root, role)[0].read_bytes()
        for role, root in zip(cli.ROLES, data[0], strict=True)
    }
    code = r"""
import json,sys,time
from pathlib import Path
from eth_hash.auto import keccak
from oasis_agent.live import valuation
from oasis_agent.live.config import LiveConfig
from oasis_agent.live.journal import LiveJournal
from oasis_agent.live.state import StateStore
from oasis_agent.models.common import canonical_json
from scripts.migrate_live_runtime_transition import apply_pair
data=json.loads(Path(sys.argv[1]).read_bytes())
valuation.FEED_RUNTIME='0x'+keccak(bytes.fromhex('6000')).hex()
marker=Path(sys.argv[2])
time.time=lambda:data['review']['reviewed_at']
original=LiveJournal._transition_in_transaction
def pause():
    marker.write_text('synthetic stopped transaction')
    time.sleep(30)
def stage(self,db,record,*,schema='main'):
    result=original(self,db,record,schema=schema)
    if data['phase']==('first_staged' if schema=='main' else 'both_staged'):pause()
    return result
LiveJournal._transition_in_transaction=stage
if data['phase']=='committed':StateStore.save=lambda *_:pause()
apply_pair(*(Path(p) for p in data['roots']),LiveConfig.from_json(canonical_json(data['target'])),
    data['references'],Path(data['gas']),data['review'],data['review']['evidence_hash'])
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(request), str(marker)],
        env={key: os.environ[key] for key in ("PATH", "PYTHONPATH") if key in os.environ},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), process.communicate(timeout=1)
        process.kill()
        process.communicate(timeout=3)
        assert process.returncode == -9
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)
    if phase != "committed":
        originals = {p: p.read_bytes() for root in data[0] for p in root.iterdir() if p.is_file()}
        with pytest.raises(ValueError, match="sidecars"):
            apply(data, report)
        assert {p: p.read_bytes() for p in originals} == originals
        # Explicit operator-style fixture inspection, not a migration retry or
        # automatic sidecar removal. Real operator recovery is still unaccepted.
        main = (
            StateStore(cli.paths(data[0][0], "strategy")[0]).journal_path.resolve().as_uri()
            + "?mode=rw"
        )
        peer = (
            StateStore(cli.paths(data[0][1], "authority")[0]).journal_path.resolve().as_uri()
            + "?mode=rw"
        )
        with closing(sqlite3.connect(main, uri=True, isolation_level=None)) as db:
            db.execute("ATTACH DATABASE ? AS peer", (peer,))
            db.execute("BEGIN")
            db.execute("SELECT COUNT(*) FROM main.events").fetchone()
            db.execute("SELECT COUNT(*) FROM peer.events").fetchone()
            db.execute("COMMIT")
    for role, root in zip(cli.ROLES, data[0], strict=True):
        rows = events(root, role)
        store = StateStore(cli.paths(root, role)[0], require_existing=True)
        assert store.load("live", read_only=True).halted
        if phase == "committed":
            assert rows[:-1] == before[role] and len(rows) == len(before[role]) + 1
            assert store.journal.config_hash == report["target_config_hash"]
        else:
            assert (
                rows == before[role] and store.journal.config_hash != report["target_config_hash"]
            )
        assert cli.paths(root, role)[0].read_bytes() == projections[root]
