"""Offline upgrade preflight over original synthetic journals and raw quorums."""

import fcntl
import json
import sys
from contextlib import closing
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.gas_oracle import LEGACY_PIN_NAMES
from oasis_agent.live.gas_oracle_evidence import retain_discovery_quorum
from oasis_agent.live.state import Pending, StateStore
from oasis_agent.live.valuation import capture_nav
from oasis_agent.models.common import BlockRef, canonical_json, sha256_hex
from oasis_agent.rpc.budget import RpcBudget
from oasis_agent.rpc.transport import JsonRpcTransport
from scripts import review_live_runtime_transition as cli
from tests.unit import test_live_gas_oracle_evidence as gas_fixture
from tests.unit import test_live_recovery_capture as recovery
from tests.unit.gas_oracle_fixtures import synthetic_gas_pins
from tests.unit.test_live_valuation import synthetic_feed_pin  # noqa: F401
from tests.unit.test_lp_receipts import sessions


def setup(tmp_path, monkeypatch, *, legacy=False, full_original=False):
    target = replace(
        recovery.CONFIG, expected_pins=recovery.CONFIG.expected_pins | synthetic_gas_pins()
    )
    if full_original:
        monkeypatch.setattr(recovery, "CONFIG", target)
    roots, _, _, posters, clock = recovery.setup(tmp_path, monkeypatch, legacy=legacy)
    fresh = {"schema": "oasis.runtime-transition-inputs.v1", "nav": {}}
    fixture = next(iter(posters.values())).current
    block = BlockRef(target.chain_id, fixture.block_number, fixture.block_hash, fixture.timestamp)
    for role, root in zip(cli.ROLES, roots, strict=True):
        path, evidence_path, _ = cli.paths(root, role)
        store = StateStore(path)
        state = store.load("live", read_only=True)
        state.halted = "private synthetic retained halt"
        store.save(state)
        with closing(EvidenceStore(evidence_path, require_existing=True)) as evidence:
            _, refs = capture_nav(
                evidence,
                target,
                block,
                state.position,
                sources=recovery.SOURCES,
                sessions=sessions(dict.fromkeys(recovery.SOURCES, fixture)),
            )
        fresh["nav"][role] = [asdict(r) for r in refs]
    monkeypatch.setattr(gas_fixture, "BLOCK", block)
    wire = gas_fixture.captures()
    for exchanges in wire:
        for index in (1, 5):
            header = json.loads(exchanges[index].response_payload)["result"]
            gas_fixture.change_result(
                exchanges, index, header | {"parentHash": fixture.parent_hash}
            )
    gas_path = tmp_path / "gas.db"
    with closing(EvidenceStore(gas_path)) as evidence:
        fresh["gas"] = [
            asdict(r)
            for r in retain_discovery_quorum(evidence, block, wire, sources=recovery.SOURCES)
        ]
    return roots, target, fresh, gas_path, fixture, clock


def snapshot(tmp_path):
    return {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def run(data, **kwargs):
    roots, target, refs, gas_path, fixture, _ = data
    return cli.review(*roots, target, refs, gas_path, now=kwargs.get("now", fixture.timestamp))


@pytest.mark.parametrize("legacy", [False, True])
def test_same_block_upgrade_preflight_is_readonly_and_never_a_permit(tmp_path, monkeypatch, legacy):
    data = setup(tmp_path, monkeypatch, legacy=legacy)
    before = snapshot(tmp_path)

    def forbidden(*_, **__):
        pytest.fail("read-only upgrade preflight crossed a write or acquisition boundary")

    monkeypatch.setattr(StateStore, "save", forbidden)
    monkeypatch.setattr(RpcBudget, "__init__", forbidden)
    monkeypatch.setattr(JsonRpcTransport, "request", forbidden)
    result = run(data)
    assert result["review_passed"] and result["rpc_requests"] == 0
    for name in (
        "execution_eligible",
        "runtime_approved",
        "migration_applied",
        "state_write_supported",
    ):
        assert result[name] is False
    assert result["valid_until"] == data[4].timestamp + 120
    assert all(v["halt_preserved"] for v in result["services"].values())
    assert "private synthetic retained halt" not in str(result)
    assert snapshot(tmp_path) == before
    digest = result.pop("evidence_hash")
    assert sha256_hex(canonical_json(result)) == digest


@pytest.mark.parametrize(
    "fault",
    [
        "five_pin_target",
        "changed_old_pin",
        "gas_pin",
        "pending",
        "unhalted",
        "basis",
        "gas_total",
        "stale",
        "old_nav_policy",
        "missing_nav",
        "gas_source",
        "sidecar",
        "same_artifact",
    ],
)
def test_preflight_refuses_unresolved_scope_without_changing_originals(
    tmp_path, monkeypatch, fault
):
    data = list(setup(tmp_path, monkeypatch))
    roots, target, refs, gas_path, fixture, _ = data
    if fault == "five_pin_target":
        data[1] = replace(
            target,
            expected_pins={k: v for k, v in target.expected_pins.items() if k in LEGACY_PIN_NAMES},
        )
    elif fault in {"changed_old_pin", "gas_pin"}:
        role = "state_view" if fault == "changed_old_pin" else "gas_oracle_proxy"
        data[1] = replace(target, expected_pins=target.expected_pins | {role: "0x" + "12" * 32})
    elif fault in {"pending", "unhalted", "basis", "gas_total"}:
        store = StateStore(cli.paths(roots[1], "authority")[0])
        state = store.load("live", read_only=True)
        if fault == "pending":
            state.pending = Pending("mint", "0x" + "ab" * 32, 4, fixture.timestamp)
        elif fault == "unhalted":
            state.halted = None
        elif fault == "basis":
            state.exact_risk.basis += 1
        else:
            state.gas_spent_wei += 1
        store.save(state)
    elif fault == "old_nav_policy":
        path, evidence_path, _ = cli.paths(roots[1], "authority")
        state = StateStore(path).load("live", read_only=True)
        with closing(EvidenceStore(evidence_path, require_existing=True)) as evidence:
            _, pair = capture_nav(
                evidence,
                recovery.CONFIG,
                gas_fixture.BLOCK,
                state.position,
                sources=recovery.SOURCES,
                sessions=sessions(dict.fromkeys(recovery.SOURCES, fixture)),
            )
        refs["nav"]["authority"] = [asdict(r) for r in pair]
    elif fault == "missing_nav":
        refs["nav"]["authority"] = []
    elif fault == "gas_source":
        refs["gas"][1]["source"] = refs["gas"][0]["source"]
    elif fault == "sidecar":
        gas_path.with_name(gas_path.name + "-wal").touch()
    elif fault == "same_artifact":
        data[3] = cli.paths(roots[0], "strategy")[1]
    before = snapshot(tmp_path)
    with pytest.raises((ValueError, OSError)):
        run(data, now=fixture.timestamp + (121 if fault == "stale" else 0))
    assert snapshot(tmp_path) == before


def test_already_expanded_original_is_not_a_five_pin_upgrade(tmp_path, monkeypatch):
    data = setup(tmp_path, monkeypatch, full_original=True)
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match="five-pin"):
        run(data)
    assert snapshot(tmp_path) == before


def test_gas_and_nav_cannot_reuse_a_hash_with_different_parents(tmp_path, monkeypatch):
    data = setup(tmp_path, monkeypatch)
    wire = gas_fixture.captures()  # The gas fixture's parent differs from the NAV parent.
    with closing(EvidenceStore(data[3], require_existing=True)) as evidence:
        data[2]["gas"] = [
            asdict(r)
            for r in retain_discovery_quorum(
                evidence, gas_fixture.BLOCK, wire, sources=recovery.SOURCES
            )
        ]
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match="headers differ"):
        run(data)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("changed", ["STOP", "directory"])
def test_original_changes_during_proof_replay_refuse(tmp_path, monkeypatch, changed):
    data = setup(tmp_path, monkeypatch)
    original = cli.review_checkpoint

    def mutate(*args, **kwargs):
        original(*args, **kwargs)
        if changed == "STOP":
            (data[0][1] / "STOP").write_bytes(b"")
        else:
            data[0][1].chmod(0o777)

    monkeypatch.setattr(cli, "review_checkpoint", mutate)
    with pytest.raises(ValueError, match="changed"):
        run(data)


def test_running_service_lock_refuses_before_gas_replay(tmp_path, monkeypatch):
    data = setup(tmp_path, monkeypatch)
    before = snapshot(tmp_path)
    with cli.paths(data[0][0], "strategy")[2].open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            run(data)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("expired_at_publish", [False, True])
def test_cli_is_redacted_and_rechecks_publication_freshness(
    tmp_path, monkeypatch, capsys, expired_at_publish
):
    data = setup(tmp_path, monkeypatch)
    config, references = tmp_path / "target.json", tmp_path / "references.json"
    config.write_bytes(canonical_json(asdict(data[1])))
    references.write_bytes(canonical_json(data[2]))
    times = iter([data[4].timestamp, data[4].timestamp + (121 if expired_at_publish else 0)])
    monkeypatch.setattr(cli.time, "time", lambda: next(times))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "review",
            "--strategy-dir",
            str(data[0][0]),
            "--authority-dir",
            str(data[0][1]),
            "--config",
            str(config),
            "--references",
            str(references),
            "--gas-evidence",
            str(data[3]),
        ],
    )
    before = snapshot(tmp_path)
    with pytest.raises(SystemExit) as done:
        cli.main()
    assert done.value.code == (3 if expired_at_publish else 0)
    result = json.loads(capsys.readouterr().out)
    assert result["review_passed"] is not expired_at_publish
    assert not result["state_write_supported"] and not result["execution_eligible"]
    assert str(tmp_path) not in str(result) and "private synthetic" not in str(result)
    assert snapshot(tmp_path) == before
