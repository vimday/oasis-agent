"""Light holds cannot postpone loss accounting past retained NAV deadlines.

All provider responses are synthetic in-memory fixtures. No endpoint or signer
is contacted; display timestamps and restart never renew the retained proof.
"""

import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest
from eth_hash.auto import keccak

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live import valuation
from oasis_agent.live.chain import ChainError
from oasis_agent.live.receipts import RetainedReceiptCostReader
from oasis_agent.live.runner import FULL_OBSERVATION_SECONDS, LiveLoop
from oasis_agent.live.state import StateStore, day_of
from oasis_agent.models.common import BlockRef
from tests.unit.test_live_lp import HELD, TICK, LightRpc, loop_with
from tests.unit.test_live_receipt_costs import position_config
from tests.unit.test_live_valuation import NavFixture
from tests.unit.test_lp_receipts import PRIMARY, SECONDARY, sessions

NOW = 1_800_000_000


def light_loop(
    tmp_path: Path,
    primary: Any,
    secondary: Any,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    *,
    nav_fixture_overrides: dict[str, Any] | None = None,
    **state: Any,
) -> LiveLoop:
    # The optimization requires real replayable synthetic NAV, not a display timestamp.
    monkeypatch.setattr(valuation, "FEED_RUNTIME", "0x" + keccak(bytes.fromhex("6000")).hex())
    config = position_config()
    packed = (
        int(config.pool_id[:52], 16) << 56
        | ((HELD.upper & 0xFFFFFF) << 32)
        | ((HELD.lower & 0xFFFFFF) << 8)
    )
    fixture = NavFixture(
        **{
            "timestamp": int(time.time()) - 60,
            "block_number": 499,
            "position_info": packed,
            "manager_liquidity": HELD.liquidity,
            "core_liquidity": HELD.liquidity,
            **(nav_fixture_overrides or {}),
        }
    )
    evidence = EvidenceStore(tmp_path / "light-nav.db")
    request.addfinalizer(evidence.close)
    value, refs = valuation.capture_nav(
        evidence,
        config,
        BlockRef(config.chain_id, fixture.block_number, fixture.block_hash, fixture.timestamp),
        HELD,
        sources=(PRIMARY, SECONDARY),
        sessions=sessions({PRIMARY: fixture, SECONDARY: replace(fixture)}),
    )
    loop = LiveLoop.recovery(config, primary, StateStore(tmp_path / "light.state.json"), "live")
    loop.secondary = secondary
    loop.state.position = HELD
    for key, item in state.items():
        setattr(loop.state, key, item)
    loop.state.nav_evidence = refs
    loop.state.integer_risk().observe(
        day=day_of(fixture.timestamp), at=fixture.timestamp, nav=value.nav_micros
    )

    def forbidden(*_args, **_kwargs):
        pytest.fail("light hold must replay retained NAV without external requests")

    loop.receipt_costs = RetainedReceiptCostReader(
        evidence,
        chain_id=config.chain_id,
        account=config.account,
        native_decimals=config.decimals0,
        sources=(PRIMARY, SECONDARY),
        sessions=forbidden,
    )
    loop.state.last_observation = {"observed_at": fixture.timestamp}
    loop.observe = lambda *_: pytest.fail("light hold must not run a full observation")  # type: ignore[method-assign]
    return loop


def test_light_hold_spends_only_head_slot0_and_liquidity(tmp_path, monkeypatch, request) -> None:
    a, b = LightRpc(TICK, HELD.liquidity), LightRpc(TICK, HELD.liquidity)
    loop = light_loop(tmp_path, a, b, monkeypatch, request)
    status = loop._tick()
    assert status.startswith("block 500 light hold in range") and loop.state.halted is None
    assert (a.calls, b.calls) == (3, 2)


@pytest.mark.parametrize(
    "case",
    [
        "out_of_range",
        "upper_edge",
        "liquidity_changed",
        "providers_disagree",
        "provider_error",
        "stale_head",
        "full_observation_due",
        "no_observation",
        "no_position",
        "flow_acknowledgement",
    ],
)
def test_light_hold_falls_back_to_the_full_observation(
    tmp_path, monkeypatch, request, case
) -> None:
    a, b = LightRpc(TICK, HELD.liquidity), LightRpc(TICK, HELD.liquidity)
    loop = light_loop(tmp_path, a, b, monkeypatch, request)
    if case == "out_of_range":
        a.tick = b.tick = HELD.lower - 1
    elif case == "upper_edge":
        a.tick = b.tick = HELD.upper  # upper tick is exclusive
    elif case == "liquidity_changed":
        a.liquidity = b.liquidity = HELD.liquidity - 1
    elif case == "providers_disagree":
        b.tick = TICK + 1
    elif case == "provider_error":
        b.fail = True
    elif case == "stale_head":
        a.age = 10_000
    elif case == "full_observation_due":
        loop.state.last_observation = {"observed_at": int(time.time()) - FULL_OBSERVATION_SECONDS}
    elif case == "no_observation":
        loop.state.last_observation = None
    elif case == "no_position":
        loop.state.position = None
    elif case == "flow_acknowledgement":
        loop.state.acknowledge_flow = True
    assert loop._light_hold() is None


def test_shadow_mode_never_takes_the_light_path(tmp_path: Path) -> None:
    loop = loop_with(tmp_path, LightRpc(TICK, HELD.liquidity), position=HELD)
    loop.state.last_observation = {"observed_at": int(time.time()) - 60}
    assert loop._light_hold() is None


def test_light_hold_records_its_check_and_full_observation_clears_it(
    tmp_path, monkeypatch, request
) -> None:
    a, b = LightRpc(TICK, HELD.liquidity), LightRpc(TICK, HELD.liquidity)
    loop = light_loop(tmp_path, a, b, monkeypatch, request)
    loop._tick()
    light = loop.state.last_light_check
    assert light is not None and light["block"] == 500 and light["tick"] == TICK
    a.tick = b.tick = HELD.lower - 1
    assert loop._light_hold() is None
    assert loop.state.last_light_check == light  # a refused light check records nothing


@pytest.fixture
def held(tmp_path, monkeypatch, request):
    clock = [NOW]
    monkeypatch.setattr("oasis_agent.live.runner.time.time", lambda: clock[0])

    def build(**overrides):
        primary, secondary = LightRpc(TICK, HELD.liquidity), LightRpc(TICK, HELD.liquidity)
        loop = light_loop(
            tmp_path,
            primary,
            secondary,
            monkeypatch,
            request,
            nav_fixture_overrides=overrides,
        )
        assert (
            loop.c.max_capital_usd,
            loop.c.max_daily_loss_usd,
            loop.c.max_cumulative_loss_usd,
            loop.c.oracle_max_age_seconds,
            loop.c.usdc_oracle_max_age_seconds,
        ) == (500, 200, 500, 1800, 1800)
        return loop, primary, secondary, clock

    return build


@pytest.mark.parametrize("bound", ["head", "eth", "usdc"])
def test_expired_retained_nav_falls_back_before_any_light_rpc(held, bound):
    overrides = {"timestamp": NOW - 121} if bound == "head" else {bound + "_age": 1800}
    loop, primary, secondary, _ = held(**overrides)
    # A recent projection must not extend the actual head/feed deadline.
    loop.state.last_observation = {"observed_at": NOW, "nav_valid_until": NOW + 3600}
    before = asdict(loop.state)
    assert loop._light_hold() is None
    assert (primary.calls, secondary.calls) == (0, 0)
    assert asdict(loop.state) == before


@pytest.mark.parametrize("fault", ["missing_reader", "missing_proof", "wrong_sources", "future"])
def test_unknown_or_future_nav_cannot_take_light_path(held, fault):
    loop, primary, secondary, _ = held(timestamp=NOW + 1 if fault == "future" else NOW - 60)
    loop.state.last_observation = {"observed_at": NOW}
    if fault == "missing_reader":
        loop.receipt_costs = None
    elif fault == "missing_proof":
        loop.state.nav_evidence = ()
    elif fault == "wrong_sources":
        loop.receipt_costs.sources = tuple(reversed(loop.receipt_costs.sources))
    assert loop._light_hold() is None
    assert (primary.calls, secondary.calls) == (0, 0)
    assert loop.state.last_light_check is None


@pytest.mark.parametrize("bound", ["head", "eth", "usdc"])
def test_nav_expiring_during_light_reads_forces_full_observation(held, monkeypatch, bound):
    overrides = (
        {"timestamp": NOW - 119}
        if bound == "head"
        else {
            "timestamp": NOW,
            bound + "_age": 1799,
        }
    )
    loop, primary, secondary, clock = held(**overrides)
    original = secondary.call_abi

    def slow_read(*args, **kwargs):
        value = original(*args, **kwargs)
        clock[0] = NOW + 2
        return value

    monkeypatch.setattr(secondary, "call_abi", slow_read)
    before = asdict(loop.state)
    assert loop._light_hold() is None
    assert (primary.calls, secondary.calls) == (3, 2)
    assert asdict(loop.state) == before


@pytest.mark.parametrize("restart", [False, True])
def test_expired_nav_reaches_full_observation_and_preserves_risk_on_failure(held, restart):
    loop, primary, secondary, clock = held(timestamp=NOW - 121)
    if restart:
        loop.store.save(loop.state)
        reader = loop.receipt_costs
        loop = LiveLoop.recovery(loop.c, primary, StateStore(loop.store.path), "live")
        loop.secondary, loop.receipt_costs = secondary, reader
    risk = asdict(loop.state.exact_risk)
    old_refs = loop.state.nav_evidence
    observations = []

    def observe():
        observations.append(clock[0])
        raise ChainError("complete account NAV unresolved (details redacted)")

    loop.observe = observe
    with pytest.raises(ChainError, match="account NAV unresolved"):
        loop._tick()
    assert observations == [NOW]
    assert (primary.calls, secondary.calls) == (0, 0)
    assert asdict(loop.state.exact_risk) == risk
    assert loop.state.nav_evidence == old_refs
    assert loop.state.last_light_check is None


def test_last_valid_second_remains_usable_without_renewing_nav(held):
    loop, primary, secondary, clock = held(timestamp=NOW - 120)
    before = (
        asdict(loop.state.exact_risk),
        loop.state.nav_evidence,
        dict(loop.state.last_observation),
    )
    assert "light hold in range" in loop._light_hold()
    assert (primary.calls, secondary.calls) == (3, 2)
    assert (
        asdict(loop.state.exact_risk),
        loop.state.nav_evidence,
        loop.state.last_observation,
    ) == before
    clock[0] += 1
    assert loop._light_hold() is None
    assert (primary.calls, secondary.calls) == (3, 2)
