"""Restart checks replay synthetic raw proof pairs without RPC or signing."""

from contextlib import closing
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.authority import TransactionAuthority
from oasis_agent.live.chain import ChainError
from oasis_agent.live.exact_risk import ExactRisk
from oasis_agent.live.precheck import check_live
from oasis_agent.live.receipts import RetainedReceiptCostReader
from oasis_agent.live.runner import LiveLoop
from oasis_agent.live.state import StateStore, day_of
from oasis_agent.live.valuation import replay_nav_quorum
from tests.unit.test_live_lp import market
from tests.unit.test_live_precheck import NonceRpc
from tests.unit.test_live_receipt_costs import position_config
from tests.unit.test_live_valuation import NavFixture, run_nav, synthetic_feed_pin  # noqa: F401
from tests.unit.test_lp_receipts import PRIMARY, SECONDARY


def forbidden(*_args, **_kwargs):
    pytest.fail("checkpoint replay must not use RPC or sign")


class NoRpc:
    call = block = nonce = receipt = forbidden


def checkpoint(tmp_path, store):
    fixture = NavFixture()
    value, refs = run_nav(store, fixture, position=False)
    loop = LiveLoop.recovery(
        position_config(), NoRpc(), StateStore(tmp_path / "state.json"), "live"
    )
    loop.receipt_costs = RetainedReceiptCostReader(
        store,
        chain_id=loop.c.chain_id,
        account=loop.c.account,
        native_decimals=18,
        sources=(PRIMARY, SECONDARY),
        sessions=forbidden,
    )
    risk = ExactRisk()
    risk.observe(day=day_of(fixture.timestamp), at=fixture.timestamp, nav=value.nav_micros)
    loop.state.exact_risk, loop.state.nav_evidence = risk, refs
    return loop, fixture


@pytest.mark.parametrize("fault", ["duplicate", "reversed", "foreign", "time", "round", "missing"])
def test_checkpoint_rejects_unbound_evidence_even_when_nav_matches(tmp_path, fault):
    with closing(EvidenceStore(tmp_path / "proof.db")) as store:
        loop, fixture = checkpoint(tmp_path, store)
        first, second = loop.state.nav_evidence
        if fault == "duplicate":
            loop.state.nav_evidence = (first, first)
        elif fault == "reversed":
            loop.state.nav_evidence = (second, first)
        elif fault == "foreign":
            loop.receipt_costs.sources = ("unexpected", SECONDARY)
        elif fault == "time":
            loop.state.exact_risk.last_at += 1
        elif fault == "round":
            _, different = run_nav(store, replace(fixture, round_id=3, answered=3), position=False)
            loop.state.nav_evidence = (first, different[1])
        elif fault == "missing":
            loop.state.nav_evidence = ()
        before = asdict(loop.state)
        with pytest.raises(ChainError, match="NAV checkpoint"):
            loop._check_nav_checkpoint()
        assert asdict(loop.state) == before


def test_checkpoint_replays_after_restart_without_claiming_current_freshness(tmp_path):
    with closing(EvidenceStore(tmp_path / "proof.db")) as store:
        loop, _ = checkpoint(tmp_path, store)
        loop.store.save(loop.state)
        loop.state = loop.store.load("live")
        before = asdict(loop.state)
        loop._check_nav_checkpoint()
        assert asdict(loop.state) == before


@pytest.mark.parametrize("state", ["pristine", "empty_risk", "legacy"])
def test_only_uninitialized_risk_can_omit_checkpoint_proofs(tmp_path, state):
    loop = LiveLoop.recovery(
        position_config(), NoRpc(), StateStore(tmp_path / "state.json"), "live"
    )
    if state == "empty_risk":
        loop.state.exact_risk = ExactRisk()
    elif state == "legacy":
        loop.state.initial_nav_usd = loop.state.checkpoint_nav_usd = 500.0
    if state == "legacy":
        with pytest.raises(ChainError, match="NAV checkpoint"):
            loop._check_nav_checkpoint()
        assert loop.state.exact_risk is None
    else:
        loop._check_nav_checkpoint()


@pytest.mark.parametrize("consumer", ["strategy", "authority"])
def test_bad_checkpoint_stops_both_services_before_fresh_rpc_or_key_use(
    tmp_path, consumer, monkeypatch
):
    monkeypatch.setattr("oasis_agent.live.authority.require_live_action", lambda *_: None)
    with closing(EvidenceStore(tmp_path / "proof.db")) as store:
        loop, _ = checkpoint(tmp_path, store)
        ref = loop.state.nav_evidence[0]
        loop.state.nav_evidence = (ref, ref)
        loop.secondary = NoRpc()
        if consumer == "strategy":
            check = loop._tick
        else:
            loop.store.save(loop.state)

            class NoKey:
                account = loop.c.account
                sign = forbidden

            authority = TransactionAuthority(loop, NoKey())

            def check():
                return authority.sign([], {})

        with pytest.raises(ChainError, match="NAV checkpoint"):
            check()


def test_external_flow_books_against_replayed_baseline_without_erasing_it(tmp_path, monkeypatch):
    with closing(EvidenceStore(tmp_path / "proof.db")) as store:
        loop, fixture = checkpoint(tmp_path, store)
        prior = replay_nav_quorum(
            store, loop.state.nav_evidence, loop.c, sources=(PRIMARY, SECONDARY)
        )
        loop.state.last_units = [prior.wallet.native, prior.wallet.token, 0]
        loop.state.acknowledge_flow = True
        newer = replace(
            fixture,
            timestamp=fixture.timestamp + 2,
            block_number=fixture.block_number + 1,
            block_hash="0x" + "55" * 32,
            native_after=fixture.native_after + 10**17,
        )
        value, refs = run_nav(store, newer, position=False)
        observed = market(
            block=newer.block_number,
            block_hash=newer.block_hash,
            timestamp=newer.timestamp,
            eth=value.wallet.native,
            usdc=value.wallet.token,
            valuation=value,
            nav_evidence=refs,
        )
        monkeypatch.setattr(loop, "observe", lambda: observed)
        monkeypatch.setattr(loop, "_decide", lambda _: None)
        loop._tick()
        assert loop.state.exact_risk.basis == prior.nav_micros
        assert loop.state.exact_risk.external == 250_000_000
        assert loop.state.exact_risk.losses(value.nav_micros) == (0, 0)
        assert not loop.state.acknowledge_flow and loop.state.halted is None
        loop._check_nav_checkpoint()


def test_precheck_still_reports_loss_with_a_replayable_checkpoint(tmp_path, monkeypatch):
    with closing(EvidenceStore(tmp_path / "proof.db")) as store:
        loop, fixture = checkpoint(tmp_path, store)
        risk = loop.state.exact_risk
        risk.basis = risk.checkpoint = 10_000_000_000
        loop.store.save(loop.state)
        before = loop.store.path.read_bytes()
        value = replay_nav_quorum(
            store, loop.state.nav_evidence, loop.c, sources=(PRIMARY, SECONDARY)
        )
        observed = market(
            block=fixture.block_number,
            block_hash=fixture.block_hash,
            timestamp=fixture.timestamp,
            eth=value.wallet.native,
            usdc=value.wallet.token,
            valuation=value,
            nav_evidence=loop.state.nav_evidence,
        )
        monkeypatch.setattr(LiveLoop, "observe", lambda _: observed)
        monkeypatch.setattr(LiveLoop, "runtime_pins", lambda *_: loop.c.expected_pins)
        result = check_live(
            loop.c,
            loop.store,
            NonceRpc(),
            NonceRpc(),
            stop_file=tmp_path / "STOP",
            receipt_costs=loop.receipt_costs,
        )
        assert "loss_budget_exhausted" in result["blockers"]
        assert not result["operational_checks_passed"] and not result["signer_contacted"]
        assert loop.store.path.read_bytes() == before
