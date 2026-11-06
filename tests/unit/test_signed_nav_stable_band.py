"""Synthetic signed prices test NAV policy, never production price acceptance."""

import base64
import json
import os
from contextlib import closing

import pytest
from eth_hash.auto import keccak
from eth_keys.datatypes import PrivateKey

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live import signed_nav, signed_prices
from oasis_agent.live.valuation import replay_nav
from tests.unit.test_capture_signed_nav import run, setup
from tests.unit.test_live_receipt_costs import position_config
from tests.unit.test_live_signed_nav import block, candidate, fixture
from tests.unit.test_lp_receipts import PRIMARY, SECONDARY
from tests.unit.test_signed_prices import TIMESTAMP


@pytest.fixture(scope="module")
def synthetic_packages():
    # Test-only keys are generated in memory. No production signer is impersonated
    # or modified; independent byte construction still exercises real recovery.
    keys = [PrivateKey(os.urandom(32)) for _ in range(3)]
    signers = frozenset(key.public_key.to_checksum_address().lower() for key in keys)
    result = {}
    for usdc in (98_000_000, 102_000_000, 97_999_999, 102_000_001, 97_000_000, 103_000_000):
        data = {}
        for feed, answer in (("ETH", 2500 * 10**8), ("USDC", usdc)):
            payload = (
                feed.encode().ljust(32, b"\0")
                + answer.to_bytes(32, "big")
                + TIMESTAMP.to_bytes(6, "big")
                + (32).to_bytes(4, "big")
                + (1).to_bytes(3, "big")
            )
            data[feed] = []
            for key in keys:
                signature = key.sign_msg_hash(keccak(payload))
                data[feed].append(
                    {
                        "dataPackageId": feed,
                        "timestampMilliseconds": TIMESTAMP,
                        "dataPoints": [
                            {
                                "dataFeedId": feed,
                                "value": base64.b64encode(answer.to_bytes(32, "big")).decode(),
                            }
                        ],
                        "signature": base64.b64encode(
                            signature.r.to_bytes(32, "big")
                            + signature.s.to_bytes(32, "big")
                            + bytes([signature.v + 27])
                        ).decode(),
                    }
                )
        result[usdc] = json.dumps(data).encode()
    return signers, result


@pytest.fixture
def signed_raw(monkeypatch, synthetic_packages):
    signers, packages = synthetic_packages
    monkeypatch.setattr(signed_prices, "SIGNERS", signers)
    return packages


@pytest.mark.parametrize("answer", [98_000_000, 102_000_000])
def test_exact_band_boundaries_value_candidate_without_production_admission(
    tmp_path, signed_raw, answer
):
    c = position_config()
    with closing(EvidenceStore(tmp_path / "candidate.db")) as store:
        value, refs = candidate(store, raw=signed_raw[answer], position=False)
        assert value.usdc_answer == answer
        result = signed_nav.verify_signed_nav_quorum(
            store, c, refs, sources=(PRIMARY, SECONDARY), position=None, now_ms=TIMESTAMP
        )
        assert result["value"]["usdc_answer"] == answer
        assert result["execution_eligible"] is False
        for ref in refs:
            assert ref.kind == signed_nav.NAV_KIND
            with pytest.raises(ValueError, match="NAV raw proof missing"):
                replay_nav(store, ref, c)


@pytest.mark.parametrize("answer", [97_999_999, 102_000_001, 97_000_000, 103_000_000])
def test_outside_band_refuses_before_any_rpc_session(tmp_path, signed_raw, answer):
    def forbidden(*_):
        pytest.fail("out-of-band price must refuse before constructing any RPC session")

    with closing(EvidenceStore(tmp_path / "candidate.db")) as store:
        with pytest.raises(ValueError, match="NAV USDC / USD answer outside stable band"):
            signed_nav.capture_signed_nav_candidate(
                store,
                position_config(),
                block(fixture(position_enabled=False)),
                None,
                raw_prices=signed_raw[answer],
                observed_at_ms=TIMESTAMP,
                sources=(PRIMARY, SECONDARY),
                sessions=forbidden,
            )
        assert store._connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


@pytest.mark.parametrize("answer", [97_000_000, 103_000_000])
def test_zero_spread_depeg_remains_research_evidence_but_refuses_nav(signed_raw, answer):
    verified = signed_prices.verify_prices(signed_raw[answer], observed_at_ms=TIMESTAMP)
    usdc = verified["prices"]["USDC"]
    assert usdc["minimum"] == usdc["maximum"] == usdc["median"] == answer
    assert verified["execution_eligible"] is False
    with pytest.raises(ValueError, match="NAV USDC / USD answer outside stable band"):
        signed_nav.verify_nav_prices(
            signed_raw[answer], position_config(), observed_at_ms=TIMESTAMP
        )


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("answer", [97_000_000, 103_000_000])
def test_historical_binding_cannot_bypass_band_on_any_replay(
    tmp_path, monkeypatch, signed_raw, version, answer
):
    c = position_config()
    original_bound_prices = signed_nav.bound_prices

    def historical_binding(*args, **kwargs):
        kwargs.setdefault("schema", f"oasis.block-bound-signed-price-candidate.v{version}")
        return original_bound_prices(*args, **kwargs)

    with closing(EvidenceStore(tmp_path / "historical.db")) as store:
        # Recreate the old acceptance rule solely to generate retained historical
        # bytes. Restore the real rule before every assertion under review.
        with monkeypatch.context() as old_policy:
            old_policy.setattr(signed_nav, "require_usdc_nav_band", lambda _: None, raising=False)
            old_policy.setattr(signed_nav, "bound_prices", historical_binding)
            _, refs = candidate(store, raw=signed_raw[answer], position=False)
            price_ref = signed_nav.replay_signed_nav(store, refs[0], c).price_evidence
        assert json.loads(store.get_record(price_ref).payload)["schema"].endswith(f"v{version}")
        with pytest.raises(ValueError, match="NAV USDC / USD answer outside stable band"):
            signed_nav.replay_prices(store, price_ref, c)
        with pytest.raises(ValueError, match="NAV USDC / USD answer outside stable band"):
            signed_nav.replay_signed_nav(store, refs[0], c)
        with pytest.raises(ValueError, match="NAV USDC / USD answer outside stable band"):
            signed_nav.verify_signed_nav_quorum(
                store, c, refs, sources=(PRIMARY, SECONDARY), position=None, now_ms=TIMESTAMP
            )


@pytest.mark.parametrize("answer", [97_000_000, 103_000_000])
def test_capture_cli_rejects_depeg_without_spending_budget(
    tmp_path, monkeypatch, signed_raw, answer
):
    paths, calls, _, before = setup(tmp_path, monkeypatch)
    paths["prices"].write_bytes(signed_raw[answer])
    report = run(paths, 3)
    assert report["refusal_stage"] == "price_verification"
    assert not calls and paths["budget"].read_bytes() == before
    assert not (paths["out"] / "check-input.json").exists()
