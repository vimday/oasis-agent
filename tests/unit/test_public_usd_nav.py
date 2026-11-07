"""Explicit exchange USD pricing: local raw fixtures, never live acceptance."""

import json
from contextlib import closing
from dataclasses import asdict, replace

import httpx
import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live import public_usd_prices as prices
from oasis_agent.live import valuation as public_nav
from oasis_agent.live.config import LiveConfig, PublicUsdConfig
from oasis_agent.live.config_epochs import retained_config
from oasis_agent.live.public_usd_prices import SCHEMA, URLS, verify_public_prices
from oasis_agent.live.startup_state import paths
from oasis_agent.live.state import Position, StateStore
from oasis_agent.live.valuation import PUBLIC_NAV_KIND as NAV_KIND
from oasis_agent.live.valuation import (
    NavRefusal,
    capture_public_nav,
    nav_fresh_until,
    replay_nav,
    replay_nav_quorum,
)
from oasis_agent.models.common import BlockRef, canonical_json, sha256_hex
from scripts import capture_live_recovery as recovery_cli
from scripts.migrate_live_runtime_transition import apply_pair
from scripts.review_live_runtime_transition import review
from tests.unit import test_live_joint_recovery_capture as joint
from tests.unit import test_live_receipt_costs as costs
from tests.unit import test_live_recovery_capture as recovery
from tests.unit.test_live_authority import setup_authority
from tests.unit.test_live_receipt_costs import position_config
from tests.unit.test_live_runtime_transition_review import setup
from tests.unit.test_live_valuation import (
    NavFixture,
    synthetic_feed_pin,  # noqa: F401
)
from tests.unit.test_lp_receipts import PRIMARY, SECONDARY, sessions

NOW = 1_794_020_000


def config():
    original = position_config()
    return PublicUsdConfig(**{k: getattr(original, k) for k in LiveConfig.__dataclass_fields__})


def bundle(*, now=NOW, age=5, eth="2243.50", usdc="0.9998"):
    observations = []
    for key, url in URLS.items():
        source, feed = key.split(":")
        price = eth if feed == "ETH" else usdc
        if source == "kraken":
            pair = "XETHZUSD" if feed == "ETH" else "USDCUSD"
            body = {
                "error": [],
                "result": {pair: [[price, "1", now - age, "b", "l", "", 7]], "last": "7"},
            }
        else:
            body = [
                {"price": price, "amount": "1", "date": str(now - age), "tid": "7", "type": "0"}
            ]
        observations.append(
            {
                "key": key,
                "url": url,
                "started_at_ms": now * 1000,
                "received_at_ms": now * 1000,
                "body": json.dumps(body),
            }
        )
    return canonical_json({"schema": SCHEMA, "observations": observations})


def block(f=None):
    f = f or NavFixture(timestamp=NOW)
    return BlockRef(f.chain_id, f.block_number, f.block_hash, f.timestamp)


def test_explicit_source_config_roundtrip_preserves_original_hashes():
    c = config()
    assert isinstance(LiveConfig.from_json(canonical_json(asdict(c))), PublicUsdConfig)
    old = position_config()
    digest = sha256_hex(canonical_json(asdict(old)))
    assert retained_config(c, digest) == old
    assert "usd_price_source" not in asdict(old)
    forbidden = replace(c, usdc_oracle_max_age_seconds=90000)
    with pytest.raises(ValueError):
        retained_config(c, sha256_hex(canonical_json(asdict(forbidden))))
    bad = asdict(c) | {"usd_price_source": "unapproved-source"}
    with pytest.raises(ValueError):
        LiveConfig.from_json(canonical_json(bad))


@pytest.mark.parametrize(
    "fault",
    [
        "stale",
        "future",
        "one_source",
        "wrong_url",
        "depeg",
        "disagree",
        "nan",
        "duplicate",
        "late_http",
    ],
)
def test_bad_price_quorums_refuse(fault):
    raw = bundle()
    data = json.loads(raw)
    if fault == "stale":
        raw = bundle(age=1801)
    elif fault == "future":
        raw = bundle(age=-1)
    elif fault == "one_source":
        data["observations"].pop()
    elif fault == "wrong_url":
        data["observations"][0]["url"] += "&pair=ETHUSDT"
    elif fault == "depeg":
        raw = bundle(usdc="0.97")
    elif fault == "disagree":
        row = data["observations"][2]
        body = json.loads(row["body"])
        body[0]["price"] = "2000"
        row["body"] = json.dumps(body)
    elif fault == "nan":
        raw = bundle(eth="NaN")
    elif fault == "duplicate":
        raw = raw.replace(b'"schema":', b'"schema":"duplicate","schema":', 1)
    else:
        data["observations"][0]["started_at_ms"] -= 16_000
    if fault in {"one_source", "wrong_url", "disagree", "late_http"}:
        raw = canonical_json(data)
    with pytest.raises((ValueError, NavRefusal)):
        verify_public_prices(raw, config(), block(), now_ms=NOW * 1000)


def test_trade_time_not_receipt_time_controls_expiry():
    c = replace(config(), usdc_oracle_max_age_seconds=30)
    result = verify_public_prices(bundle(age=25), c, block(), now_ms=NOW * 1000)
    assert result["prices"] == {"ETH": 224350000000, "USDC": 99980000}
    assert result["valid_until"] == NOW + 5


@pytest.mark.parametrize("position", [False, True])
def test_production_nav_replays_full_inventory_only_under_explicit_policy(tmp_path, position):
    c = config()
    f = NavFixture(timestamp=NOW, position_enabled=position)
    p = Position(123, -100, 100, 77, 0) if position else None
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        value, refs = capture_public_nav(
            store,
            c,
            block(f),
            p,
            raw_prices=bundle(),
            now_ms=NOW * 1000,
            sources=(PRIMARY, SECONDARY),
            sessions=sessions({PRIMARY: f, SECONDARY: replace(f)}),
        )
        assert all(r.kind == NAV_KIND for r in refs)
        assert (value.eth_answer, value.usdc_answer) == (224350000000, 99980000)
        assert replay_nav_quorum(store, refs, c, sources=(PRIMARY, SECONDARY)) == value
        assert nav_fresh_until(store, refs, c, sources=(PRIMARY, SECONDARY)) == NOW + 120
        assert replay_nav(store, refs[0], c).position == p
        with pytest.raises(ValueError):
            replay_nav(store, refs[0], position_config())


def test_authority_fetches_own_public_price_quorum_before_ephemeral_key(tmp_path, monkeypatch):

    base = costs.position_config()
    public = PublicUsdConfig(
        **{name: getattr(base, name) for name in LiveConfig.__dataclass_fields__}
    )
    monkeypatch.setattr(costs, "position_config", lambda: public)
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        authority, key, fields, context = setup_authority(tmp_path, store, monkeypatch)
        now = authority.loop.rpc.fixture.timestamp
        calls = []
        monkeypatch.setattr(public_nav.time, "time_ns", lambda: now * 10**9)

        def fetch():
            calls.append("independent authority fetch")
            return bundle(now=now, eth="2500", usdc="1")

        monkeypatch.setattr(public_nav, "fetch_public_prices", fetch)
        authority.sign(fields, context)
        assert calls == ["independent authority fetch"] and key.calls == 1


def test_public_nav_supports_original_runtime_review_without_relabeling_history(
    tmp_path, monkeypatch
):

    roots, old_target, refs, gas, f, _ = setup(tmp_path, monkeypatch)
    c = PublicUsdConfig(
        **{name: getattr(old_target, name) for name in LiveConfig.__dataclass_fields__}
    )
    monkeypatch.setattr(public_nav.time, "time_ns", lambda: f.timestamp * 10**9)
    monkeypatch.setattr(public_nav.time, "time", lambda: f.timestamp)
    for role, root in zip(("strategy", "authority"), roots, strict=True):
        state_path, evidence_path, _ = paths(root, role)
        state = StateStore(state_path).load("live", read_only=True)
        with closing(EvidenceStore(evidence_path, require_existing=True)) as store:
            _, pair = capture_public_nav(
                store,
                c,
                block(f),
                state.position,
                raw_prices=bundle(now=f.timestamp, eth="2500", usdc="0.99"),
                now_ms=f.timestamp * 1000,
                sources=("live_primary", "live_secondary"),
                sessions=sessions(dict.fromkeys(("live_primary", "live_secondary"), f)),
            )
        refs["nav"][role] = [asdict(r) for r in pair]
    report = review(*roots, c, refs, gas, now=f.timestamp)
    assert report["review_passed"] and report["execution_eligible"] is False
    result = apply_pair(*roots, c, refs, gas, report, report["evidence_hash"])
    assert result["migration_applied"] and result["execution_eligible"] is False
    for role, root in zip(("strategy", "authority"), roots, strict=True):
        state_path, _, _ = paths(root, role)
        state = StateStore(state_path).load("live", read_only=True)
        assert state.halted and (root / "STOP").exists()


@pytest.mark.parametrize("failure", [False, True])
def test_public_transport_is_bounded_credential_free_and_never_retries(monkeypatch, failure):
    bodies = {r["url"]: r["body"] for r in json.loads(bundle())["observations"]}
    calls = []
    client_type = httpx.AsyncClient

    def handler(request):
        calls.append(str(request.url))
        assert not any(k.lower() in {"authorization", "x-api-key"} for k in request.headers)

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield bodies[str(request.url)].encode()

        return httpx.Response(403 if failure else 200, stream=Stream())

    def factory(**kwargs):
        assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        return client_type(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(prices.httpx, "AsyncClient", factory)
    if failure:
        with pytest.raises(NavRefusal):
            prices.fetch_public_prices()
        assert len(calls) == 1
    else:
        raw = prices.fetch_public_prices()
        assert list(bodies) == calls
        assert len(json.loads(raw)["observations"]) == 4


def test_price_refusal_precedes_any_inventory_rpc(tmp_path):
    def forbidden(*_):
        pytest.fail("bad prices must refuse before RPC")

    with closing(EvidenceStore(tmp_path / "evidence.db")) as store, pytest.raises(NavRefusal):
        capture_public_nav(
            store,
            config(),
            block(),
            None,
            raw_prices=bundle(age=1801),
            now_ms=NOW * 1000,
            sources=(PRIMARY, SECONDARY),
            sessions=forbidden,
        )


def test_joint_capture_plan_counts_public_http_and_replays_both_services(tmp_path, monkeypatch):
    roots, _, scope, posters, clock, _ = joint.setup(tmp_path, monkeypatch)
    c = PublicUsdConfig(
        **{name: getattr(joint.CONFIG, name) for name in LiveConfig.__dataclass_fields__}
    )
    with recovery_cli.prepared(*roots, c, recovery.PROFILE, discover_gas_oracle=True) as prepared:
        plan = prepared[0]
    assert plan["total_requests"] == 95
    assert plan["public_https_requests"] == 4
    assert plan["request_limits"]["authority"] == dict.fromkeys(
        ("live_primary", "live_secondary"), 0
    )
    original_authority_budget = (roots[1] / "live.receipt-budget.db").read_bytes()
    f = next(iter(posters.values())).base.current
    fetches = []

    def fetch(*, on_request=None):
        fetches.append(1)
        if on_request:
            for key in URLS:
                on_request(key)
        return bundle(now=f.timestamp, eth="2500", usdc="0.99")

    monkeypatch.setattr(recovery_cli, "fetch_public_prices", fetch)
    result = recovery_cli.capture(
        *roots,
        c,
        recovery.PROFILE,
        tmp_path / "public-attempt",
        plan["plan_hash"],
        scope,
        discover_gas_oracle=True,
        clock=clock.wall,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        transport=lambda url, timeout: posters[url],
    )
    assert result["capture_passed"] and not result["execution_eligible"]
    assert result["public_https_requests_attempted"] == 4 and len(fetches) == 1
    assert result["runtime_approved"] is False and result["configuration_applied"] is False
    assert (roots[1] / "live.receipt-budget.db").read_bytes() == original_authority_budget
