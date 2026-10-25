"""Unreported NFTs cannot disappear from current NAV; historical replay stays explicit."""

from contextlib import closing
from dataclasses import replace

import pytest

from oasis_agent.evidence.exact_capture import wire_envelope
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.signed_nav import NAV_KIND, replay_signed_nav, verify_signed_nav_quorum
from oasis_agent.live.valuation import nav_fresh_until, replay_nav, replay_nav_quorum
from tests.unit.test_live_receipt_costs import position_config
from tests.unit.test_live_signed_nav import candidate, fixture
from tests.unit.test_live_valuation import NavFixture, run_nav, synthetic_feed_pin  # noqa: F401
from tests.unit.test_lp_receipts import PRIMARY, SECONDARY
from tests.unit.test_signed_prices import TIMESTAMP


@pytest.mark.parametrize("position,count", [(False, 1), (False, 2), (True, 0), (True, 2)])
@pytest.mark.parametrize("peer_only", [False, True])
def test_undeclared_or_missing_nft_refuses_before_nav_is_retained(
    tmp_path, position, count, peer_only
):
    f = NavFixture(nft_count=int(position) if peer_only else count)
    peer = replace(f, nft_count=count)
    with closing(EvidenceStore(tmp_path / "proof.db")) as store:
        with pytest.raises(ValueError, match="position inventory"):
            run_nav(store, f, peer, position=position)
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM evidence WHERE kind = 'live_nav'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("position", [False, True])
def test_signed_price_candidate_cannot_omit_an_extra_nft(tmp_path, position):
    with (
        closing(EvidenceStore(tmp_path / "proof.db")) as store,
        pytest.raises(ValueError, match="position inventory"),
    ):
        candidate(store, f=fixture(nft_count=int(position) + 1), position=position)


@pytest.mark.parametrize("position", [False, True])
def test_old_nav_remains_replayable_but_cannot_supply_current_admission(tmp_path, position):
    config = position_config()
    sources = (PRIMARY, SECONDARY)
    with closing(EvidenceStore(tmp_path / "proof.db")) as store:
        value, refs = run_nav(store, NavFixture(), position=position)
        legacy = []
        for ref in refs:
            current = replay_nav(store, ref, config)
            old = replace(
                current,
                exchanges=current.exchanges[:12] + current.exchanges[14:],
                inventory_checked=False,
            )
            legacy.append(
                store.put(
                    "live_nav",
                    old.payload(store, config),
                    ref.block,
                    ref.source,
                    request=wire_envelope(old.exchanges, response=False),
                    raw_response=wire_envelope(old.exchanges, response=True),
                    interpretation={"execution_eligible": False},
                )
            )
        assert replay_nav_quorum(store, tuple(legacy), config, sources=sources) == value
        with pytest.raises(ValueError, match="historical proof lacks"):
            nav_fresh_until(store, tuple(legacy), config, sources=sources)
        with pytest.raises(ValueError, match="declarations disagree"):
            replay_nav_quorum(store, (refs[0], legacy[1]), config, sources=sources)
        assert (
            nav_fresh_until(store, refs, config, sources=sources)
            > value.wallet.header.ref.timestamp
        )


@pytest.mark.parametrize("position", [False, True])
def test_legacy_signed_candidate_is_replayable_but_not_current_inventory(tmp_path, position):
    config = position_config()
    with closing(EvidenceStore(tmp_path / "proof.db")) as store:
        value, refs = candidate(store, position=position)
        legacy = []
        for ref in refs:
            current = replay_signed_nav(store, ref, config)
            old = replace(
                current,
                exchanges=current.exchanges[:4] + current.exchanges[6:],
                inventory_checked=False,
            )
            retained = store.put(
                NAV_KIND,
                old.payload(store, config),
                ref.block,
                ref.source,
                request=wire_envelope(old.exchanges, response=False),
                raw_response=wire_envelope(old.exchanges, response=True),
            )
            assert replay_signed_nav(store, retained, config).reconstruct(store, config) == value
            legacy.append(retained)
        for pair in (tuple(legacy), (refs[0], legacy[1])):
            with pytest.raises(
                ValueError, match="historical proof lacks current position inventory"
            ):
                verify_signed_nav_quorum(
                    store,
                    config,
                    pair,
                    sources=(PRIMARY, SECONDARY),
                    position=current.position,
                    now_ms=TIMESTAMP,
                )
