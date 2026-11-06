"""Per-asset candidate deadlines over historical signatures and synthetic RPC only."""

import json
import sys
from contextlib import closing
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.signed_nav import (
    PRICE_KIND,
    capture_signed_nav_candidate,
    replay_prices,
    replay_signed_nav,
    verify_signed_nav_quorum,
)
from oasis_agent.live.signed_prices import verify_prices
from oasis_agent.models.common import canonical_json
from scripts import capture_signed_nav as cli
from scripts import verify_signed_nav as check_cli
from tests.unit.test_capture_signed_nav import run, setup
from tests.unit.test_live_receipt_costs import position_config
from tests.unit.test_live_signed_nav import block, fixture
from tests.unit.test_lp_receipts import PRIMARY, SECONDARY, sessions
from tests.unit.test_signed_prices import FIXTURE, TIMESTAMP


def policy(strict):
    return replace(
        position_config(),
        oracle_max_age_seconds=60 if strict == "ETH" else 1800,
        usdc_oracle_max_age_seconds=60 if strict == "USDC" else 1800,
    )


def deadlines(strict):
    return {
        feed: {
            "max_age_ms": (60 if feed == strict else 1800) * 1000,
            "valid_until_ms": TIMESTAMP + (60 if feed == strict else 1800) * 1000,
        }
        for feed in ("ETH", "USDC")
    }


def capture(store, c, observed):
    f = fixture(position_enabled=False)
    return capture_signed_nav_candidate(
        store,
        c,
        block(f),
        None,
        raw_prices=FIXTURE.read_bytes(),
        observed_at_ms=observed,
        sources=(PRIMARY, SECONDARY),
        sessions=sessions({PRIMARY: f, SECONDARY: replace(f)}),
    )


@pytest.mark.parametrize("strict", ["ETH", "USDC"])
def test_expired_feed_refuses_before_any_capture(tmp_path, strict):
    def forbidden(*_):
        pytest.fail("expired feed must refuse before constructing any RPC session")

    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        with pytest.raises(ValueError, match="stale or future"):
            capture_signed_nav_candidate(
                store,
                policy(strict),
                block(fixture(position_enabled=False)),
                None,
                raw_prices=FIXTURE.read_bytes(),
                observed_at_ms=TIMESTAMP + 60_001,
                sources=(PRIMARY, SECONDARY),
                sessions=forbidden,
            )
        assert store._connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


@pytest.mark.parametrize("strict", ["ETH", "USDC"])
def test_capture_retains_distinct_feed_deadlines_at_exact_boundary(tmp_path, strict):
    c = policy(strict)
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        _, refs = capture(store, c, TIMESTAMP + 60_000)
        proof = replay_signed_nav(store, refs[0], c)
        binding = replay_prices(store, proof.price_evidence, c)
        assert binding["feed_deadlines"] == deadlines(strict)
        result = verify_signed_nav_quorum(
            store,
            c,
            refs,
            sources=(PRIMARY, SECONDARY),
            position=None,
            now_ms=TIMESTAMP + 60_000,
        )
        assert result["feed_deadlines"] == deadlines(strict)
        assert result["execution_eligible"] is False


@pytest.mark.parametrize("strict", ["ETH", "USDC"])
def test_consumer_rechecks_feed_while_account_block_remains_fresh(tmp_path, strict):
    c = policy(strict)
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        _, refs = capture(store, c, TIMESTAMP)
        with pytest.raises(ValueError, match="stale or future"):
            verify_signed_nav_quorum(
                store,
                c,
                refs,
                sources=(PRIMARY, SECONDARY),
                position=None,
                now_ms=TIMESTAMP + 60_001,
            )


@pytest.mark.parametrize("strict", ["ETH", "USDC"])
def test_cli_refuses_expired_feed_without_budget_spending(tmp_path, monkeypatch, strict):
    paths, calls, _, before = setup(tmp_path, monkeypatch)
    paths["config"].write_bytes(canonical_json(asdict(policy(strict))))
    monkeypatch.setattr(cli.time, "time_ns", lambda: (TIMESTAMP + 60_001) * 1_000_000)
    report = run(paths, 3)
    assert report["refusal_stage"] == "price_verification"
    assert not calls and paths["budget"].read_bytes() == before
    assert not (paths["out"] / "check-input.json").exists()


@pytest.mark.parametrize("strict", ["ETH", "USDC"])
def test_cli_expiry_during_wallet_reads_stops_without_retry(tmp_path, monkeypatch, strict):
    paths, calls, _, _ = setup(tmp_path, monkeypatch)
    paths["config"].write_bytes(canonical_json(asdict(policy(strict))))
    monkeypatch.setattr(
        cli.time,
        "time_ns",
        lambda: (TIMESTAMP + (60_001 if len(calls) >= 5 else 0)) * 1_000_000,
    )
    report = run(paths, 3)
    assert report["refusal_stage"] == "nav_capture"
    assert 5 <= len(calls) <= 6  # At most one already admitted concurrent request.
    assert sum(report["request_attempts"].values()) == len(calls)
    assert not report["candidate_verified"]
    assert not (paths["out"] / "check-input.json").exists()


@pytest.mark.parametrize("strict", ["ETH", "USDC"])
@pytest.mark.parametrize("phase", ["replay", "publication"])
def test_cli_checks_both_feed_deadlines_after_replay(tmp_path, monkeypatch, strict, phase):
    paths, calls, _, _ = setup(tmp_path, monkeypatch)
    paths["config"].write_bytes(canonical_json(asdict(policy(strict))))
    original = cli.verify_signed_nav_quorum

    def verify(*args, **kwargs):
        result = original(*args, **kwargs)
        clocks = iter([TIMESTAMP, TIMESTAMP + 60_001])
        monkeypatch.setattr(
            cli.time,
            "time_ns",
            lambda: (next(clocks) if phase == "publication" else TIMESTAMP + 60_001) * 1_000_000,
        )
        return result

    monkeypatch.setattr(cli, "verify_signed_nav_quorum", verify)
    report = run(paths, 3)
    assert report["refusal_stage"] == (
        "current_clock_replay" if phase == "replay" else "publication_clock"
    )
    assert len(calls) == 35
    assert not (paths["out"] / "check-input.json").exists()


@pytest.mark.parametrize("strict", ["ETH", "USDC"])
def test_offline_plan_preserves_each_policy_and_deadline(tmp_path, monkeypatch, strict):
    paths, calls, _, before = setup(tmp_path, monkeypatch, capture=False)
    paths["config"].write_bytes(canonical_json(asdict(policy(strict))))
    report = run(paths, 0)
    assert report["feed_deadlines"] == deadlines(strict)
    assert not report["candidate_verified"]
    assert not calls and paths["budget"].read_bytes() == before
    assert json.loads((paths["out"] / "report.json").read_bytes())["execution_eligible"] is False


@pytest.mark.parametrize("strict", ["ETH", "USDC"])
def test_legacy_binding_replays_without_rewriting_historical_bytes(tmp_path, strict):
    c = policy(strict)
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        _, refs = capture(store, c, TIMESTAMP)
        ref = replay_signed_nav(store, refs[0], c).price_evidence
        old = store.get_record(ref)
        payload = json.loads(old.payload)
        payload["schema"] = "oasis.block-bound-signed-price-candidate.v1"
        payload.pop("feed_deadlines")
        payload["verified"] = verify_prices(
            old.raw_response_payload,
            observed_at_ms=TIMESTAMP,
            max_age_ms=c.oracle_max_age_seconds * 1000,
        )
        legacy = store.put(
            PRICE_KIND,
            canonical_json(payload),
            ref.block,
            ref.source,
            request=old.request_payload,
            raw_response=old.raw_response_payload,
        )
        assert replay_prices(store, legacy, c) == payload
        assert store.get_record(legacy).payload == canonical_json(payload)


def test_legacy_binding_cannot_waive_usdc_policy(tmp_path):
    c = policy("USDC")
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        _, refs = capture(store, c, TIMESTAMP)
        ref = replay_signed_nav(store, refs[0], c).price_evidence
        old = store.get_record(ref)
        payload, request = json.loads(old.payload), json.loads(old.request_payload)
        payload["schema"] = "oasis.block-bound-signed-price-candidate.v1"
        payload.pop("feed_deadlines")
        payload["verified"] = verify_prices(
            old.raw_response_payload,
            observed_at_ms=TIMESTAMP + 60_001,
            max_age_ms=c.oracle_max_age_seconds * 1000,
        )
        request["observed_at_ms"] = TIMESTAMP + 60_001
        legacy = store.put(
            PRICE_KIND,
            canonical_json(payload),
            ref.block,
            ref.source,
            request=canonical_json(request),
            raw_response=old.raw_response_payload,
        )
        with pytest.raises(ValueError, match="USDC price stale or future"):
            replay_prices(store, legacy, c)


def test_rehashed_deadline_cannot_extend_authenticated_price(tmp_path):
    c = policy("USDC")
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        _, refs = capture(store, c, TIMESTAMP)
        ref = replay_signed_nav(store, refs[0], c).price_evidence
        old = store.get_record(ref)
        payload = json.loads(old.payload)
        payload["feed_deadlines"]["USDC"]["valid_until_ms"] += 1
        forged = store.put(
            PRICE_KIND,
            canonical_json(payload),
            ref.block,
            ref.source,
            request=old.request_payload,
            raw_response=old.raw_response_payload,
        )
        with pytest.raises(ValueError, match="contradicts retained bytes"):
            replay_prices(store, forged, c)


@pytest.mark.parametrize("strict", ["ETH", "USDC"])
def test_offline_checker_refuses_expiry_during_replay(tmp_path, monkeypatch, strict):
    c = policy(strict)
    database = tmp_path / "evidence.db"
    with closing(EvidenceStore(database)) as store:
        _, refs = capture(store, c, TIMESTAMP)
    request, config, output = (tmp_path / name for name in ("request", "config", "out"))
    request.write_bytes(
        canonical_json(
            {
                "schema": "oasis.signed-nav-check-input.v1",
                "evidence": [asdict(ref) for ref in refs],
                "sources": [PRIMARY, SECONDARY],
                "position": None,
            }
        )
    )
    config.write_bytes(canonical_json(asdict(c)))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify",
            "--input",
            str(request),
            "--config",
            str(config),
            "--evidence",
            str(database),
            "--out",
            str(output),
        ],
    )
    clocks = iter([TIMESTAMP, TIMESTAMP + 60_001])
    monkeypatch.setattr(check_cli.time, "time_ns", lambda: next(clocks) * 1_000_000)
    before = database.read_bytes()
    with pytest.raises(SystemExit) as exc:
        check_cli.main()
    assert exc.value.code == 3
    report = json.loads(output.read_bytes())
    assert not report["passed"] and not report["execution_eligible"]
    assert database.read_bytes() == before
