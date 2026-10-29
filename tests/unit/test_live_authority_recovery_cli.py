"""Recovery CLI must settle old proofs without reopening signing authority."""

import json
import sys
from contextlib import closing
from dataclasses import asdict

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.receipts import RetainedReceiptCostReader
from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.live.state import StateStore
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.reporting import live_release as release_module
from oasis_agent.rpc.budget import RpcBudget
from scripts import run_live_authority as cli
from tests.unit.test_live_approval_cash import TEST_CONFIG, ApprovalFixture, approval_loop
from tests.unit.test_live_lp import ScriptedRpc
from tests.unit.test_lp_receipts import PRIMARY, SECONDARY, sessions


def forbidden(*args, **kwargs):
    raise AssertionError("recovery must not acquire signing capability")


def setup_cli(tmp_path, monkeypatch, *options):
    directory = tmp_path / "authority"
    directory.mkdir(mode=0o700)
    config = tmp_path / "config.json"
    config.write_text(json.dumps(asdict(TEST_CONFIG)))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "authority",
            "--config",
            str(config),
            "--state-dir",
            str(directory),
            *options,
        ],
    )
    for name in (
        "load_private_key",
        "prepare_endpoint",
        "serve_connections",
        "TransactionAuthority",
    ):
        monkeypatch.setattr(cli, name, forbidden)
    return directory


def test_normal_signing_still_refuses_before_key_or_network(tmp_path, monkeypatch):
    monkeypatch.setattr(release_module, "ACCEPTANCE_PERMIT", None)  # closed, no permit
    directory = setup_cli(tmp_path, monkeypatch, "--socket", "/unused.sock")
    monkeypatch.setattr(cli, "BudgetedRpc", forbidden)
    with pytest.raises(ValueError, match="live release blocked"):
        cli.main()
    assert not list(directory.iterdir())


@pytest.mark.parametrize("endpoint", ["", " ", "not-a-url"])
def test_missing_endpoint_refuses_before_key_even_after_release(tmp_path, monkeypatch, endpoint):
    directory = setup_cli(tmp_path, monkeypatch, "--socket", "/unused.sock")
    # Test the endpoint boundary independently of the closed production release.
    monkeypatch.setattr(cli, "require_live_start", lambda: None)
    monkeypatch.setenv("BASE_RPC_PRIMARY_ALT", endpoint)
    monkeypatch.setenv(TEST_CONFIG.secondary_rpc_env, endpoint)
    with pytest.raises(ValueError, match="HTTP\\(S\\) endpoint"):
        cli.main()
    assert not (directory / "live.receipt-budget.db").exists()


@pytest.mark.parametrize("option", ["--key-file", "--key-env", "--socket"])
def test_recovery_rejects_signing_options_before_io(tmp_path, monkeypatch, option):
    directory = setup_cli(tmp_path, monkeypatch, "--reconcile-only", option, "unused")
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 2 and not list(directory.iterdir())


def test_absent_journal_is_not_reported_as_successful_empty_recovery(tmp_path, monkeypatch, capsys):
    directory = setup_cli(tmp_path, monkeypatch, "--reconcile-only")
    monkeypatch.setattr(cli, "BudgetedRpc", forbidden)
    monkeypatch.setattr(cli, "require_live_start", forbidden)
    monkeypatch.delenv("BASE_RPC_PRIMARY_ALT", raising=False)
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 3
    assert "no pending" not in capsys.readouterr().out
    assert not (directory / "state.json").exists()
    assert not (directory / "state.json.journal.sqlite").exists()


def test_existing_empty_journal_needs_no_rpc_environment_or_key(tmp_path, monkeypatch, capsys):
    directory = setup_cli(tmp_path, monkeypatch, "--reconcile-only")
    store = StateStore(directory / "state.json")
    store.bind(sha256_hex(canonical_json(asdict(TEST_CONFIG))))
    before = (store.path.read_bytes(), store.journal_path.read_bytes())
    monkeypatch.setattr(cli, "BudgetedRpc", forbidden)
    monkeypatch.delenv("BASE_RPC_PRIMARY_ALT", raising=False)
    cli.main()
    assert "no pending" in capsys.readouterr().out
    assert (store.path.read_bytes(), store.journal_path.read_bytes()) == before
    assert not (directory / "live.receipt-budget.db").exists()


def test_uncertain_signature_never_cleared_by_recovery(tmp_path, monkeypatch):
    directory = setup_cli(tmp_path, monkeypatch, "--reconcile-only")
    store = StateStore(directory / "state.json")
    store.bind(sha256_hex(canonical_json(asdict(TEST_CONFIG))))
    state = store.load("live")
    state.signing_attempt = {"unresolved": True}
    store.save(state)
    before = store.path.read_bytes()
    monkeypatch.setattr(cli, "BudgetedRpc", forbidden)
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 3 and store.path.read_bytes() == before


@pytest.mark.parametrize("disagreement", [False, True])
def test_receipt_recovery_retains_cash_proof_and_settles_once(
    tmp_path, monkeypatch, disagreement, capsys
):
    directory = setup_cli(tmp_path, monkeypatch, "--reconcile-only")
    a = ApprovalFixture()
    b = ApprovalFixture(native_after=99_783) if disagreement else ApprovalFixture()
    with closing(EvidenceStore(directory / "evidence.db")) as evidence:
        original = approval_loop(directory, evidence, a, b)
        store = StateStore(directory / "state.json")
        # Publish the synthetic pending into the authority's own journal.
        store.bind(sha256_hex(canonical_json(asdict(TEST_CONFIG))))
        store.save(original.state)
    with RpcBudget(
        directory / "live.receipt-budget.db",
        LiveRpcProfile().policies(("live_primary", "live_secondary")),
    ):
        pass
    monkeypatch.setenv("BASE_RPC_PRIMARY_ALT", "primary-fixture")
    monkeypatch.setenv(TEST_CONFIG.secondary_rpc_env, "secondary-fixture")
    monkeypatch.setattr(cli, "require_live_start", forbidden)
    monkeypatch.setattr(
        cli, "BudgetedRpc", lambda *a, **k: ScriptedRpc(receipt=ApprovalFixture()._receipt())
    )
    monkeypatch.setattr(
        cli,
        "receipt_cost_reader",
        lambda config, evidence, *args, **kwargs: RetainedReceiptCostReader(
            evidence,
            chain_id=config.chain_id,
            account=config.account,
            native_decimals=18,
            sources=(PRIMARY, SECONDARY),
            sessions=sessions({PRIMARY: a, SECONDARY: b}),
        ),
    )
    before = store.path.read_bytes()
    if disagreement:
        with pytest.raises(SystemExit) as caught:
            cli.main()
        assert caught.value.code == 3 and store.path.read_bytes() == before
        assert store.load("live").pending is not None
    else:
        cli.main()
        for _ in range(2):
            cli.main()
            state = store.load("live")
            assert state.pending is None and state.gas_spent_wei == 218
            assert state.actions_today == 1 and len(state.history) == 1
        assert "confirmed" in capsys.readouterr().out


@pytest.mark.parametrize(
    "options",
    [
        ["--key-file", "unused"],
        ["--key-env", "unused"],
        ["--reconcile-only"],
    ],
)
def test_status_mode_rejects_authority_options(tmp_path, monkeypatch, options):
    directory = setup_cli(tmp_path, monkeypatch, "--status-only", "--socket", "/unused", *options)
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 2 and not list(directory.iterdir())


def test_status_mode_refuses_absent_journal_without_rpc_or_key(tmp_path, monkeypatch):
    directory = setup_cli(tmp_path, monkeypatch, "--status-only", "--socket", "/unused")
    monkeypatch.setattr(cli, "BudgetedRpc", forbidden)
    monkeypatch.setattr(cli, "require_live_start", forbidden)
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 3
    assert not (directory / "state.json").exists()
    assert not (directory / "state.json.journal.sqlite").exists()
