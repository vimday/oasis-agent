"""Account valuation and LP reference prices must share the same retained round."""

from dataclasses import replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.lp_reference_price import (
    CurrencyFeed,
    currency_feed_from_inventory,
    persist_currency_feed_evidence,
    replay_currency_feed_evidence,
)
from oasis_agent.farming.account_inventory import collect_account_inventory_quorum
from tests.unit.test_account_risk_reader import (
    BLOCK_NUMBER,
    NATIVE_SPEC,
    WETH_SPEC,
    InventoryFixture,
    chain_inventory,
    inventory_session,
)


def captures(fixture=None):
    fixture = InventoryFixture() if fixture is None else fixture
    entry = chain_inventory(fixture)
    return collect_account_inventory_quorum(
        inventory_session("rpc_a", fixture),
        inventory_session("rpc_b", replace(fixture, methods=[], block_calls=0)),
        entry=entry,
        block_number=BLOCK_NUMBER,
    )


@pytest.mark.parametrize("asset_id,expected_calls", [("native", 7), ("usdc", 8)])
def test_reference_reuses_account_wire_bytes_and_replays(tmp_path, asset_id, expected_calls):
    quorum = captures()
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        results = []
        for capture in (quorum.left, quorum.right):
            asset = capture.entry.asset(asset_id)
            feed = CurrencyFeed(
                asset.address, asset.decimals, asset.usd_feed(capture.entry.chain_id)
            )
            view = currency_feed_from_inventory(capture, currency=feed)
            assert len(view.exchanges) == expected_calls
            assert all(exchange in capture.exchanges for exchange in view.exchanges)
            reference = persist_currency_feed_evidence(store, view)
            assert replay_currency_feed_evidence(store, reference, currency=feed) == view
            results.append(view.reconstruct())
        assert results[0] == results[1]
    finally:
        store.close()


def test_reference_cannot_weaken_account_freshness_or_relabel_decimals():
    capture = captures().left
    asset = capture.entry.asset("usdc")
    policy = asset.usd_feed(capture.entry.chain_id)
    for currency in (
        CurrencyFeed(asset.address, 18, policy),
        CurrencyFeed(asset.address, 6, replace(policy, max_age_seconds=86400)),
    ):
        with pytest.raises(ValueError, match="differs from account inventory"):
            currency_feed_from_inventory(capture, currency=currency)


def test_native_and_wrapped_eth_can_share_one_feed_without_duplicating_exchanges():
    fixture = InventoryFixture(tokens=(replace(WETH_SPEC, feed=NATIVE_SPEC),))
    capture = captures(fixture).left
    for asset in capture.entry.assets:
        currency = CurrencyFeed(
            asset.address, asset.decimals, asset.usd_feed(capture.entry.chain_id)
        )
        view = currency_feed_from_inventory(capture, currency=currency)
        assert len(view.exchanges) == (7 if asset.is_native else 8)
        assert view.reconstruct()[1].answer == NATIVE_SPEC.answer
