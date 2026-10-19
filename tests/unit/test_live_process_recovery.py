"""Kill/restart the real authority process after a lost socket reply, with test keys."""

import json
import multiprocessing
import socket
import sys
import tempfile
import time
from contextlib import closing, contextmanager
from dataclasses import asdict
from pathlib import Path

import pytest
from eth_account import Account
from eth_hash.auto import keccak

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.lp_signer_daemon import prepare_endpoint
from oasis_agent.live import valuation
from oasis_agent.live.authority_socket import (
    SCHEMA,
    TransactionSocketClient,
    fields_json,
    serve_connections,
)
from oasis_agent.live.state import StateStore
from oasis_agent.models.common import canonical_json
from scripts import run_live_authority as cli
from tests.unit import test_live_authority as fixtures


def child(directory, endpoint, seed, ready):
    key = fixtures.Key()
    key._key = Account.from_key(seed)
    key.account = key._key.address.lower()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fixtures, "Key", lambda: key)
        patch.setattr(valuation, "FEED_RUNTIME", "0x" + keccak(b"\x60\x00").hex())
        with closing(EvidenceStore(Path(directory) / "evidence.db")) as evidence:
            authority, _, fields, context = fixtures.setup_authority(
                Path(directory), evidence, patch
            )
            # Use the production CLI's state names for the keyless restart.
            root = Path(directory)
            root.chmod(0o700)
            (root / "config.json").write_text(json.dumps(asdict(authority.loop.c)))
            store = StateStore(root / "state.json")
            store.bind(authority.loop.store.journal.config_hash)
            store.save(authority.loop.state)
            authority.loop.store = store
            with prepare_endpoint(Path(endpoint)) as server:
                ready.put((key.account, fields_json(fields), context, key.calls))
                serve_connections(
                    server, authority, account=key.account, max_signatures=2, ttl_seconds=20
                )


def status_child(directory, endpoint, ready):
    """No seed argument: restart cannot obtain the fixture or production key."""

    def forbidden(*args, **kwargs):
        raise AssertionError("keyless status attempted signing, release or RPC access")

    original = cli.prepare_endpoint

    @contextmanager
    def prepared(path):
        with original(path) as server:
            ready.put("keyless-status-ready")
            yield server

    with pytest.MonkeyPatch.context() as patch:
        for name in (
            "load_private_key",
            "BudgetedRpc",
            "TransactionAuthority",
            "require_live_release",
        ):
            patch.setattr(cli, name, forbidden)
        patch.setattr(cli, "prepare_endpoint", prepared)
        patch.setattr(
            sys,
            "argv",
            [
                "authority",
                "--status-only",
                "--config",
                str(Path(directory) / "config.json"),
                "--state-dir",
                directory,
                "--socket",
                endpoint,
                "--max-status-requests",
                "1",
                "--ttl-seconds",
                "15",
            ],
        )
        cli.main()


def stop(process):
    if process.is_alive():
        process.kill()
    process.join(timeout=5)
    assert not process.is_alive()


def test_lost_reply_survives_sigkill_and_new_process_without_resigning(tmp_path: Path):
    # New random fixture key; never read an environment, owner key, or RPC endpoint.
    seed = bytes(Account.create().key)
    ctx = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="oa-restart-", dir=Path("/tmp").resolve()) as name:
        directory = Path(name)
        directory.chmod(0o700)
        endpoint = directory / "a.sock"
        ready = ctx.Queue()
        first = ctx.Process(target=child, args=(str(tmp_path), str(endpoint), seed, ready))
        first.start()
        try:
            account, fields, context, calls = ready.get(timeout=10)
            assert calls == 0
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(5)
                conn.connect(str(endpoint))
                conn.sendall(
                    canonical_json(
                        {"schema": SCHEMA, "account": account, "fields": fields, "context": context}
                    )
                    + b"\n"
                )
                # Drop the response deliberately; there is no broadcast path in this test.
            deadline = time.monotonic() + 8
            store = StateStore(tmp_path / "state.json")
            while time.monotonic() < deadline:
                saved = store.load("live")
                if saved.pending:
                    break
                time.sleep(0.05)
            assert saved.pending and saved.signing_attempt is None
            commitment = saved.pending.transaction
        finally:
            stop(first)
        before = store.journal_path.read_bytes()
        second = ctx.Process(target=status_child, args=(str(tmp_path), str(endpoint), ready))
        second.start()
        try:
            assert ready.get(timeout=10) == "keyless-status-ready"
            public = TransactionSocketClient(endpoint, account, timeout=5).pending_status()
            assert (
                public["pending"]["transaction"]["transaction_hash"] == commitment.transaction_hash
            )
            assert not public["signing_unresolved"]
            assert StateStore(store.path).load("live").pending.transaction == commitment
            second.join(timeout=5)
            assert second.exitcode == 0 and store.journal_path.read_bytes() == before
        finally:
            stop(second)
            ready.close()
            ready.join_thread()
