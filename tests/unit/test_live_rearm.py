"""Synthetic retained-wire rearm: no production data, network, keys or execution."""

import fcntl
import json
import os
import sqlite3
import sys
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live import rearm
from oasis_agent.live.chain import ChainError
from oasis_agent.live.feed_epoch import HALT
from oasis_agent.live.journal import LiveJournal
from oasis_agent.live.rearm_settlements import (
    SettlementHeadCapture,
    persist_settlement_head,
    settlement_requests,
)
from oasis_agent.live.startup_state import SOURCES, paths
from oasis_agent.live.state import Pending, StateStore
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.transport import JsonRpcTransport, RpcExchange
from scripts import rearm_live_services as cli
from tests.unit.test_live_precheck_evidence import capture
from tests.unit.test_live_startup_state import prepare, snapshot
from tests.unit.test_live_valuation import NavFixture, synthetic_feed_pin  # noqa: F401


def wire_capture(head, anchors=(), receipts=(), source=SOURCES[0], *, fault=None):
    header = {
        "number": hex(head.number),
        "hash": head.block_hash,
        "timestamp": hex(head.timestamp),
        "parentHash": "0x" + "77" * 32,
    }
    results = [hex(head.chain_id), header]
    for anchor, receipt in zip(anchors, receipts, strict=True):
        results.extend(
            [
                {**header, "number": hex(anchor["number"]), "hash": anchor["hash"]},
                receipt,
            ]
        )
    results.append(deepcopy(header))
    if fault == "closing_head":
        results[-1]["hash"] = "0x" + "55" * 32
    if fault == "chain":
        results[0] = "0x1"
    exchanges = tuple(
        RpcExchange(
            method,
            i,
            canonical_json({"jsonrpc": "2.0", "id": i, "method": method, "params": params}),
            canonical_json({"jsonrpc": "2.0", "id": i, "result": value}),
            1,
        )
        for i, ((method, params), value) in enumerate(
            zip(settlement_requests(head, anchors), results, strict=True), 1
        )
    )
    return SettlementHeadCapture(head, anchors, source, exchanges)


def pair(tmp_path, monkeypatch):
    directories, stores, refs = {}, {}, {}
    for role in rearm.ROLES:
        directory = tmp_path / role
        config, profile, store = prepare(directory, role)
        state = store.load("live")
        state.halted = HALT
        state.journal_required = True
        state.exact_risk.basis += 7_000_000
        state.exact_risk.external += 2_000_000
        state.exact_risk.daily_locked += 3_000_000
        state.consecutive_reverts = 1
        store.save(state)
        (directory / "STOP").touch()
        fixture = NavFixture(
            position_enabled=False,
            native_after=10**17,
            token_after=100_000_000,
            usdc_price=100_000_000,
            timestamp=state.exact_risk.last_at + 20,
            block_number=120,
            block_hash="0x" + "44" * 32,
        )
        with closing(EvidenceStore(paths(directory, role)[1])) as evidence:
            _, nav = capture(evidence, config, fixture)
            settlements = tuple(
                persist_settlement_head(evidence, wire_capture(nav[0].block, source=source))
                for source in SOURCES
            )
        directories[role], stores[role] = directory, store
        refs[role] = {"nav": nav, "settlements": settlements}
    monkeypatch.setattr(rearm, "_clock", lambda: fixture.timestamp)
    return directories, stores, config, profile, refs


def review(data):
    directories, _, config, profile, refs = data
    return rearm.review_pair(
        directories["strategy"], directories["authority"], config, profile, refs
    )


def apply(data, digest):
    directories, _, config, profile, refs = data
    return rearm.apply_pair(
        directories["strategy"],
        directories["authority"],
        config,
        profile,
        refs,
        approved_review_hash=digest,
    )


def rows(store):
    with closing(sqlite3.connect(store.journal_path)) as db:
        return db.execute("SELECT * FROM events ORDER BY seq").fetchall()


def test_paired_append_preserves_capital_losses_history_stop_budget_and_old_writer_cas(
    tmp_path, monkeypatch
):
    data = pair(tmp_path, monkeypatch)
    directories, stores, _, _, refs = data
    prior = {role: store.load("live", read_only=True) for role, store in stores.items()}
    before = {role: rows(store) for role, store in stores.items()}
    files = {role: snapshot(directory) for role, directory in directories.items()}
    old_writer = LiveJournal(
        stores["authority"].journal_path, rearm._config(data[2]), require_existing=True
    )
    old_writer.load(read_only=True)
    monkeypatch.setattr(
        JsonRpcTransport, "request", lambda *_: pytest.fail("offline rearm made RPC")
    )
    report = review(data)
    assert {role: snapshot(directory) for role, directory in directories.items()} == files
    result = apply(data, report["review_hash"])
    assert (
        result["rearm_applied"]
        and result["projection_complete"]
        and not result["execution_eligible"]
    )
    for role, store in stores.items():
        state = store.load("live", read_only=True)
        assert state.halted is None and (directories[role] / "STOP").is_file()
        assert rows(store)[:-1] == before[role]
        assert state.exact_risk.basis == prior[role].exact_risk.basis
        assert state.exact_risk.external == prior[role].exact_risk.external
        assert state.exact_risk.daily_locked == prior[role].exact_risk.daily_locked
        assert state.consecutive_reverts == 1
        assert state.nav_evidence == refs[role]["nav"]
        assert state.history[-1]["review"]["review_hash"] == report["review_hash"]
        assert (directories[role] / "live.receipt-budget.db").read_bytes() == files[role][
            "live.receipt-budget.db"
        ]
    with pytest.raises(ValueError):
        old_writer.append(asdict(prior["authority"]))
    with pytest.raises(ValueError):
        apply(data, report["review_hash"])


@pytest.mark.parametrize(
    "fault",
    [
        "no_stop",
        "symlink_stop",
        "missing_journal",
        "empty_journal",
        "wal",
        "hardlink",
        "active",
        "pending",
        "signing",
        "arbitrary_halt",
        "reverts",
        "loss",
        "legacy",
        "flow",
        "no_basis",
        "missing_anchor",
        "old_anchor",
        "expired",
        "source",
        "tip",
        "limits",
        "bad_nav",
        "gas",
        "budget",
        "projection",
        "risk_day",
        "risk_checkpoint",
    ],
)
def test_rearm_refuses_without_modifying_originals(tmp_path, monkeypatch, fault):
    data = pair(tmp_path, monkeypatch)
    directories, stores, config, profile, refs = data
    digest = review(data)["review_hash"]
    directory, store = directories["authority"], stores["authority"]
    state = store.load("live")
    lock = None
    if fault == "no_stop":
        (directory / "STOP").unlink()
    elif fault == "symlink_stop":
        (directory / "STOP").unlink()
        (directory / "STOP").symlink_to(directory / "missing")
    elif fault == "missing_journal":
        store.journal_path.unlink()
    elif fault == "empty_journal":
        store.journal_path.write_bytes(b"")
    elif fault == "wal":
        with sqlite3.connect(store.journal_path) as db:
            db.execute("PRAGMA journal_mode=WAL")
    elif fault == "hardlink":
        store.journal_path.unlink()
        os.link(stores["strategy"].journal_path, store.journal_path)
    elif fault == "active":
        lock = (directory / "authority.lock").open("rb")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif fault == "missing_anchor":
        refs["authority"]["settlements"] = ()
    elif fault == "old_anchor":
        refs["authority"]["settlements"] = state.nav_evidence
    elif fault == "expired":
        monkeypatch.setattr(
            rearm, "_clock", lambda: refs["authority"]["nav"][0].block.timestamp + 121
        )
    elif fault == "source":
        refs["authority"]["nav"] = tuple(reversed(refs["authority"]["nav"]))
    elif fault == "limits":
        data = (
            directories,
            stores,
            replace(config, usdc_oracle_max_age_seconds=90_000),
            profile,
            refs,
        )
    elif fault == "budget":
        (directory / "live.receipt-budget.db").unlink()
    elif fault == "projection":
        store.path.write_text("{}")
    else:
        if fault == "pending":
            state.pending = Pending("swap", "0x" + "99" * 32, 4, 100)
        elif fault == "signing":
            state.signing_attempt = {"nonce": 4}
        elif fault == "arbitrary_halt":
            state.halted = "settled transaction block reorged or unavailable"
        elif fault == "reverts":
            state.consecutive_reverts = 2
        elif fault == "loss":
            state.exact_risk.daily_locked = 200_000_000
        elif fault == "legacy":
            state.legacy_gas_cost_incomplete = True
        elif fault == "flow":
            state.acknowledge_flow = True
        elif fault == "no_basis":
            state.exact_risk = None
        elif fault == "gas":
            state.gas_spent_wei += 1
        elif fault == "risk_day":
            state.exact_risk.day = "2020-01-01"
        elif fault == "risk_checkpoint":
            state.exact_risk.checkpoint_at = 0
        elif fault == "bad_nav":
            state.last_units[0] += 1
        else:
            state.actions_today += 1
        store.save(state)
    before = {role: snapshot(directory) for role, directory in directories.items()}
    try:
        with pytest.raises((ValueError, OSError, sqlite3.Error, ChainError)):
            apply(data, digest)
    finally:
        if lock:
            lock.close()
    assert {role: snapshot(directory) for role, directory in directories.items()} == before


@pytest.mark.parametrize(
    "fault", ["second_append", "commit", "expiry", "stop_replaced", "stop_modified"]
)
def test_failure_after_first_append_rolls_back_both_halts(tmp_path, monkeypatch, fault):
    data = pair(tmp_path, monkeypatch)
    digest = review(data)["review_hash"]
    directories, stores, _, _, refs = data
    before = {role: rows(store) for role, store in stores.items()}
    original = rearm._append

    def append(db, schema, binding, parent, state):
        result = original(db, schema, binding, parent, state)
        if schema == "peer":
            if fault == "second_append":
                raise OSError("injected failure")
            if fault == "expiry":
                monkeypatch.setattr(
                    rearm, "_clock", lambda: refs["authority"]["nav"][0].block.timestamp + 121
                )
            if fault == "stop_replaced":
                stop = directories["authority"] / "STOP"
                replacement = stop.with_name("replacement")
                replacement.touch()
                replacement.replace(stop)
            if fault == "stop_modified":
                (directories["authority"] / "STOP").write_text("changed in place")
        return result

    monkeypatch.setattr(rearm, "_append", append)
    if fault == "commit":
        original_connect = sqlite3.connect

        class FailingCommit(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if (
                    sql == "COMMIT"
                    and self.execute("PRAGMA database_list").fetchall()[-1][1] == "peer"
                ):
                    raise sqlite3.OperationalError("injected commit failure")
                return super().execute(sql, parameters)

        monkeypatch.setattr(
            sqlite3,
            "connect",
            lambda *args, **kwargs: original_connect(*args, **kwargs, factory=FailingCommit),
        )
    with pytest.raises((ValueError, OSError, sqlite3.Error)):
        apply(data, digest)
    assert {role: rows(store) for role, store in stores.items()} == before
    assert all(store.load("live", read_only=True).halted == HALT for store in stores.values())


def test_committed_pair_survives_projection_failure_without_retry(tmp_path, monkeypatch):
    data = pair(tmp_path, monkeypatch)
    digest = review(data)["review_hash"]
    before = {role: store.path.read_bytes() for role, store in data[1].items()}
    monkeypatch.setattr(
        StateStore, "save", lambda *_: (_ for _ in ()).throw(OSError("projection crash"))
    )
    result = apply(data, digest)
    assert result["rearm_applied"] and not result["projection_complete"]
    for role, store in data[1].items():
        assert store.path.read_bytes() == before[role]
        assert store.load("live", read_only=True).halted is None
    with pytest.raises(ValueError):
        apply(data, digest)


@pytest.mark.parametrize("projection_failure", [False, True])
def test_cli_review_and_explicit_apply_preserve_attempt_marker(
    tmp_path, monkeypatch, capsys, projection_failure
):
    data = pair(tmp_path, monkeypatch)
    directories, _, config, profile, refs = data
    reference = tmp_path / "references.json"
    reference.write_bytes(
        canonical_json(
            {
                role: {kind: [asdict(r) for r in pair] for kind, pair in values.items()}
                for role, values in refs.items()
            }
        )
    )
    config_path, profile_path = tmp_path / "config.json", tmp_path / "profile.json"
    config_path.write_bytes(canonical_json(asdict(config)))
    profile_path.write_bytes(canonical_json(asdict(profile)))
    argv = [
        "rearm",
        "--strategy-dir",
        str(directories["strategy"]),
        "--authority-dir",
        str(directories["authority"]),
        "--references",
        str(reference),
        "--config",
        str(config_path),
        "--rpc-profile",
        str(profile_path),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    result = json.loads(capsys.readouterr().out)
    output = tmp_path / "attempt"
    monkeypatch.setattr(
        sys,
        "argv",
        [*argv, "--apply", "--approved-review-hash", result["review_hash"], "--out", str(output)],
    )
    if projection_failure:
        monkeypatch.setattr(
            StateStore, "save", lambda *_: (_ for _ in ()).throw(OSError("projection failed"))
        )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == (3 if projection_failure else 0)
    assert (
        json.loads((output / "report.json").read_text())["projection_complete"]
        is not projection_failure
    )
    assert json.loads((output / "attempt.json").read_text())["rearm_applied"] is None
    assert json.loads((output / "report.json").read_text())["rearm_applied"]


@pytest.mark.parametrize("fault", ["closing_head", "chain"])
def test_raw_settlement_window_refuses_head_or_chain_disagreement(tmp_path, monkeypatch, fault):
    data = pair(tmp_path, monkeypatch)
    head = data[4]["strategy"]["nav"][0].block
    with pytest.raises(ValueError):
        wire_capture(head, fault=fault).reconstruct()


def test_journal_schema_is_not_operator_sql(tmp_path, monkeypatch):
    data = pair(tmp_path, monkeypatch)
    journal = data[1]["strategy"].journal
    with closing(journal._connect(read_only=True)) as db, pytest.raises(ValueError):
        journal._read(db, schema="main; DROP TABLE events")


@pytest.mark.parametrize("projection_day", [None, "2020-01-01"])
def test_authority_stale_display_day_never_resets_same_risk_day_actions(
    tmp_path, monkeypatch, projection_day
):
    data = pair(tmp_path, monkeypatch)
    authority = data[1]["authority"]
    state = authority.load("live")
    state.day = projection_day
    state.actions_today = 9
    authority.save(state)
    digest = review(data)["review_hash"]
    apply(data, digest)
    assert authority.load("live", read_only=True).actions_today == 9


def test_fresh_nav_cannot_hide_unexplained_nonce_with_unchanged_wallet(tmp_path, monkeypatch):
    data = pair(tmp_path, monkeypatch)
    directories, _, config, _, refs = data
    digest = review(data)["review_hash"]
    head = refs["authority"]["nav"][0].block
    fixture = NavFixture(
        position_enabled=False,
        native_after=10**17,
        token_after=100_000_000,
        usdc_price=100_000_000,
        timestamp=head.timestamp,
        block_number=head.number,
        block_hash=head.block_hash,
        nonce_after=5,
    )
    with closing(EvidenceStore(paths(directories["authority"], "authority")[1])) as evidence:
        _, refs["authority"]["nav"] = capture(evidence, config, fixture)
    with pytest.raises(ValueError, match="reconcile"):
        apply(data, digest)
