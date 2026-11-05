"""Real disposable journals/ATTACH commits with synthetic raw proof only."""

import json
import sqlite3
import sys
import time
from contextlib import ExitStack, closing
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live import feed_transition
from oasis_agent.live.gas_oracle import LEGACY_PIN_NAMES
from oasis_agent.live.gas_oracle_evidence import retain_discovery_quorum
from oasis_agent.live.journal import LiveJournal
from oasis_agent.live.runtime_epoch import SCHEMA, transition_snapshot
from oasis_agent.live.state import StateStore
from oasis_agent.live.valuation import capture_nav
from oasis_agent.models.common import BlockRef, canonical_json, sha256_hex
from oasis_agent.rpc.budget import RpcBudget
from oasis_agent.rpc.transport import JsonRpcTransport
from scripts import migrate_live_runtime_transition as cli
from scripts.review_live_runtime_transition import review
from tests.unit import test_live_gas_oracle_evidence as gas_fixture
from tests.unit import test_live_runtime_transition_review as fixtures
from tests.unit.test_live_valuation import synthetic_feed_pin  # noqa: F401
from tests.unit.test_lp_receipts import sessions


def prepare(tmp_path, monkeypatch, *, legacy=False):
    data = fixtures.setup(tmp_path, monkeypatch, legacy=legacy)
    roots, target, refs, gas, fixture, _ = data
    monkeypatch.setattr(time, "time", lambda: fixture.timestamp)
    report = review(*roots, target, refs, gas, now=fixture.timestamp)
    return data, report


def apply(data, report):
    roots, target, refs, gas, *_ = data
    return cli.apply_pair(*roots, target, refs, gas, report, report["evidence_hash"])


def events(root, role):
    path = cli.paths(root, role)[0]
    with closing(sqlite3.connect(StateStore(path).journal_path)) as db:
        return db.execute("SELECT * FROM events ORDER BY seq").fetchall()


def epoch(data, report, role="strategy"):
    index = cli.ROLES.index(role)
    store = StateStore(cli.paths(data[0][index], role)[0], require_existing=True)
    prior = asdict(store.load("live", read_only=True))
    return (
        store,
        prior,
        {
            "schema": SCHEMA,
            "source_tip": store.journal.revision,
            "source_state_hash": sha256_hex(canonical_json(prior)),
            "target_config": asdict(data[1]),
            "service": role,
            "references": deepcopy(data[2]),
            "review": deepcopy(report),
            "approved_review_hash": report["evidence_hash"],
        },
    )


def seal(record):
    report = record["review"]
    report["references_hash"] = sha256_hex(canonical_json(record["references"]))
    report["evidence_hash"] = sha256_hex(
        canonical_json({key: value for key, value in report.items() if key != "evidence_hash"})
    )
    record["approved_review_hash"] = report["evidence_hash"]


@pytest.mark.parametrize("legacy", [False, True])
def test_paired_epoch_keeps_history_halt_and_budget_without_acquisition(
    tmp_path, monkeypatch, legacy
):
    data, report = prepare(tmp_path, monkeypatch, legacy=legacy)
    roots, target, *_ = data
    before = {role: events(root, role) for role, root in zip(cli.ROLES, roots, strict=True)}
    budgets = {
        root: (
            (root / "live.receipt-budget.db").stat().st_ino,
            (root / "live.receipt-budget.db").read_bytes(),
        )
        for root in roots
    }
    states = {
        role: StateStore(cli.paths(root, role)[0]).load("live", read_only=True)
        for role, root in zip(cli.ROLES, roots, strict=True)
    }

    def forbidden(*_, **__):
        pytest.fail("migration crossed budget, RPC or evidence-write boundary")

    monkeypatch.setattr(RpcBudget, "__init__", forbidden)
    monkeypatch.setattr(JsonRpcTransport, "request", forbidden)
    monkeypatch.setattr(EvidenceStore, "put", forbidden)
    result = apply(data, report)
    assert result["atomic_pair"] and result["migration_applied"] and result["projection_complete"]
    assert result["execution_eligible"] is False and result["runtime_approved"] is False
    target_hash = sha256_hex(canonical_json(asdict(target)))
    for role, root in zip(cli.ROLES, roots, strict=True):
        rows = events(root, role)
        assert rows[:-1] == before[role] and len(rows) == len(before[role]) + 1
        store = StateStore(cli.paths(root, role)[0], require_existing=True)
        state = store.load("live", read_only=True)
        assert store.journal.config_hash == target_hash
        for key in (
            "history",
            "position",
            "last_units",
            "gas_spent_wei",
            "settled_blocks",
            "halted",
            "acknowledge_flow",
            "legacy_gas_cost_incomplete",
        ):
            assert getattr(state, key) == getattr(states[role], key)
        assert state.exact_risk.basis == states[role].exact_risk.basis
        assert state.exact_risk.external == states[role].exact_risk.external
        assert state.exact_risk.daily_locked >= states[role].exact_risk.daily_locked
        assert [asdict(ref) for ref in state.nav_evidence] == data[2]["nav"][role]
        assert state.checkpoint_at == report["block"]["timestamp"]
        assert (root / "STOP").is_file()
        assert budgets[root] == (
            (root / "live.receipt-budget.db").stat().st_ino,
            (root / "live.receipt-budget.db").read_bytes(),
        )
        store.bind(target_hash, require_existing=True)
    after = fixtures.snapshot(tmp_path)
    with pytest.raises(ValueError):
        apply(data, report)
    assert fixtures.snapshot(tmp_path) == after


@pytest.mark.parametrize(
    "fault",
    [
        "approval",
        "report_hash",
        "source_tip",
        "source_hash",
        "unhalted",
        "pending",
        "basis",
        "external",
        "peer_nav",
        "foreign_pin",
        "gas_pin",
        "missing_pin",
        "expired_bound",
        "source_alias",
        "foreign_block",
        "runtime_approved",
        "rpc_bool",
    ],
)
def test_pure_epoch_refuses_altered_authority_or_accounting(tmp_path, monkeypatch, fault):
    data, report = prepare(tmp_path, monkeypatch)
    store, prior, record = epoch(data, report)
    if fault == "approval":
        record["approved_review_hash"] = "a" * 64
    elif fault == "report_hash":
        record["review"]["evidence_hash"] = "b" * 64
    elif fault == "source_tip":
        record["source_tip"] = "c" * 64
    elif fault == "source_hash":
        record["source_state_hash"] = "d" * 64
    elif fault == "unhalted":
        prior["halted"] = None
        record["source_state_hash"] = sha256_hex(canonical_json(prior))
    elif fault == "pending":
        prior["pending"] = {"nonce": 2}
        record["source_state_hash"] = sha256_hex(canonical_json(prior))
    elif fault in {"basis", "external"}:
        record["review"]["services"]["strategy"]["risk_preview"][fault] += 1
        record["review"]["services"]["authority"]["risk_preview"][fault] += 1
        seal(record)
    elif fault == "peer_nav":
        record["review"]["services"]["authority"]["nav_usd_micros"] += 1
        seal(record)
    elif fault == "foreign_pin":
        record["target_config"]["expected_pins"]["state_view"] = "0x" + "88" * 32
    elif fault == "gas_pin":
        record["target_config"]["expected_pins"]["gas_oracle_proxy"] = "0x" + "77" * 32
    elif fault == "missing_pin":
        del record["target_config"]["expected_pins"]["gas_oracle_implementation"]
    elif fault == "expired_bound":
        record["review"]["valid_until"] = report["block"]["timestamp"] + 121
        seal(record)
    elif fault == "source_alias":
        record["references"]["gas"][0]["source"] = "foreign"
        seal(record)
    elif fault == "foreign_block":
        record["references"]["nav"]["authority"][0]["block"]["number"] += 1
        seal(record)
    elif fault == "rpc_bool":
        record["review"]["rpc_requests"] = False
        seal(record)
    else:
        record["review"]["runtime_approved"] = True
        seal(record)
    with pytest.raises(ValueError):
        transition_snapshot(
            prior, record, binding=store.journal.config_hash, parent=store.journal.revision
        )


@pytest.mark.parametrize("fault", ["peer", "commit", "expired", "proof_change", "record_change"])
def test_any_second_stage_or_commit_failure_rolls_back_both(tmp_path, monkeypatch, fault):
    data, report = prepare(tmp_path, monkeypatch)
    roots, target, *_ = data
    target_hash = sha256_hex(canonical_json(asdict(target)))
    if fault == "peer":
        with closing(
            sqlite3.connect(StateStore(cli.paths(roots[1], "authority")[0]).journal_path)
        ) as db:
            db.execute(
                "CREATE TRIGGER refuse_epoch BEFORE INSERT ON events WHEN NEW.config='"
                + target_hash
                + "' BEGIN SELECT RAISE(ABORT,'synthetic peer refusal'); END"
            )
            db.commit()
    elif fault == "commit":
        original_connect = sqlite3.connect

        class RefuseCommit(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if (
                    sql == "COMMIT"
                    and self.execute("PRAGMA database_list").fetchall()[-1][1] == "peer"
                ):
                    raise sqlite3.OperationalError("synthetic commit refusal")
                return super().execute(sql, parameters)

        monkeypatch.setattr(
            sqlite3, "connect", lambda *a, **k: original_connect(*a, **k, factory=RefuseCommit)
        )
    else:
        original_stage = LiveJournal._transition_in_transaction

        def stage(self, db, record, *, schema="main"):
            result = original_stage(self, db, record, schema=schema)
            if schema == "peer":
                if fault == "expired":
                    monkeypatch.setattr(time, "time", lambda: report["valid_until"] + 1)
                elif fault == "proof_change":
                    with closing(sqlite3.connect(data[3])) as changed:
                        changed.execute("CREATE TABLE synthetic_change(x)")
                        changed.commit()
                else:
                    record["source_tip"] = "a" * 64
            return result

        monkeypatch.setattr(LiveJournal, "_transition_in_transaction", stage)
    before = {role: events(root, role) for role, root in zip(cli.ROLES, roots, strict=True)}
    projections = {
        root: cli.paths(root, role)[0].read_bytes()
        for role, root in zip(cli.ROLES, roots, strict=True)
    }
    with pytest.raises((ValueError, sqlite3.Error)):
        apply(data, report)
    for role, root in zip(cli.ROLES, roots, strict=True):
        assert events(root, role) == before[role]
        assert cli.paths(root, role)[0].read_bytes() == projections[root]
        store = StateStore(cli.paths(root, role)[0])
        assert store.load("live", read_only=True).halted
        assert store.journal.config_hash != target_hash
        with closing(sqlite3.connect(store.journal_path)) as db:
            assert not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='config_transitions'"
            ).fetchone()


def test_failed_projections_leave_both_committed_epochs_and_old_projections(tmp_path, monkeypatch):
    data, report = prepare(tmp_path, monkeypatch)
    before = {
        root: cli.paths(root, role)[0].read_bytes()
        for role, root in zip(cli.ROLES, data[0], strict=True)
    }
    monkeypatch.setattr(
        StateStore, "save", lambda *_: (_ for _ in ()).throw(OSError("synthetic crash"))
    )
    result = apply(data, report)
    assert (
        result["migration_applied"] and result["atomic_pair"] and not result["projection_complete"]
    )
    for role, root in zip(cli.ROLES, data[0], strict=True):
        path = cli.paths(root, role)[0]
        assert path.read_bytes() == before[root]
        reopened = StateStore(path, require_existing=True)
        state = reopened.load("live", read_only=True)
        assert state.halted and reopened.journal.config_hash == report["target_config_hash"]


def test_stale_unapproved_or_changed_raw_proofs_never_write(tmp_path, monkeypatch):
    data, report = prepare(tmp_path, monkeypatch)
    before = fixtures.snapshot(tmp_path)
    with pytest.raises(ValueError, match="approval"):
        cli.apply_pair(*data[0], data[1], data[2], data[3], report, "0" * 64)
    foreign = deepcopy(data[2])
    foreign["gas"] = []
    with pytest.raises(ValueError):
        cli.apply_pair(*data[0], data[1], foreign, data[3], report, report["evidence_hash"])
    ambiguous = deepcopy(report)
    ambiguous["rpc_requests"] = False
    with pytest.raises(ValueError, match="preflight"):
        cli.apply_pair(*data[0], data[1], data[2], data[3], ambiguous, report["evidence_hash"])
    monkeypatch.setattr(time, "time", lambda: report["valid_until"] + 1)
    with pytest.raises(ValueError, match="expired"):
        apply(data, report)
    assert fixtures.snapshot(tmp_path) == before


def test_target_binding_and_default_cli_remain_closed_without_apply(tmp_path, monkeypatch, capsys):
    data, report = prepare(tmp_path, monkeypatch)
    before = fixtures.snapshot(tmp_path)
    args = [
        "migrate_live_runtime_transition",
        "--strategy-dir",
        str(data[0][0]),
        "--authority-dir",
        str(data[0][1]),
    ]
    for flag, value in (("config", asdict(data[1])), ("references", data[2])):
        path = tmp_path / (flag + ".json")
        path.write_bytes(canonical_json(value))
        args.extend(["--" + flag, str(path)])
    args.extend(["--gas-evidence", str(data[3])])
    monkeypatch.setattr(sys, "argv", args)
    with pytest.raises(SystemExit) as code:
        cli.main()
    assert code.value.code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["review_passed"] and result["state_write_supported"] is False
    assert {p: p.read_bytes() for p in before} == before
    old = StateStore(cli.paths(data[0][0], "strategy")[0], require_existing=True)
    with pytest.raises(ValueError, match="mismatch"):
        old.bind(report["target_config_hash"], require_existing=True)


def cli_args(tmp_path, data, report, out):
    paths = {}
    for key, value in (("config", asdict(data[1])), ("references", data[2]), ("review", report)):
        path = tmp_path / (key + ".json")
        path.write_bytes(canonical_json(value))
        paths[key] = path
    return [
        "migrate_live_runtime_transition",
        "--strategy-dir",
        str(data[0][0]),
        "--authority-dir",
        str(data[0][1]),
        "--config",
        str(paths["config"]),
        "--references",
        str(paths["references"]),
        "--gas-evidence",
        str(data[3]),
        "--apply",
        "--review",
        str(paths["review"]),
        "--approved-review-hash",
        report["evidence_hash"],
        "--out",
        str(out),
    ]


@pytest.mark.parametrize("projection_failure", [False, True])
def test_explicit_cli_retains_attempt_and_reports_committed_projection_failure(
    tmp_path, monkeypatch, capsys, projection_failure
):
    data, report = prepare(tmp_path, monkeypatch)
    out = tmp_path / "migration-attempt"
    monkeypatch.setattr(sys, "argv", cli_args(tmp_path, data, report, out))
    if projection_failure:
        monkeypatch.setattr(
            StateStore, "save", lambda *_: (_ for _ in ()).throw(OSError("synthetic full disk"))
        )
    with pytest.raises(SystemExit) as code:
        cli.main()
    assert code.value.code == (3 if projection_failure else 0)
    result = json.loads(capsys.readouterr().out)
    assert result["migration_applied"] and result["atomic_pair"]
    assert result["projection_complete"] is not projection_failure
    assert result["execution_eligible"] is False and result["runtime_approved"] is False
    assert json.loads((out / "attempt.json").read_bytes())["migration_applied"] is None
    assert json.loads((out / "report.json").read_bytes()) == result
    for role, root in zip(cli.ROLES, data[0], strict=True):
        store = StateStore(cli.paths(root, role)[0], require_existing=True)
        assert store.load("live", read_only=True).halted
        assert store.journal.config_hash == report["target_config_hash"]


@pytest.mark.parametrize("fault", ["WAL", "sidecar", "budget", "filesystem"])
def test_original_prerequisite_refusal_never_opens_budget_or_recovers(tmp_path, monkeypatch, fault):
    data, report = prepare(tmp_path, monkeypatch)
    root = data[0][1]
    journal = StateStore(cli.paths(root, "authority")[0]).journal_path
    if fault == "WAL":
        with closing(sqlite3.connect(journal)) as db:
            assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    elif fault == "sidecar":
        journal.with_name(journal.name + "-journal").write_bytes(b"synthetic inspection required")
    elif fault == "budget":
        (root / "live.receipt-budget.db").unlink()
    else:
        signature = cli._signature

        def different(path):
            result = signature(path)
            return (result[0] + 1, *result[1:]) if path == journal else result

        monkeypatch.setattr(cli, "_signature", different)
    before = fixtures.snapshot(tmp_path)
    monkeypatch.setattr(RpcBudget, "__init__", lambda *_a, **_k: pytest.fail("opened budget"))
    with pytest.raises((ValueError, FileNotFoundError)):
        apply(data, report)
    assert fixtures.snapshot(tmp_path) == before


def after_feed(tmp_path, monkeypatch):
    data, _ = prepare(tmp_path, monkeypatch, legacy=True)
    roots, target, _, _, fixture, clock = data
    strict = replace(
        target,
        expected_pins={k: v for k, v in target.expected_pins.items() if k in LEGACY_PIN_NAMES},
    )
    source_pair = ("live_primary", "live_secondary")
    with ExitStack() as stack:
        proof = {
            role: stack.enter_context(
                closing(EvidenceStore(cli.paths(root, role)[1], require_existing=True))
            )
            for role, root in zip(cli.ROLES, roots, strict=True)
        }
        stores = {
            role: StateStore(cli.paths(root, role)[0], require_existing=True)
            for role, root in zip(cli.ROLES, roots, strict=True)
        }
        fresh = {}
        block = BlockRef(
            target.chain_id, fixture.block_number, fixture.block_hash, fixture.timestamp
        )
        for role, store in stores.items():
            state = store.load("live", read_only=True)
            _, fresh[role] = capture_nav(
                proof[role],
                strict,
                block,
                state.position,
                sources=source_pair,
                sessions=sessions(dict.fromkeys(source_pair, fixture)),
            )
        result = feed_transition.apply_pair(
            stores["strategy"], stores["authority"], strict, proof, fresh, sources=source_pair
        )
        assert result["migration_applied"] and result["projection_complete"]
        newer = replace(
            fixture,
            timestamp=fixture.timestamp + 60,
            block_number=fixture.block_number + 30,
            block_hash="0x" + "66" * 32,
        )
        block = BlockRef(target.chain_id, newer.block_number, newer.block_hash, newer.timestamp)
        refs = {"schema": "oasis.runtime-transition-inputs.v1", "nav": {}}
        for role, store in stores.items():
            state = store.load("live", read_only=True)
            _, fresh_refs = capture_nav(
                proof[role],
                target,
                block,
                state.position,
                sources=source_pair,
                sessions=sessions(dict.fromkeys(source_pair, newer)),
            )
            refs["nav"][role] = [asdict(ref) for ref in fresh_refs]
    monkeypatch.setattr(gas_fixture, "BLOCK", block)
    wire = gas_fixture.captures()
    for exchanges in wire:
        for index in (1, 5):
            header = json.loads(exchanges[index].response_payload)["result"]
            gas_fixture.change_result(exchanges, index, header | {"parentHash": newer.parent_hash})
    gas = tmp_path / "new-gas.db"
    with closing(EvidenceStore(gas)) as proof:
        refs["gas"] = [
            asdict(ref) for ref in retain_discovery_quorum(proof, block, wire, sources=source_pair)
        ]
    monkeypatch.setattr(time, "time", lambda: newer.timestamp)
    data = roots, target, refs, gas, newer, clock
    return data, review(*roots, target, refs, gas, now=newer.timestamp)


def test_existing_feed_epoch_is_retained_before_runtime_and_third_epoch_refuses(
    tmp_path, monkeypatch
):
    data, report = after_feed(tmp_path, monkeypatch)
    before = {role: events(root, role) for role, root in zip(cli.ROLES, data[0], strict=True)}
    assert apply(data, report)["migration_applied"]
    for role, root in zip(cli.ROLES, data[0], strict=True):
        assert events(root, role)[:-1] == before[role]
        store = StateStore(cli.paths(root, role)[0], require_existing=True)
        state = store.load("live", read_only=True)
        assert state.halted and store.journal.config_hash == report["target_config_hash"]
        with closing(sqlite3.connect(store.journal_path)) as db:
            payloads = db.execute("SELECT payload FROM config_transitions ORDER BY seq").fetchall()
        assert [json.loads(row[0])["schema"] for row in payloads] == [
            "oasis.strict-feed-epoch.v1",
            SCHEMA,
        ]
        record = epoch(data, report, role)[2]
        record["source_tip"] = store.journal.revision
        record["source_state_hash"] = sha256_hex(canonical_json(asdict(state)))
        before_third = events(root, role)
        with pytest.raises(ValueError, match="no repeat"):
            store.journal.transition(record)
        assert events(root, role) == before_third
