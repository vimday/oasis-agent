"""Opening-proof checks use only existing local evidence before recovery RPC."""

import sqlite3
from contextlib import closing
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.recovery import require_recovery_wallets
from oasis_agent.live.wallet import capture_wallet
from tests.unit.test_live_approval_cash import TEST_CONFIG, ApprovalFixture, approval_loop
from tests.unit.test_lp_receipts import PRIMARY, SECONDARY, sessions


@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize(
    "fault",
    [None, "missing", "empty", "reference", "duplicate", "nonce", "scope", "disagree", "block"],
)
def test_recovery_replays_original_wallet_quorum_without_writing(tmp_path, uncertain, fault):
    path = tmp_path / "evidence.db"
    fixture = ApprovalFixture()
    with closing(EvidenceStore(path)) as evidence:
        loop = approval_loop(tmp_path, evidence, fixture, replace(fixture))
        state = loop.state
        pending = state.pending
        assert pending is not None
        refs = pending.wallet_evidence
        if fault == "reference":
            refs = (replace(refs[0], content_hash="ff" * 32), refs[1])
        elif fault == "duplicate":
            refs = (refs[0], refs[0])
        elif fault == "disagree":
            changed = replace(fixture, native_before=fixture.native_before + 1)
            different = capture_wallet(
                evidence,
                TEST_CONFIG,
                fixture.opening,
                sources=(PRIMARY, SECONDARY),
                sessions=sessions({PRIMARY: changed, SECONDARY: replace(changed)}),
            )
            refs = (refs[0], different[1])
        elif fault == "block":
            refs = (refs[0], replace(refs[1], block=replace(refs[1].block, number=1)))
        pending.wallet_evidence = refs
        if fault == "nonce":
            pending.nonce += 1
        if uncertain:
            state.signing_attempt = {
                "nonce": pending.nonce,
                "block": fixture.opening.number,
                "block_hash": fixture.opening.block_hash,
                "wallet_evidence": [asdict(r) for r in refs],
            }
            state.pending = None
    if fault == "missing":
        path.unlink()
    elif fault == "empty":
        path.write_bytes(b"")
    config = replace(TEST_CONFIG, account="0x" + "aa" * 20) if fault == "scope" else TEST_CONFIG
    before_state = asdict(state)
    before_files = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    if fault:
        with pytest.raises((ValueError, KeyError, sqlite3.DatabaseError)):
            require_recovery_wallets(path, state, config)
    else:
        require_recovery_wallets(path, state, config)
    assert asdict(state) == before_state
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before_files
