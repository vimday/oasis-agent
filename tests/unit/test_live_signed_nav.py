"""Official historical price signatures + synthetic chain inventory; no live approval."""

import json
from contextlib import closing
from dataclasses import asdict, replace

import pytest
from eth_hash.auto import keccak

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live import valuation
from oasis_agent.live.signed_nav import (
    NAV_KIND,
    PRICE_KIND,
    bound_prices,
    capture_signed_nav_candidate,
    replay_prices,
    replay_signed_nav,
)
from oasis_agent.live.state import Position
from oasis_agent.live.valuation import capture_nav, replay_nav
from oasis_agent.live.wallet import capture_wallet
from oasis_agent.models.common import BlockRef, canonical_json
from tests.unit.test_live_receipt_costs import position_config
from tests.unit.test_live_valuation import NavFixture
from tests.unit.test_lp_receipts import PRIMARY, SECONDARY, sessions
from tests.unit.test_signed_prices import FIXTURE, TIMESTAMP


def fixture(**kwargs):
    return NavFixture(timestamp=TIMESTAMP // 1000, **kwargs)


def block(f):
    return BlockRef(f.chain_id, f.block_number, f.block_hash, f.timestamp)


def candidate(store, f=None, peer=None, position=True, observed=TIMESTAMP, raw=None):
    f = f or fixture()
    return capture_signed_nav_candidate(
        store,
        position_config(),
        block(f),
        Position(123, -100, 100, 77, 0) if position else None,
        raw_prices=FIXTURE.read_bytes() if raw is None else raw,
        observed_at_ms=observed,
        sources=(PRIMARY, SECONDARY),
        sessions=sessions({PRIMARY: f, SECONDARY: peer or replace(f)}),
    )


@pytest.mark.parametrize("position", [False, True])
def test_signed_nav_uses_official_prices_all_inventory_and_replays(tmp_path, position):
    config = position_config()
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        value, refs = candidate(store, position=position)
        assert (value.eth_answer, value.usdc_answer) == (348695203485, 99988697)
        assert (value.fee0, value.fee1) == ((154, 231) if position else (0, 0))
        expected = (
            value.wallet.native * 348695203485 // 10**20
            + value.wallet.token * 99988697 // 10**8
            + (value.principal0 + value.fee0) * 348695203485 // 10**20
            + (value.principal1 + value.fee1) * 99988697 // 10**8
        )
        assert value.nav_micros == expected
        for ref in refs:
            proof = replay_signed_nav(store, ref, config)
            assert proof.reconstruct(store, config) == value
            binding = replay_prices(store, proof.price_evidence, config)
            assert binding["block"] == asdict(block(fixture()))
            assert binding["execution_eligible"] is False
            assert {r["source"] for r in binding["wallets"]} == {PRIMARY, SECONDARY}
            assert json.loads(store.get_record(ref).payload)["execution_eligible"] is False
            with pytest.raises(ValueError, match="NAV raw proof missing"):
                replay_nav(store, ref, config)


@pytest.mark.parametrize(
    "offset,accepted", [(0, True), (120000, True), (120001, False), (-1, False)]
)
def test_observation_block_clock_boundary(tmp_path, offset, accepted):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        if accepted:
            candidate(store, observed=TIMESTAMP + offset, position=False)
        else:
            with pytest.raises(ValueError):
                candidate(store, observed=TIMESTAMP + offset, position=False)
            assert store._connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


def test_quote_cannot_postdate_inventory_even_when_fresh_at_observation(tmp_path):
    f = replace(fixture(), timestamp=TIMESTAMP // 1000 - 1)
    with (
        closing(EvidenceStore(tmp_path / "evidence.db")) as store,
        pytest.raises(ValueError, match="block/clock"),
    ):
        candidate(store, f=f)
    # Validation precedes capture, so no raw wallet or NAV records were emitted.


@pytest.mark.parametrize("fault", ["stale", "tampered", "missing_feed"])
def test_bad_price_refuses_before_any_acquisition(tmp_path, fault):
    raw = json.loads(FIXTURE.read_bytes())
    observed = TIMESTAMP
    if fault == "tampered":
        raw["USDC"][0]["dataPoints"][0]["value"] = 1
    elif fault == "missing_feed":
        raw.pop("USDC")
    else:
        observed += 1800001

    def forbidden(*args):
        raise AssertionError("bad prices must not spend RPC budget")

    with closing(EvidenceStore(tmp_path / "evidence.db")) as store, pytest.raises(ValueError):
        capture_signed_nav_candidate(
            store,
            position_config(),
            block(fixture()),
            None,
            raw_prices=json.dumps(raw).encode(),
            observed_at_ms=observed,
            sources=(PRIMARY, SECONDARY),
            sessions=forbidden,
        )


@pytest.mark.parametrize("fault", ["wallet", "slot0", "liquidity", "ownership", "fees"])
def test_two_rpc_sources_must_agree_before_nav_retention(tmp_path, fault):
    f = fixture()
    changes = {
        "wallet": {"token_after": 1001},
        "slot0": {"sqrt": f.sqrt + 1},
        "liquidity": {"manager_liquidity": 76},
        "ownership": {"position_owner": "0x" + "11" * 20},
        "fees": {"growth1": f.growth1 + 1},
    }
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        with pytest.raises(ValueError):
            candidate(store, f=f, peer=replace(f, **changes[fault]))
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM evidence WHERE kind=?", (NAV_KIND,)
            ).fetchone()[0]
            == 0
        )


def test_duplicate_or_foreign_account_wallet_binding_refused(tmp_path):
    c = position_config()
    f = fixture()
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        wallets = capture_wallet(
            store,
            c,
            block(f),
            sources=(PRIMARY, SECONDARY),
            sessions=sessions({PRIMARY: f, SECONDARY: replace(f)}),
        )
        with pytest.raises(ValueError, match="two wallet sources"):
            bound_prices(store, c, (wallets[0], wallets[0]), FIXTURE.read_bytes(), TIMESTAMP)
        other = replace(c, account="0x" + "11" * 20)
        with pytest.raises(ValueError, match="configured account"):
            bound_prices(store, other, wallets, FIXTURE.read_bytes(), TIMESTAMP)


def test_signed_price_binding_cannot_be_reused_for_another_wallet_pair(tmp_path):
    c = position_config()
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        _, first = candidate(store)
        a = replay_signed_nav(store, first[0], c)
        _, second = candidate(store, f=fixture(token_after=2000))
        b = replay_signed_nav(store, second[0], c)
        forged = replace(b, price_evidence=a.price_evidence)
        with pytest.raises(ValueError, match="another wallet proof"):
            forged.reconstruct(store, c)


def test_rehashed_false_price_interpretation_refused(tmp_path):
    c = position_config()
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        _, refs = candidate(store)
        proof = replay_signed_nav(store, refs[0], c)
        old = store.get_record(proof.price_evidence)
        data = json.loads(old.payload)
        data["verified"]["prices"]["USDC"]["median"] = 100000000
        forged = store.put(
            PRICE_KIND,
            canonical_json(data),
            proof.price_evidence.block,
            proof.price_evidence.source,
            request=old.request_payload,
            raw_response=old.raw_response_payload,
        )
        with pytest.raises(ValueError, match="contradicts retained bytes"):
            replay_prices(store, forged, c)


def test_candidate_cannot_be_relabelled_as_production_nav(tmp_path):
    c = position_config()
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        _, refs = candidate(store)
        old = store.get_record(refs[0])
        data = json.loads(old.payload)
        data.pop("price_evidence")
        data["schema"] = "oasis.live-nav.v1"
        forged = store.put(
            "live_nav",
            canonical_json(data),
            refs[0].block,
            refs[0].source,
            request=old.request_payload,
            raw_response=old.raw_response_payload,
        )
        with pytest.raises(ValueError):
            replay_nav(store, forged, c)


def test_legacy_chainlink_path_does_not_fallback_to_signed_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(valuation, "FEED_RUNTIME", "0x" + keccak(bytes.fromhex("6000")).hex())
    c, f = position_config(), fixture(age=1801)
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        candidate(store, f=f, position=False)
        with pytest.raises(ValueError, match="stale"):
            capture_nav(
                store,
                c,
                block(f),
                None,
                sources=(PRIMARY, SECONDARY),
                sessions=sessions({PRIMARY: f, SECONDARY: replace(f)}),
            )


@pytest.mark.parametrize("age,accepted", [(1800, True), (1801, False)])
def test_signed_price_age_at_account_block_boundary(tmp_path, age, accepted):
    f = replace(fixture(), timestamp=TIMESTAMP // 1000 + age)
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        if accepted:
            candidate(store, f=f, observed=TIMESTAMP + age * 1000, position=False)
        else:
            with pytest.raises(ValueError):
                candidate(store, f=f, observed=TIMESTAMP + age * 1000, position=False)
            assert store._connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


def test_price_binding_cannot_move_between_account_blocks(tmp_path):
    c = position_config()
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        _, refs = candidate(store, position=False)
        first = replay_signed_nav(store, refs[0], c)
        f = replace(fixture(), block_number=fixture().block_number + 1, block_hash="0x" + "12" * 32)
        _, next_refs = candidate(store, f=f, position=False)
        second = replay_signed_nav(store, next_refs[0], c)
        with pytest.raises(ValueError, match="another wallet proof/block"):
            replace(second, price_evidence=first.price_evidence).reconstruct(store, c)


def test_replay_enforces_tightened_freshness_policy(tmp_path):
    c = position_config()
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        f = replace(fixture(), timestamp=TIMESTAMP // 1000 + 120)
        _, refs = candidate(store, f=f, observed=TIMESTAMP + 120000, position=False)
        with pytest.raises(ValueError):
            replay_signed_nav(store, refs[0], replace(c, oracle_max_age_seconds=60))
