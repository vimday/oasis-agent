"""Keyless local process death must preserve unknown signing capital commitments."""

import json
import os
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from oasis_agent.execution.account_risk_guard import AccountRiskGuard
from oasis_agent.execution.lp_signer_sidecar import sidecar_request_hash
from oasis_agent.risk.account_risk import GlobalRiskJournal
from tests.unit.test_account_risk_guard import Reader, sidecar_for
from tests.unit.test_lp_signer_sidecar import ACCOUNT, RISK_POLICY, make_request

RESERVED_USD_MICROS = 400_000_000

CHILD = """
import json, os, sys
from pathlib import Path
from oasis_agent.execution.account_risk_guard import AccountRiskGuard
from oasis_agent.risk.account_risk import GlobalRiskJournal
from tests.unit.test_account_risk_guard import Reader, sidecar_for
from tests.unit.test_lp_signer_sidecar import ACCOUNT, RISK_POLICY, make_request

directory, stage = Path(sys.argv[1]), sys.argv[2]
def die(code, provider_calls):
    # Only fixed fixture metadata: no digest, signature, transaction or key.
    with (directory / 'crash-marker.json').open('x') as marker:
        json.dump({'stage': stage, 'provider_calls': provider_calls,
                   'synthetic_fixture': True}, marker)
        marker.flush()
        os.fsync(marker.fileno())
    os._exit(code)

class CrashGuard(AccountRiskGuard):
    def reserve(self, request):
        result = super().reserve(request)
        if stage == 'capital-reserved':
            die(75, 0)
        return result

class KeylessCrashProvider:
    def sign(self, digest):
        assert len(digest) == 32
        die(76, 1)

journal = GlobalRiskJournal(directory / 'risk.db', RISK_POLICY)
guard = CrashGuard(journal, Reader(amount=400_000_000), clock=lambda: 1010)
with sidecar_for(directory, guard=guard, provider=KeylessCrashProvider()) as sidecar:
    sidecar.handle(make_request(ACCOUNT, nonce=91))
raise AssertionError('fixture crash point must be reached')
"""


class CountingReader(Reader):
    calls = 0

    def reconcile_and_price(self, request):  # type: ignore[no-untyped-def]
        self.calls += 1
        return super().reconcile_and_price(request)


class MustNotSign:
    """No key or transport; any restart provider call violates the tested gate."""

    calls = 0

    def sign(self, digest: bytes) -> tuple[int, int, int]:
        self.calls += 1
        raise AssertionError("uncertain capital commitment must block this new request")


def assert_pending(journal: GlobalRiskJournal, request_hash: str) -> None:
    state = journal.current_state()
    assert [(r.request_hash, r.usd_micros) for r in state.reservations] == [
        (request_hash, RESERVED_USD_MICROS)
    ]
    assert state.settlements == ()
    assert state.cumulative_loss == 0
    # Refusal must be caused by pending capital, not a loss latch masking it.
    assert not state.halted


@pytest.mark.parametrize("stage", ["capital-reserved", "provider-entered"])
def test_unknown_signing_capital_survives_process_death_and_new_signer_journal(
    tmp_path: Path, stage: str
) -> None:
    root = Path(__file__).resolve().parents[2]
    child = subprocess.run(
        [sys.executable, "-c", CHILD, str(tmp_path), stage],
        cwd=root,
        env={"PATH": os.defpath, "PYTHONPATH": str(root)},
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert child.returncode == (75 if stage == "capital-reserved" else 76), child.stderr.decode()
    marker = json.loads((tmp_path / "crash-marker.json").read_text())
    assert marker["provider_calls"] == (0 if stage == "capital-reserved" else 1)
    request_hash = sidecar_request_hash(make_request(ACCOUNT, nonce=91))[2:]
    reader = CountingReader(amount=RESERVED_USD_MICROS)
    provider = MustNotSign()
    with closing(GlobalRiskJournal(tmp_path / "risk.db", RISK_POLICY)) as journal:
        assert_pending(journal, request_hash)
        guard = AccountRiskGuard(journal, reader, clock=lambda: 1010)
        with sidecar_for(tmp_path, guard=guard, provider=provider) as sidecar:
            rows = sidecar.journal.db.execute(
                "SELECT account_nonce,status FROM nonce_reservations"
            ).fetchall()
            assert rows == ([] if stage == "capital-reserved" else [(91, "reserved")])
            decision = sidecar.handle(make_request(ACCOUNT, nonce=92))
            assert decision.outcome == "denied"
            assert decision.raw_transaction_hex is None
            expected = (
                "independent account risk admission failed"
                if stage == "capital-reserved"
                else "an unresolved signing attempt must be reconciled first"
            )
            assert decision.reasons == (expected,)
        assert reader.calls == (1 if stage == "capital-reserved" else 0)
        assert provider.calls == 0
        assert_pending(journal, request_hash)
        # A different signer DB has no nonce latch to hide a capital reset.
        # USD 400 + USD 100.000001 must exceed the USD 500 shared cap.
        reader.amount = 100_000_001
        with sidecar_for(
            tmp_path, guard=guard, provider=provider, name="fresh-sidecar.db"
        ) as fresh:
            decision = fresh.handle(make_request(ACCOUNT, nonce=93))
            assert decision.reasons == ("independent account risk admission failed",)
            assert decision.raw_transaction_hex is None
        assert_pending(journal, request_hash)
        assert provider.calls == 0
    with closing(GlobalRiskJournal(tmp_path / "risk.db", RISK_POLICY)) as reopened:
        assert_pending(reopened, request_hash)
        guard = AccountRiskGuard(reopened, reader, clock=lambda: 1010)
        with sidecar_for(
            tmp_path, guard=guard, provider=provider, name="second-fresh-sidecar.db"
        ) as sidecar:
            decision = sidecar.handle(make_request(ACCOUNT, nonce=94))
        assert decision.reasons == ("independent account risk admission failed",)
        assert decision.raw_transaction_hex is None
        assert_pending(reopened, request_hash)
        assert provider.calls == 0
    (tmp_path / "case-summary.json").write_text(
        json.dumps(
            {
                "synthetic_fixture": True,
                "external_network_calls": 0,
                "real_key_access": False,
                "stage": stage,
                "child_exit_code": child.returncode,
                "fixture_provider_calls_before_crash": marker["provider_calls"],
                "provider_calls_after_restart": provider.calls,
                "pending_usd_micros": RESERVED_USD_MICROS,
                "total_capital_usd_micros": RISK_POLICY.total_capital_usd_micros,
                "fresh_attempt_usd_micros": reader.amount,
                "risk_reader_calls_after_restart": reader.calls,
                "unknown_commitment_preserved": True,
                "reservation_settlement_verified": False,
                "canary_accepted": False,
                "execution_eligible": False,
            },
            sort_keys=True,
        )
    )
