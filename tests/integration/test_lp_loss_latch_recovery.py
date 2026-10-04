"""Synthetic loss latch survives process death and blocks the signer before its provider."""

import json
import os
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from oasis_agent.execution.account_risk_guard import AccountRiskGuard
from oasis_agent.risk.account_risk import (
    AccountFrame,
    AssetBalance,
    GlobalRiskJournal,
    PortfolioFrame,
)
from tests.unit.test_account_risk_guard import sidecar_for
from tests.unit.test_lp_signer_sidecar import (
    ACCOUNT,
    RISK_ASSET,
    RISK_PER_ATTEMPT_USD_MICROS,
    RISK_POLICY,
    make_request,
)


def synthetic_frame(loss: int, *, inception: bool = False) -> PortfolioFrame:
    timestamp = 1009 if inception else 1010
    asset = AssetBalance(
        asset_id=RISK_ASSET,
        units=500_000_000 - loss,
        decimals=6,
        price_usd_micros_numerator=1_000_000,
        price_denominator=1,
        price_timestamp=timestamp,
        role="cash",
        evidence_hash="e" * 64,
    )
    account = AccountFrame(
        chain_id=4663,
        account=ACCOUNT,
        block_number=100 if inception else 101,
        block_hash="0x" + ("ab" if inception else "cd") * 32,
        timestamp=timestamp,
        assets=(asset,),
        movements={RISK_ASSET: -loss},
        non_trading_flow_usd_micros=0,
        inventory_evidence_hash="f" * 64,
        unresolved_operations=0,
    )
    return PortfolioFrame(accounts=(account,))


CHILD = """
import os, sys
from pathlib import Path
from oasis_agent.risk.account_risk import Commitment, GlobalRiskJournal
from tests.integration.test_lp_loss_latch_recovery import synthetic_frame
from tests.unit.test_lp_signer_sidecar import ACCOUNT, RISK_POLICY

journal = GlobalRiskJournal(Path(sys.argv[1]), RISK_POLICY)
key = '4663:' + ACCOUNT
journal.admit(synthetic_frame(0, inception=True),
              Commitment(request_hash='b' * 64, account_key=key,
                         usd_micros=100_000_000), now=1009)
try:
    journal.admit(synthetic_frame(int(sys.argv[2])),
                  Commitment(request_hash='c' * 64, account_key=key,
                             usd_micros=100_000_000), now=1010)
except ValueError as error:
    assert str(error) == 'global loss latch or capital ceiling blocked admission'
else:
    raise AssertionError('loss threshold must refuse admission')
assert journal.current_state().halted
# No cleanup: the loss-triggered refusal must have durably committed the latch.
os._exit(74)
"""


class CountingReader:
    def __init__(self, frame: PortfolioFrame) -> None:
        self.frame = frame
        self.calls = 0

    def reconcile_and_price(self, request) -> tuple[PortfolioFrame, int]:  # type: ignore[no-untyped-def]
        self.calls += 1
        return self.frame, RISK_PER_ATTEMPT_USD_MICROS


class MustNotSign:
    """Has no key and no network; any invocation is a failed safety invariant."""

    calls = 0

    def sign(self, digest: bytes) -> tuple[int, int, int]:
        self.calls += 1
        raise AssertionError("a latched loss must stop before the signing provider")


@pytest.mark.parametrize("loss", [200_000_000, 200_000_001], ids=["at-limit", "above-limit"])
def test_durable_loss_latch_survives_process_death_and_fresh_signer_journals(
    tmp_path: Path, loss: int
) -> None:
    root = Path(__file__).resolve().parents[2]
    risk_path = tmp_path / "risk.db"
    child = subprocess.run(
        [sys.executable, "-c", CHILD, str(risk_path), str(loss)],
        cwd=root,
        env={"PATH": os.defpath, "PYTHONPATH": str(root)},
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert child.returncode == 74, child.stderr.decode()
    reader = CountingReader(synthetic_frame(loss))
    provider = MustNotSign()
    with closing(GlobalRiskJournal(risk_path, RISK_POLICY)) as journal:
        state = journal.current_state()
        assert state.halted
        assert state.cumulative_loss == loss
        assert sum(state.daily_losses.values()) == loss
        assert tuple(r.request_hash for r in state.reservations) == ("b" * 64,)
        guard = AccountRiskGuard(journal, reader, clock=lambda: 1010)
        # A new signer journal has no nonce or signer-failure latch to hide the
        # tested result: the independent global loss gate itself must deny.
        with sidecar_for(
            tmp_path, guard=guard, provider=provider, name="fresh-sidecar.db"
        ) as sidecar:
            for nonce in (91, 92):
                decision = sidecar.handle(make_request(ACCOUNT, nonce=nonce))
                assert decision.outcome == "denied"
                assert decision.reasons == ("independent account risk admission failed",)
                assert decision.raw_transaction_hex is None
        assert reader.calls == 2
        assert provider.calls == 0
        assert tuple(r.request_hash for r in journal.current_state().reservations) == ("b" * 64,)
        # The baseline fixture commitment never produced a transaction. Removing
        # that commitment restores no loss allowance and must not clear the halt.
        journal.release("b" * 64, reason="fixture commitment never produced a transaction")
        assert journal.current_state().reservations == ()
        assert journal.current_state().halted
    with closing(GlobalRiskJournal(risk_path, RISK_POLICY)) as reopened:
        guard = AccountRiskGuard(reopened, reader, clock=lambda: 1010)
        with sidecar_for(
            tmp_path, guard=guard, provider=provider, name="second-sidecar.db"
        ) as sidecar:
            again = sidecar.handle(make_request(ACCOUNT, nonce=93))
        assert again.reasons == ("independent account risk admission failed",)
        assert provider.calls == 0
        assert reopened.current_state().cumulative_loss == loss
        assert reopened.current_state().halted
        assert reopened.current_state().reservations == ()
    (tmp_path / "case-summary.json").write_text(
        json.dumps(
            {
                "synthetic_fixture": True,
                "external_network_calls": 0,
                "child_exit_code": child.returncode,
                "observed_loss_usd_micros": loss,
                "daily_loss_cap_usd_micros": RISK_POLICY.daily_loss_usd_micros,
                "reader_calls": reader.calls,
                "provider_calls": provider.calls,
                "halt_survived_commitment_release_and_restart": True,
                "execution_eligible": False,
            },
            sort_keys=True,
        )
    )
